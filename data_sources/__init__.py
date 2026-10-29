"""Resolve data strategies and queue unsupported-source notices without Tk calls."""

import importlib
import queue
import threading

from . import settings as source_settings


_IMPLEMENTATIONS = {
    "ADO": {
        "board": ("data_sources.ado.board", "AdoBoard"),
        "repositories": ("data_sources.ado.repositories", "AdoRepositories"),
        "pipelines": ("data_sources.ado.pipelines", "AdoPipelines"),
        "nuget": ("data_sources.ado.nuget", "AdoNuget"),
    },
    "GitHub": {
        "board": ("data_sources.github", "GitHubBoard"),
        "repositories": ("data_sources.github", "GitHubRepositories"),
        "pipelines": ("data_sources.github", "GitHubPipelines"),
        "nuget": ("data_sources.github", "GitHubNuget"),
    },
}
_instances = {}
_lock = threading.RLock()
_notices = queue.Queue()
_reported = set()


class SourceUnavailableError(RuntimeError):
    """A provider cannot supply a value for a non-result-style query."""


def strategy(source):
    """Return the selected strategy, never inferring a provider from a remote."""
    provider = source_settings.current()[source]
    key = (source, provider)
    with _lock:
        if key not in _instances:
            module, name = _IMPLEMENTATIONS[provider][source]
            _instances[key] = getattr(importlib.import_module(module), name)()
        return _instances[key]


def unsupported(source):
    """Report one notice per source per process; safe on any worker thread."""
    with _lock:
        if source not in _reported:
            _reported.add(source)
            _notices.put(source)
    return f"{source_settings.SOURCES[source]}: GitHub not implemented yet"


def ensure_available(source):
    """Preflight operations that otherwise do network work before data access."""
    if strategy(source).available:
        return True
    unsupported(source)
    return False


def pending_notices():
    """Drain notifications on the UI thread, not from background strategies."""
    notices = []
    while True:
        try:
            notices.append(_notices.get_nowait())
        except queue.Empty:
            return notices
