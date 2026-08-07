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
``parameters:`` block), falling back to the standard
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
import datetime
import urllib.parse
import urllib.request
import urllib.error

from gitutils import (
    is_git_repo, git_remote_url, parse_ado_remote, get_git_credential,
    remote_branch_head,
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

# Words in a build/pipeline definition name that mark it as a non-deployment
# build (PR validation, NuGet library, etc.). When a repository has several
# definitions, these are skipped in favour of the deployment pipeline.
_NON_DEPLOY_DEFINITION_WORDS = ("pr", "validation", "nuget", "library")

# Build reasons produced when a run is started by completing a pull request /
# pushing the merge commit to master - exactly what Azure DevOps auto-triggers
# on a manual PR merge. Runs started by any other trigger (notably a scheduled
# security scan, reason "schedule") are excluded, so the master deployment run
# is identified structurally by its trigger reason rather than by name.
_MERGE_BUILD_REASONS = ("individualci", "batchedci", "manual")


def _is_deploy_definition_name(name):
    """Return True when *name* looks like a deployment pipeline (not a PR/library build).

    Matches on whole words only so a repo name that merely contains one of the
    words (e.g. "MyValidations" contains "validation") is not misclassified.
    """
    lowered = (name or "").lower()
    return not any(
        re.search(rf"\b{re.escape(word)}\b", lowered)
        for word in _NON_DEPLOY_DEFINITION_WORDS
    )


PIPELINE_STAGE_KEYS = ("build", "development", "acceptance", "production")
PIPELINE_STAGE_DEFAULTS = {
    "build": "waiting",
    "development": "waiting",
    "acceptance": "waiting",
    "production": "waiting",
}

_STAGE_DONE_STATES = {"done", "skipped"}


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


def _visible_stages_for_run(repo_path, template_parameters):
    """Return ordered monitor stages enabled by the run's template parameters."""
    role_to_name = _discover_role_params(repo_path) or DEFAULT_ROLE_PARAMS
    visible = ["build"]

    dev_name = role_to_name.get("dev")
    if dev_name and bool(template_parameters.get(dev_name)):
        visible.append("development")

    acc_name = role_to_name.get("acc")
    if acc_name and bool(template_parameters.get(acc_name)):
        visible.append("acceptance")

    prod_name = role_to_name.get("prod")
    if prod_name and bool(template_parameters.get(prod_name)):
        visible.append("production")

    return visible


# --------------------------------------------------------------------------- #
# Azure DevOps REST helpers
# --------------------------------------------------------------------------- #

def _auth_for_host(host, org=None):
    """Return (authorization_header, error). Prefers ADO_PAT, then Git creds.

    When *org* is given an org-scoped URL is built for the Git credential
    lookup: Azure DevOps stores dev.azure.com credentials per-organization
    (useHttpPath), so a bare host finds nothing.
    """
    pat = os.environ.get("ADO_PAT", "").strip()
    if pat:
        token = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
        return f"Basic {token}", ""

    url = f"https://{host}/{urllib.parse.quote(org)}" if org else None
    username, password = get_git_credential(host, url)
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
    """Extract a human-readable message from an HTTPError response body.

    The body is cached on the exception because an HTTPError can only be read
    once; callers may inspect the detail more than once (e.g. to classify the
    error and then to report it).
    """
    cached = getattr(exc, "_cached_detail", None)
    if cached is not None:
        return cached
    raw = exc.read().decode("utf-8", "replace").strip()
    detail = raw
    try:
        payload = json.loads(raw)
        detail = payload.get("message", raw)
        # The classic Build Queue API reports "validation errors or warnings"
        # generically; the real reasons live in validationResults (sometimes
        # nested under customProperties), so gather every message we can find.
        extra = _collect_validation_messages(payload)
        if extra:
            detail = "; ".join([detail] + extra) if detail else "; ".join(extra)
        elif detail == payload.get("message") and _looks_generic(detail):
            # No structured detail from ADO: fall back to the raw body so the
            # real cause is at least visible rather than the generic sentence.
            detail = f"{detail} [body: {raw[:600]}]"
    except ValueError:
        pass
    result = detail or f"HTTP {exc.code}"
    try:
        exc._cached_detail = result
    except (AttributeError, TypeError):
        pass
    return result


def _looks_generic(message):
    """Return True for ADO's uninformative 'validation errors or warnings' text."""
    return "validation errors or warnings" in (message or "").lower()


def _collect_validation_messages(payload):
    """Return distinct validation messages found anywhere in an ADO error body.

    Handles validationResults at the top level and nested under
    customProperties, plus any dict with a 'result' + 'message' shape.
    """
    messages = []

    def visit(node):
        if isinstance(node, dict):
            msg = node.get("message")
            if node.get("result") and isinstance(msg, str) and msg.strip():
                messages.append(msg.strip())
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    for key in ("validationResults", "customProperties"):
        visit(payload.get(key))
    seen = set()
    unique = []
    for msg in messages:
        if msg not in seen:
            seen.add(msg)
            unique.append(msg)
    return unique


def _api_get(url, auth):
    """GET *url* with the given auth header and return the parsed JSON body."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", auth)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _api_patch(url, body, auth):
    """PATCH *url* with *body* and return parsed JSON (or {} for empty body)."""
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="PATCH")
    req.add_header("Authorization", auth)
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8").strip()
    return json.loads(raw) if raw else {}


def _resolve_repo_id(org, project, repo, auth):
    """Return the Azure DevOps repository id for *repo*, or None."""
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/git/repositories/"
        f"{urllib.parse.quote(repo)}?api-version=7.1"
    )
    return _api_get(url, auth).get("id")


def _full_definition(org, project, definition_id, auth):
    """Return the full build definition (triggers, process/YAML), or {} on error."""
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/build/definitions/"
        f"{urllib.parse.quote(str(definition_id))}?api-version=7.1"
    )
    try:
        return _api_get(url, auth)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return {}


def _definition_is_live(full_def):
    """Return True for a runnable modern deploy pipeline, by structure not name.

    A live pipeline is enabled, YAML-based, and CI-triggered - exactly what Azure
    DevOps auto-runs when a branch merges. A retired definition (e.g. one renamed
    "To be removed", whose agent pool is often deleted) has no CI trigger and no
    YAML, so it fails this test without any name matching.
    """
    if (full_def.get("queueStatus") or "enabled") != "enabled":
        return False
    if not ((full_def.get("process") or {}).get("yamlFilename")):
        return False
    triggers = full_def.get("triggers") or []
    return any((t.get("triggerType") or "") == "continuousIntegration" for t in triggers)


def _definition_yaml_basename(full_def):
    """Return the lower-cased basename of a definition's YAML file, or ''."""
    yaml = (full_def.get("process") or {}).get("yamlFilename") or ""
    return os.path.basename(yaml).lower()


def _resolve_pipeline_id(org, project, repo_id, auth):
    """Return the build/pipeline definition id that builds *repo_id*, or None.

    When several definitions target the repository the live deployment pipeline
    is chosen by structural signals (CI trigger + YAML) rather than by name:
    retired definitions are dropped, PR/library builds are skipped, and
    the one whose YAML is the standard deploy file (azure-pipelines.yml) wins,
    with the "EVC-<repo>" naming as a final tie-break.
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

    # Fetch each in full so triggers/YAML are available (the list omits them).
    detailed = [
        _full_definition(org, project, d.get("id"), auth) or d
        for d in definitions
    ]

    live = [d for d in detailed if _definition_is_live(d)] or detailed
    deploy = [d for d in live if _is_deploy_definition_name(d.get("name"))] or live
    standard = [d for d in deploy if _definition_yaml_basename(d) in _PIPELINE_YAML_NAMES]
    pool = standard or deploy
    evc = [d for d in pool if (d.get("name") or "").lower().startswith("evc-")]
    chosen = (evc or pool)[0]
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


def _parse_build_id_from_url(url):
    """Return buildId integer parsed from a build results URL, else None."""
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    values = urllib.parse.parse_qs(parsed.query)
    raw = (values.get("buildId") or [None])[0]
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _completed_pr_for_branch(org, project, repo, branch, auth):
    """Return the latest completed PR object for *branch* -> master, or None."""
    query = urllib.parse.urlencode({
        "searchCriteria.sourceRefName": f"refs/heads/{branch}",
        "searchCriteria.targetRefName": "refs/heads/master",
        "searchCriteria.status": "completed",
        "$top": "50",
        "api-version": "7.1",
    })
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/git/repositories/"
        f"{urllib.parse.quote(repo)}/pullrequests?{query}"
    )
    prs = (_api_get(url, auth).get("value") or [])
    if not prs:
        return None
    prs.sort(key=lambda pr: pr.get("closedDate") or "", reverse=True)
    return prs[0]


def _pipeline_build_for_commit(org, project, repo_id, commit_id, auth):
    """Return the newest master build for *commit_id* across all definitions."""
    query = urllib.parse.urlencode({
        "repositoryId": str(repo_id),
        "repositoryType": "TfsGit",
        "branchName": "refs/heads/master",
        "queryOrder": "queueTimeDescending",
        "$top": "200",
        "api-version": "7.1",
    })
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/build/builds?{query}"
    )
    builds = (_api_get(url, auth).get("value") or [])
    commit_id = (commit_id or "").lower()
    # Only consider runs started by the merge itself. Completing a pull request
    # pushes the merge commit to master, which Azure DevOps builds with a CI
    # merge reason; a scheduled scan of the same pipeline uses a different reason
    # and must never be shown as the master deployment run.
    matched = [
        build for build in builds
        if (build.get("sourceVersion") or "").lower() == commit_id
        and (build.get("reason") or "").lower() in _MERGE_BUILD_REASONS
    ]
    if not matched:
        return None
    # A repo can have several definitions build the same merge commit (e.g. a
    # deployment pipeline plus a NuGet "Library" build). Prefer the deployment
    # pipeline so viewing opens the run that actually deploys the branch.
    deploy_builds = [
        build for build in matched
        if _is_deploy_definition_name((build.get("definition") or {}).get("name"))
    ]
    candidates = deploy_builds or matched
    candidates.sort(
        key=lambda build: build.get("queueTime") or "",
        reverse=True,
    )
    return candidates[0]


def _build_web_url(org, project, build):
    """Return a browser URL for a build result page."""
    build_id = build.get("id")
    if build_id:
        return (
            f"https://dev.azure.com/{urllib.parse.quote(org)}/"
            f"{urllib.parse.quote(project)}/_build/results?buildId={build_id}&view=results"
        )
    web = ((build.get("_links") or {}).get("web") or {}).get("href") or ""
    if web:
        return web
    return ""


def _branch_builds_for_commit(org, project, repo_id, branch, commit_id, auth):
    """Return deploy builds run on *branch* for *commit_id*, newest first."""
    query = urllib.parse.urlencode({
        "repositoryId": str(repo_id),
        "repositoryType": "TfsGit",
        "branchName": f"refs/heads/{branch}",
        "queryOrder": "queueTimeDescending",
        "$top": "100",
        "api-version": "7.1",
    })
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/build/builds?{query}"
    )
    builds = (_api_get(url, auth).get("value") or [])
    commit_id = (commit_id or "").lower()
    matched = [
        build for build in builds
        if (build.get("sourceVersion") or "").lower() == commit_id
    ]
    deploy = [
        build for build in matched
        if _is_deploy_definition_name((build.get("definition") or {}).get("name"))
    ]
    return deploy or matched


def _build_stage_states(org, project, build_id, auth):
    """Return {stage_key: state} for a build's timeline stages."""
    timeline = _api_build_timeline(org, project, int(build_id), auth)
    stages = {}
    for record in timeline.get("records") or []:
        if (record.get("type") or "").lower() != "stage":
            continue
        key = _timeline_stage_key(record.get("name") or record.get("identifier"))
        if not key:
            continue
        stages[key] = _timeline_state(record)
    return stages


def find_env_deployment_for_branch(name, path, branch, environment):
    """Check whether origin/*branch*'s tip already deployed to *environment*.

    Returns (ok, info). *info* always carries the Azure DevOps context needed to
    later queue or display a run:
      {
        "already_deployed": bool,   # tip commit deployed to env successfully
        "commit": str,              # branch tip commit sha ('' if unknown)
        "org","project","host","repo","branch","pipeline_id",
        "build_id": int|None,       # the successful deploy build, if any
        "url": str,                 # that build's web URL
        "visible_stages": [...],    # stages that build ran
      }
    A "successful deployment" means the environment's stage (Development for dev,
    Acceptance for acc) of a build for the tip commit completed successfully. On
    any lookup failure returns (False, error_message); callers treat that as
    "unknown" and keep the repo selected for deployment.
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository"

    parsed = parse_ado_remote(git_remote_url(path))
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, repo, host = parsed

    auth, err = _auth_for_host(host, org)
    if err:
        return False, f"{name}: {err}"

    commit = remote_branch_head(path, branch)
    info = {
        "already_deployed": False,
        "commit": commit,
        "org": org, "project": project, "host": host, "repo": repo,
        "branch": branch, "pipeline_id": None,
        "build_id": None, "url": "", "visible_stages": [],
    }
    if not commit:
        return False, f"{name}: could not read the remote branch tip"

    target = "development" if environment == "dev" else "acceptance"
    try:
        repo_id = _resolve_repo_id(org, project, repo, auth)
        if not repo_id:
            return False, f"{name}: could not resolve the repository in Azure DevOps"
        info["pipeline_id"] = _resolve_pipeline_id(org, project, repo_id, auth)
        for build in _branch_builds_for_commit(
            org, project, repo_id, branch, commit, auth
        ):
            build_id = build.get("id")
            if build_id is None:
                continue
            status = (build.get("status") or "").lower()
            stages = _build_stage_states(org, project, build_id, auth)
            target_state = stages.get(target)
            build_running = status in ("inprogress", "notstarted", "postponed")
            # Accept when the env is already deployed (done) or deploying
            # (running), or when the pipeline is still running and its env stage
            # is queued/running - i.e. it will deploy once the build finishes.
            # Reject failed/canceled/skipped stages and different commits (the
            # latter are filtered out earlier).
            accepted = target_state in ("done", "running") or (
                build_running and target_state in ("waiting", "running")
            )
            if accepted:
                info["already_deployed"] = True
                info["build_id"] = int(build_id)
                info["url"] = _build_web_url(org, project, build)
                info["visible_stages"] = [
                    key for key in PIPELINE_STAGE_KEYS if key in stages
                ]
                break
    except urllib.error.HTTPError as exc:
        return False, (
            f"{name}: deployment check failed ({exc.code}): "
            f"{_http_error_detail(exc)}"
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{name}: deployment check failed: {exc}"

    return True, info


def _timeline_stage_key(name):
    """Map a timeline stage name to one of the canonical stage keys, or None."""
    text = (name or "").strip().lower()
    if not text:
        return None
    if "build" in text:
        return "build"
    if "develop" in text or text == "dev":
        return "development"
    if "accept" in text or text == "acc":
        return "acceptance"
    if "prod" in text:
        return "production"
    return None


def _timeline_state(record):
    """Map Azure timeline record state/result to a UI stage state."""
    state = (record.get("state") or "").lower()
    result = (record.get("result") or "").lower()
    if state in ("inprogress", "in_progress"):
        return "running"
    if state in ("pending", "notstarted", "queued"):
        return "waiting"
    if state == "completed":
        if result == "skipped":
            return "skipped"
        if result in ("failed", "canceled", "cancelled"):
            return "failed"
        if result in ("succeeded", "partiallysucceeded"):
            return "done"
    return "waiting"


def _api_build_timeline(org, project, build_id, auth):
    """Return timeline JSON for a build id."""
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/build/builds/{build_id}/timeline"
        f"?api-version=7.1"
    )
    return _api_get(url, auth)


def _pending_approvals_for_build(org, project, build_id, auth):
    """Return pending approvals for *build_id* as [{"id", "partially_approved"}].

    *partially_approved* is True when at least one approval step is already
    recorded (e.g. the first of two required Production approvals is in): the
    gate is still pending but must not be auto-approved again.
    """
    query = urllib.parse.urlencode({
        "state": "pending",
        "$expand": "steps",
        "api-version": "7.1-preview.1",
    })
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/pipelines/approvals?{query}"
    )
    pending = (_api_get(url, auth).get("value") or [])
    approvals = []
    for approval in pending:
        owner = ((approval.get("pipeline") or {}).get("owner") or {})
        if str(owner.get("id") or "") != str(build_id):
            continue
        approval_id = approval.get("id")
        if not approval_id:
            continue
        steps = approval.get("steps") or []
        partially = any(
            (step.get("status") or "").lower() == "approved" for step in steps
        )
        approvals.append({
            "id": str(approval_id),
            "partially_approved": partially,
        })
    return approvals


def _approve_pending_approvals(org, project, approval_ids, auth):
    """Approve all *approval_ids*. Returns (ok, error_message)."""
    if not approval_ids:
        return True, ""
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/pipelines/approvals?"
        f"api-version=7.1-preview.1"
    )
    body = [
        {
            "approvalId": approval_id,
            "status": "approved",
            "comment": "Auto-approved by Feature Manager",
        }
        for approval_id in approval_ids
    ]
    try:
        _api_patch(url, body, auth)
        return True, ""
    except urllib.error.HTTPError as exc:
        return False, f"auto-approval failed ({exc.code}): {_http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"auto-approval failed: {exc}"


def get_pipeline_stage_statuses(run_info):
    """Return (ok, data_or_error) for the run status of a started pipeline.

    *run_info* must contain: org, project, host, build_id.
    On success the payload contains:
      {
        "build_id": int,
        "updated_at": "ISO-UTC",
        "stages": {
          "build": "waiting|running|failed|skipped|done",
          "development": ...,
          "acceptance": ...,
          "production": ...,
        }
      }
    """
    org = run_info.get("org")
    project = run_info.get("project")
    host = run_info.get("host")
    build_id = run_info.get("build_id")
    if not org or not project or not host or build_id is None:
        return False, "run info is missing org/project/host/build_id"

    auth, err = _auth_for_host(host, org)
    if err:
        return False, err

    try:
        timeline = _api_build_timeline(org, project, int(build_id), auth)
    except urllib.error.HTTPError as exc:
        return False, f"timeline lookup failed ({exc.code}): {_http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"timeline lookup failed: {exc}"

    stages = dict(PIPELINE_STAGE_DEFAULTS)
    stage_identifiers = {}
    for record in timeline.get("records") or []:
        if (record.get("type") or "").lower() != "stage":
            continue
        key = _timeline_stage_key(record.get("name") or record.get("identifier"))
        if not key:
            continue
        stages[key] = _timeline_state(record)
        # The stage refName is needed to retry the stage's failed jobs.
        stage_identifiers[key] = record.get("identifier") or record.get("name")

    pending_approval_ids = []
    any_partially_approved = False
    try:
        pending = _pending_approvals_for_build(
            org, project, int(build_id), auth
        )
        pending_approval_ids = [a["id"] for a in pending]
        any_partially_approved = any(a["partially_approved"] for a in pending)
    except urllib.error.HTTPError as exc:
        # Non-fatal for status rendering: keep stage view even if approvals fail.
        pending_approval_ids = []
    except (urllib.error.URLError, OSError, ValueError):
        pending_approval_ids = []

    visible_stages = list(run_info.get("visible_stages") or [])

    def _stage_complete_or_not_applicable(stage_key):
        if visible_stages and stage_key not in visible_stages:
            return True
        return stages.get(stage_key) in _STAGE_DONE_STATES

    # Infer which gate is pending approval from stage progression.
    approval_target = ""
    if pending_approval_ids:
        # Acceptance may depend only on build (not development), so ADO can open
        # its approval while dev is still running; auto-approve once build is done.
        if (
            stages.get("acceptance") == "waiting"
            and _stage_complete_or_not_applicable("build")
        ):
            stages["acceptance"] = "approval"
            approval_target = "acceptance"
        elif (
            stages.get("production") == "waiting"
            and _stage_complete_or_not_applicable("build")
            and _stage_complete_or_not_applicable("development")
            and _stage_complete_or_not_applicable("acceptance")
        ):
            stages["production"] = "approval"
            approval_target = "production"

    # A partially approved gate (e.g. the first of Production's two required
    # approvals is already in) is shown as "ready" and never auto-approved again.
    if approval_target and any_partially_approved:
        stages[approval_target] = "ready"

    autoapprove_acceptance = bool(run_info.get("autoapprove_acceptance")) or (
        run_info.get("environment") == "acc"
        and bool(run_info.get("autoapprove_acc"))
    )
    autoapprove_production = bool(run_info.get("autoapprove_production"))

    autoapproved = False
    autoapproved_target = ""
    autoapprove_error = ""
    if (
        approval_target == "acceptance"
        and autoapprove_acceptance
        and pending_approval_ids
        and not any_partially_approved
        and not run_info.get("_autoapprove_acceptance_done")
    ):
        ok_approve, approve_error = _approve_pending_approvals(
            org, project, pending_approval_ids, auth
        )
        if ok_approve:
            run_info["_autoapprove_acceptance_done"] = True
            autoapproved = True
            autoapproved_target = "acceptance"
            stages["acceptance"] = "running"
        else:
            autoapprove_error = approve_error
    elif (
        approval_target == "production"
        and autoapprove_production
        and pending_approval_ids
        and not any_partially_approved
        and not run_info.get("_autoapprove_production_done")
    ):
        ok_approve, approve_error = _approve_pending_approvals(
            org, project, pending_approval_ids, auth
        )
        if ok_approve:
            run_info["_autoapprove_production_done"] = True
            autoapproved = True
            autoapproved_target = "production"
            stages["production"] = "running"
        else:
            autoapprove_error = approve_error

    return True, {
        "build_id": int(build_id),
        "updated_at": datetime.datetime.now(
            datetime.timezone.utc
        ).astimezone().isoformat(timespec="seconds"),
        "stages": stages,
        "stage_identifiers": stage_identifiers,
        "approval_target": approval_target,
        "autoapproved": autoapproved,
        "autoapproved_target": autoapproved_target,
        "autoapprove_error": autoapprove_error,
    }


def rerun_failed_stage(run_info, stage_ref_name):
    """Retry the failed jobs of one pipeline stage. Returns (ok, error).

    Uses the Azure DevOps "Stages - Update" API with state "retry" and
    ``forceRetryAllJobs=false`` so only the failed/canceled jobs of the stage
    are re-run (matching the ADO web "Rerun failed jobs" action). *run_info*
    must contain org, project, host and build_id; *stage_ref_name* is the
    stage identifier reported by ``get_pipeline_stage_statuses``.
    """
    org = run_info.get("org")
    project = run_info.get("project")
    host = run_info.get("host")
    build_id = run_info.get("build_id")
    if not org or not project or not host or build_id is None:
        return False, "run info is missing org/project/host/build_id"
    if not stage_ref_name:
        return False, "this stage cannot be retried yet (no stage id available)"

    auth, err = _auth_for_host(host, org)
    if err:
        return False, err

    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/build/builds/{int(build_id)}/"
        f"stages/{urllib.parse.quote(str(stage_ref_name))}"
        f"?api-version=7.1-preview.1"
    )
    body = {"state": "retry", "forceRetryAllJobs": False}
    try:
        _api_patch(url, body, auth)
        return True, ""
    except urllib.error.HTTPError as exc:
        return False, f"rerun failed ({exc.code}): {_http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"rerun failed: {exc}"


def run_pipeline_for_repo_details(name, path, branch, environment):
    """Queue a run and return structured metadata used by the monitor."""
    if not is_git_repo(path):
        return False, f"{name}: not a git repository"

    parsed = parse_ado_remote(git_remote_url(path))
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, repo, host = parsed

    auth, err = _auth_for_host(host, org)
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
        visible_stages = _visible_stages_for_run(path, params)
        data = _queue_run(org, project, pipeline_id, branch, params, auth)
    except urllib.error.HTTPError as exc:
        return False, f"{name}: pipeline run failed ({exc.code}): {_http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{name}: pipeline run failed: {exc}"

    url = ((data.get("_links") or {}).get("web") or {}).get("href") or ""
    build_id = _parse_build_id_from_url(url)
    if build_id is None:
        try:
            build_id = int(data.get("id"))
        except (TypeError, ValueError):
            build_id = None
    return True, {
        "url": url,
        "build_id": build_id,
        "org": org,
        "project": project,
        "repo": repo,
        "host": host,
        "branch": branch,
        "pipeline_id": pipeline_id,
        "visible_stages": visible_stages,
        "template_parameters": params,
    }


def rerun_pipeline_from_latest_commit(run_info):
    """Queue the pipeline again on the branch tip with the same parameters.

    Returns (ok, details_or_error). Reuses the org/project/host/pipeline_id/
    branch/template_parameters captured when the run was first started, so the
    new run uses exactly the same parameters but from the latest commit of the
    branch (Azure DevOps always queues from the tip of the branch ref).
    """
    org = run_info.get("org")
    project = run_info.get("project")
    host = run_info.get("host")
    pipeline_id = run_info.get("pipeline_id")
    branch = run_info.get("branch")
    params = run_info.get("template_parameters") or {}
    if not (org and project and host and pipeline_id and branch):
        return False, "run info is missing org/project/host/pipeline_id/branch"

    auth, err = _auth_for_host(host, org)
    if err:
        return False, err

    try:
        data = _queue_run(org, project, pipeline_id, branch, params, auth)
    except urllib.error.HTTPError as exc:
        return False, f"pipeline run failed ({exc.code}): {_http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"pipeline run failed: {exc}"

    url = ((data.get("_links") or {}).get("web") or {}).get("href") or ""
    build_id = _parse_build_id_from_url(url)
    if build_id is None:
        try:
            build_id = int(data.get("id"))
        except (TypeError, ValueError):
            build_id = None
    return True, {
        "url": url,
        "build_id": build_id,
        "org": org,
        "project": project,
        "repo": run_info.get("repo"),
        "host": host,
        "branch": branch,
        "pipeline_id": pipeline_id,
        "visible_stages": list(run_info.get("visible_stages") or []),
        "template_parameters": dict(params),
    }


def run_pipeline_for_repo(name, path, branch, environment):
    """Queue a pipeline run for one repo's *branch*. Returns (ok, url_or_error).

    *environment* is "dev" or "acc". On success the second value is a browser
    link to the started run; on failure it is an error message prefixed with the
    repository name.
    """
    ok, result = run_pipeline_for_repo_details(name, path, branch, environment)
    if not ok:
        return False, result
    return True, result.get("url") or ""


def get_master_pipeline_run_for_merged_branch_details(name, path, branch):
    """Return (ok, details_or_error) for merged-PR master pipeline run lookup.

    The returned details are monitor-ready and include build id and ADO context.
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository"
    if not branch:
        return False, f"{name}: branch is empty"

    parsed = parse_ado_remote(git_remote_url(path))
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, repo, host = parsed

    auth, err = _auth_for_host(host, org)
    if err:
        return False, f"{name}: {err}"

    try:
        repo_id = _resolve_repo_id(org, project, repo, auth)
        if not repo_id:
            return False, f"{name}: could not resolve the repository in Azure DevOps"

        pr = _completed_pr_for_branch(org, project, repo, branch, auth)
        if not pr:
            return False, (
                f"{name}: no completed pull request found for branch '{branch}' to master"
            )
        merge_commit = ((pr.get("lastMergeCommit") or {}).get("commitId") or "")
        if not merge_commit:
            return False, (
                f"{name}: completed pull request has no merge commit for branch '{branch}'"
            )

        build = _pipeline_build_for_commit(org, project, repo_id, merge_commit, auth)
        if not build:
            return False, (
                f"{name}: no master pipeline run found yet for merged commit "
                f"{merge_commit[:8]} from branch '{branch}'"
            )

    except urllib.error.HTTPError as exc:
        return False, f"{name}: pipeline run lookup failed ({exc.code}): {_http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{name}: pipeline run lookup failed: {exc}"

    web = _build_web_url(org, project, build)
    if not web:
        return False, f"{name}: pipeline run found but could not build a web URL"
    return True, {
        "url": web,
        "build_id": build.get("id"),
        "org": org,
        "project": project,
        "repo": repo,
        "host": host,
        "branch": branch,
    }


def get_master_pipeline_run_for_merged_branch(name, path, branch):
    """Return (ok, url_or_error) for the master run tied to *branch*'s merged PR.

    Flow:
      1. Find the latest completed PR from refs/heads/<branch> to master.
      2. Read its merge commit id.
      3. Find the repository pipeline build on refs/heads/master for that commit.
    """
    ok, result = get_master_pipeline_run_for_merged_branch_details(name, path, branch)
    if not ok:
        return False, result
    return True, result.get("url") or ""


# --------------------------------------------------------------------------- #
# Work item title + test reports (the "Tested By" linked work items)
# --------------------------------------------------------------------------- #

# Reference name of the "Tested By" (forward) work-item relation.
_TESTED_BY_REL = "Microsoft.VSTS.Common.TestedBy-Forward"


def _work_item_edit_url(org, project, work_item_id):
    """Return the human-facing Azure DevOps edit URL for a work item."""
    return (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_workitems/edit/{work_item_id}"
    )


def get_work_item_report_details(org, project, work_item_id, host):
    """Return (ok, details_or_error) for a work item's title and test reports.

    On success *details* is ``{"title": str, "test_reports": [(name, url), ...]}``
    where each test report is a work item linked to *work_item_id* via the
    "Tested By" relation (name is its title, url is its edit page).
    """
    auth, err = _auth_for_host(host, org)
    if err:
        return False, err

    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/wit/workitems/"
        f"{urllib.parse.quote(str(work_item_id))}"
        "?$expand=relations&api-version=7.1"
    )
    try:
        data = _api_get(url, auth)
    except urllib.error.HTTPError as exc:
        return False, (
            f"work item {work_item_id} lookup failed ({exc.code}): "
            f"{_http_error_detail(exc)}"
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"work item {work_item_id} lookup failed: {exc}"

    title = ((data.get("fields") or {}).get("System.Title") or "").strip()

    tested_by_ids = []
    for rel in data.get("relations") or []:
        if (rel.get("rel") or "") != _TESTED_BY_REL:
            continue
        rel_id = (rel.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
        if rel_id.isdigit() and rel_id not in tested_by_ids:
            tested_by_ids.append(rel_id)

    test_reports = []
    for rid in tested_by_ids:
        report_url = (
            f"https://dev.azure.com/{urllib.parse.quote(org)}/"
            f"{urllib.parse.quote(project)}/_apis/wit/workitems/"
            f"{urllib.parse.quote(rid)}?fields=System.Title&api-version=7.1"
        )
        try:
            report_data = _api_get(report_url, auth)
            name = ((report_data.get("fields") or {}).get("System.Title")
                    or "").strip() or f"Work item {rid}"
        except (urllib.error.HTTPError, urllib.error.URLError,
                OSError, ValueError):
            name = f"Work item {rid}"
        test_reports.append((name, _work_item_edit_url(org, project, rid)))

    return True, {"title": title, "test_reports": test_reports}


def get_work_item_report_details_for_repo(name, path, work_item_id):
    """Return (ok, details_or_error) using *path*'s Azure DevOps org/project.

    Convenience wrapper around ``get_work_item_report_details`` that resolves the
    org/project/host from the repository remote.
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository"
    parsed = parse_ado_remote(git_remote_url(path))
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, _repo, host = parsed
    return get_work_item_report_details(org, project, work_item_id, host)
