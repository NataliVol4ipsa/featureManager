"""ADO pipelines strategy; existing provider behavior."""

from ..contracts import PipelineSource
import ado_auth
import datetime
import json
import os
import pipeline_ids
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request



def _auth_for_host(host, org=None):
    """Return (authorization_header, error) for Azure DevOps REST calls.

    Delegates to the shared in-memory auth-header cache so the credential is
    derived (ADO_PAT, then Git) at most once per host until it expires.
    """
    return ado_auth.auth_header(host, org)


def _http_error_detail(exc):
    """Extract a human-readable message from an HTTPError response body.

    The body is cached on the exception because an HTTPError can only be read
    once; callers may inspect the detail more than once (e.g. to classify the
    error and then to report it).
    """
    import pipelines
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
        extra = pipelines._collect_validation_messages(payload)
        if extra:
            detail = "; ".join([detail] + extra) if detail else "; ".join(extra)
        elif detail == payload.get("message") and pipelines._looks_generic(detail):
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
    """Return (repo_id, project_id) for *repo* in Azure DevOps, or (None, None).

    Both come from the same repository lookup, so the project id is captured for
    free alongside the repository id.
    """
    import pipelines
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/git/repositories/"
        f"{urllib.parse.quote(repo)}?api-version=7.1"
    )
    data = pipelines._api_get(url, auth)
    return data.get("id"), (data.get("project") or {}).get("id")


def _full_definition(org, project, definition_id, auth):
    """Return the full build definition (triggers, process/YAML), or {} on error."""
    import pipelines
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/build/definitions/"
        f"{urllib.parse.quote(str(definition_id))}?api-version=7.1"
    )
    try:
        return pipelines._api_get(url, auth)
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
    import pipelines
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/build/definitions?"
        f"repositoryId={urllib.parse.quote(str(repo_id))}"
        f"&repositoryType=TfsGit&api-version=7.1"
    )
    definitions = pipelines._api_get(url, auth).get("value") or []
    if not definitions:
        return None
    if len(definitions) == 1:
        return definitions[0].get("id")

    # Fetch each in full so triggers/YAML are available (the list omits them).
    detailed = [
        pipelines._full_definition(org, project, d.get("id"), auth) or d
        for d in definitions
    ]

    live = [d for d in detailed if pipelines._definition_is_live(d)] or detailed
    deploy = [d for d in live if pipelines._is_deploy_definition_name(d.get("name"))] or live
    standard = [d for d in deploy if pipelines._definition_yaml_basename(d) in pipelines._PIPELINE_YAML_NAMES]
    pool = standard or deploy
    evc = [d for d in pool if (d.get("name") or "").lower().startswith("evc-")]
    chosen = (evc or pool)[0]
    return chosen.get("id")


def _id_resolve_lock(key):
    """Return the shared lock for *key*, creating it on first use."""
    import pipelines
    with pipelines._id_resolve_guard:
        lock = pipelines._id_resolve_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            pipelines._id_resolve_locks[key] = lock
        return lock


def _cached_repo_id(org, project, repo, auth):
    """Return the repository id, consulting the persistent id cache first.

    A repository never changes its id, so a cached value is trusted; a miss
    resolves it from Azure DevOps and stores it for next time. Concurrent
    callers for the same repo (e.g. a deploy probe and a pipeline monitor)
    resolve it only once via the per-key lock; the rest reuse the stored value.
    """
    import pipelines
    cached = pipeline_ids.get_repo_id(org, project, repo)
    if cached:
        return cached
    with pipelines._id_resolve_lock(("repo", org, project, repo)):
        # Another thread may have resolved it while we waited for the lock.
        cached = pipeline_ids.get_repo_id(org, project, repo)
        if cached:
            return cached
        repo_id, project_id = pipelines._resolve_repo_id(org, project, repo, auth)
        if repo_id:
            pipeline_ids.store(
                org, project, repo, repo_id=repo_id, project_id=project_id
            )
        return repo_id


def _cached_pipeline_id(org, project, repo, repo_id, auth):
    """Return the deploy pipeline id, consulting the persistent id cache first.

    Resolving it can cost one extra REST call per definition when a repo has
    several, so the result is cached; it is only re-resolved after the cache is
    cleared from the Settings menu. Concurrent callers for the same repo resolve
    it only once via the per-key lock; the rest reuse the stored value.
    """
    import pipelines
    cached = pipeline_ids.get_pipeline_id(org, project, repo)
    if cached:
        return cached
    with pipelines._id_resolve_lock(("pipeline", org, project, repo)):
        cached = pipeline_ids.get_pipeline_id(org, project, repo)
        if cached:
            return cached
        pipeline_id = pipelines._resolve_pipeline_id(org, project, repo_id, auth)
        if pipeline_id:
            pipeline_ids.store(org, project, repo, pipeline_id=pipeline_id)
        return pipeline_id


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


def _run_source_commit(data):
    """Return the commit sha a queued run resolved for its 'self' repo, or ''."""
    self_repo = (
        ((data.get("resources") or {}).get("repositories") or {}).get("self")
        or {}
    )
    return (self_repo.get("version") or "").strip()


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
    import pipelines
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
    prs = (pipelines._api_get(url, auth).get("value") or [])
    if not prs:
        return None
    prs.sort(key=lambda pr: pr.get("closedDate") or "", reverse=True)
    return prs[0]


def _pipeline_build_for_commit(org, project, repo_id, commit_id, auth):
    """Return the newest master build for *commit_id* across all definitions."""
    import pipelines
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
    builds = (pipelines._api_get(url, auth).get("value") or [])
    commit_id = (commit_id or "").lower()
    # Only consider runs started by the merge itself. Completing a pull request
    # pushes the merge commit to master, which Azure DevOps builds with a CI
    # merge reason; a scheduled scan of the same pipeline uses a different reason
    # and must never be shown as the master deployment run.
    matched = [
        build for build in builds
        if (build.get("sourceVersion") or "").lower() == commit_id
        and (build.get("reason") or "").lower() in pipelines._MERGE_BUILD_REASONS
    ]
    if not matched:
        return None
    # A repo can have several definitions build the same merge commit (e.g. a
    # deployment pipeline plus a NuGet "Library" build). Prefer the deployment
    # pipeline so viewing opens the run that actually deploys the branch.
    deploy_builds = [
        build for build in matched
        if pipelines._is_deploy_definition_name((build.get("definition") or {}).get("name"))
    ]
    candidates = deploy_builds or matched
    candidates.sort(
        key=lambda build: build.get("queueTime") or "",
        reverse=True,
    )
    return candidates[0]


def _latest_master_deploy_build(org, project, repo_id, auth):
    """Return the newest master deployment build, or None.

    Considers every recent build on ``refs/heads/master`` regardless of status
    (running, failed, succeeded), but excludes non-deployment definitions
    (PR/library builds) and scheduled runs such as the nightly Veracode scan, so
    the row follows the real master deployment pipeline.
    """
    import pipelines
    query = urllib.parse.urlencode({
        "repositoryId": str(repo_id),
        "repositoryType": "TfsGit",
        "branchName": "refs/heads/master",
        "queryOrder": "queueTimeDescending",
        "$top": "100",
        "api-version": "7.1",
    })
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/build/builds?{query}"
    )
    builds = (pipelines._api_get(url, auth).get("value") or [])
    candidates = [
        build for build in builds
        if (build.get("reason") or "").lower() != "schedule"
        and pipelines._is_deploy_definition_name((build.get("definition") or {}).get("name"))
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda build: build.get("queueTime") or "", reverse=True)
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
    import pipelines
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
    builds = (pipelines._api_get(url, auth).get("value") or [])
    commit_id = (commit_id or "").lower()
    matched = [
        build for build in builds
        if (build.get("sourceVersion") or "").lower() == commit_id
    ]
    deploy = [
        build for build in matched
        if pipelines._is_deploy_definition_name((build.get("definition") or {}).get("name"))
    ]
    return deploy or matched


def _build_stage_states(org, project, build_id, auth):
    """Return {stage_key: state} for a build's timeline stages."""
    import pipelines
    timeline = pipelines._api_build_timeline(org, project, int(build_id), auth)
    stages = {}
    for record in timeline.get("records") or []:
        if (record.get("type") or "").lower() != "stage":
            continue
        key = pipelines._timeline_stage_key(record.get("name") or record.get("identifier"))
        if not key:
            continue
        stages[key] = pipelines._timeline_state(record)
    return stages


def _stage_timings_from_timeline(timeline):
    """Return {stage_key: {"state","start","finish"}} from a build timeline."""
    import pipelines
    result = {}
    for record in (timeline or {}).get("records") or []:
        if (record.get("type") or "").lower() != "stage":
            continue
        key = pipelines._timeline_stage_key(record.get("name") or record.get("identifier"))
        if not key:
            continue
        result[key] = {
            "state": pipelines._timeline_state(record),
            "start": record.get("startTime"),
            "finish": record.get("finishTime"),
        }
    return result


def _stage_timings_from_timeline(timeline):
    """Return {stage_key: {"state","start","finish"}} from a build timeline."""
    import pipelines
    result = {}
    for record in (timeline or {}).get("records") or []:
        if (record.get("type") or "").lower() != "stage":
            continue
        key = pipelines._timeline_stage_key(record.get("name") or record.get("identifier"))
        if not key:
            continue
        result[key] = {
            "state": pipelines._timeline_state(record),
            "start": record.get("startTime"),
            "finish": record.get("finishTime"),
        }
    return result


def _api_build_timeline_safe(org, project, build_id, auth):
    """Return a build timeline, or {} on any lookup error (never raises).

    Azure DevOps occasionally answers a burst of concurrent timeline requests
    with ``409 Conflict`` (or throttles with ``429``); a couple of short,
    backing-off retries clear that without failing the whole refresh.
    """
    import pipelines
    delay = 0.4
    for attempt in range(3):
        try:
            return pipelines._api_build_timeline(org, project, int(build_id), auth)
        except urllib.error.HTTPError as exc:
            if exc.code in (409, 429) and attempt < 2:
                time.sleep(delay)
                delay *= 2
                continue
            return {}
        except (urllib.error.URLError, OSError, ValueError, TypeError):
            return {}
    return {}


def _node_key(stage_key, segments):
    """Return a stable path key for a timeline node.

    *segments* is the ordered ``(type, identifier, order)`` chain from the
    stage's direct child down to the node (empty for the stage record itself).
    The identifier (or name for tasks, which have none) plus the sibling order
    keeps matrix jobs and repeated step names distinct, and stays stable across
    runs of the same pipeline definition.
    """
    parts = [stage_key]
    for node_type, ident, order in segments:
        parts.append(f"{node_type[0]}:{ident}#{order}")
    return "/".join(parts)


def _walk_timeline_nodes(timeline):
    """Return ``(run_start_iso, nodes)`` for a build timeline.

    *nodes* maps each node's stable key (see :func:`_node_key`) to::

        {"type","stage","name","order","state","start","finish","parent"}

    where ``stage`` is the canonical stage key the node belongs to and
    ``parent`` is the key of its enclosing node (the stage key for a top-level
    child, ``None`` for a stage). The same walk is used both to aggregate
    historical averages and to read a live run's structure, so a live run keys
    identically to its cached profile.
    """
    import pipelines
    records = (timeline or {}).get("records") or []
    by_id = {r.get("id"): r for r in records if r.get("id")}

    def rtype(record):
        return (record.get("type") or "").lower()

    def ancestry(record):
        """Return ``(stage_key, segments)`` from stage-child down to *record*."""
        chain = []
        current = record
        seen = set()
        while current is not None:
            cid = current.get("id")
            if cid in seen:
                return None, None
            seen.add(cid)
            if rtype(current) == "stage":
                stage_key = pipelines._timeline_stage_key(
                    current.get("name") or current.get("identifier")
                )
                chain.reverse()
                return stage_key, chain
            ident = current.get("identifier") or current.get("name") or ""
            chain.append((rtype(current), ident, current.get("order") or 0))
            current = by_id.get(current.get("parentId"))
        return None, None

    nodes = {}
    starts = []
    for record in records:
        node_type = rtype(record)
        if node_type not in pipelines._NODE_TYPES:
            continue
        if node_type == "stage":
            stage_key = pipelines._timeline_stage_key(
                record.get("name") or record.get("identifier")
            )
            if not stage_key:
                continue
            key = stage_key
            parent = None
        else:
            stage_key, segments = ancestry(record)
            if not stage_key or not segments:
                continue
            key = pipelines._node_key(stage_key, segments)
            parent = pipelines._node_key(stage_key, segments[:-1])
        nodes[key] = {
            "type": node_type,
            "stage": stage_key,
            "name": record.get("name") or "",
            "order": record.get("order") or 0,
            "state": pipelines._timeline_state(record),
            "start": record.get("startTime"),
            "finish": record.get("finishTime"),
            "parent": parent,
        }
        started = pipelines._parse_iso_utc(record.get("startTime"))
        if started is not None:
            starts.append(started)

    run_start = min(starts).isoformat() if starts else None
    return run_start, nodes


def _accumulate_profile_nodes(agg, run_start_iso, nodes):
    """Fold one run's completed nodes into the running aggregate *agg*.

    For every node that ran and succeeded, accumulate its wall-clock duration
    and its start offset relative to the run start (used later to reason about
    which nodes overlap in time - i.e. run in parallel). *agg* is keyed by the
    node's stable path key.
    """
    import pipelines
    run_start = pipelines._parse_iso_utc(run_start_iso)
    for key, node in nodes.items():
        if node.get("state") != "done":
            continue
        secs = pipelines._duration_seconds(node.get("start"), node.get("finish"))
        if secs is None:
            continue
        entry = agg.get(key)
        if entry is None:
            entry = agg[key] = {
                "type": node.get("type"),
                "stage": node.get("stage"),
                "name": node.get("name") or "",
                "parent": node.get("parent"),
                "order": node.get("order") or 0,
                "sum": 0.0,
                "count": 0,
                "sum_offset": 0.0,
                "offset_count": 0,
            }
        entry["sum"] += secs
        entry["count"] += 1
        started = pipelines._parse_iso_utc(node.get("start"))
        if run_start is not None and started is not None:
            entry["sum_offset"] += (started - run_start).total_seconds()
            entry["offset_count"] += 1


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
    import pipelines
    # Stable "no estimate" facts about the repo: report empty so it is cached.
    if not pipelines.is_git_repo(path):
        return True, dict(pipelines._EMPTY_PROFILE)

    parsed = pipelines.parse_ado_remote(pipelines.git_remote_url(path))
    if not parsed:
        return True, dict(pipelines._EMPTY_PROFILE)
    org, project, repo, host = parsed

    # A missing credential is transient (VPN/PAT): keep it uncached to retry.
    auth, err = pipelines._auth_for_host(host, org)
    if err:
        return False, f"{name}: {err}"

    try:
        repo_id = pipelines._cached_repo_id(org, project, repo, auth)
        if not repo_id:
            return True, dict(pipelines._EMPTY_PROFILE)
        pipeline_id = pipelines._cached_pipeline_id(org, project, repo, repo_id, auth)
        if not pipeline_id:
            # No deploy pipeline (e.g. a plain NuGet repo): cache the empty result.
            return True, dict(pipelines._EMPTY_PROFILE)
        query = urllib.parse.urlencode({
            "definitions": str(pipeline_id),
            "branchName": "refs/heads/master",
            "statusFilter": "completed",
            "resultFilter": "succeeded,partiallySucceeded",
            "queryOrder": "finishTimeDescending",
            "$top": str(scan),
            "api-version": "7.1",
        })
        url = (
            f"https://dev.azure.com/{urllib.parse.quote(org)}/"
            f"{urllib.parse.quote(project)}/_apis/build/builds?{query}"
        )
        builds = pipelines._api_get(url, auth).get("value") or []
    except urllib.error.HTTPError as exc:
        return False, f"{name}: run history lookup failed ({exc.code}): {pipelines._http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{name}: run history lookup failed: {exc}"

    # Only real merge deployments; drop scheduled scans (e.g. nightly Veracode).
    builds = [
        build for build in builds
        if (build.get("reason") or "").lower() in pipelines._MERGE_BUILD_REASONS
    ]
    if not builds:
        return True, dict(pipelines._EMPTY_PROFILE)

    # One timeline call per build returns the whole node tree; fan them out but
    # keep the pool bounded (and the calls retry on 409/429) so we do not throttle.
    timelines = pipelines.run_in_parallel(
        builds,
        lambda b: pipelines._api_build_timeline_safe(org, project, b.get("id"), auth),
    )

    agg = {}
    parallel_votes = 0
    overlap_samples = 0
    runs_used = 0
    for timeline in timelines:
        run_start_iso, nodes = pipelines._walk_timeline_nodes(timeline)
        # A run contributes if any of its nodes completed successfully.
        if not any(n.get("state") == "done" for n in nodes.values()):
            continue
        pipelines._accumulate_profile_nodes(agg, run_start_iso, nodes)
        # Decide whether Acceptance overlaps Development (parallel) or strictly
        # follows it, from runs where both stages ran.
        dev = nodes.get("development") or {}
        acc = nodes.get("acceptance") or {}
        if dev.get("state") == "done" and acc.get("state") == "done":
            dev_finish = pipelines._parse_iso_utc(dev.get("finish"))
            acc_start = pipelines._parse_iso_utc(acc.get("start"))
            if dev_finish and acc_start:
                overlap_samples += 1
                if acc_start < dev_finish:
                    parallel_votes += 1
        runs_used += 1
        if runs_used >= count:
            break

    if not agg:
        return True, dict(pipelines._EMPTY_PROFILE)

    profile_nodes = {}
    stages = {}
    for key, entry in agg.items():
        avg = entry["sum"] / entry["count"]
        node = {
            "type": entry["type"],
            "stage": entry["stage"],
            "name": entry["name"],
            "parent": entry["parent"],
            "order": entry["order"],
            "avg": avg,
            "count": entry["count"],
            "avg_offset": (
                entry["sum_offset"] / entry["offset_count"]
                if entry["offset_count"] else 0.0
            ),
        }
        profile_nodes[key] = node
        if entry["type"] == "stage":
            stages[entry["stage"]] = avg

    acc_parallel = overlap_samples > 0 and parallel_votes * 2 >= overlap_samples
    return True, {
        "stages": stages,
        "nodes": profile_nodes,
        "acc_parallel": acc_parallel,
        "samples": runs_used,
    }


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
    import pipelines
    if not pipelines.is_git_repo(path):
        return False, f"{name}: not a git repository"

    parsed = pipelines.parse_ado_remote(pipelines.git_remote_url(path))
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, repo, host = parsed

    auth, err = pipelines._auth_for_host(host, org)
    if err:
        return False, f"{name}: {err}"

    commit = pipelines.remote_branch_head(path, branch)
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
        repo_id = pipelines._cached_repo_id(org, project, repo, auth)
        if not repo_id:
            return False, f"{name}: could not resolve the repository in Azure DevOps"
        info["pipeline_id"] = pipelines._cached_pipeline_id(org, project, repo, repo_id, auth)
        for build in pipelines._branch_builds_for_commit(
            org, project, repo_id, branch, commit, auth
        ):
            build_id = build.get("id")
            if build_id is None:
                continue
            status = (build.get("status") or "").lower()
            stages = pipelines._build_stage_states(org, project, build_id, auth)
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
                info["url"] = pipelines._build_web_url(org, project, build)
                info["visible_stages"] = [
                    key for key in pipelines.PIPELINE_STAGE_KEYS if key in stages
                ]
                break
    except urllib.error.HTTPError as exc:
        return False, (
            f"{name}: deployment check failed ({exc.code}): "
            f"{pipelines._http_error_detail(exc)}"
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{name}: deployment check failed: {exc}"

    return True, info


def _stage_progress(records, stage_id, auth):
    """Return {'current': step_name, 'percent': int} for a running stage.

    Steps are weighted by count, not time. Each phase (the "big chunks" under a
    stage - e.g. deploy infra / push docker / deploy software) is an equal slice
    of the stage, and each of the running phase's tasks is an equal slice of that
    phase. Phases (not jobs) are the unit because a not-yet-started job has only
    a phase record - its job/task records appear when it runs - so counting jobs
    would ignore queued chunks and overstate the percentage. When the next phase
    is queued waiting for an agent, the current step reports the queue position.
    Returns None when the stage has no phases.
    """
    import pipelines
    by_id = {r.get("id"): r for r in records if r.get("id")}

    def rtype(record):
        return (record.get("type") or "").lower()

    def stage_ancestor(record):
        seen = set()
        current = record
        while current is not None:
            parent_id = current.get("parentId")
            if not parent_id or parent_id in seen:
                return None
            seen.add(parent_id)
            parent = by_id.get(parent_id)
            if parent is None:
                return None
            if rtype(parent) == "stage":
                return parent.get("id")
            current = parent
        return None

    # Chunks are phases; fall back to jobs for pipelines without phase records.
    chunks = [r for r in records
              if rtype(r) == "phase" and stage_ancestor(r) == stage_id]
    if not chunks:
        chunks = [r for r in records
                  if rtype(r) == "job" and stage_ancestor(r) == stage_id]
    if not chunks:
        return None
    chunks.sort(key=lambda r: r.get("order") or 0)

    def chunk_task_fraction(chunk):
        """Return (fraction_done, running_task_name) for one chunk's tasks."""
        job_ids = {
            r.get("id") for r in records
            if rtype(r) == "job" and r.get("parentId") == chunk.get("id")
        }
        if rtype(chunk) == "job":
            job_ids.add(chunk.get("id"))
        tasks = [
            r for r in records
            if rtype(r) == "task" and r.get("parentId") in job_ids
        ]
        if not tasks:
            return 0.0, ""
        done_tasks = sum(
            1 for t in tasks if (t.get("state") or "").lower() == "completed"
        )
        running_task = next(
            (t for t in tasks
             if (t.get("state") or "").lower() in ("inprogress", "in_progress")),
            None,
        )
        return done_tasks / len(tasks), (running_task or {}).get("name") or ""

    total = len(chunks)
    # Jobs can run in parallel / out of order, so count every completed phase -
    # not just those preceding the running one.
    done = sum(
        1 for c in chunks if (c.get("state") or "").lower() == "completed"
    )
    running = [
        c for c in chunks
        if (c.get("state") or "").lower() in ("inprogress", "in_progress")
    ]
    if running:
        fraction_sum = 0.0
        current_name = ""
        for chunk in running:
            fraction, task_name = chunk_task_fraction(chunk)
            fraction_sum += fraction
            if not current_name:
                current_name = task_name or chunk.get("name") or ""
        percent = int(round((done + fraction_sum) / total * 100))
        # Never show a full bar while a phase is still running.
        percent = min(percent, 99)
        return {"current": current_name, "percent": percent}

    # No phase is running: the earliest queued one is waiting for an agent.
    pending = next(
        (c for c in chunks
         if (c.get("state") or "").lower() in ("pending", "notstarted", "queued")),
        None,
    )
    if pending is not None:
        percent = int(round(done / total * 100))
        job = next(
            (r for r in records
             if rtype(r) == "job" and r.get("parentId") == pending.get("id")),
            None,
        )
        log_url = ((job or pending).get("log") or {}).get("url")
        position = pipelines._job_queue_position(log_url, auth)
        current = (
            f"(waiting in queue: {position})" if position is not None
            else "(waiting for agent)"
        )
        return {"current": current, "percent": percent, "waiting_for_agent": True}

    return {"current": "", "percent": 100}


def _job_queue_position(log_url, auth):
    """Return the agent-queue position parsed from a queued job's log, or None.

    Only called while a job is waiting for an agent, when its log is tiny.
    """
    if not log_url:
        return None
    try:
        req = urllib.request.Request(log_url, method="GET")
        req.add_header("Authorization", auth)
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", "replace")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return None
    # Several positions may be logged as the queue advances; the last is current.
    matches = re.findall(r"position in queue:\s*(\d+)", text, re.IGNORECASE)
    return int(matches[-1]) if matches else None


def _api_build_timeline(org, project, build_id, auth):
    """Return timeline JSON for a build id."""
    import pipelines
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/build/builds/{build_id}/timeline"
        f"?api-version=7.1"
    )
    return pipelines._api_get(url, auth)


def get_build_errors(org, project, host, build_id):
    """Return (ok, [error_message, ...]) for a build's failed timeline tasks.

    Lazy helper for the PR-status details view: only called when a PR's build
    policy is failed. Reads the build timeline once and collects every ``error``
    issue recorded against a timeline record (prefixed with the record name so
    the failing step is identifiable). Uses the Build-scope PAT (ado_auth), not
    the PR Code-scope Git credential.
    """
    import pipelines
    auth, err = pipelines._auth_for_host(host, org)
    if err:
        return False, err
    try:
        timeline = pipelines._api_build_timeline(org, project, int(build_id), auth)
    except urllib.error.HTTPError as exc:
        return False, f"build log lookup failed ({exc.code}): {pipelines._http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"build log lookup failed: {exc}"

    errors = []
    for record in timeline.get("records") or []:
        for issue in record.get("issues") or []:
            if (issue.get("type") or "").lower() == "error":
                name = record.get("name") or record.get("identifier") or ""
                message = (issue.get("message") or "").strip()
                errors.append(f"{name}: {message}" if name else message)
    return True, errors


def _pending_approvals_for_build(org, project, build_id, auth):
    """Return pending approvals for *build_id* as [{"id", "partially_approved"}].

    *partially_approved* is True when at least one approval step is already
    recorded (e.g. the first of two required Production approvals is in): the
    gate is still pending but must not be auto-approved again.
    """
    import pipelines
    query = urllib.parse.urlencode({
        "state": "pending",
        "$expand": "steps",
        "api-version": "7.1-preview.1",
    })
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/pipelines/approvals?{query}"
    )
    pending = (pipelines._api_get(url, auth).get("value") or [])
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


def _approve_pending_approvals(org, project, approval_ids, auth,
                              comment="Auto-approved by Feature Manager"):
    """Approve all *approval_ids*. Returns (ok, error_message)."""
    import pipelines
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
            "comment": comment,
        }
        for approval_id in approval_ids
    ]
    try:
        pipelines._api_patch(url, body, auth)
        return True, ""
    except urllib.error.HTTPError as exc:
        return False, f"approval failed ({exc.code}): {pipelines._http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"approval failed: {exc}"


def approve_pipeline_stage(run_info, approval_ids):
    """Manually approve the pending deployment gate of a run. Returns (ok, error).

    *approval_ids* are the pending approval ids reported for the run's current
    gate (see ``pending_approval_ids`` in ``get_pipeline_stage_statuses``).
    """
    import pipelines
    if not approval_ids:
        return False, "no pending approval to approve"
    org = run_info.get("org")
    project = run_info.get("project")
    host = run_info.get("host")
    if not org or not project or not host:
        return False, "run info is missing org/project/host"
    auth, err = pipelines._auth_for_host(host, org)
    if err:
        return False, err
    return pipelines._approve_pending_approvals(
        org, project, list(approval_ids), auth,
        comment="Approved via Feature Manager",
    )


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
    import pipelines
    org = run_info.get("org")
    project = run_info.get("project")
    host = run_info.get("host")
    build_id = run_info.get("build_id")
    if not org or not project or not host or build_id is None:
        return False, "run info is missing org/project/host/build_id"

    auth, err = pipelines._auth_for_host(host, org)
    if err:
        return False, err

    try:
        timeline = pipelines._api_build_timeline(org, project, int(build_id), auth)
    except urllib.error.HTTPError as exc:
        return False, f"timeline lookup failed ({exc.code}): {pipelines._http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"timeline lookup failed: {exc}"

    stages = dict(pipelines.PIPELINE_STAGE_DEFAULTS)
    stage_identifiers = {}
    stage_records = {}
    records = timeline.get("records") or []
    for record in records:
        if (record.get("type") or "").lower() != "stage":
            continue
        key = pipelines._timeline_stage_key(record.get("name") or record.get("identifier"))
        if not key:
            continue
        stages[key] = pipelines._timeline_state(record)
        # The stage refName is needed to retry the stage's failed jobs.
        stage_identifiers[key] = record.get("identifier") or record.get("name")
        stage_records[key] = record

    # Per-stage start/finish timestamps, used to estimate remaining time.
    stage_times = {
        key: {"start": record.get("startTime"), "finish": record.get("finishTime")}
        for key, record in stage_records.items()
    }

    # A stage that never appears in this run's timeline is not part of its
    # pipeline (e.g. a master run with no Development deployment). Once Build
    # has finished, treat any still-absent stage as skipped so the monitor
    # drops it - a long line between the neighbours - instead of showing a
    # phantom "waiting" gate, and stops counting it toward the time-left estimate.
    present_keys = set(stage_records)
    build_finished = stages.get("build") not in ("waiting", "running")
    if present_keys and build_finished:
        for key in list(stages):
            if key not in present_keys:
                stages[key] = "skipped"

    # For running stages, derive the current step + step-count completion %
    # from the same timeline (no extra API calls).
    stage_progress = {}
    for key, record in stage_records.items():
        if stages.get(key) == "running":
            progress = pipelines._stage_progress(records, record.get("id"), auth)
            if progress:
                stage_progress[key] = progress

    pending_approval_ids = []
    any_partially_approved = False
    try:
        pending = pipelines._pending_approvals_for_build(
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
        return stages.get(stage_key) in pipelines._STAGE_DONE_STATES

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
    want_target = ""
    if approval_target == "acceptance" and autoapprove_acceptance:
        want_target = "acceptance"
    elif approval_target == "production" and autoapprove_production:
        want_target = "production"

    if want_target and pending_approval_ids and not any_partially_approved:
        # Guard per approval id, not with a once-only boolean: a failed-stage
        # retry reopens the gate with a NEW approval id, so tracking approved ids
        # lets the retry be auto-approved again while still avoiding re-hammering
        # the same id before ADO finishes processing it.
        approved_ids = run_info.setdefault("_approved_approval_ids", [])
        new_ids = [aid for aid in pending_approval_ids if aid not in approved_ids]
        if new_ids:
            ok_approve, approve_error = pipelines._approve_pending_approvals(
                org, project, new_ids, auth
            )
            if ok_approve:
                approved_ids.extend(new_ids)
                autoapproved = True
                autoapproved_target = want_target
                stages[want_target] = "running"
            else:
                autoapprove_error = approve_error

    return True, {
        "build_id": int(build_id),
        "updated_at": datetime.datetime.now(
            datetime.timezone.utc
        ).astimezone().isoformat(timespec="seconds"),
        "stages": stages,
        "stage_identifiers": stage_identifiers,
        "stage_progress": stage_progress,
        "stage_times": stage_times,
        "approval_target": approval_target,
        "pending_approval_ids": pending_approval_ids,
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
    import pipelines
    org = run_info.get("org")
    project = run_info.get("project")
    host = run_info.get("host")
    build_id = run_info.get("build_id")
    if not org or not project or not host or build_id is None:
        return False, "run info is missing org/project/host/build_id"
    if not stage_ref_name:
        return False, "this stage cannot be retried yet (no stage id available)"

    auth, err = pipelines._auth_for_host(host, org)
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
        pipelines._api_patch(url, body, auth)
        return True, ""
    except urllib.error.HTTPError as exc:
        return False, f"rerun failed ({exc.code}): {pipelines._http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"rerun failed: {exc}"


def cancel_pipeline_run(run_info):
    """Cancel a still-running pipeline build. Returns (ok, error).

    Sends the Azure DevOps "Update Build" status ``cancelling`` so a run that is
    in progress - including one paused at an approval gate (Acceptance or
    Production waiting for approval) - is stopped. A build that has already
    completed is left unchanged by Azure DevOps. *run_info* must contain org,
    project, host and build_id.
    """
    import pipelines
    org = run_info.get("org")
    project = run_info.get("project")
    host = run_info.get("host")
    build_id = run_info.get("build_id")
    if not org or not project or not host or build_id is None:
        return False, "run info is missing org/project/host/build_id"

    auth, err = pipelines._auth_for_host(host, org)
    if err:
        return False, err

    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/build/builds/{int(build_id)}"
        f"?api-version=7.1"
    )
    try:
        pipelines._api_patch(url, {"status": "cancelling"}, auth)
        return True, ""
    except urllib.error.HTTPError as exc:
        return False, f"cancel failed ({exc.code}): {pipelines._http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"cancel failed: {exc}"


def run_pipeline_for_repo_details(name, path, branch, environment,
                                  extra_parameters=None):
    """Queue a run and return structured metadata used by the monitor.

    *extra_parameters* is an optional ``{parameter_name: value}`` dict for the
    non-environment run parameters (Skip build, Force build, Deploy
    infrastructure, Docker caching and any custom flags) as configured by the
    user; it overrides the standard defaults while the environment deployment
    toggles stay driven by *environment*.
    """
    import pipelines
    if not pipelines.is_git_repo(path):
        return False, f"{name}: not a git repository"

    parsed = pipelines.parse_ado_remote(pipelines.git_remote_url(path))
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, repo, host = parsed

    auth, err = pipelines._auth_for_host(host, org)
    if err:
        return False, f"{name}: {err}"

    try:
        repo_id = pipelines._cached_repo_id(org, project, repo, auth)
        if not repo_id:
            return False, f"{name}: could not resolve the repository in Azure DevOps"
        pipeline_id = pipelines._cached_pipeline_id(org, project, repo, repo_id, auth)
        if not pipeline_id:
            return False, f"{name}: no pipeline is configured for this repository"
        params = pipelines.build_template_parameters(path, environment)
        if extra_parameters:
            params.update(extra_parameters)
        visible_stages = pipelines._visible_stages_for_run(path, params)
        data = pipelines._queue_run(org, project, pipeline_id, branch, params, auth)
    except urllib.error.HTTPError as exc:
        return False, f"{name}: pipeline run failed ({exc.code}): {pipelines._http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{name}: pipeline run failed: {exc}"

    url = ((data.get("_links") or {}).get("web") or {}).get("href") or ""
    build_id = pipelines._parse_build_id_from_url(url)
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
        "repo_path": path,
        "host": host,
        "branch": branch,
        "pipeline_id": pipeline_id,
        "visible_stages": visible_stages,
        "template_parameters": params,
    }


def rerun_pipeline_from_latest_commit(run_info, override_parameters=None):
    """Queue the pipeline again on the branch tip. Returns (ok, details_or_error).

    Reuses the org/project/host/pipeline_id/branch captured when the run was
    first started, so the new run uses the same identity but from the latest
    commit of the branch (Azure DevOps always queues from the tip of the branch
    ref). *override_parameters* is an optional ``{name: value}`` dict merged over
    the previous run's template parameters so the user can change the run flags.
    """
    import pipelines
    org = run_info.get("org")
    project = run_info.get("project")
    host = run_info.get("host")
    pipeline_id = run_info.get("pipeline_id")
    branch = run_info.get("branch")
    params = dict(run_info.get("template_parameters") or {})
    if override_parameters:
        params.update(override_parameters)
    if not (org and project and host and pipeline_id and branch):
        return False, "run info is missing org/project/host/pipeline_id/branch"

    auth, err = pipelines._auth_for_host(host, org)
    if err:
        return False, err

    try:
        data = pipelines._queue_run(org, project, pipeline_id, branch, params, auth)
    except urllib.error.HTTPError as exc:
        return False, f"pipeline run failed ({exc.code}): {pipelines._http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"pipeline run failed: {exc}"

    url = ((data.get("_links") or {}).get("web") or {}).get("href") or ""
    build_id = pipelines._parse_build_id_from_url(url)
    if build_id is None:
        try:
            build_id = int(data.get("id"))
        except (TypeError, ValueError):
            build_id = None
    # Record which commit the branch tip resolved to, so "Run new" shows a
    # commit id/message in pipeline history like the initial run does.
    path = run_info.get("repo_path")
    commit_id = pipelines._run_source_commit(data) or pipelines.remote_branch_head(path, branch)
    commit_message = pipelines.git_commit_message(path, commit_id) if (path and commit_id) else ""
    return True, {
        "url": url,
        "build_id": build_id,
        "org": org,
        "project": project,
        "repo": run_info.get("repo"),
        "repo_path": run_info.get("repo_path"),
        "host": host,
        "branch": branch,
        "pipeline_id": pipeline_id,
        "visible_stages": list(run_info.get("visible_stages") or []),
        "template_parameters": dict(params),
        "commit_id": commit_id,
        "commit_message": commit_message,
    }


def redeploy_master_for_repo_details(name, path, deploy_dev, deploy_acc):
    """Queue a master-branch run for one repo and return monitor-ready metadata.

    The run is queued on ``refs/heads/master`` (Azure DevOps builds the tip of
    that ref, i.e. the latest master commit) with the Development and Acceptance
    deploy toggles set independently and Production always off. *deploy_dev* and
    *deploy_acc* are booleans; at least one is expected to be true.
    """
    import pipelines
    if not pipelines.is_git_repo(path):
        return False, f"{name}: not a git repository"

    parsed = pipelines.parse_ado_remote(pipelines.git_remote_url(path))
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, repo, host = parsed

    auth, err = pipelines._auth_for_host(host, org)
    if err:
        return False, f"{name}: {err}"

    try:
        repo_id = pipelines._cached_repo_id(org, project, repo, auth)
        if not repo_id:
            return False, f"{name}: could not resolve the repository in Azure DevOps"
        pipeline_id = pipelines._cached_pipeline_id(org, project, repo, repo_id, auth)
        if not pipeline_id:
            return False, f"{name}: no pipeline is configured for this repository"
        params = pipelines.build_template_parameters_for_roles(path, deploy_dev, deploy_acc)
        visible_stages = pipelines._visible_stages_for_run(path, params)
        data = pipelines._queue_run(org, project, pipeline_id, "master", params, auth)
    except urllib.error.HTTPError as exc:
        return False, f"{name}: pipeline run failed ({exc.code}): {pipelines._http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{name}: pipeline run failed: {exc}"

    url = ((data.get("_links") or {}).get("web") or {}).get("href") or ""
    build_id = pipelines._parse_build_id_from_url(url)
    if build_id is None:
        try:
            build_id = int(data.get("id"))
        except (TypeError, ValueError):
            build_id = None
    # Record which master commit the run built so a triggered "cleanup" master
    # run still shows its commit in history (the run response usually carries it;
    # fall back to the remote master tip).
    commit_id = pipelines._run_source_commit(data) or pipelines.remote_branch_head(path, "master")
    return True, {
        "url": url,
        "build_id": build_id,
        "org": org,
        "project": project,
        "repo": repo,
        "repo_path": path,
        "host": host,
        "branch": "master",
        "pipeline_id": pipeline_id,
        "visible_stages": visible_stages,
        "template_parameters": params,
        "commit_id": commit_id,
    }


def get_latest_master_pipeline_run_details(name, path):
    """Return (ok, details_or_error) for a repo's newest master deployment run.

    Finds the most recent build on ``refs/heads/master`` regardless of status,
    ignoring scheduled/Veracode scans and non-deployment definitions, and returns
    monitor-ready metadata so the run can be followed live. Production is left out
    of the tracked stages.
    """
    import pipelines
    if not pipelines.is_git_repo(path):
        return False, f"{name}: not a git repository"

    parsed = pipelines.parse_ado_remote(pipelines.git_remote_url(path))
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, repo, host = parsed

    auth, err = pipelines._auth_for_host(host, org)
    if err:
        return False, f"{name}: {err}"

    try:
        repo_id = pipelines._cached_repo_id(org, project, repo, auth)
        if not repo_id:
            return False, f"{name}: could not resolve the repository in Azure DevOps"
        build = pipelines._latest_master_deploy_build(org, project, repo_id, auth)
        if not build:
            return False, f"{name}: no master pipeline run found"
        pipeline_id = pipelines._cached_pipeline_id(org, project, repo, repo_id, auth)
    except urllib.error.HTTPError as exc:
        return False, f"{name}: pipeline run lookup failed ({exc.code}): {pipelines._http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{name}: pipeline run lookup failed: {exc}"

    web = pipelines._build_web_url(org, project, build)
    if not web:
        return False, f"{name}: pipeline run found but could not build a web URL"
    return True, {
        "url": web,
        "build_id": build.get("id"),
        "org": org,
        "project": project,
        "repo": repo,
        "repo_path": path,
        "host": host,
        "branch": "master",
        "pipeline_id": pipeline_id,
        "visible_stages": list(pipelines.REDEPLOY_VISIBLE_STAGES),
        "commit_id": (build.get("sourceVersion") or ""),
    }


def run_pipeline_for_repo(name, path, branch, environment):
    """Queue a pipeline run for one repo's *branch*. Returns (ok, url_or_error).

    *environment* is "dev" or "acc". On success the second value is a browser
    link to the started run; on failure it is an error message prefixed with the
    repository name.
    """
    import pipelines
    ok, result = pipelines.run_pipeline_for_repo_details(name, path, branch, environment)
    if not ok:
        return False, result
    return True, result.get("url") or ""


def get_master_pipeline_run_for_merged_branch_details(name, path, branch):
    """Return (ok, details_or_error) for merged-PR master pipeline run lookup.

    The returned details are monitor-ready and include build id and ADO context.
    """
    import pipelines
    if not pipelines.is_git_repo(path):
        return False, f"{name}: not a git repository"
    if not branch:
        return False, f"{name}: branch is empty"

    parsed = pipelines.parse_ado_remote(pipelines.git_remote_url(path))
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, repo, host = parsed

    auth, err = pipelines._auth_for_host(host, org)
    if err:
        return False, f"{name}: {err}"

    try:
        repo_id = pipelines._cached_repo_id(org, project, repo, auth)
        if not repo_id:
            return False, f"{name}: could not resolve the repository in Azure DevOps"

        pr = pipelines._completed_pr_for_branch(org, project, repo, branch, auth)
        if not pr:
            return False, (
                f"{name}: no completed pull request found for branch '{branch}' to master"
            )
        merge_commit = ((pr.get("lastMergeCommit") or {}).get("commitId") or "")
        if not merge_commit:
            return False, (
                f"{name}: completed pull request has no merge commit for branch '{branch}'"
            )

        build = pipelines._pipeline_build_for_commit(org, project, repo_id, merge_commit, auth)
        if not build:
            return False, (
                f"{name}: no master pipeline run found yet for merged commit "
                f"{merge_commit[:8]} from branch '{branch}'"
            )

    except urllib.error.HTTPError as exc:
        return False, f"{name}: pipeline run lookup failed ({exc.code}): {pipelines._http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{name}: pipeline run lookup failed: {exc}"

    web = pipelines._build_web_url(org, project, build)
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
        "commit_id": (build.get("sourceVersion") or merge_commit or ""),
    }


def get_master_pipeline_run_for_merged_branch(name, path, branch):
    """Return (ok, url_or_error) for the master run tied to *branch*'s merged PR.

    Flow:
      1. Find the latest completed PR from refs/heads/<branch> to master.
      2. Read its merge commit id.
      3. Find the repository pipeline build on refs/heads/master for that commit.
    """
    import pipelines
    ok, result = pipelines.get_master_pipeline_run_for_merged_branch_details(name, path, branch)
    if not ok:
        return False, result
    return True, result.get("url") or ""


def _commit_details_from_ado(org, project, repo, commit_id, auth):
    """Return a commit's subject and author timestamp, or {} if unavailable."""
    import pipelines
    if not commit_id:
        return {}
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/git/repositories/"
        f"{urllib.parse.quote(repo)}/commits/{urllib.parse.quote(commit_id)}"
        f"?api-version=7.1"
    )
    try:
        data = pipelines._api_get(url, auth)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return {}
    return {
        "message": (data.get("comment") or "").splitlines()[0] if data.get("comment") else "",
        "commit_created_at": (data.get("author") or {}).get("date") or "",
    }


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
    import pipelines
    if not pipelines.is_git_repo(path):
        return False, f"{name}: not a git repository"

    parsed = pipelines.parse_ado_remote(pipelines.git_remote_url(path))
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, repo, host = parsed

    cache_key = (org.lower(), project.lower(), repo.lower())
    if not force:
        with pipelines._deploy_status_cache_lock:
            entry = pipelines._deploy_status_cache.get(cache_key)
        if entry and (time.monotonic() - entry[0]) < pipelines._DEPLOY_STATUS_CACHE_TTL:
            return True, entry[1]

    auth, err = pipelines._auth_for_host(host, org)
    if err:
        return False, f"{name}: {err}"

    try:
        repo_id = pipelines._cached_repo_id(org, project, repo, auth)
        if not repo_id:
            return True, {key: None for key in pipelines.DEPLOY_STATUS_STAGE_KEYS}
        pipeline_id = pipelines._cached_pipeline_id(org, project, repo, repo_id, auth)
        if not pipeline_id:
            return True, {key: None for key in pipelines.DEPLOY_STATUS_STAGE_KEYS}
    except urllib.error.HTTPError as exc:
        return False, f"{name}: deployment status lookup failed ({exc.code}): {pipelines._http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{name}: deployment status lookup failed: {exc}"

    pipeline_url = pipelines._pipeline_web_url(org, project, pipeline_id)
    ok_scan, result = pipelines._get_latest_deployed_artifacts_scan(
        name, org, project, repo, auth, pipeline_id
    )
    if not ok_scan:
        return False, result

    # Fetch each distinct commit's details once (several stages often share it).
    commits = {}
    for key in pipelines.DEPLOY_STATUS_STAGE_KEYS:
        entry = result.get(key)
        if entry and entry["commit"] and entry["commit"] not in commits:
            commits[entry["commit"]] = pipelines._commit_details_from_ado(
                org, project, repo, entry["commit"], auth
            )
    for key in pipelines.DEPLOY_STATUS_STAGE_KEYS:
        entry = result.get(key)
        if entry:
            details = commits.get(entry["commit"]) or {}
            entry["message"] = details.get("message", "")
            entry["commit_created_at"] = details.get("commit_created_at", "")

    result["_pipeline_url"] = pipeline_url
    with pipelines._deploy_status_cache_lock:
        pipelines._deploy_status_cache[cache_key] = (time.monotonic(), result)
    return True, result


def _get_latest_deployed_artifacts_scan(name, org, project, repo, auth, pipeline_id):
    """Fallback path: scan recent builds (any status) and read each timeline.

    Returns (ok, {stage_key: entry_or_None}) - same per-stage shape as the
    hierarchy-query fast path, without "_source"/"_pipeline_url" (the caller
    adds those uniformly for both paths).

    Deliberately NOT filtered to statusFilter=completed: a build can still be
    "in progress" overall (e.g. waiting on a later Production approval) while
    an EARLIER stage like Development already finished successfully - filtering
    to completed builds skipped that stage's real result entirely and fell
    back to an older, fully-finished build instead (observed: an in-progress
    build's already-succeeded Development stage was ignored in favour of a
    days-older run). Sorted by queueTime (not finishTime, which is null/absent
    for a build still running) so ordering stays well-defined regardless of
    status.
    """
    import pipelines
    try:
        query = urllib.parse.urlencode({
            "definitions": str(pipeline_id),
            "queryOrder": "queueTimeDescending",
            "$top": str(pipelines._DEPLOY_STATUS_SCAN),
            "api-version": "7.1",
        })
        url = (
            f"https://dev.azure.com/{urllib.parse.quote(org)}/"
            f"{urllib.parse.quote(project)}/_apis/build/builds?{query}"
        )
        builds = pipelines._api_get(url, auth).get("value") or []
    except urllib.error.HTTPError as exc:
        return False, f"{name}: deployment status lookup failed ({exc.code}): {pipelines._http_error_detail(exc)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{name}: deployment status lookup failed: {exc}"

    builds = [
        build for build in builds
        if (build.get("reason") or "").lower() not in ("schedule", "pullrequest")
    ]
    result = {key: None for key in pipelines.DEPLOY_STATUS_STAGE_KEYS}
    if not builds:
        return True, result

    timelines = pipelines.run_in_parallel(
        builds,
        lambda b: pipelines._api_build_timeline_safe(org, project, b.get("id"), auth),
    )

    pending = set(pipelines.DEPLOY_STATUS_STAGE_KEYS)
    for build, timeline in zip(builds, timelines):
        if not pending:
            break
        stages = pipelines._stage_timings_from_timeline(timeline)
        for key in list(pending):
            stage = stages.get(key) or {}
            if stage.get("state") == "done":
                result[key] = {
                    "commit": build.get("sourceVersion") or "",
                    "message": "",
                    "build_id": build.get("id"),
                    "url": pipelines._build_web_url(org, project, build),
                    "triggered_by": pipelines._build_triggered_by(build),
                    "deployed_at": stage.get("finish") or "",
                }
                pending.discard(key)
    return True, result


def _pipeline_web_url(org, project, pipeline_id):
    """Return the web URL for a pipeline definition's run history overview."""
    if not pipeline_id:
        return ""
    return (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_build?definitionId={pipeline_id}"
    )


def _build_triggered_by(build):
    """Return a short "triggered by" display string for a build, or ''."""
    person = build.get("requestedFor") or build.get("requestedBy") or {}
    return person.get("displayName") or person.get("uniqueName") or person.get("id") or ""


class AdoPipelines(PipelineSource):
    @staticmethod
    def build_template_parameters(repo_path, environment):
        import pipelines
        role_to_name = pipelines._discover_role_params(repo_path) or pipelines.DEFAULT_ROLE_PARAMS
        params = {}
        for role, value in pipelines._env_roles(environment).items():
            name = role_to_name.get(role)
            if name:
                params[name] = value
        return params

    @staticmethod
    def build_template_parameters_for_roles(repo_path, deploy_dev, deploy_acc):
        import pipelines
        role_to_name = pipelines._discover_role_params(repo_path) or pipelines.DEFAULT_ROLE_PARAMS
        roles = {
            "infra": True, "dev": bool(deploy_dev),
            "acc": bool(deploy_acc), "prod": False,
        }
        params = {}
        for role, value in roles.items():
            name = role_to_name.get(role)
            if name:
                params[name] = value
        return params

    @staticmethod
    def get_pipeline_parameters(repo_path):
        import pipelines
        text = pipelines._read_pipeline_yaml(repo_path)
        if not text:
            return []
        params = []
        for raw in pipelines._parse_yaml_parameters_full(text):
            role = pipelines._param_role(raw)
            params.append({
                "name": raw["name"],
                "display": raw.get("display") or raw["name"],
                "type": (raw.get("type") or "string").lower(),
                "default": pipelines._coerce_param_default(
                    raw.get("type"), raw.get("default")),
                "role": role,
                "is_env": role == "env",
                "is_custom": role is None,
            })
        return params

    get_master_pipeline_profile = staticmethod(get_master_pipeline_profile)
    find_env_deployment_for_branch = staticmethod(find_env_deployment_for_branch)
    get_build_errors = staticmethod(get_build_errors)
    approve_pipeline_stage = staticmethod(approve_pipeline_stage)
    get_pipeline_stage_statuses = staticmethod(get_pipeline_stage_statuses)
    rerun_failed_stage = staticmethod(rerun_failed_stage)
    cancel_pipeline_run = staticmethod(cancel_pipeline_run)
    run_pipeline_for_repo_details = staticmethod(run_pipeline_for_repo_details)
    rerun_pipeline_from_latest_commit = staticmethod(rerun_pipeline_from_latest_commit)
    redeploy_master_for_repo_details = staticmethod(redeploy_master_for_repo_details)
    get_latest_master_pipeline_run_details = staticmethod(get_latest_master_pipeline_run_details)
    run_pipeline_for_repo = staticmethod(run_pipeline_for_repo)
    get_master_pipeline_run_for_merged_branch_details = staticmethod(get_master_pipeline_run_for_merged_branch_details)
    get_master_pipeline_run_for_merged_branch = staticmethod(get_master_pipeline_run_for_merged_branch)
    get_latest_deployed_artifacts = staticmethod(get_latest_deployed_artifacts)
