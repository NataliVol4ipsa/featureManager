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

_STAGE_KEYS = ("build", "development", "acceptance", "production")

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
    """Return True when *entry* was written within the cache TTL."""
    updated = (entry or {}).get("updated_at")
    dt = pipelines._parse_iso_utc(updated) if updated else None
    if dt is None:
        return False
    age = datetime.datetime.now(datetime.timezone.utc) - dt
    return age.total_seconds() < CACHE_TTL_DAYS * 86400


def get_estimate(name):
    """Return {"stages", "acc_parallel"} for a repo, or None if missing/stale.

    *name* is the local repository folder name (the monitor's row key).
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
    return {"stages": stages, "acc_parallel": bool(entry.get("acc_parallel"))}


def _store(name, payload):
    with _lock:
        data = _load_cache()
        data[_cache_key(name)] = {
            "updated_at": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(timespec="seconds"),
            "stages": payload.get("stages") or {},
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
    ``pipelines.get_master_stage_durations``). Callers must run this on a
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
        ok, result = pipelines.get_master_stage_durations(name, path)
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


def total_time_left(estimate, stages_state, stage_times, environment=None,
                    visible_stages=None, now=None):
    """Return estimated total seconds left for a run, excluding Production.

    Only stages that have not completed are summed. For a Development run this is
    Build+Development; for an Acceptance run Build+Acceptance. For a master run
    with both environments it is Build+max(dev, acc) when Acceptance runs in
    parallel with Development, otherwise Build+Development+Acceptance.
    """
    if not estimate:
        return None
    avgs = estimate.get("stages") or {}
    stages_state = stages_state or {}
    stage_times = stage_times or {}
    now = now or datetime.datetime.now(datetime.timezone.utc)

    def remaining(key):
        if key not in avgs:
            return 0.0
        value = stage_time_left(
            avgs.get(key),
            stages_state.get(key, "waiting"),
            (stage_times.get(key) or {}).get("start"),
            now,
        )
        return value or 0.0

    build = remaining("build")
    dev = remaining("development")
    acc = remaining("acceptance")

    if environment == "dev":
        return build + dev
    if environment == "acc":
        return build + acc

    # Master (or unspecified) run: use the stages the run actually includes.
    visible = set(visible_stages or [])
    dev_in = ("development" in visible) if visible else True
    acc_in = ("acceptance" in visible) if visible else True
    if dev_in and acc_in:
        if estimate.get("acc_parallel"):
            return build + max(dev, acc)
        return build + dev + acc
    if dev_in:
        return build + dev
    if acc_in:
        return build + acc
    return build
