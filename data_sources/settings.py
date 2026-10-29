"""Persistent provider selection, held fixed for the lifetime of the process."""

import json
import os
import tempfile
import threading


SOURCES = {
    "board": "Board",
    "repositories": "Repositories",
    "pipelines": "Pipelines",
    "nuget": "NuGet feed",
}
PROVIDERS = ("ADO", "GitHub")
_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "data_sources.json")
_selection = None
_lock = threading.Lock()


def _validate(selection):
    if not isinstance(selection, dict):
        raise ValueError("Data sources must be a JSON object.")
    unknown = selection.keys() - SOURCES.keys()
    if unknown:
        raise ValueError(f"Unknown data sources: {', '.join(sorted(unknown))}")
    result = dict.fromkeys(SOURCES, "ADO")
    for source, provider in selection.items():
        if provider not in PROVIDERS:
            raise ValueError(f"Invalid provider for {SOURCES[source]}: {provider!r}")
        result[source] = provider
    return result


def current():
    """Return a copy of the active selections; absent settings default to ADO."""
    global _selection
    with _lock:
        if _selection is None:
            try:
                with open(_PATH, encoding="utf-8") as handle:
                    saved = json.load(handle)
            except FileNotFoundError:
                saved = {}
            _selection = _validate(saved)
        return dict(_selection)


def save(selection):
    """Atomically persist selections for the next process, without live changes."""
    selection = _validate(selection)
    directory = os.path.dirname(_PATH)
    os.makedirs(directory, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory,
                prefix="data_sources.", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            json.dump(selection, handle, indent=4)
            handle.write("\n")
        os.replace(temporary, _PATH)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.remove(temporary)
