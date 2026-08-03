"""Bump centrally-managed NuGet package versions for a repository.

This module contains no UI. For one repository it:

  * locates the ``Directory.Packages.props`` file in the repo root (Central
    Package Management), and
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
import urllib.parse
import urllib.request
import urllib.error


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
# Public feed lookup
# --------------------------------------------------------------------------- #

def fetch_latest_stable(package_id):
    """Return the highest stable version of *package_id* on nuget.org, or None.

    Prerelease versions (those containing a ``-``) are ignored. Returns None
    when the package is not found on the public feed or the request fails, so
    private-feed packages are simply left untouched by the caller.
    """
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
    request error yields None so the package is left untouched.
    """
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


def healthcheck_private_feeds(repo_paths, token):
    """Return ``(ok, error)`` after checking every private feed is reachable.

    Collects the distinct Azure DevOps feeds declared across *repo_paths* and
    verifies each one responds with the given token. Returns ``(True, "")`` when
    there are no private feeds to check.
    """
    feeds = []
    for path in repo_paths:
        for feed in find_private_feeds(path):
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


def bump_repo_packages(repo_path, include_public=True, include_private=False,
                       token=None):
    """Bump every out-of-date package in the repo's props file.

    *include_public* consults nuget.org; *include_private* consults the Azure
    DevOps feed(s) from the repo's nuget.config (using *token*, or a freshly
    fetched Azure CLI token when None). The highest stable version across the
    consulted feeds wins.

    Returns ``(ok, result)``. On success *result* is a list of
    ``(package_id, old_version, new_version)`` tuples describing the bumps made
    (empty when everything is already up to date). On failure *ok* is False and
    *result* is an error message. Packages not found on the consulted feeds are
    left unchanged and omitted from the list.
    """
    props = find_props_file(repo_path)
    if not props:
        return False, "no Directory.Packages.props in repo root"

    try:
        with open(props, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        return False, f"could not read Directory.Packages.props: {error}"

    entries = parse_package_versions(text)
    if not entries:
        return True, []

    private_bases = []
    if include_private:
        if token is None:
            token = get_azure_devops_token()
        if not token:
            return False, "no Azure DevOps token - run 'az login' first"
        for feed in find_private_feeds(repo_path):
            base = _flat_container_base(feed, token)
            if base:
                private_bases.append(base)
        if not private_bases and not include_public:
            return False, "no Azure DevOps feed found in nuget.config"

    resolver = _FeedResolver(include_public, private_bases, token)

    bumps = []
    new_versions = {}
    for package_id, current in entries:
        latest = resolver.latest(package_id)
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
        return False, f"could not write Directory.Packages.props: {error}"

    return True, bumps
