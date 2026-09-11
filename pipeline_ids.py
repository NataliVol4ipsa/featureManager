"""Persistent cache of resolved Azure DevOps repository and pipeline ids.

Before the deploy-to-dev/acc modal (and every pipeline run/lookup) the app must
translate a repository's Git remote into two Azure DevOps ids:

  * the repository id (name -> GUID), and
  * the deployment pipeline/definition id (which, when a repo has several
    definitions, costs one extra REST call per definition to disambiguate).

Neither changes from commit to commit - a repository keeps its id forever and a
pipeline is only re-created rarely - so they are cached here on disk (keyed by
``org/project/repo``) and reused across sessions. Only the volatile per-commit
build/timeline lookups still hit Azure DevOps every time.

The cache has no automatic expiry: it is refreshed manually from the Settings
menu ("Pipeline id cache"), where entries can also be viewed and edited.
"""

import os
import json
import datetime
import threading


_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "pipeline_ids.json"
)

_lock = threading.Lock()


def _cache_key(org, project, repo):
    return f"{org}/{project}/{repo}".strip().lower()


def _load_cache():
    """Return the cached ids dict, or {} when unavailable."""
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(data):
    """Persist the cache atomically; returns (ok, error_message).

    Writes to a per-process temp file (fsync'd) then os.replace()s it over the
    real file, which is atomic on Windows and POSIX. A reader therefore always
    sees a complete old or new file - never a torn write - even across a crash
    or a second app instance writing at the same time.
    """
    tmp = f"{_CACHE_PATH}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, _CACHE_PATH)
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, f"could not save the pipeline id cache: {exc}"
    return True, ""


def get_repo_id(org, project, repo):
    """Return the cached repository id for *repo*, or None when not stored."""
    with _lock:
        entry = _load_cache().get(_cache_key(org, project, repo)) or {}
    value = entry.get("repo_id")
    return value or None


def get_pipeline_id(org, project, repo):
    """Return the cached deploy pipeline id for *repo*, or None when not stored."""
    with _lock:
        entry = _load_cache().get(_cache_key(org, project, repo)) or {}
    value = entry.get("pipeline_id")
    return value or None


def get_project_id(org, project, repo):
    """Return the cached Azure DevOps project id for *repo*, or None."""
    with _lock:
        entry = _load_cache().get(_cache_key(org, project, repo)) or {}
    value = entry.get("project_id")
    return value or None


def store(org, project, repo, repo_id=None, pipeline_id=None, project_id=None):
    """Merge a resolved *repo_id*, *pipeline_id* and/or *project_id* into the cache."""
    if repo_id is None and pipeline_id is None and project_id is None:
        return
    with _lock:
        data = _load_cache()
        entry = data.get(_cache_key(org, project, repo)) or {}
        entry.update({"org": org, "project": project, "repo": repo})
        if repo_id is not None:
            entry["repo_id"] = repo_id
        if pipeline_id is not None:
            entry["pipeline_id"] = pipeline_id
        if project_id is not None:
            entry["project_id"] = project_id
        entry["updated_at"] = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(timespec="seconds")
        data[_cache_key(org, project, repo)] = entry
        _save_cache(data)


def all_entries():
    """Return the whole cache dict (for the Settings viewer)."""
    with _lock:
        return _load_cache()


def save_all(data):
    """Replace the whole cache with *data*. Returns (ok, error_message)."""
    if not isinstance(data, dict):
        return False, "Expected an object of \"org/project/repo\": { ... }."
    with _lock:
        return _save_cache(data)


def clear():
    """Empty the cache so every id is re-resolved on next use. Returns (ok, err)."""
    with _lock:
        return _save_cache({})
