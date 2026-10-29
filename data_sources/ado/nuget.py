"""ADO nuget strategy; existing provider behavior."""

from ..contracts import NugetSource
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request



def find_private_feeds(repo_path):
    """Return the Azure DevOps Artifacts feed URLs configured for *repo_path*.

    Reads the repo's nearest ``nuget.config`` and keeps only sources whose URL
    starts with ``https://pkgs.dev.azure.com``. Returns an empty list when there
    is no config or no such source.
    """
    import packages
    config = packages.find_nuget_config(repo_path)
    if not config:
        return []
    try:
        with open(config, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return []
    return [
        source
        for source in packages.parse_nuget_sources(text)
        if source.lower().startswith(packages._AZURE_FEED_PREFIX)
    ]


def _private_feed_prefixes(repo_path):
    """Return a ``;``-joined list of Azure DevOps org URI prefixes for the repo.

    The Azure Artifacts credential provider only uses ``VSS_NUGET_ACCESSTOKEN``
    for feeds whose URI starts with one of these prefixes. Each private feed is
    reduced to its ``https://pkgs.dev.azure.com/<org>/`` root. Falls back to the
    bare host prefix when a feed has no org segment.
    """
    import packages
    prefixes = []
    for feed in packages.find_private_feeds(repo_path):
        parts = urllib.parse.urlsplit(feed)
        org = parts.path.strip("/").split("/", 1)[0]
        prefix = f"{parts.scheme}://{parts.netloc}/{org}/" if org else \
            f"{parts.scheme}://{parts.netloc}/"
        if prefix not in prefixes:
            prefixes.append(prefix)
    return ";".join(prefixes)


def get_azure_devops_token():
    """Return an AAD bearer token for Azure DevOps via the Azure CLI, or None.

    Runs ``az account get-access-token`` scoped to the Azure DevOps resource id
    and caches the result for a while. Returns None when the Azure CLI is not
    installed or the user is not logged in (``az login`` needed). Concurrent
    callers coalesce behind a lock so only one ``az`` process is ever spawned
    per cache miss (the slow part), rather than one per caller.
    """
    import packages
    now = time.time()
    if packages._token_cache["token"] and packages._token_cache["expires"] > now:
        return packages._token_cache["token"]
    with packages._token_lock:
        # Re-check inside the lock: another caller may have just fetched it.
        now = time.time()
        if packages._token_cache["token"] and packages._token_cache["expires"] > now:
            return packages._token_cache["token"]
        az = shutil.which("az")
        if not az:
            return None
        try:
            result = subprocess.run(
                [az, "account", "get-access-token", "--resource",
                 packages._AZURE_DEVOPS_RESOURCE, "--query", "accessToken", "-o", "tsv"],
                capture_output=True, text=True, timeout=60,
                creationflags=packages._NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        token = result.stdout.strip()
        if not token:
            return None
        # Tokens last ~1h; cache for 50 minutes to stay comfortably valid.
        packages._token_cache["token"] = token
        packages._token_cache["expires"] = now + 50 * 60
        return token


def prewarm_azure_devops_token():
    """Fetch the Azure DevOps token in the background to warm the cache.

    Called once at startup so the slow ``az`` cold start happens before the
    user triggers a bump/restore, making those actions feel instant. Silent:
    a missing/not-logged-in Azure CLI just leaves the cache empty.
    """
    import packages
    threading.Thread(target=packages.get_azure_devops_token, daemon=True).start()


def _flat_container_base(feed_index_url, token):
    """Return the PackageBaseAddress (flat-container) base URL of a feed, or None.

    Reads the feed's v3 service index with the bearer token and returns the
    ``PackageBaseAddress`` resource's ``@id`` (with a trailing slash). Cached
    per feed URL.
    """
    import packages
    if feed_index_url in packages._feed_base_cache:
        return packages._feed_base_cache[feed_index_url]
    # Reuse a value resolved in a previous session before hitting the network.
    disk_hit = packages._feed_base_disk.get(feed_index_url)
    if disk_hit:
        packages._feed_base_cache[feed_index_url] = disk_hit
        return disk_hit
    base = None
    try:
        request = urllib.request.Request(
            feed_index_url,
            headers={"Accept": "application/json",
                     "Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
        for resource in data.get("resources", []):
            if str(resource.get("@type", "")).startswith("PackageBaseAddress"):
                base = resource.get("@id")
                break
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        base = None
    if base and not base.endswith("/"):
        base += "/"
    packages._feed_base_cache[feed_index_url] = base
    # Only persist successful resolutions; transient failures stay uncached so
    # they are retried (the in-memory None still throttles retries this session).
    if base:
        packages._feed_base_disk.set(feed_index_url, base)
    return base


def _fetch_latest_stable_private(package_id, base_url, token):
    """Return the highest stable version of *package_id* on a private feed, or None.

    *base_url* is the feed's flat-container base (see ``_flat_container_base``).
    Prerelease versions are ignored; a 404 (package not on this feed) or any
    request error yields None so the package is left untouched. Results are
    memoized per batch (see ``reset_version_cache``), keyed by feed base URL.
    """
    import packages
    return packages._cached_version(
        ("private", base_url, package_id.lower()),
        lambda: packages._fetch_latest_stable_private_uncached(package_id, base_url, token),
    )


def _fetch_latest_stable_private_uncached(package_id, base_url, token):
    """Query a private feed for the highest stable version of *package_id*."""
    import packages
    url = base_url + urllib.parse.quote(package_id.lower()) + "/index.json"
    try:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json",
                     "Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return None
    versions = data.get("versions") or []
    stable = [v for v in versions if "-" not in v]
    if not stable:
        return None
    stable.sort(key=packages._version_key)
    return stable[-1]


def check_private_feed(feed_url, token):
    """Return ``(ok, error)`` after trying to reach *feed_url*'s service index.

    Used as a pre-flight healthcheck: an unreachable host usually means the VPN
    is not connected, while a 401/403 means the token lacks access. On success
    *error* is an empty string.
    """
    try:
        request = urllib.request.Request(
            feed_url,
            headers={"Accept": "application/json",
                     "Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            json.load(response)
        return True, ""
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return False, (
                f"access denied by {feed_url} (HTTP {error.code}) - check your "
                "Azure DevOps feed permissions"
            )
        return False, f"feed returned HTTP {error.code}: {feed_url}"
    except (urllib.error.URLError, OSError) as error:
        return False, (
            f"could not reach {feed_url} ({error}) - are you connected to the "
            "VPN?"
        )
    except ValueError as error:
        return False, f"invalid response from {feed_url}: {error}"


def repo_private_feeds(repo_path):
    """Return the distinct Azure DevOps feeds across the repo's props file(s).

    Each ``Directory.Packages.props`` is resolved against the ``nuget.config``
    nearest to it, so a multi-repository repo's per-sub-solution feeds are all
    included. Returns an empty list when there is no private feed.
    """
    import packages
    feeds = []
    for props in packages.find_all_props_files(repo_path):
        for feed in packages.find_private_feeds(os.path.dirname(props)):
            if feed not in feeds:
                feeds.append(feed)
    return feeds


def healthcheck_private_feeds(repo_paths, token):
    """Return ``(ok, error)`` after checking every private feed is reachable.

    Collects the distinct Azure DevOps feeds declared across *repo_paths* and
    verifies each one responds with the given token. Returns ``(True, "")`` when
    there are no private feeds to check.
    """
    import packages
    feeds = []
    for path in repo_paths:
        for feed in packages.repo_private_feeds(path):
            if feed not in feeds:
                feeds.append(feed)
    for feed in feeds:
        ok, error = packages.check_private_feed(feed, token)
        if not ok:
            return False, error
    return True, ""


def dotnet_restore(repo_path, token=None):
    """Run ``dotnet restore`` for every solution in *repo_path*.

    A single-repository layout restores its root ``.sln``; a multi-repository
    repo restores each sub-solution in its own directory (so that directory's
    ``nuget.config`` and private-feed prefixes apply). When *token* (an Azure
    DevOps AAD access token) is given it is exposed via ``VSS_NUGET_ACCESSTOKEN``
    so the Azure Artifacts credential provider can authenticate against private
    feeds without a browser prompt.

    Returns ``(ok, error_message)``. When the .NET SDK is not installed *ok* is
    False with a "dotnet not found" message; a non-zero exit returns the actual
    NuGet error line(s) so the cause is visible in the error list. The first
    solution that fails aborts the rest.
    """
    import packages
    dotnet = shutil.which("dotnet")
    if not dotnet:
        return False, "dotnet not found - is the .NET SDK installed?"
    solutions = packages.find_all_solution_files(repo_path)
    if not solutions:
        return False, "no .sln found"
    for solution in solutions:
        ok, error = packages._restore_solution(dotnet, solution, token)
        if not ok:
            return False, error
    return True, ""


def _restore_solution(dotnet, solution, token=None):
    """Run ``dotnet restore`` for a single ``.sln`` in its own directory."""
    import packages
    sln_dir = os.path.dirname(solution)
    env = os.environ.copy()
    # Never let the Azure Artifacts credential provider cache a cleartext
    # session token into the user's NuGet.Config; it uses the encrypted MSAL
    # cache instead (and the env token below when a private feed is present).
    env["NUGET_CREDENTIALPROVIDER_SESSIONTOKENCACHE_ENABLED"] = "false"
    if token:
        env["VSS_NUGET_ACCESSTOKEN"] = token
        # The credential provider only applies the token to feeds whose URI
        # matches one of these prefixes; without it the token is ignored.
        prefixes = packages._private_feed_prefixes(sln_dir)
        if prefixes:
            env["VSS_NUGET_URI_PREFIXES"] = prefixes
    ok, error = packages._run_restore(dotnet, solution, sln_dir, env)
    if ok:
        return True, ""
    # A solution that references a project file which is not on disk (e.g. a
    # stale ".probe/reflect/reflect.csproj" left in the .sln) fails the whole
    # restore with MSB3202. Fall back to restoring only the projects that exist,
    # one at a time (dotnet restore accepts a single target only).
    if packages._is_missing_project_error(error):
        projects = [p for p in packages._solution_project_paths(solution)
                    if os.path.isfile(p)]
        if projects:
            for project in projects:
                ok, error = packages._run_restore(dotnet, project, sln_dir, env)
                if not ok:
                    return False, error
            return True, ""
    return False, error


def _run_restore(dotnet, target, cwd, env):
    """Run ``dotnet restore`` for a single *target*. Returns (ok, error)."""
    import packages
    try:
        result = subprocess.run(
            [dotnet, "restore", target],
            cwd=cwd, capture_output=True, text=True, timeout=600, env=env,
            creationflags=packages._NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"dotnet restore failed: {error}"
    if result.returncode != 0:
        detail = packages._extract_restore_error(result.stdout, result.stderr)
        return False, f"dotnet restore failed: {detail}"
    return True, ""


class AdoNuget(NugetSource):
    @staticmethod
    def private_versions(repo_path, package_ids, token):
        import packages
        bases = []
        for feed in packages.find_private_feeds(repo_path):
            base = packages._flat_container_base(feed, token)
            if base:
                bases.append(base)

        def latest(package_id):
            versions = []
            for base in bases:
                version = packages._fetch_latest_stable_private(
                    package_id, base, token)
                if version:
                    versions.append(version)
            return max(versions, key=packages._version_key) if versions else None

        versions = packages.run_in_parallel(package_ids, latest)
        return True, {
            "configured": bool(bases),
            "versions": dict(zip(package_ids, versions)),
        }

    get_token = staticmethod(get_azure_devops_token)
    prewarm_token = staticmethod(prewarm_azure_devops_token)
    check_private_feed = staticmethod(check_private_feed)
    healthcheck_private_feeds = staticmethod(healthcheck_private_feeds)
    dotnet_restore = staticmethod(dotnet_restore)
