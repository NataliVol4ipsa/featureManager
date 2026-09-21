"""Historical pipeline stage-duration estimates and their local cache.

When the "Estimate pipeline time left" setting is on, the app collects the
average per-stage execution time of each repository's most recent successful
master runs and caches them for a week. The pipeline monitor then uses those
averages, together with each running stage's start time, to estimate the time
left for a stage and the total time left for the whole run (Production
excluded).

This module owns the cache file and the estimation maths; the Azure DevOps
fetch itself lives in :mod:`pipelines`.
"""

import os
import json
import datetime
import threading

import pipelines


_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "pipeline_estimates.json"
)

# How long a cached average stays valid before it is refreshed from ADO.
CACHE_TTL_DAYS = 7

# Cache schema version. Bump when the estimate computation changes so existing
# entries are treated as stale and refetched with the new logic. Schema 3
# replaced the flat per-stage averages with a granular stage->job->task tree,
# so every schema-2 entry is automatically invalidated and refetched.
_SCHEMA = 3

_STAGE_KEYS = ("build", "development", "acceptance", "production")

# States that leave no time on the clock for a node.
_DONE_STATES = ("done", "skipped", "canceled", "failed")

_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Cache persistence
# --------------------------------------------------------------------------- #

def _cache_key(name):
    return (name or "").strip().lower()


def _load_cache():
    """Return the cached estimates dict, or {} when unavailable."""
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(data):
    """Persist the cache; write failures are ignored."""
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
    except OSError:
        pass


def _is_fresh(entry):
    """Return True when *entry* matches the current schema and is within the TTL."""
    entry = entry or {}
    if entry.get("schema") != _SCHEMA:
        return False
    updated = entry.get("updated_at")
    dt = pipelines._parse_iso_utc(updated) if updated else None
    if dt is None:
        return False
    age = datetime.datetime.now(datetime.timezone.utc) - dt
    return age.total_seconds() < CACHE_TTL_DAYS * 86400


def get_estimate(name):
    """Return the granular timing profile for a repo, or None if missing/stale.

    The profile is ``{"stages", "nodes", "acc_parallel"}`` where ``stages`` maps
    each stage to its average wall-clock seconds and ``nodes`` is the granular
    stage->job->task tree used to reflect skipped stages/jobs in the live
    estimate. *name* is the local repository folder name (the monitor's row key).
    """
    if not name:
        return None
    with _lock:
        entry = _load_cache().get(_cache_key(name))
    if not entry or not _is_fresh(entry):
        return None
    stages = entry.get("stages")
    if not isinstance(stages, dict) or not stages:
        return None
    return {
        "stages": stages,
        "nodes": entry.get("nodes") or {},
        "acc_parallel": bool(entry.get("acc_parallel")),
    }


def _store(name, payload):
    with _lock:
        data = _load_cache()
        data[_cache_key(name)] = {
            "updated_at": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(timespec="seconds"),
            "schema": _SCHEMA,
            "stages": payload.get("stages") or {},
            "nodes": payload.get("nodes") or {},
            "acc_parallel": bool(payload.get("acc_parallel")),
            "samples": payload.get("samples"),
        }
        _save_cache(data)


def _next_refresh_local():
    """Return the soonest cache-entry expiry as a local-time string, or None."""
    stamps = []
    with _lock:
        for entry in _load_cache().values():
            dt = pipelines._parse_iso_utc(entry.get("updated_at"))
            if dt is not None:
                stamps.append(dt)
    if not stamps:
        return None
    soonest = min(stamps) + datetime.timedelta(days=CACHE_TTL_DAYS)
    return soonest.astimezone().strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# Refresh (hits Azure DevOps for every repo) - run on a background thread
# --------------------------------------------------------------------------- #

def _discover_entries():
    """Return [(folder_name, path)] for every service and nuget repository."""
    from config import REPOS_ROOT, NUGETS_ROOT
    from gitutils import get_service_folders, get_nuget_folders

    entries = [(name, os.path.join(REPOS_ROOT, name))
               for name in get_service_folders()]
    entries += [(name, os.path.join(NUGETS_ROOT, name))
                for name in get_nuget_folders()]
    return entries


def _stale_targets(cache, entries):
    """Return the entries whose cached estimate is missing or expired."""
    return [
        (name, path) for name, path in entries
        if not _is_fresh(cache.get(_cache_key(name)))
    ]


def needs_refresh():
    """Return True when any repo's cached estimate is missing or expired.

    Cheap - reads the local cache and folder list only (no git/ADO calls).
    """
    with _lock:
        cache = _load_cache()
    return bool(_stale_targets(cache, _discover_entries()))


def status_message():
    """Return the 'cache up to date; next refresh: ...' info line."""
    when = _next_refresh_local()
    suffix = f" next refresh: {when}." if when else "."
    return "Pipeline time-left estimates: cache up to date;" + suffix


def refresh_all(force=False, log=None):
    """Refresh cached estimates for every service and nuget repository.

    Entries are keyed by folder name, so the freshness check needs no git/ADO
    call - only repos that actually need fetching resolve their remote (inside
    ``pipelines.get_master_pipeline_profile``). Callers must run this on a
    background (daemon) thread. *log*, when given, receives info messages.
    """
    from parallel import run_in_parallel

    entries = _discover_entries()
    if force:
        targets = entries
    else:
        with _lock:
            cache = _load_cache()
        targets = _stale_targets(cache, entries)

    # Nothing to do: report when the cache next goes stale instead of pretending
    # to fetch.
    if not targets:
        if log:
            log(status_message())
        return

    if log:
        log("Pipeline time-left estimates: fetching run history\u2026")

    def _fetch(target):
        name, path = target
        ok, result = pipelines.get_master_pipeline_profile(name, path)
        # A definitive result (including "no estimate here") is cached so the
        # repo is not rescanned until the entry expires; only transient failures
        # (ok is False) are left uncached to retry next time.
        if ok:
            _store(name, result)
        return bool(ok)

    cached = sum(1 for ok in run_in_parallel(targets, _fetch) if ok)

    if log:
        log(
            f"Pipeline time-left estimates: fetch complete "
            f"({cached} repositor{'y' if cached == 1 else 'ies'} cached)."
        )


# --------------------------------------------------------------------------- #
# Estimation maths
# --------------------------------------------------------------------------- #

def fmt_mmss(seconds):
    """Return *seconds* as ``mm:ss`` (clamped to zero), or '' for None."""
    if seconds is None:
        return ""
    total = int(round(max(0.0, seconds)))
    return f"{total // 60:02d}:{total % 60:02d}"


def stage_time_left(avg_seconds, state, start_iso, now=None):
    """Return estimated seconds left for one stage, or None when unknown.

    A running stage subtracts its elapsed time (remote start vs. the PC clock,
    both in UTC) from the historical average; a not-yet-started stage counts its
    full average; a finished stage counts zero.
    """
    if avg_seconds is None:
        return None
    if state in ("done", "skipped", "canceled"):
        return 0.0
    if state == "running":
        start = pipelines._parse_iso_utc(start_iso)
        if start is not None:
            now = now or datetime.datetime.now(datetime.timezone.utc)
            elapsed = (now - start).total_seconds()
            return max(0.0, avg_seconds - elapsed)
    return avg_seconds


# --------------------------------------------------------------------------- #
# Granular estimation (matches a live run's actual stage/job/task structure)
# --------------------------------------------------------------------------- #

def _elapsed_seconds(start_iso, now):
    """Return seconds elapsed since *start_iso* (UTC), or None when unknown."""
    start = pipelines._parse_iso_utc(start_iso)
    if start is None:
        return None
    return (now - start).total_seconds()


def _stage_jobs(nodes, stage_key):
    """Return {key: node} for every job the *stage* historically ran."""
    return {
        key: node for key, node in nodes.items()
        if node.get("stage") == stage_key and node.get("type") == "job"
    }


def _job_tasks(nodes, job_key):
    """Return {key: node} for every task the *job* historically ran, in order."""
    tasks = {
        key: node for key, node in nodes.items()
        if node.get("parent") == job_key and node.get("type") == "task"
    }
    return dict(sorted(tasks.items(), key=lambda kv: kv[1].get("order") or 0))


def _running_job_remaining(job_key, job, nodes, live_nodes, job_live, now):
    """Estimate seconds left in a running job from its (sequential) tasks.

    Tasks inside a job run one after another, so the remaining time is the sum
    of each unfinished task's remaining time. A task that has not appeared in
    the live timeline yet is still going to run, so it counts its full average;
    a task the run skipped (present but skipped) counts zero. Falls back to the
    job's own wall-clock average minus elapsed when no task profile exists.
    """
    tasks = _job_tasks(nodes, job_key)
    if not tasks:
        elapsed = _elapsed_seconds((job_live or {}).get("start"), now)
        avg = job.get("avg") or 0.0
        return max(0.0, avg - elapsed) if elapsed is not None else avg

    total = 0.0
    for task_key, task in tasks.items():
        live = (live_nodes or {}).get(task_key)
        state = (live or {}).get("state")
        avg = task.get("avg") or 0.0
        if state in _DONE_STATES:
            continue
        if state == "running":
            elapsed = _elapsed_seconds((live or {}).get("start"), now)
            total += max(0.0, avg - elapsed) if elapsed is not None else avg
        else:
            # Waiting, or not yet materialised in the timeline: it will still run.
            total += avg
    return total


def _running_stage_remaining(stage_key, profile, live_nodes, stage_times, now):
    """Estimate seconds left in a running stage from its jobs, or None.

    Jobs inside a stage can run in parallel (each on its own agent), so the
    stage finishes when its last job finishes - the maximum projected finish
    over the jobs, not their sum. Each job is placed at its historical start
    offset (relative to the stage start), which lets sequential jobs stack while
    concurrent jobs overlap. A job the run dropped (absent from the live
    timeline) contributes nothing, so skipping jobs via pipeline parameters is
    reflected automatically. Returns None (fall back to the stage average) when
    the stage has no job profile or has not actually started.
    """
    nodes = profile.get("nodes") or {}
    jobs = _stage_jobs(nodes, stage_key)
    stage_start = pipelines._parse_iso_utc((stage_times.get(stage_key) or {}).get("start"))
    if not jobs or stage_start is None:
        return None
    stage_elapsed = (now - stage_start).total_seconds()
    stage_offset = (nodes.get(stage_key) or {}).get("avg_offset", 0.0)

    present = 0
    max_finish = 0.0  # projected finish, relative to the stage start
    for job_key, job in jobs.items():
        live = (live_nodes or {}).get(job_key)
        if live is None:
            # The job is not part of this run (skipped via parameters): ignore.
            continue
        present += 1
        state = live.get("state")
        if state in _DONE_STATES:
            continue
        # Historical start offset of the job relative to the stage start.
        job_offset = max(0.0, (job.get("avg_offset", stage_offset)) - stage_offset)
        if state == "running":
            finish = stage_elapsed + _running_job_remaining(
                job_key, job, nodes, live_nodes, live, now
            )
        else:
            # Waiting job: it starts at its historical offset, or now if we are
            # already past that offset, then runs for its average.
            finish = max(job_offset, stage_elapsed) + (job.get("avg") or 0.0)
        if finish > max_finish:
            max_finish = finish

    if present == 0:
        # No live job data yet: let the caller use the stage-level average.
        return None
    return max(0.0, max_finish - stage_elapsed)


def _stage_avg_remaining(stage_key, stage_avgs, stage_times, now):
    """Return a stage's remaining time from its wall-clock average alone."""
    avg = stage_avgs.get(stage_key)
    if avg is None:
        return 0.0
    elapsed = _elapsed_seconds((stage_times.get(stage_key) or {}).get("start"), now)
    return max(0.0, avg - elapsed) if elapsed is not None else avg


def total_time_left(profile, stages_state, stage_times, live_nodes=None,
                    environment=None, visible_stages=None, now=None):
    """Return estimated total seconds left for a run, excluding Production.

    The estimate is built from the run's *actual* structure: each stage's
    remaining time is refined by its live jobs and tasks (granular), and only
    the stages the run actually includes are combined. A Development run is
    Build+Development, an Acceptance run Build+Acceptance, and a master run is
    Build+max(dev, acc) when Acceptance overlaps Development, else
    Build+Development+Acceptance. Stages or jobs skipped via pipeline parameters
    simply do not appear in the live timeline, so they add nothing.
    """
    if not profile:
        return None
    stage_avgs = profile.get("stages") or {}
    stages_state = stages_state or {}
    stage_times = stage_times or {}
    now = now or datetime.datetime.now(datetime.timezone.utc)

    def stage_remaining(key):
        state = stages_state.get(key, "waiting")
        if state in _DONE_STATES:
            return 0.0
        if state == "running":
            refined = _running_stage_remaining(
                key, profile, live_nodes, stage_times, now
            )
            if refined is not None:
                return refined
            return _stage_avg_remaining(key, stage_avgs, stage_times, now)
        # Waiting / approval / ready / not-yet-started: full historical average.
        return stage_avgs.get(key) or 0.0

    build = stage_remaining("build")

    if environment == "dev":
        return build + stage_remaining("development")
    if environment == "acc":
        return build + stage_remaining("acceptance")

    # Master (or unspecified) run: use the stages the run actually includes.
    visible = set(visible_stages or [])
    dev_in = ("development" in visible) if visible else True
    acc_in = ("acceptance" in visible) if visible else True
    dev = stage_remaining("development") if dev_in else 0.0
    acc = stage_remaining("acceptance") if acc_in else 0.0
    if dev_in and acc_in:
        if profile.get("acc_parallel"):
            return build + max(dev, acc)
        return build + dev + acc
    if dev_in:
        return build + dev
    if acc_in:
        return build + acc
    return build
