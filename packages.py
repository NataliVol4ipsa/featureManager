"""Bump centrally-managed NuGet package versions for a repository.

This module contains no UI. For one repository it:

  * locates the ``Directory.Packages.props`` file(s) - the one in the repo root
    (Central Package Management), or, for a multi-repository repo that has no
    root props file, every one under its sub-solution directories - and
  * for every ``<PackageVersion Include="..." Version="..." />`` entry checks
    the configured NuGet feed(s) for a newer stable release and, when one
    exists, rewrites the version in place.

Two kinds of feed are supported:

  * the **public** feed (nuget.org), queried anonymously, and
  * **private** Azure DevOps Artifacts feeds (``https://pkgs.dev.azure.com``)
    discovered from the repo's ``nuget.config``. These are queried with an AAD
    bearer token obtained from the Azure CLI (``az account get-access-token``),
    so ``az login`` must have been run first.

A bump run consults the public feed, the private feed(s), or both; the highest
stable version found across the consulted feeds wins. Packages not found on the
consulted feeds are left untouched. Prerelease versions are ignored. The file is
edited textually (regex, no XML/MSBuild dependency) so formatting and comments
are preserved; any resulting build errors are intentionally ignored - the user
commits and resolves the remaining packages later.
"""

import os
import re
import json
import time
import shutil
import subprocess
import threading
import urllib.parse
import urllib.request
import urllib.error

from parallel import run_in_parallel


# Suppress the console window Windows would otherwise pop up (and steal focus
# with) for each child process; a no-op on other platforms.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# The public NuGet v3 flat-container package index (lists every version of a
# package id). ``{id}`` must be the lower-cased, url-encoded package id.
_FLAT_CONTAINER = "https://api.nuget.org/v3-flatcontainer/{id}/index.json"

# URL prefixes used to classify a nuget.config source as public or private.
_PUBLIC_FEED_PREFIX = "https://api.nuget.org"
_AZURE_FEED_PREFIX = "https://pkgs.dev.azure.com"

# The Azure DevOps application (resource) id an AAD token must be scoped to so
# it is accepted by an Azure Artifacts feed.
_AZURE_DEVOPS_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"

# One ``<PackageVersion ... />`` element (self-closing or with a separate end
# tag - only the opening tag is matched, which carries the attributes).
_ELEMENT_RE = re.compile(r"<PackageVersion\b[^>]*?/?>", re.IGNORECASE)
_INCLUDE_RE = re.compile(r'Include\s*=\s*"([^"]*)"', re.IGNORECASE)
_VERSION_RE = re.compile(r'Version\s*=\s*"([^"]*)"', re.IGNORECASE)

# The <packageSources> block of a nuget.config and the source URLs within it
# (each source is an ``<add key="..." value="URL" />`` element).
_PACKAGE_SOURCES_RE = re.compile(
    r"<packageSources\b.*?</packageSources>", re.IGNORECASE | re.DOTALL
)
_ADD_RE = re.compile(r"<add\b[^>]*?/?>", re.IGNORECASE)
_VALUE_ATTR_RE = re.compile(r'value\s*=\s*"([^"]*)"', re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Directory.Packages.props discovery / parsing
# --------------------------------------------------------------------------- #

def find_props_file(repo_path):
    """Return the path to the repo's Directory.Packages.props, or None.

    The lookup is case-insensitive (the file is conventionally
    ``Directory.Packages.props`` but the on-disk casing can vary).
    """
    target = "directory.packages.props"
    try:
        for entry in os.listdir(repo_path):
            if entry.lower() == target:
                return os.path.join(repo_path, entry)
    except OSError:
        pass
    return None


# Directories skipped when scanning a multi-repository repo for props files:
# build output, VCS/IDE metadata and restored-package folders never hold a
# hand-maintained Directory.Packages.props.
_SKIP_DIRS = {
    "bin", "obj", ".git", ".vs", ".vscode", "node_modules", "packages",
    "testresults",
}


def find_all_props_files(repo_path):
    """Return every ``Directory.Packages.props`` to bump for *repo_path*.

    A single-repository layout keeps one props file in the repo root; the
    returned list then holds just that file. A multi-repository repo (e.g.
    ``InvestableUniverseCreation``) has no root props file but one per
    sub-solution directory - all of them are returned so a bump covers the whole
    repo. Build-output and tooling folders are skipped and the result is sorted
    for stable ordering.
    """
    root = find_props_file(repo_path)
    if root:
        return [root]
    target = "directory.packages.props"
    found = []
    for current, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d.lower() not in _SKIP_DIRS]
        for entry in files:
            if entry.lower() == target:
                found.append(os.path.join(current, entry))
    found.sort()
    return found


def find_solution_file(repo_path):
    """Return the path to a ``.sln`` file in the repo root, or None.

    ``dotnet restore`` with no arguments restores the solution/project in the
    working directory.
    """
    try:
        for entry in os.listdir(repo_path):
            if entry.lower().endswith(".sln"):
                return os.path.join(repo_path, entry)
    except OSError:
        pass
    return None


def find_all_solution_files(repo_path):
    """Return every ``.sln`` to restore for *repo_path*.

    A single-repository layout has one ``.sln`` in the repo root; a
    multi-repository repo has none there but one per sub-solution directory - all
    of them are returned so a restore covers the whole repo. Build-output and
    tooling folders are skipped and the result is sorted for stable ordering.
    """
    root = find_solution_file(repo_path)
    if root:
        return [root]
    found = []
    for current, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d.lower() not in _SKIP_DIRS]
        for entry in files:
            if entry.lower().endswith(".sln"):
                found.append(os.path.join(current, entry))
    found.sort()
    return found


def parse_package_versions(text):
    """Return [(package_id, version), ...] from the props file *text*.

    Only ``<PackageVersion>`` elements that carry both an ``Include`` and a
    ``Version`` attribute are returned; anything else is skipped.
    """
    result = []
    for match in _ELEMENT_RE.finditer(text):
        element = match.group(0)
        include = _INCLUDE_RE.search(element)
        version = _VERSION_RE.search(element)
        if include and version:
            result.append((include.group(1), version.group(1)))
    return result


# --------------------------------------------------------------------------- #
# Version comparison
# --------------------------------------------------------------------------- #

def _version_key(version):
    """Return the numeric core of *version* as a tuple of ints.

    Prerelease (``-``) and build-metadata (``+``) suffixes are dropped and each
    dotted segment is parsed as an int (non-numeric segments count as 0), so
    ``"1.2.3-beta+meta"`` -> ``(1, 2, 3)``.
    """
    core = version.split("+", 1)[0].split("-", 1)[0]
    parts = []
    for segment in core.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _is_newer(candidate, current):
    """Return True when *candidate* is a strictly higher version than *current*.

    Both keys are zero-padded to the same length first so ``"1.2"`` and
    ``"1.2.0"`` compare equal instead of the shorter one sorting lower.
    """
    a, b = _version_key(candidate), _version_key(current)
    length = max(len(a), len(b))
    a += (0,) * (length - len(a))
    b += (0,) * (length - len(b))
    return a > b


# --------------------------------------------------------------------------- #
# Latest-version memo cache (batch-scoped)
# --------------------------------------------------------------------------- #

# The same package id commonly appears in several props files (across a
# multi-repository repo's sub-solutions, or across the repos of a workspace), so
# its latest version is memoized for the duration of a bump batch and fetched
# from the feed only once. Keyed by feed scope ("public" or a private feed's
# base URL) so identical ids on different feeds stay independent. Guarded by a
# lock because lookups run in parallel; reset once per batch so a later batch
# still sees versions published in the meantime.
_version_cache = {}
_version_cache_lock = threading.Lock()


def reset_version_cache():
    """Clear the memoized latest-version lookups; call once per bump batch."""
    with _version_cache_lock:
        _version_cache.clear()


def _cached_version(key, fetch):
    """Return the memoized result of *fetch()* for *key* (thread-safe).

    Distinct keys still resolve concurrently - only a repeat of the same key is
    served from the cache. A ``None`` result (package absent or lookup failed) is
    cached too, so a missing package is not re-queried for every props file.
    """
    with _version_cache_lock:
        if key in _version_cache:
            return _version_cache[key]
    value = fetch()
    with _version_cache_lock:
        return _version_cache.setdefault(key, value)


# --------------------------------------------------------------------------- #
# Public feed lookup
# --------------------------------------------------------------------------- #

def fetch_latest_stable(package_id):
    """Return the highest stable version of *package_id* on nuget.org, or None.

    Prerelease versions (those containing a ``-``) are ignored. Returns None
    when the package is not found on the public feed or the request fails, so
    private-feed packages are simply left untouched by the caller. Results are
    memoized per batch (see ``reset_version_cache``).
    """
    return _cached_version(
        ("public", package_id.lower()),
        lambda: _fetch_latest_stable_public(package_id),
    )


def _fetch_latest_stable_public(package_id):
    """Query nuget.org for the highest stable version of *package_id* (uncached)."""
    url = _FLAT_CONTAINER.format(id=urllib.parse.quote(package_id.lower()))
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return None
    versions = data.get("versions") or []
    stable = [v for v in versions if "-" not in v]
    if not stable:
        return None
    # flat-container returns versions ascending, but sort defensively.
    stable.sort(key=_version_key)
    return stable[-1]


# --------------------------------------------------------------------------- #
# nuget.config feed discovery
# --------------------------------------------------------------------------- #

def find_nuget_config(repo_path):
    """Return the nearest ``nuget.config`` at or above *repo_path*, or None.

    The lookup walks up from the repo folder to the filesystem root (matching
    NuGet's own nearest-config behaviour) and is case-insensitive.
    """
    current = os.path.abspath(repo_path)
    while True:
        try:
            for entry in os.listdir(current):
                if entry.lower() == "nuget.config":
                    return os.path.join(current, entry)
        except OSError:
            pass
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def parse_nuget_sources(text):
    """Return the list of source URLs declared in <packageSources> of *text*."""
    block = _PACKAGE_SOURCES_RE.search(text)
    if not block:
        return []
    sources = []
    for match in _ADD_RE.finditer(block.group(0)):
        value = _VALUE_ATTR_RE.search(match.group(0))
        if value:
            sources.append(value.group(1))
    return sources


def find_private_feeds(repo_path):
    """Return the Azure DevOps Artifacts feed URLs configured for *repo_path*.

    Reads the repo's nearest ``nuget.config`` and keeps only sources whose URL
    starts with ``https://pkgs.dev.azure.com``. Returns an empty list when there
    is no config or no such source.
    """
    config = find_nuget_config(repo_path)
    if not config:
        return []
    try:
        with open(config, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return []
    return [
        source
        for source in parse_nuget_sources(text)
        if source.lower().startswith(_AZURE_FEED_PREFIX)
    ]


def _private_feed_prefixes(repo_path):
    """Return a ``;``-joined list of Azure DevOps org URI prefixes for the repo.

    The Azure Artifacts credential provider only uses ``VSS_NUGET_ACCESSTOKEN``
    for feeds whose URI starts with one of these prefixes. Each private feed is
    reduced to its ``https://pkgs.dev.azure.com/<org>/`` root. Falls back to the
    bare host prefix when a feed has no org segment.
    """
    prefixes = []
    for feed in find_private_feeds(repo_path):
        parts = urllib.parse.urlsplit(feed)
        org = parts.path.strip("/").split("/", 1)[0]
        prefix = f"{parts.scheme}://{parts.netloc}/{org}/" if org else \
            f"{parts.scheme}://{parts.netloc}/"
        if prefix not in prefixes:
            prefixes.append(prefix)
    return ";".join(prefixes)


# --------------------------------------------------------------------------- #
# Private feed lookup (Azure DevOps Artifacts, AAD bearer token)
# --------------------------------------------------------------------------- #

# Cached AAD token ({"token", "expires"}) and per-feed flat-container base URLs.
_token_cache = {"token": None, "expires": 0.0}
_feed_base_cache = {}


def get_azure_devops_token():
    """Return an AAD bearer token for Azure DevOps via the Azure CLI, or None.

    Runs ``az account get-access-token`` scoped to the Azure DevOps resource id
    and caches the result for a while. Returns None when the Azure CLI is not
    installed or the user is not logged in (``az login`` needed).
    """
    now = time.time()
    if _token_cache["token"] and _token_cache["expires"] > now:
        return _token_cache["token"]
    az = shutil.which("az")
    if not az:
        return None
    try:
        result = subprocess.run(
            [az, "account", "get-access-token", "--resource",
             _AZURE_DEVOPS_RESOURCE, "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, timeout=60,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    if not token:
        return None
    # Tokens last ~1h; cache for 50 minutes to stay comfortably valid.
    _token_cache["token"] = token
    _token_cache["expires"] = now + 50 * 60
    return token


def _flat_container_base(feed_index_url, token):
    """Return the PackageBaseAddress (flat-container) base URL of a feed, or None.

    Reads the feed's v3 service index with the bearer token and returns the
    ``PackageBaseAddress`` resource's ``@id`` (with a trailing slash). Cached
    per feed URL.
    """
    if feed_index_url in _feed_base_cache:
        return _feed_base_cache[feed_index_url]
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
    _feed_base_cache[feed_index_url] = base
    return base


def _fetch_latest_stable_private(package_id, base_url, token):
    """Return the highest stable version of *package_id* on a private feed, or None.

    *base_url* is the feed's flat-container base (see ``_flat_container_base``).
    Prerelease versions are ignored; a 404 (package not on this feed) or any
    request error yields None so the package is left untouched. Results are
    memoized per batch (see ``reset_version_cache``), keyed by feed base URL.
    """
    return _cached_version(
        ("private", base_url, package_id.lower()),
        lambda: _fetch_latest_stable_private_uncached(package_id, base_url, token),
    )


def _fetch_latest_stable_private_uncached(package_id, base_url, token):
    """Query a private feed for the highest stable version of *package_id*."""
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
    stable.sort(key=_version_key)
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
    feeds = []
    for props in find_all_props_files(repo_path):
        for feed in find_private_feeds(os.path.dirname(props)):
            if feed not in feeds:
                feeds.append(feed)
    return feeds


def healthcheck_private_feeds(repo_paths, token):
    """Return ``(ok, error)`` after checking every private feed is reachable.

    Collects the distinct Azure DevOps feeds declared across *repo_paths* and
    verifies each one responds with the given token. Returns ``(True, "")`` when
    there are no private feeds to check.
    """
    feeds = []
    for path in repo_paths:
        for feed in repo_private_feeds(path):
            if feed not in feeds:
                feeds.append(feed)
    for feed in feeds:
        ok, error = check_private_feed(feed, token)
        if not ok:
            return False, error
    return True, ""


class _FeedResolver:
    """Resolves the highest stable version of a package across the chosen feeds."""

    def __init__(self, include_public, private_bases, token):
        self._include_public = include_public
        self._private_bases = private_bases
        self._token = token

    def latest(self, package_id):
        """Return the highest stable version across all consulted feeds, or None."""
        candidates = []
        if self._include_public:
            version = fetch_latest_stable(package_id)
            if version:
                candidates.append(version)
        for base in self._private_bases:
            version = _fetch_latest_stable_private(package_id, base, self._token)
            if version:
                candidates.append(version)
        if not candidates:
            return None
        candidates.sort(key=_version_key)
        return candidates[-1]


# --------------------------------------------------------------------------- #
# Bumping
# --------------------------------------------------------------------------- #

def _apply_bumps(text, new_versions):
    """Return *text* with each package's Version attribute set to its new value.

    *new_versions* maps a lower-cased package id to its new version string. Only
    the ``Version`` attribute of the matching ``<PackageVersion>`` element is
    replaced; everything else (spacing, comments, attribute order) is preserved.
    """
    def replace_element(match):
        element = match.group(0)
        include = _INCLUDE_RE.search(element)
        if not include:
            return element
        new_version = new_versions.get(include.group(1).lower())
        if not new_version:
            return element
        return _VERSION_RE.sub(f'Version="{new_version}"', element, count=1)

    return _ELEMENT_RE.sub(replace_element, text)


def _bump_props_file(props, include_public, include_private, token):
    """Bump every out-of-date package in a single props file.

    Private feeds are resolved from the ``nuget.config`` nearest to *props* so a
    multi-repository repo's per-sub-solution feeds are honoured. Returns
    ``(ok, result)`` where *result* is the list of ``(id, old, new)`` bumps on
    success or an error message on failure.
    """
    try:
        with open(props, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        return False, f"could not read {os.path.basename(props)}: {error}"

    entries = parse_package_versions(text)
    if not entries:
        return True, []

    private_bases = []
    if include_private:
        for feed in find_private_feeds(os.path.dirname(props)):
            base = _flat_container_base(feed, token)
            if base:
                private_bases.append(base)
        if not private_bases and not include_public:
            return False, "no Azure DevOps feed found in nuget.config"

    resolver = _FeedResolver(include_public, private_bases, token)

    # Each lookup is an independent network round trip; fan them out so a props
    # file with many packages resolves in a fraction of the sequential time.
    latests = run_in_parallel(entries, lambda entry: resolver.latest(entry[0]))

    bumps = []
    new_versions = {}
    for (package_id, current), latest in zip(entries, latests):
        if latest and _is_newer(latest, current):
            bumps.append((package_id, current, latest))
            new_versions[package_id.lower()] = latest

    if not bumps:
        return True, []

    new_text = _apply_bumps(text, new_versions)
    try:
        with open(props, "w", encoding="utf-8") as handle:
            handle.write(new_text)
    except OSError as error:
        return False, f"could not write {os.path.basename(props)}: {error}"

    return True, bumps


def bump_repo_packages(repo_path, include_public=True, include_private=False,
                       token=None):
    """Bump every out-of-date package across the repo's props file(s).

    *include_public* consults nuget.org; *include_private* consults the Azure
    DevOps feed(s) from the relevant nuget.config (using *token*, or a freshly
    fetched Azure CLI token when None). The highest stable version across the
    consulted feeds wins. A multi-repository repo (no root props file) has every
    sub-solution's ``Directory.Packages.props`` bumped.

    Returns ``(ok, result)``. On success *result* is a list of
    ``(package_id, old_version, new_version)`` tuples describing the bumps made
    (empty when everything is already up to date; duplicates across props files
    are collapsed). On failure *ok* is False and *result* is an error message.
    Packages not found on the consulted feeds are left unchanged and omitted.
    """
    props_files = find_all_props_files(repo_path)
    if not props_files:
        return False, "no Directory.Packages.props found"

    if include_private and token is None:
        token = get_azure_devops_token()
        if not token:
            return False, "no Azure DevOps token - run 'az login' first"

    all_bumps = []
    for props in props_files:
        ok, result = _bump_props_file(
            props, include_public, include_private, token
        )
        if not ok:
            return False, result
        for bump in result:
            if bump not in all_bumps:
                all_bumps.append(bump)

    return True, all_bumps


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
    dotnet = shutil.which("dotnet")
    if not dotnet:
        return False, "dotnet not found - is the .NET SDK installed?"
    solutions = find_all_solution_files(repo_path)
    if not solutions:
        return False, "no .sln found"
    for solution in solutions:
        ok, error = _restore_solution(dotnet, solution, token)
        if not ok:
            return False, error
    return True, ""


def _restore_solution(dotnet, solution, token=None):
    """Run ``dotnet restore`` for a single ``.sln`` in its own directory."""
    sln_dir = os.path.dirname(solution)
    env = None
    if token:
        env = os.environ.copy()
        env["VSS_NUGET_ACCESSTOKEN"] = token
        # The credential provider only applies the token to feeds whose URI
        # matches one of these prefixes; without it the token is ignored.
        prefixes = _private_feed_prefixes(sln_dir)
        if prefixes:
            env["VSS_NUGET_URI_PREFIXES"] = prefixes
    try:
        result = subprocess.run(
            [dotnet, "restore"],
            cwd=sln_dir, capture_output=True, text=True, timeout=600, env=env,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"dotnet restore failed: {error}"
    if result.returncode != 0:
        detail = _extract_restore_error(result.stdout, result.stderr)
        return False, f"dotnet restore failed: {detail}"
    return True, ""


def _extract_restore_error(stdout, stderr):
    """Pull the meaningful NuGet error line(s) out of a failed restore's output.

    ``dotnet restore`` ends with a generic "Failed to restore ...csproj" summary;
    the real cause is an earlier ``error NUxxxx``/``error :`` line. Prefer those
    (deduped, project paths trimmed); fall back to the last non-empty line.
    """
    text = f"{stdout or ''}\n{stderr or ''}"
    errors = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if "error" in lowered and "failed to restore" not in lowered:
            # Drop a leading "<project path> : " prefix for readability.
            if " : " in line:
                line = line.split(" : ", 1)[1].strip()
            if line not in errors:
                errors.append(line)
    if errors:
        return "; ".join(errors[:3])
    tail = [l.strip() for l in text.splitlines() if l.strip()]
    return tail[-1] if tail else "unknown error"
