"""In-memory, thread-safe cache for the Azure DevOps REST auth header.

Single source of truth for the Basic authorization header used by the app's
Azure DevOps REST calls that need read/execute access - pipeline runs and
lookups (:mod:`pipelines`) and work-item reads (:mod:`pbi`). The header is
derived once, preferring the ``ADO_PAT`` environment variable and then the
Git-stored credential, and kept in memory for the process so the (sometimes
slow) ``git credential fill`` runs at most once per host until the cached value
expires.

The header is NEVER written to disk - it lives in RAM for the session only.

Thread safety
-------------
A per-host lock serialises refreshes: while one thread re-derives the header,
other callers for the same host block on that lock and then receive the freshly
derived value - they are never handed an expired header. Different hosts use
different locks and refresh independently, so parallel calls across repositories
are not serialised behind one another.

Deliberately NOT handled here (different credentials by design):
  * Pull-request create/lookup/complete and work-item linking in
    :mod:`gitutils` use the per-repository Git credential (Code scope, with a
    Basic-then-Bearer fallback) via ``get_git_credential``.
  * NuGet restore uses an AAD bearer token, cached separately in
    :mod:`packages` (``get_azure_devops_token``).
"""

import os
import time
import base64
import threading
import urllib.parse

from gitutils import get_git_credential


# A PAT / Git credential is long-lived, but a bounded TTL means a rotated
# credential is picked up without restarting the app: the header is re-derived
# on the first call made after this many seconds have elapsed.
AUTH_TTL_SECONDS = 30 * 60

_cache = {}                 # key -> (header, expires_at_monotonic)
_cache_lock = threading.Lock()
_key_locks = {}             # key -> threading.Lock (one refresh at a time)
_key_guard = threading.Lock()


def _key(host, org):
    return ((host or "").lower(), (org or "").lower())


def _key_lock(key):
    """Return the shared refresh lock for *key*, creating it on first use."""
    with _key_guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _key_locks[key] = lock
        return lock


def _fresh_cached(key):
    """Return the cached header for *key* when still within its TTL, else None."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry[1] > time.monotonic():
            return entry[0]
    return None


def _derive(host, org, cred_url):
    """Derive the Basic auth header from ADO_PAT or the Git credential.

    Returns (header, error); *header* is None when no credential is available.
    """
    pat = os.environ.get("ADO_PAT", "").strip()
    if pat:
        token = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
        return f"Basic {token}", ""

    # Azure DevOps stores dev.azure.com credentials per-organization
    # (useHttpPath), so an org-scoped URL is needed or the lookup finds nothing.
    url = cred_url or (
        f"https://{host}/{urllib.parse.quote(org)}" if (host and org) else None
    )
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


def auth_header(host, org=None, cred_url=None, force=False):
    """Return (header, error) for Azure DevOps REST calls to *host*.

    Uses the in-memory cache when fresh. On a miss or after expiry the header is
    re-derived under a per-host lock, so concurrent callers for the same host
    wait for the single refresh and then receive the fresh header - never an
    expired one. *cred_url* overrides the URL used for the Git-credential lookup
    (e.g. a configured organization URL); *force* bypasses the cache and
    re-derives unconditionally.
    """
    key = _key(host, org)
    if not force:
        cached = _fresh_cached(key)
        if cached:
            return cached, ""
    with _key_lock(key):
        # Another thread may have refreshed the header while we waited.
        if not force:
            cached = _fresh_cached(key)
            if cached:
                return cached, ""
        header, err = _derive(host, org, cred_url)
        if err:
            return None, err
        with _cache_lock:
            _cache[key] = (header, time.monotonic() + AUTH_TTL_SECONDS)
        return header, ""


def invalidate(host=None, org=None):
    """Drop cached header(s) so the next call re-derives. No args clears all."""
    with _cache_lock:
        if host is None:
            _cache.clear()
        else:
            _cache.pop(_key(host, org), None)
