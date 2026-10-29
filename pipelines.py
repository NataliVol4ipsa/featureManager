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

import data_sources
import os
import re
import json
import time
import datetime
import threading
import urllib.parse
import urllib.request
import urllib.error

from gitutils import (
    is_git_repo, git_remote_url, parse_ado_remote,
    remote_branch_head, git_commit_message, git_commit_is_ancestor,
)
from parallel import run_in_parallel
import ado_auth
import pipeline_ids
from config import (
    PIPELINE_YAML_LOCATION,
    PIPELINE_ENVIRONMENT_STAGES,
    PIPELINE_STANDARD_STAGES,
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

# Stages shown for a "Redeploy latest master" monitor row: Production is never
# part of the overview, so a viewed run's Production stage is excluded.
REDEPLOY_VISIBLE_STAGES = ("build", "development", "acceptance")

# Stages tracked by the "View deployment status" feature - Build is excluded,
# it is not a deployment target (only environments are).
DEPLOY_STATUS_STAGE_KEYS = ("development", "acceptance", "production")

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


def _read_pipeline_yaml(repo_path):
    """Return the text of the repo's pipeline YAML, or '' if absent.

    Looks in the configured pipeline YAML location (a repo-relative subfolder,
    e.g. ``deployment``) first, then the repository root, so both layouts are
    handled regardless of where a service keeps its azure-pipelines.yml.
    """
    locations = []
    for loc in (PIPELINE_YAML_LOCATION, ""):
        if loc not in locations:
            locations.append(loc)
    for loc in locations:
        base = os.path.join(repo_path, loc) if loc else repo_path
        for fname in _PIPELINE_YAML_NAMES:
            candidate = os.path.join(base, fname)
            if os.path.isfile(candidate):
                try:
                    with open(candidate, encoding="utf-8") as handle:
                        return handle.read()
                except OSError:
                    return ""
    return ""


def _parse_yaml_parameters_full(text):
    """Return [{name, display, type, default}, ...] from the parameters: block.

    A superset of :func:`_parse_yaml_parameters` that also captures each
    parameter's ``type`` and ``default`` so the run dialog can render the right
    control (checkbox / text field) pre-filled with the template's default. Uses
    the same light, dependency-free scan of the top-level ``parameters:`` block.
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
    cur = None
    for line in lines[start:]:
        # An unindented, non-comment line is the next top-level key: block ends.
        if line and not line[0].isspace() and not line.lstrip().startswith("#"):
            break
        name_match = re.match(r"\s*-\s*name:\s*(.+?)\s*$", line)
        if name_match:
            if cur is not None:
                result.append(cur)
            cur = {
                "name": name_match.group(1).strip().strip("\"'"),
                "display": None, "type": None, "default": None,
            }
            continue
        if cur is None:
            continue
        display_match = re.match(r"\s*displayName:\s*(.+?)\s*$", line)
        if display_match:
            cur["display"] = display_match.group(1).strip().strip("\"'")
            continue
        type_match = re.match(r"\s*type:\s*(.+?)\s*$", line)
        if type_match:
            cur["type"] = type_match.group(1).strip().strip("\"'").lower()
            continue
        default_match = re.match(r"\s*default:\s*(.+?)\s*$", line)
        if default_match:
            cur["default"] = default_match.group(1).strip()
            continue
    if cur is not None:
        result.append(cur)
    return result


def _param_matches_stage(param, stage):
    """Return True when *stage* is the whole name or display phrase of *param*.

    The stage phrase must equal the parameter's name or its full display phrase
    exactly - never a substring - so a custom flag is not misclassified just
    because it happens to contain a stage word (e.g. "Enforce Veracode scan"
    contains "force").
    """
    name = (param.get("name") or "").strip().lower()
    display = (param.get("display") or "").strip().lower()
    return stage == name or stage == display


def _param_role(param):
    """Classify a YAML parameter as ``env``, ``standard`` or None (custom).

    Matches the configured stage phrases (config.json ``pipeline_parameters``)
    against the parameter's display name and name as whole phrases. ``env`` marks
    a deployment target toggle chosen by the environment selection; ``standard``
    marks a recognised template flag; anything matching neither is a custom
    parameter the user is prompted for.
    """
    if any(_param_matches_stage(param, s) for s in PIPELINE_ENVIRONMENT_STAGES):
        return "env"
    if any(_param_matches_stage(param, s) for s in PIPELINE_STANDARD_STAGES):
        return "standard"
    return None


def _coerce_param_default(param_type, raw):
    """Return a Python default value for a YAML parameter's raw default text."""
    is_bool = (param_type or "").lower() == "boolean"
    if raw is None:
        return False if is_bool else ""
    value = raw.strip().strip("\"'")
    if is_bool:
        return value.lower() == "true"
    return value


def get_pipeline_parameters(repo_path):
    """Return the repo's runtime pipeline parameters classified for the UI.

    Each item is ``{name, display, type, default, role, is_env, is_custom}``
    where *role* is ``env`` / ``standard`` / None, *is_env* marks the environment
    deployment toggles that are driven by the environment selection, and
    *is_custom* marks a parameter that is not part of the standard EVC template.
    Returns [] when the repo has no readable pipeline YAML.
    """
    return data_sources.strategy("pipelines").get_pipeline_parameters(repo_path)


def has_custom_pipeline_parameters(repo_path):
    """Return True when the repo declares a non-standard (custom) parameter."""
    return any(p["is_custom"] for p in get_pipeline_parameters(repo_path))


def configurable_pipeline_parameters(repo_path):
    """Return the parameters to show in the run dialog (all but env deployments).

    Environment deployment toggles (Development / Acceptance / Production) are
    left out because they are already chosen by the environment/redeploy
    selection; everything else - Skip build, Force build, Deploy infrastructure,
    Docker caching and any custom flags - is configurable.
    """
    return [p for p in get_pipeline_parameters(repo_path) if not p["is_env"]]


def _discover_role_params(repo_path):
    """Return {role: param_name} discovered from the repo's pipeline YAML.

    Only roles actually found are returned (so undeclared parameters are never
    sent to Azure DevOps). Returns {} when no pipeline YAML is present.
    """
    text = _read_pipeline_yaml(repo_path)
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
    return data_sources.strategy("pipelines").build_template_parameters(
        repo_path, environment)


def build_template_parameters_for_roles(repo_path, deploy_dev, deploy_acc):
    """Return the {parameter_name: bool} template parameters for a combined run.

    Like :func:`build_template_parameters` but the Development and Acceptance
    toggles are chosen independently so a single run can deploy to both (or just
    one). Infrastructure is always on; Production is always off.
    """
    return data_sources.strategy("pipelines").build_template_parameters_for_roles(
        repo_path, deploy_dev, deploy_acc)


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


# Per-key locks so concurrent callers resolve a given repo's ids only once
# (single-flight). Different repos keep different locks and still run in
# parallel; the guard only protects the lock registry, never a network call.
_id_resolve_locks = {}
_id_resolve_guard = threading.Lock()


# --------------------------------------------------------------------------- #
# Historical stage durations (for time-left estimates)
# --------------------------------------------------------------------------- #

def _parse_iso_utc(value):
    """Return an aware UTC datetime for an ISO timestamp, or None.

    Azure DevOps timeline timestamps are UTC (trailing ``Z`` or an explicit
    offset). Normalising everything to UTC lets callers compare a remote stage
    start against the local PC clock (``datetime.now(timezone.utc)``) regardless
    of the machine's timezone.
    """
    if not value:
        return None
    iso = value
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _duration_seconds(start, finish):
    """Return finish-start in seconds for two ISO timestamps, or None."""
    start_dt = _parse_iso_utc(start)
    finish_dt = _parse_iso_utc(finish)
    if start_dt is None or finish_dt is None:
        return None
    return max(0.0, (finish_dt - start_dt).total_seconds())


# --------------------------------------------------------------------------- #
# Granular timeline tree (stages -> phases -> jobs -> tasks)
# --------------------------------------------------------------------------- #

_NODE_TYPES = ("stage", "phase", "job", "task")


# A repo that reached Azure DevOps but has no fully successful master run to
# measure - a stable fact worth caching so it is not rescanned every refresh.
_EMPTY_PROFILE = {"stages": {}, "nodes": {}, "acc_parallel": False, "samples": 0}


def get_master_pipeline_profile(name, path, count=15, scan=40):
    """Return (ok, profile_or_error): a granular timing profile of a pipeline.

    Collects the *count* most recent successful master runs of the repo's
    deployment pipeline and, from each run's build timeline, averages the
    wall-clock duration of *every* node - stage, phase, job and task -
    independently over the runs in which that node ran and succeeded. Because a
    single timeline call already returns the whole stage->phase->job->task tree,
    this granularity costs no extra Azure DevOps requests. Scheduled runs (e.g.
    the nightly Veracode scan) are excluded so only real merge deployments are
    measured.

    On success the profile is::

        {
          "stages": {stage_key: avg_seconds, ...},   # per-stage wall clock
          "nodes":  {node_key: {                     # granular tree
              "type","stage","name","parent","order",
              "avg","count","avg_offset",            # avg_offset = start vs run start
          }, ...},
          "acc_parallel": bool,   # Acceptance overlaps Development
          "samples": int,
        }

    Keeping the full node tree lets the estimator match a *live* run's actual
    structure (which reflects the run's template parameters - skipped stages and
    jobs simply do not appear) instead of blindly summing every historical
    stage. A repo that definitively yields no estimate (not cloned, no ADO
    remote, no deploy pipeline, or no successful runs) returns ``ok=True`` with
    an empty profile so callers cache the "nothing here" answer; only transient
    problems (missing credential, network/HTTP errors) return ``ok=False``.
    """
    return data_sources.strategy("pipelines").get_master_pipeline_profile(name, path, count, scan)


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
    return data_sources.strategy("pipelines").find_env_deployment_for_branch(name, path, branch, environment)


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
        if result in ("canceled", "cancelled"):
            return "canceled"
        if result == "failed":
            return "failed"
        if result in ("succeeded", "partiallysucceeded"):
            return "done"
    return "waiting"


def get_build_errors(org, project, host, build_id):
    """Return (ok, [error_message, ...]) for a build's failed timeline tasks.

    Lazy helper for the PR-status details view: only called when a PR's build
    policy is failed. Reads the build timeline once and collects every ``error``
    issue recorded against a timeline record (prefixed with the record name so
    the failing step is identifiable). Uses the Build-scope PAT (ado_auth), not
    the PR Code-scope Git credential.
    """
    return data_sources.strategy("pipelines").get_build_errors(org, project, host, build_id)


def approve_pipeline_stage(run_info, approval_ids):
    """Manually approve the pending deployment gate of a run. Returns (ok, error).

    *approval_ids* are the pending approval ids reported for the run's current
    gate (see ``pending_approval_ids`` in ``get_pipeline_stage_statuses``).
    """
    return data_sources.strategy("pipelines").approve_pipeline_stage(run_info, approval_ids)


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
    return data_sources.strategy("pipelines").get_pipeline_stage_statuses(run_info)


def rerun_failed_stage(run_info, stage_ref_name):
    """Retry the failed jobs of one pipeline stage. Returns (ok, error).

    Uses the Azure DevOps "Stages - Update" API with state "retry" and
    ``forceRetryAllJobs=false`` so only the failed/canceled jobs of the stage
    are re-run (matching the ADO web "Rerun failed jobs" action). *run_info*
    must contain org, project, host and build_id; *stage_ref_name* is the
    stage identifier reported by ``get_pipeline_stage_statuses``.
    """
    return data_sources.strategy("pipelines").rerun_failed_stage(run_info, stage_ref_name)


def cancel_pipeline_run(run_info):
    """Cancel a still-running pipeline build. Returns (ok, error).

    Sends the Azure DevOps "Update Build" status ``cancelling`` so a run that is
    in progress - including one paused at an approval gate (Acceptance or
    Production waiting for approval) - is stopped. A build that has already
    completed is left unchanged by Azure DevOps. *run_info* must contain org,
    project, host and build_id.
    """
    return data_sources.strategy("pipelines").cancel_pipeline_run(run_info)


def run_pipeline_for_repo_details(name, path, branch, environment, extra_parameters=None):
    """Queue a run and return structured metadata used by the monitor.

    *extra_parameters* is an optional ``{parameter_name: value}`` dict for the
    non-environment run parameters (Skip build, Force build, Deploy
    infrastructure, Docker caching and any custom flags) as configured by the
    user; it overrides the standard defaults while the environment deployment
    toggles stay driven by *environment*.
    """
    return data_sources.strategy("pipelines").run_pipeline_for_repo_details(name, path, branch, environment, extra_parameters)


def rerun_pipeline_from_latest_commit(run_info, override_parameters=None):
    """Queue the pipeline again on the branch tip. Returns (ok, details_or_error).

    Reuses the org/project/host/pipeline_id/branch captured when the run was
    first started, so the new run uses the same identity but from the latest
    commit of the branch (Azure DevOps always queues from the tip of the branch
    ref). *override_parameters* is an optional ``{name: value}`` dict merged over
    the previous run's template parameters so the user can change the run flags.
    """
    return data_sources.strategy("pipelines").rerun_pipeline_from_latest_commit(run_info, override_parameters)


def redeploy_master_for_repo_details(name, path, deploy_dev, deploy_acc):
    """Queue a master-branch run for one repo and return monitor-ready metadata.

    The run is queued on ``refs/heads/master`` (Azure DevOps builds the tip of
    that ref, i.e. the latest master commit) with the Development and Acceptance
    deploy toggles set independently and Production always off. *deploy_dev* and
    *deploy_acc* are booleans; at least one is expected to be true.
    """
    return data_sources.strategy("pipelines").redeploy_master_for_repo_details(name, path, deploy_dev, deploy_acc)


def get_latest_master_pipeline_run_details(name, path):
    """Return (ok, details_or_error) for a repo's newest master deployment run.

    Finds the most recent build on ``refs/heads/master`` regardless of status,
    ignoring scheduled/Veracode scans and non-deployment definitions, and returns
    monitor-ready metadata so the run can be followed live. Production is left out
    of the tracked stages.
    """
    return data_sources.strategy("pipelines").get_latest_master_pipeline_run_details(name, path)


def run_pipeline_for_repo(name, path, branch, environment):
    """Queue a pipeline run for one repo's *branch*. Returns (ok, url_or_error).

    *environment* is "dev" or "acc". On success the second value is a browser
    link to the started run; on failure it is an error message prefixed with the
    repository name.
    """
    return data_sources.strategy("pipelines").run_pipeline_for_repo(name, path, branch, environment)


def get_master_pipeline_run_for_merged_branch_details(name, path, branch):
    """Return (ok, details_or_error) for merged-PR master pipeline run lookup.

    The returned details are monitor-ready and include build id and ADO context.
    """
    return data_sources.strategy("pipelines").get_master_pipeline_run_for_merged_branch_details(name, path, branch)


def get_master_pipeline_run_for_merged_branch(name, path, branch):
    """Return (ok, url_or_error) for the master run tied to *branch*'s merged PR.

    Flow:
      1. Find the latest completed PR from refs/heads/<branch> to master.
      2. Read its merge commit id.
      3. Find the repository pipeline build on refs/heads/master for that commit.
    """
    return data_sources.strategy("pipelines").get_master_pipeline_run_for_merged_branch(name, path, branch)


# --------------------------------------------------------------------------- #
# Work item title + test reports (the "Tested By" linked work items)
# --------------------------------------------------------------------------- #

# Reference name of the "Tested By" (forward) work-item relation.
_TESTED_BY_REL = "Microsoft.VSTS.Common.TestedBy-Forward"


def get_work_item_report_details(org, project, work_item_id, host):
    """Return (ok, details_or_error) for a work item's title and test reports.

    On success *details* is ``{"title": str, "test_reports": [(name, url), ...]}``
    where each test report is a work item linked to *work_item_id* via the
    "Tested By" relation (name is its title, url is its edit page).
    """
    return data_sources.strategy("board").get_work_item_report_details(org, project, work_item_id, host)


def get_work_item_report_details_for_repo(name, path, work_item_id):
    """Return (ok, details_or_error) using *path*'s Azure DevOps org/project.

    Convenience wrapper around ``get_work_item_report_details`` that resolves the
    org/project/host from the repository remote.
    """
    return data_sources.strategy("board").get_work_item_report_details_for_repo(name, path, work_item_id)


# --------------------------------------------------------------------------- #
# Deployment status (latest commit deployed per stage, across master runs)
# --------------------------------------------------------------------------- #

# How many recent completed builds (across every branch) to scan for a
# completed stage before giving up.
_DEPLOY_STATUS_SCAN = 100

# In-memory only (never persisted): an inline "Rerun" changes a stage's finish
# time without reordering the build list, so this is recomputed from the
# timeline each time, but a short TTL avoids re-scanning on every dialog open.
_DEPLOY_STATUS_CACHE_TTL = 120  # seconds
_deploy_status_cache = {}
_deploy_status_cache_lock = threading.Lock()


def get_latest_deployed_artifacts(name, path, force=False):
    """Return (ok, data_or_error) with the latest commit deployed per stage.

    Scans the repository's recent builds of its deploy pipeline (ANY status,
    not just completed - see _get_latest_deployed_artifacts_scan) ACROSS EVERY
    BRANCH (not just master) and, for each of ``DEPLOY_STATUS_STAGE_KEYS``
    (development/acceptance/production - Build is excluded, it is not a
    deployment target), keeps the commit from the newest build whose stage
    actually completed. This mirrors Azure DevOps's own pipeline "Stages" tab,
    which lists the latest run per stage regardless of branch - Development/
    Acceptance are routinely deployed from a feature branch (the app's own
    "Run dev/acc pipelines" actions do exactly that), so a master-only scan
    would report a stale commit for those stages. It also reflects an inline
    "Rerun" correctly even though that does not change a build's position in
    the Runs list. Scheduled runs (e.g. the nightly Veracode scan) and pull
    request validation builds are excluded - PR builds never run a deploy
    stage, so keeping them in the scan window only pushed the real deploy run
    further back and could surface a stale commit.

    (Azure DevOps's own "Stages" tab is powered by an internal, undocumented
    data-provider endpoint that would avoid this scan entirely - tried as an
    optional fast path, but it rejected this app's PAT auth with a 401, so it
    was removed; this scan against the public Build REST API is the only path.)

    On success *data* is ``{stage_key: {"commit","message","build_id","url",
    "triggered_by","deployed_at","commit_created_at"} or None}`` (timestamps
    are the stage finish time and commit author date from existing responses;
    all three stage keys always present; a stage
    with no completed run is None) plus a ``"_pipeline_url"`` entry (the repo's
    deployment pipeline overview page, built from the already-resolved pipeline
    id - no extra HTTP call). Results are cached in memory per repository for a
    short time (``_DEPLOY_STATUS_CACHE_TTL``); pass *force=True* to bypass the
    cache.
    """
    return data_sources.strategy("pipelines").get_latest_deployed_artifacts(name, path, force)


def get_deployment_commit_context(name, path, branch):
    """Return (ok, context_or_error) used to classify a deployed commit's origin.

    *context* carries the master/branch tip commit ids (lowercase) plus the
    local repo *path* so classify_deployed_commit can run exact, on-demand
    ancestry checks per commit via LOCAL git (gitutils.git_commit_is_ancestor),
    not a guessed/bounded Azure DevOps REST query - a repo only ever has a
    handful of distinct deployed commits to classify, so this stays cheap.
    *branch* should be the exact branch this repository uses in its workspace
    (workspaces can override the branch per repo, so it must not be assumed to
    be ``feature/<workspace>``).
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository"

    master_tip = (remote_branch_head(path, "master") or "").lower()
    branch_tip = (
        (remote_branch_head(path, branch) or "").lower()
        if branch and branch != "master" else master_tip
    )

    return True, {
        "master_tip": master_tip,
        "branch_tip": branch_tip,
        "branch": branch,
        "_path": path,
        "_ancestor_cache": {},
    }


def classify_deployed_commit(context, commit):
    """Return one of "master"/"branch_latest"/"branch_older"/"master_older"/
    "unknown" for a deployed *commit*, given a ``get_deployment_commit_context``
    *context*.

    Order matters: a feature branch is cut FROM master, so every master commit
    merged before the branch point is ALSO an ancestor of the branch. "On
    master" must therefore be decided BEFORE "on the branch", otherwise a plain
    merged master commit is mislabelled as branch-only ("branch_older"). Hence
    "branch_older" means exactly "on the feature branch but NOT on master" (i.e.
    genuinely unmerged feature work), which only holds when master ancestry is
    tested first.
    """
    if not context or not commit:
        return "unknown"
    commit = commit.lower()
    if commit == context.get("master_tip"):
        return "master"
    if commit == context.get("branch_tip"):
        return "branch_latest"

    path = context.get("_path")
    if not path:
        return "unknown"

    cache = context.setdefault("_ancestor_cache", {})

    def is_ancestor_of(ref):
        key = (commit, ref)
        if key not in cache:
            cache[key] = git_commit_is_ancestor(path, commit, ref)
        return cache[key]

    if is_ancestor_of("master"):
        return "master_older"
    branch = context.get("branch")
    if branch and branch != "master" and is_ancestor_of(branch):
        return "branch_older"
    return "unknown"


# Compatibility exports for shared helpers and existing offline tests.
from data_sources.ado.pipelines import (
    _auth_for_host,
    _http_error_detail,
    _looks_generic,
    _collect_validation_messages,
    _api_get,
    _api_patch,
    _resolve_repo_id,
    _full_definition,
    _definition_is_live,
    _definition_yaml_basename,
    _resolve_pipeline_id,
    _id_resolve_lock,
    _cached_repo_id,
    _cached_pipeline_id,
    _queue_run,
    _run_source_commit,
    _parse_build_id_from_url,
    _completed_pr_for_branch,
    _pipeline_build_for_commit,
    _latest_master_deploy_build,
    _build_web_url,
    _branch_builds_for_commit,
    _build_stage_states,
    _stage_timings_from_timeline,
    _api_build_timeline_safe,
    _node_key,
    _walk_timeline_nodes,
    _accumulate_profile_nodes,
    _stage_progress,
    _job_queue_position,
    _api_build_timeline,
    _pending_approvals_for_build,
    _approve_pending_approvals,
    _commit_details_from_ado,
    _get_latest_deployed_artifacts_scan,
    _pipeline_web_url,
    _build_triggered_by,
)
from data_sources.ado.board import (
    _work_item_edit_url,
)
