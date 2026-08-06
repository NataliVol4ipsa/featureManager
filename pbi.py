"""Azure DevOps PBI (work item) download, WBS parsing and repo mapping.

This module contains no UI. It handles:

  * loading local-only settings (``secrets.json``) and the editable repository
    synonyms dictionary (``repo_synonyms.json``) - both are git-ignored;
  * downloading a work item from Azure DevOps;
  * parsing the work item's description to find the repositories listed in its
    "WBS" (work breakdown) section;
  * mapping those free-text service names to local repository folders using the
    synonyms dictionary, and persisting newly learned mappings.

Both JSON files live next to this module. Templates with a ``.example`` suffix
are committed so a fresh checkout knows the expected shape.
"""

import os
import re
import json
import base64
import html
import urllib.parse
import urllib.request
import urllib.error
from html.parser import HTMLParser

from config import REPOS_ROOT
from gitutils import list_subfolders, get_git_credential


_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_SECRETS_PATH = os.path.join(_BASE_DIR, "secrets.json")
_SYNONYMS_PATH = os.path.join(_BASE_DIR, "repo_synonyms.json")

# Bold labels that mark the end of the WBS section in a work item description.
_SECTION_BREAK_WORDS = (
    "dependencies", "permission", "open question", "acceptance",
    "out of scope", "notes", "design", "definition of done", "risks",
)


# --------------------------------------------------------------------------- #
# Local-only settings (secrets.json)
# --------------------------------------------------------------------------- #

def load_secrets():
    """Return the local secrets/settings dict, or {} if the file is missing.

    A missing file or invalid JSON yields {} so callers can show a clear "not
    configured" message instead of crashing.
    """
    try:
        with open(_SECRETS_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# Repository synonyms dictionary (repo_synonyms.json)
# --------------------------------------------------------------------------- #

def load_synonyms():
    """Return the {folder_name: [synonym, ...]} dictionary (or {} if absent)."""
    try:
        with open(_SYNONYMS_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Keep only well-formed entries (folder -> list of strings).
    clean = {}
    for folder, synonyms in data.items():
        if isinstance(synonyms, list):
            clean[str(folder)] = [str(s) for s in synonyms]
    return clean


def save_synonyms(synonyms):
    """Write the synonyms dictionary back to disk. Returns (ok, error_message)."""
    try:
        with open(_SYNONYMS_PATH, "w", encoding="utf-8") as handle:
            json.dump(synonyms, handle, indent=4, sort_keys=True)
        return True, ""
    except OSError as exc:
        return False, f"could not save synonyms: {exc}"


def add_synonym(synonyms, folder, service_name):
    """Add *service_name* as a synonym of *folder* in-place if not already known.

    Returns True if a new synonym was added. The canonical folder name itself is
    never stored as a synonym (it always matches on its own).
    """
    norm = normalize(service_name)
    if not norm or norm == normalize(folder):
        return False
    existing = synonyms.setdefault(folder, [])
    if any(normalize(s) == norm for s in existing):
        return False
    existing.append(service_name.strip())
    return True


# --------------------------------------------------------------------------- #
# Name normalisation and mapping
# --------------------------------------------------------------------------- #

def normalize(text):
    """Lower-case and collapse non-alphanumeric runs to single spaces."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def available_folders():
    """Return the repository folder names that exist locally under REPOS_ROOT."""
    return list_subfolders(REPOS_ROOT)


def map_service(service_name, synonyms, folders):
    """Map a free-text *service_name* to a local folder name, or None.

    A match is only returned when the resolved folder actually exists in
    *folders* (a stale dictionary entry pointing at a missing folder is treated
    as unknown so the user can re-map it).
    """
    norm = normalize(service_name)
    if not norm:
        return None

    folder_by_norm = {normalize(f): f for f in folders}

    # 1) Direct match against an existing folder name.
    if norm in folder_by_norm:
        return folder_by_norm[norm]

    # 2) Match against the synonyms of each folder.
    for folder, synonyms_list in synonyms.items():
        if any(normalize(s) == norm for s in synonyms_list):
            return folder if folder in folders else folder_by_norm.get(
                normalize(folder)
            )
    return None


def map_services(service_names, synonyms=None, folders=None):
    """Return [(service_name, folder_or_None), ...] for each parsed service."""
    if synonyms is None:
        synonyms = load_synonyms()
    if folders is None:
        folders = available_folders()
    return [(name, map_service(name, synonyms, folders)) for name in service_names]


# --------------------------------------------------------------------------- #
# Azure DevOps work item download
# --------------------------------------------------------------------------- #

def _ado_auth_header():
    """Return (auth_header_value, error). Reuses the ADO_PAT env var, then git."""
    secrets = load_secrets()
    org_url = (secrets.get("ado_organization_url") or "").strip()
    if not org_url:
        return None, (
            "Azure DevOps organization URL is not configured. Set "
            "'ado_organization_url' in secrets.json."
        )

    # Reuse the same ADO_PAT environment variable used for PR/work-item linking
    # (set once via `setx ADO_PAT "<pat>"`) instead of duplicating it anywhere.
    pat = os.environ.get("ADO_PAT", "").strip()
    if pat:
        token = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
        return f"Basic {token}", ""

    # Fall back to the Git-stored credential for the org host.
    host = (urllib.parse.urlparse(org_url).hostname or "").lower()
    username, password = get_git_credential(host, org_url)
    if password:
        token = base64.b64encode(
            f"{username or ''}:{password}".encode("utf-8")
        ).decode("ascii")
        return f"Basic {token}", ""

    return None, (
        "No Azure DevOps credential found. Set the ADO_PAT environment variable "
        '(setx ADO_PAT "<your-pat>") or sign in with Git for the org host.'
    )


def fetch_work_item(work_item_id):
    """Download a work item from Azure DevOps. Returns (ok, result).

    On success *result* is a dict with "id", "title" and "description" (plain
    text). On failure *result* is an error message string.
    """
    secrets = load_secrets()
    org_url = (secrets.get("ado_organization_url") or "").strip().rstrip("/")
    if not org_url:
        return False, (
            "Azure DevOps organization URL is not configured. Set "
            "'ado_organization_url' in secrets.json."
        )

    auth, err = _ado_auth_header()
    if err:
        return False, err

    api_url = (
        f"{org_url}/_apis/wit/workitems/"
        f"{urllib.parse.quote(str(work_item_id))}"
        "?fields=System.Title,System.Description&api-version=7.1"
    )
    req = urllib.request.Request(api_url, method="GET")
    req.add_header("Authorization", auth)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        try:
            detail = json.loads(detail).get("message", detail)
        except ValueError:
            pass
        if exc.code == 404:
            detail = detail or f"work item {work_item_id} was not found"
        return False, f"could not download PBI {work_item_id} ({exc.code}): {detail}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"could not download PBI {work_item_id}: {exc}"

    fields = data.get("fields", {})
    return True, {
        "id": data.get("id", work_item_id),
        "title": fields.get("System.Title", "") or "",
        "description": fields.get("System.Description", "") or "",
    }


# --------------------------------------------------------------------------- #
# WBS parsing
# --------------------------------------------------------------------------- #

class _WbsHtmlParser(HTMLParser):
    """Collect an ordered list of bold headings and list items from HTML.

    ``events`` is a list of either ("heading", text) for bold/strong runs or
    ("item", depth, text) for list items, in document order. *depth* is the list
    nesting level (1 = top level). Only an item's own text (excluding nested
    lists) is captured.
    """

    def __init__(self):
        super().__init__()
        self.events = []
        self._depth = 0
        self._cur_item = None      # text buffer of the li currently being read
        self._bold = 0
        self._bold_buf = []

    def handle_starttag(self, tag, attrs):
        if tag in ("ul", "ol"):
            self._depth += 1
            self._cur_item = None   # text now belongs to nested items
        elif tag == "li":
            self._cur_item = []
            self.events.append(("item", self._depth, self._cur_item))
        elif tag in ("b", "strong"):
            self._bold += 1
            self._bold_buf = []

    def handle_endtag(self, tag):
        if tag in ("ul", "ol"):
            self._depth = max(0, self._depth - 1)
            self._cur_item = None
        elif tag == "li":
            self._cur_item = None
        elif tag in ("b", "strong") and self._bold > 0:
            self._bold -= 1
            text = "".join(self._bold_buf).strip()
            # Only bold runs *outside* a list item are treated as section
            # headings; bold inside a bullet (e.g. "**XAPI** service") is inline
            # emphasis and must not be mistaken for the start of a new section.
            if text and self._cur_item is None:
                self.events.append(("heading", text))
            self._bold_buf = []

    def handle_data(self, data):
        if self._bold > 0:
            self._bold_buf.append(data)
        if self._cur_item is not None:
            self._cur_item.append(data)


def _clean_service(text):
    """Normalise a raw service label: unescape, strip markup/punctuation."""
    text = html.unescape(text)
    text = text.replace("\u00a0", " ")
    # Strip inline markdown emphasis markers: **bold**, __bold__, ~~strike~~,
    # *italic*, _italic_, `code`.
    text = re.sub(r"(\*\*|__|~~|`)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(" :.-\u2022*_~`")            # leading/trailing bullets etc.
    # Drop any trailing parenthetical note, e.g. "Algo config (service)".
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
    return text


def _services_from_events(events):
    """Pull the top-level WBS list items from parsed HTML *events*."""
    start = None
    for index, event in enumerate(events):
        if event[0] == "heading" and "wbs" in event[1].lower():
            start = index
            break
    if start is None:
        return []

    collected = []
    for event in events[start + 1:]:
        if event[0] == "heading":
            # Any heading after we have started collecting items marks the next
            # section and ends the WBS list. Before the first item, only a known
            # later-section label ends it (a sub-label is skipped).
            if collected or any(
                word in event[1].lower() for word in _SECTION_BREAK_WORDS
            ):
                break
            continue
        _, depth, buffer = event
        text = _clean_service("".join(buffer))
        if text:
            collected.append((depth, text))

    if not collected:
        return []
    min_depth = min(depth for depth, _ in collected)
    return [text for depth, text in collected if depth == min_depth]


def _services_from_text(raw):
    """Parse top-level WBS bullets from a plain-text / markdown description."""
    text = html.unescape(raw.replace("&nbsp;", " "))
    lines = text.split("\n")

    start = None
    for index, line in enumerate(lines):
        if re.search(r"\bwbs\b", line, re.I):
            start = index
            break
    if start is None:
        return []

    bullet_re = re.compile(r"^(\s*)[\*\-\u2022]\s+(.*)$")
    collected = []
    for line in lines[start + 1:]:
        match = bullet_re.match(line)
        if not match:
            stripped = line.strip()
            if not stripped:
                continue
            # A non-bullet, non-empty line ends the WBS list once it has started
            # (it is the next section heading/paragraph, e.g. "Testing:").
            # Before any bullet is collected, intro text is skipped, but a known
            # later-section label still ends an (empty) WBS section.
            if collected or any(
                w in stripped.lower() for w in _SECTION_BREAK_WORDS
            ):
                break
            continue
        indent = len(match.group(1).replace("\t", "    "))
        label = _clean_service(match.group(2))
        if label:
            collected.append((indent, label))

    if not collected:
        return []
    min_indent = min(indent for indent, _ in collected)
    return [label for indent, label in collected if indent == min_indent]


def parse_wbs_services(description):
    """Return the list of repository/service names found in the WBS section.

    Handles both the HTML that Azure DevOps stores for ``System.Description``
    and plain-text/markdown descriptions. Returns [] when no WBS section or no
    top-level items are found.
    """
    if not description:
        return []
    if re.search(r"<(ul|ol|li|div|p|b|strong)\b", description, re.I):
        parser = _WbsHtmlParser()
        parser.feed(description)
        services = _services_from_events(parser.events)
        if services:
            return services
        # Fall back to text parsing if the HTML had no recognisable WBS list.
    return _services_from_text(description)


def slugify_title(title):
    """Turn a work item title into a valid branch-name fragment (no spaces).

    If the title contains a ``|``, only the part to the right of the last ``|``
    is used (the pipe and everything before it are dropped).
    """
    if "|" in title:
        title = title.rsplit("|", 1)[1]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", title.strip().lower())
    return slug.strip("_")
