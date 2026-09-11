"""Tiny thread-safe, atomic, cross-session JSON dict cache.

Backs the app's persistent "resolve once, reuse across restarts" caches (ADO
identity id, NuGet feed roots, ...). Values must be JSON-serialisable.

NEVER store secrets here - the file is plain JSON on disk. Writes are atomic
(temp file + os.replace) so a crash or a second app instance never leaves a
torn file; reads tolerate a corrupt/missing file by returning {}.
"""

import os
import json
import threading


class JsonDiskCache:
    """A dict-like JSON file cache with a lock and atomic writes."""

    def __init__(self, filename):
        self._path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), filename
        )
        self._lock = threading.Lock()

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data):
        tmp = f"{self._path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        return True

    def get(self, key, default=None):
        """Return the cached value for *key*, or *default*."""
        with self._lock:
            return self._load().get(key, default)

    def set(self, key, value):
        """Store *value* under *key* (merged into the existing file)."""
        with self._lock:
            data = self._load()
            data[key] = value
            self._save(data)

    def all(self):
        """Return the whole cache dict."""
        with self._lock:
            return self._load()

    def replace(self, data):
        """Replace the whole cache with *data*. Returns True on a successful write."""
        if not isinstance(data, dict):
            return False
        with self._lock:
            return self._save(data)

    def clear(self):
        """Empty the cache. Returns True on a successful write."""
        with self._lock:
            return self._save({})
