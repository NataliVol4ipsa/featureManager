"""Azure DevOps pipeline runs for a workspace's feature branches.

This module contains no UI. For one repository it:

  * resolves the repository's build/pipeline definition from its Azure DevOps
    remote (org / project / repo), and
  * queues a pipeline run on a given branch with the deploy-environment
    template parameters set for a Development or Acceptance deployment (never
    Production).

The deploy toggles shown in the "Run pipeline" dialog are YAML *runtime
parameters*. Their real names are discovered from the repository's local
pipeline YAML (no YAML dependency - a light regex scan of the top-level
``parameters:`` block), falling back to the standard Equity Value Chain
deployment-template names when the file cannot be read.

Authentication reuses the ``ADO_PAT`` environment variable (the same one used
for PR / work-item linking); queuing a run needs a token with **Build: Read &
execute** (and Code: Read to resolve the repository/definition). It falls back
to the Git-stored credential, but that is normally Code-scoped only and will be
rejected by the pipelines API.
"""

import os
import re
import json
import base64
import urllib.parse
import urllib.request
import urllib.error

from gitutils import (
    is_git_repo, git_remote_url, parse_ado_remote, get_git_credential,
)


# Standard EVC deployment-template parameter names, used when a repo's pipeline
# YAML cannot be read to discover them. Keyed by deployment "role".
DEFAULT_ROLE_PARAMS = {
    "infra": "deployInfrastructure",
    "dev": "deployDevelopment",
    "acc": "deployAcceptance",
    "prod": "deployProduction",
}

# Candidate file names holding the pipeline definition at the repo root.
_PIPELINE_YAML_NAMES = (
    "azure-pipelines.yml", "azure-pipeline.yml",
    "azure-pipelines.yaml", "azure-pipeline.yaml",
)


# --------------------------------------------------------------------------- #
# Environment -> template parameter values
# --------------------------------------------------------------------------- #

def _env_roles(environment):
    """Return {role: bool} deploy toggles for a 'dev' or 'acc' run.

    Production and the "other" environment are always turned off so a Feature
    Manager run can never deploy to production.
    """
    if environment == "dev":
        return {"infra": True, "dev": True, "acc": False, "prod": False}
    if environment == "acc":
        return {"infra": True, "dev": False, "acc": True, "prod": False}
    raise ValueError(f"unknown environment: {environment!r}")


def _parse_yaml_parameters(text):
    """Return [(name, display_name), ...] from the top-level parameters: block.

    A deliberately small scan (no YAML library): it starts at an unindented
    ``parameters:`` line and reads each ``- name:`` entry with its optional
    ``displayName:``, stopping at the next unindented top-level key.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if re.match(r"^parameters:\s*(#.*)?$", line):
            start = index + 1
            break
    if start is None:
        return []

    result = []
    cur_name = None
    cur_display = None
    for line in lines[start:]:
        # An unindented, non-comment line is the next top-level key: block ends.
        if line and not line[0].isspace() and not line.lstrip().startswith("#"):
            break
        name_match = re.match(r"\s*-\s*name:\s*(.+?)\s*$", line)
        if name_match:
            if cur_name is not None:
                result.append((cur_name, cur_display))
            cur_name = name_match.group(1).strip().strip("\"'")
            cur_display = None
            continue
        display_match = re.match(r"\s*displayName:\s*(.+?)\s*$", line)
        if display_match and cur_name is not None:
            cur_display = display_match.group(1).strip().strip("\"'")
    if cur_name is not None:
        result.append((cur_name, cur_display))
    return result


def _discover_role_params(repo_path):
    """Return {role: param_name} discovered from the repo's pipeline YAML.

    Only roles actually found are returned (so undeclared parameters are never
    sent to Azure DevOps). Returns {} when no pipeline YAML is present.
    """
    text = None
    for fname in _PIPELINE_YAML_NAMES:
        candidate = os.path.join(repo_path, fname)
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding="utf-8") as handle:
                    text = handle.read()
            except OSError:
                text = None
            break
    if not text:
        return {}

    roles = {}
    for name, display in _parse_yaml_parameters(text):
        key = (display or name).lower()
        if "infrastructure" in key or "infra" in key:
            roles.setdefault("infra", name)
        elif "development" in key or "develop" in key:
            roles.setdefault("dev", name)
        elif "acceptance" in key:
            roles.setdefault("acc", name)
        elif "production" in key:
            roles.setdefault("prod", name)
    return roles


def build_template_parameters(repo_path, environment):
    """Return the {parameter_name: bool} template parameters for a run.

    Parameter names come from the repo's pipeline YAML when readable, otherwise
    from the standard EVC deployment-template names.
    """
    role_to_name = _discover_role_params(repo_path) or DEFAULT_ROLE_PARAMS
    params = {}
    for role, value in _env_roles(environment).items():
        name = role_to_name.get(role)
        if name:
            params[name] = value
    return params


# --------------------------------------------------------------------------- #
# Azure DevOps REST helpers
# --------------------------------------------------------------------------- #

def _auth_for_host(host):
    """Return (authorization_header, error). Prefers ADO_PAT, then Git creds."""
    pat = os.environ.get("ADO_PAT", "").strip()
    if pat:
        token = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
        return f"Basic {token}", ""

    username, password = get_git_credential(host)
    if password:
        token = base64.b64encode(
            f"{username or ''}:{password}".encode("utf-8")
        ).decode("ascii")
        return f"Basic {token}", ""

    return None, (
        "no Azure DevOps credential found. Set the ADO_PAT environment variable "
        "to a token with Build (Read & execute) and Code (Read) scopes."
    )


def _http_error_detail(exc):
    """Extract a human-readable message from an HTTPError response body."""
    detail = exc.read().decode("utf-8", "replace").strip()
    try:
        detail = json.loads(detail).get("message", detail)
    except ValueError:
        pass
    return detail or f"HTTP {exc.code}"


def _api_get(url, auth):
    """GET *url* with the given auth header and return the parsed JSON body."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", auth)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _resolve_repo_id(org, project, repo, auth):
    """Return the Azure DevOps repository id for *repo*, or None."""
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/git/repositories/"
        f"{urllib.parse.quote(repo)}?api-version=7.1"
    )
    return _api_get(url, auth).get("id")


def _resolve_pipeline_id(org, project, repo_id, auth):
    """Return the build/pipeline definition id that builds *repo_id*, or None.

    When several definitions target the repository the deployment pipeline is
    preferred: the one whose name looks like a PR/CI/Veracode validation build
    is skipped, otherwise the first is used.
    """
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/build/definitions?"
        f"repositoryId={urllib.parse.quote(str(repo_id))}"
        f"&repositoryType=TfsGit&api-version=7.1"
    )
    definitions = _api_get(url, auth).get("value") or []
    if not definitions:
        return None
    if len(definitions) == 1:
        return definitions[0].get("id")

    skip_words = ("pr", "veracode", "validation", "nuget")
    preferred = [
        d for d in definitions
        if not any(word in (d.get("name") or "").lower() for word in skip_words)
    ]
    chosen = preferred[0] if preferred else definitions[0]
    return chosen.get("id")


def _queue_run(org, project, pipeline_id, branch, template_parameters, auth):
    """POST a pipeline run and return the parsed JSON response."""
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/pipelines/{pipeline_id}/"
        f"runs?api-version=7.1"
    )
    body = json.dumps({
        "resources": {
            "repositories": {"self": {"refName": f"refs/heads/{branch}"}}
        },
        "templateParameters": template_parameters,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", auth)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_pipeline_for_repo(name, path, branch, environment):
    """Queue a pipeline run for one repo's *branch*. Returns (ok, url_or_error).

    *environment* is "dev" or "acc". On success the second value is a browser
    link to the started run; on failure it is an error message prefixed with the
    repository name.
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository"

    parsed = parse_ado_remote(git_remote_url(path))
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, repo, host = parsed

    auth, err = _auth_for_host(host)
    if err:
        return False, f"{name}: {err}"

    try:
        repo_id = _resolve_repo_id(org, project, repo, auth)
        if not repo_id:
            return False, f"{name}: could not resolve the repository in Azure DevOps"
        pipeline_id = _resolve_pipeline_id(org, project, repo_id, auth)
        if not pipeline_id:
            return False, f"{name}: no pipeline is configured for this repository"
        params = build_template_parameters(path, environment)
        data = _queue_run(org, project, pipeline_id, branch, params, auth)
    except urllib.error.HTTPError as exc:
        return False, f"{name}: pipeline run failed ({exc.code}): {_http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{name}: pipeline run failed: {exc}"

    web = ((data.get("_links") or {}).get("web") or {}).get("href") or ""
    return True, web
