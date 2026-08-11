"""Git and filesystem helpers for Feature Manager.

These functions contain no UI code so they can be reused and tested on their
own. ``run_git`` never raises; it returns ``(ok, combined_output)``.
"""

import os
import re
import json
import time
import base64
import shutil
import socket
import threading
import subprocess
import urllib.parse
import urllib.request
import urllib.error

from config import (
    REPOS_ROOT, NUGETS_ROOT, WORKSPACES_ROOT,
    EXCLUDED_FOLDERS, EXCLUDED_NUGETS, EXCLUDED_WORKSPACES,
)


# Base message for the generic "commit changes as savepos" commits. Using
# save_uncommitted with this message keeps the staged/unstaged split so the
# work can later be restored exactly.
SAVEPOS_MSG = "savepos"


# On Windows a GUI (``--windowed``) build has no console, so every child
# process (git, git credential, ...) would otherwise flash its own console
# window. CREATE_NO_WINDOW suppresses that. It's 0 on non-Windows / older
# Pythons, so passing it as creationflags is always safe.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- #
# Folder / workspace discovery
# --------------------------------------------------------------------------- #

def list_subfolders(path):
    """Return a sorted list of immediate sub-folder names inside *path*.

    Files in the root are ignored (only directories are returned). Returns an
    empty list if the path does not exist so the UI can still render.
    """
    if not os.path.isdir(path):
        return []
    return sorted(
        name for name in os.listdir(path)
        if os.path.isdir(os.path.join(path, name))
    )


def get_service_folders():
    """Folders for the 'Services' tab: every repo folder except the excluded ones."""
    return [
        name for name in list_subfolders(REPOS_ROOT)
        if name.lower() not in EXCLUDED_FOLDERS
    ]


def get_nuget_folders():
    """Folders for the 'Nugets' tab: sub-folders of the nugets root, minus exclusions."""
    return [
        name for name in list_subfolders(NUGETS_ROOT)
        if name.lower() not in EXCLUDED_NUGETS
    ]


def list_workspaces():
    """Return the sorted names (without extension) of feature workspace files."""
    if not os.path.isdir(WORKSPACES_ROOT):
        return []
    suffix = ".code-workspace"
    return sorted(
        name[: -len(suffix)]
        for name in os.listdir(WORKSPACES_ROOT)
        if name.endswith(suffix)
        and name[: -len(suffix)].lower() not in EXCLUDED_WORKSPACES
    )


def list_workspaces_detailed():
    """Return workspace info sorted with the most recently modified first.

    Each item is (name, created_at, modified_at) where the timestamps are
    epoch-second floats. Files that cannot be stat'd are skipped.
    """
    if not os.path.isdir(WORKSPACES_ROOT):
        return []
    suffix = ".code-workspace"
    items = []
    for fname in os.listdir(WORKSPACES_ROOT):
        if not fname.endswith(suffix):
            continue
        name = fname[: -len(suffix)]
        if name.lower() in EXCLUDED_WORKSPACES:
            continue
        try:
            info = os.stat(os.path.join(WORKSPACES_ROOT, fname))
        except OSError:
            continue
        items.append((name, info.st_ctime, info.st_mtime))
    # Freshest (most recently modified) on top.
    items.sort(key=lambda item: item[2], reverse=True)
    return items



def read_workspace_repos(workspace_name):
    """Return (ok, repos_or_error).

    On success *repos* is a list of (repo_name, absolute_path) for every folder
    referenced by the workspace file. Paths are resolved relative to
    WORKSPACES_ROOT, matching how the files are written.
    """
    path = os.path.join(WORKSPACES_ROOT, f"{workspace_name}.code-workspace")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"could not read workspace: {exc}"

    repos = []
    for folder in data.get("folders", []):
        rel = folder.get("path", "")
        if not rel:
            continue
        abs_path = os.path.normpath(os.path.join(WORKSPACES_ROOT, rel))
        repos.append((os.path.basename(abs_path), abs_path))
    return True, repos


def write_workspace(feature_name, repos):
    """Write a .code-workspace file for *repos*. Returns (ok, message).

    Folder paths are stored relative to WORKSPACES_ROOT (e.g.
    "../../Repositories/<repo>") to match the existing workspace files.
    """
    folders = []
    for _, path in repos:
        rel = os.path.relpath(path, WORKSPACES_ROOT).replace("\\", "/")
        folders.append({"path": rel})

    content = {"folders": folders, "settings": {}}
    target = os.path.join(WORKSPACES_ROOT, f"{feature_name}.code-workspace")
    try:
        os.makedirs(WORKSPACES_ROOT, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(content, handle, indent=4)
        return True, f"Workspace created: {target}"
    except OSError as exc:
        return False, f"could not create workspace: {exc}"


# --------------------------------------------------------------------------- #
# Per-workspace feature-branch configuration
# --------------------------------------------------------------------------- #
#
# A workspace's repositories normally all share one feature branch named after
# the workspace ("feature/<workspace>"). Two things can differ per repo and are
# stored in a custom "featureManagerSettings" block inside the .code-workspace
# file (VS Code ignores unknown top-level keys, so it still opens cleanly):
#   * a per-repo branch override (the repo's feature branch has a different
#     name than the workspace), and
#   * an "ignoreGit" flag marking a repo that keeps its own branch and is
#     excluded from the workspace's git commands.
# Only differences are written; a repo on the default branch needs no entry.

# Top-level key in the .code-workspace file holding Feature Manager settings.
FM_SETTINGS_KEY = "featureManagerSettings"

# Sub-key holding the {folder: {...}} per-repo branch override map.
BRANCH_OVERRIDES_KEY = "branchOverrides"

# Override flag marking a repo as excluded from the workspace's git commands.
IGNORE_GIT_KEY = "ignoreGit"


def default_workspace_branch(workspace_name):
    """Return the default feature branch for a workspace ('feature/<name>')."""
    return f"feature/{workspace_name}"


def _read_workspace_json(workspace_name):
    """Return (ok, data_or_error) for a workspace's raw .code-workspace JSON."""
    path = os.path.join(WORKSPACES_ROOT, f"{workspace_name}.code-workspace")
    try:
        with open(path, encoding="utf-8") as handle:
            return True, json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"could not read workspace: {exc}"


def read_branch_overrides(workspace_name):
    """Return the {folder: {...}} branch-override map from a workspace, or {}.

    A missing file, missing section or malformed data all yield an empty dict so
    callers can treat "no overrides" and "unreadable" the same (defaults apply).
    """
    ok, data = _read_workspace_json(workspace_name)
    if not ok or not isinstance(data, dict):
        return {}
    section = data.get(FM_SETTINGS_KEY)
    if not isinstance(section, dict):
        return {}
    overrides = section.get(BRANCH_OVERRIDES_KEY)
    return overrides if isinstance(overrides, dict) else {}


def workspace_branch_entries(workspace_name):
    """Return (ok, entries) describing every folder of a workspace.

    Each entry is a dict with:
      * ``name`` / ``path`` - the folder name and absolute path,
      * ``branch`` - the effective feature branch (the per-repo override if set,
        otherwise 'feature/<workspace>'), and
      * ``ignoreGit`` - True when the repo is excluded from the workspace's git
        commands (it keeps whatever branch it is on).

    This is the single source of truth every workspace git feature uses to
    decide which branch a repo belongs to.
    """
    ok, repos = read_workspace_repos(workspace_name)
    if not ok:
        return False, repos
    overrides = read_branch_overrides(workspace_name)
    default = default_workspace_branch(workspace_name)
    entries = []
    for name, path in repos:
        override = overrides.get(name)
        if not isinstance(override, dict):
            override = {}
        entries.append({
            "name": name,
            "path": path,
            "branch": override.get("branch") or default,
            "ignoreGit": bool(override.get(IGNORE_GIT_KEY)),
        })
    return True, entries


def save_branch_overrides(workspace_name, overrides):
    """Persist the branch-override map into a workspace file. Returns (ok, msg).

    The existing folders / settings blocks are preserved; only the
    featureManagerSettings.branchOverrides section is replaced. An empty
    *overrides* removes the section (and an emptied featureManagerSettings block)
    so the file stays clean.
    """
    ok, data = _read_workspace_json(workspace_name)
    if not ok:
        return False, data
    if not isinstance(data, dict):
        data = {}

    section = data.get(FM_SETTINGS_KEY)
    if not isinstance(section, dict):
        section = {}
    if overrides:
        section[BRANCH_OVERRIDES_KEY] = overrides
    else:
        section.pop(BRANCH_OVERRIDES_KEY, None)
    if section:
        data[FM_SETTINGS_KEY] = section
    else:
        data.pop(FM_SETTINGS_KEY, None)

    target = os.path.join(WORKSPACES_ROOT, f"{workspace_name}.code-workspace")
    try:
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=4)
        return True, "Branch overrides saved."
    except OSError as exc:
        return False, f"could not save branch overrides: {exc}"


# --------------------------------------------------------------------------- #
# Git Bash terminal launching
# --------------------------------------------------------------------------- #

def _git_install_root():
    """Return the Git for Windows install root, or None if git can't be found.

    git.exe lives in either ``<root>\\cmd`` or ``<root>\\bin``, so the install
    root is two levels up from the resolved executable.
    """
    git = shutil.which("git")
    if not git:
        return None
    return os.path.dirname(os.path.dirname(git))


def _bash_exe():
    """Return the path to Git Bash's bash.exe (falls back to 'bash' on PATH)."""
    root = _git_install_root()
    if root:
        candidate = os.path.join(root, "bin", "bash.exe")
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("bash") or "bash"


def open_in_terminal_tabs(repos):
    """Open each repo as a Git Bash tab in one Windows Terminal window.

    *repos* is a list of (name, path) pairs. Each existing repo opens as its own
    tab, titled with the repo name, with the shell started in that repo's
    directory. A login+interactive shell is used so the user's ~/.bash_profile
    (aliases, prompt) is loaded. When Windows Terminal (wt.exe) is not
    available, falls back to a separate Git Bash (MinTTY) window per repo.

    Returns (ok, error_message); ok is False only when there is nothing to open
    or the launcher cannot be started.
    """
    repos = [(name, path) for name, path in repos if os.path.isdir(path)]
    if not repos:
        return False, "no existing repository folders to open"

    bash = _bash_exe()
    wt = shutil.which("wt")
    try:
        if wt:
            # One wt invocation with a tab per repo. Subcommands are separated by
            # a literal ';' argument: the first new-tab opens a new window and
            # each subsequent one becomes a tab in that same window.
            # --suppressApplicationTitle keeps our --title from being overwritten
            # by the shell's own title escape (Git's prompt sets it to the path).
            args = [wt]
            for index, (name, path) in enumerate(repos):
                if index:
                    args.append(";")
                args += [
                    "new-tab", "--title", name,
                    "--suppressApplicationTitle", "-d", path,
                    bash, "--login", "-i",
                ]
            subprocess.Popen(args)
            return True, ""

        # Fallback: a standalone Git Bash window per repo, each in its own dir.
        root = _git_install_root()
        git_bash = os.path.join(root, "git-bash.exe") if root else None
        for _, path in repos:
            if git_bash and os.path.isfile(git_bash):
                subprocess.Popen([git_bash], cwd=path)
            else:
                subprocess.Popen([bash, "--login", "-i"], cwd=path)
        return True, ""
    except OSError as exc:
        return False, f"could not launch terminal: {exc}"


def open_in_vscode(path):
    """Open *path* (a folder or .code-workspace file) in VS Code.

    On Windows a .code-workspace file is associated with VS Code, so
    ``os.startfile`` launches it directly; otherwise the ``code`` launcher on
    PATH is used as a fallback. Returns (ok, error_message).
    """
    if not os.path.exists(path):
        return False, f"path does not exist: {path}"

    startfile = getattr(os, "startfile", None)
    if startfile is not None:
        try:
            startfile(path)
            return True, ""
        except OSError as exc:
            return False, f"could not open VS Code: {exc}"

    code = shutil.which("code")
    if not code:
        return False, "VS Code 'code' launcher not found on PATH"
    try:
        subprocess.Popen([code, path], creationflags=NO_WINDOW)
        return True, ""
    except OSError as exc:
        return False, f"could not open VS Code: {exc}"


# --------------------------------------------------------------------------- #
# Core git runner
# --------------------------------------------------------------------------- #

def run_git(repo_path, args):
    """Run a git command inside *repo_path* and return (ok, output).

    *args* is the list of git arguments (e.g. ["pull"]). The combined
    stdout/stderr is returned as text so callers can log or display it.
    Never raises: a non-zero exit just yields ok=False.
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True, text=True,
            creationflags=NO_WINDOW,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output.strip()
    except FileNotFoundError:
        return False, "git executable not found on PATH"
    except OSError as exc:
        return False, str(exc)


# --------------------------------------------------------------------------- #
# Git state queries
# --------------------------------------------------------------------------- #

def is_git_repo(path):
    """Return True if *path* contains a .git directory."""
    return os.path.isdir(os.path.join(path, ".git"))


def git_current_branch(repo_path):
    """Return the current branch name, or '' if it cannot be determined."""
    ok, out = run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    return out if ok else ""


def git_has_changes(repo_path):
    """Return True if the working tree has uncommitted (tracked/untracked) changes."""
    ok, out = run_git(repo_path, ["status", "--porcelain"])
    return ok and bool(out)


def git_has_staged(repo_path):
    """Return True if there are staged (index) changes."""
    # 'diff --cached --quiet' exits non-zero when the index differs from HEAD.
    ok, _ = run_git(repo_path, ["diff", "--cached", "--quiet"])
    return not ok


def git_has_unstaged(repo_path):
    """Return True if there are unstaged tracked changes or untracked files."""
    # 'diff --quiet' exits non-zero when the working tree differs from the index.
    ok, _ = run_git(repo_path, ["diff", "--quiet"])
    has_tracked = not ok
    _, untracked = run_git(repo_path, ["ls-files", "--others", "--exclude-standard"])
    return has_tracked or bool(untracked.strip())


def git_rebase_in_progress(repo_path):
    """Return True if an unfinished rebase already exists in the repo."""
    git_dir = os.path.join(repo_path, ".git")
    return (
        os.path.isdir(os.path.join(git_dir, "rebase-merge"))
        or os.path.isdir(os.path.join(git_dir, "rebase-apply"))
    )


def git_branch_exists(repo_path, branch):
    """Return True if a local branch named *branch* exists."""
    ok, _ = run_git(
        repo_path, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]
    )
    return ok


def remote_branch_exists(repo_path, branch):
    """Return True if *branch* exists on origin (queried live via ls-remote).

    Hits the network, so call it off the UI thread. A missing remote, auth
    failure or any git error yields False (treated as "branch not present").
    """
    ok, out = run_git(repo_path, ["ls-remote", "--heads", "origin", branch])
    return ok and bool(out.strip())


def remote_branch_head(repo_path, branch):
    """Return the commit sha at the tip of origin/*branch*, or '' on failure.

    Hits the network (ls-remote), so call it off the UI thread.
    """
    ok, out = run_git(repo_path, ["ls-remote", "--heads", "origin", branch])
    if not ok or not out.strip():
        return ""
    return out.split()[0].strip()


def git_commit_message(repo_path, ref):
    """Return the subject line of the commit at *ref*, or '' on failure."""
    ok, out = run_git(repo_path, ["log", "-1", "--format=%s", ref])
    return out if ok else ""


def git_last_commit(repo_path, ref):
    """Return (short_hash, subject) of the commit at *ref*, or ('', '') on failure."""
    ok, out = run_git(repo_path, ["log", "-1", "--format=%h%x1f%s", ref])
    if not ok or "\x1f" not in out:
        return "", ""
    short, subject = out.split("\x1f", 1)
    return short.strip(), subject.strip()


def git_branch_is_empty(repo_path, target="master"):
    """Return True if the current branch has no changes versus *target*.

    "Empty" means a pull request from this branch to *target* would show zero
    file changes (no diff against their merge-base, matching what a PR displays).
    Repos that are not git repos, are on a detached HEAD, are already on
    *target*, or whose target ref cannot be resolved are treated as not empty
    (False) so they are never silently skipped.
    """
    if not is_git_repo(repo_path):
        return False
    branch = git_current_branch(repo_path)
    if not branch or branch == target:
        return False
    # Prefer the remote-tracking branch so the comparison mirrors what the PR
    # would show; fall back to a local branch of the same name.
    ref = None
    for candidate in (f"origin/{target}", target):
        ok, _ = run_git(
            repo_path, ["rev-parse", "--verify", "--quiet", candidate]
        )
        if ok:
            ref = candidate
            break
    if ref is None:
        return False
    # Three-dot diff compares against the merge-base, exactly like a PR. An exit
    # code of 0 (ok) means there is no diff, i.e. the branch is empty.
    ok, _ = run_git(repo_path, ["diff", "--quiet", f"{ref}...HEAD"])
    return ok


# --------------------------------------------------------------------------- #
# Branch name validation
# --------------------------------------------------------------------------- #

# Practical subset of git's ref rules: letters, digits and ._/- with no spaces.
_BRANCH_CHARS = re.compile(r"^[A-Za-z0-9._/-]+$")


def is_valid_branch_name(name):
    """Return True if *name* is acceptable as a git branch name (no spaces)."""
    if not name or " " in name:
        return False
    if name.startswith("/") or name.endswith("/") or name.endswith("."):
        return False
    if ".." in name or "//" in name or name.endswith(".lock"):
        return False
    return bool(_BRANCH_CHARS.match(name))


# --------------------------------------------------------------------------- #
# Save / restore uncommitted changes (preserving the staged/unstaged split)
# --------------------------------------------------------------------------- #
#
# These let an action stash the user's work as commit(s) and later put it back
# exactly as it was - staged files staged, unstaged files unstaged. The commit
# messages encode whether the change set was staged, unstaged or both, so
# restore can reconstruct the original index/working-tree split. A second
# commit is only created when both staged AND unstaged changes are present.

def save_uncommitted(repo_path, base_msg):
    """Commit uncommitted changes preserving the staged/unstaged split.

    Returns (ok, error_message). Commit subjects are "<base_msg> - staged"
    and/or "<base_msg> - unstaged".
    """
    staged = git_has_staged(repo_path)
    unstaged = git_has_unstaged(repo_path)

    if staged and unstaged:
        # Commit the index (staged) first, then everything else.
        ok, out = run_git(repo_path, ["commit", "-m", f"{base_msg} - staged"])
        if not ok:
            return False, out
        ok, out = run_git(repo_path, ["add", "-A"])
        if not ok:
            return False, out
        ok, out = run_git(repo_path, ["commit", "-m", f"{base_msg} - unstaged"])
        if not ok:
            return False, out
    elif staged:
        ok, out = run_git(repo_path, ["commit", "-m", f"{base_msg} - staged"])
        if not ok:
            return False, out
    elif unstaged:
        ok, out = run_git(repo_path, ["add", "-A"])
        if not ok:
            return False, out
        ok, out = run_git(repo_path, ["commit", "-m", f"{base_msg} - unstaged"])
        if not ok:
            return False, out

    return True, ""


def has_savepos(repo_path, base_msg):
    """Return True if HEAD is a savepos commit created by save_uncommitted."""
    head = git_commit_message(repo_path, "HEAD")
    return head in (f"{base_msg} - staged", f"{base_msg} - unstaged")


def commit_all(name, path, message):
    """Stage and commit all changes in one repo with *message*. Returns (ok, err).

    Skips repos that are not git repos or have nothing to commit (those are
    reported as errors so the user sees why they were not committed).
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository"
    if not git_has_changes(path):
        return False, f"{name}: no changes to commit"

    ok, out = run_git(path, ["add", "-A"])
    if not ok:
        return False, f"{name}: {out}"
    ok, out = run_git(path, ["commit", "-m", message])
    if not ok:
        return False, f"{name}: {out}"
    return True, ""


def git_push(name, path):
    """Push one repo's current branch to origin. Returns (ok, error).

    Uses ``push -u origin HEAD`` so the remote branch is created and tracking
    is set up automatically when it does not exist yet, with no interaction.
    Repos that are not git repos or are in a detached HEAD state are reported
    as errors.
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository"
    branch = git_current_branch(path)
    if not branch:
        return False, f"{name}: not on a branch (detached HEAD)"
    ok, out = run_git(path, ["push", "-u", "origin", "HEAD"])
    if not ok:
        return False, f"{name}: {out}"
    return True, ""


def git_remote_url(repo_path, remote="origin"):
    """Return the configured URL for *remote* (e.g. origin), or "" if none."""
    ok, out = run_git(repo_path, ["remote", "get-url", remote])
    if not ok:
        return ""
    return out.strip()


def _branch_web_url(remote_url, branch):
    """Translate a git *remote_url* + *branch* into a browser URL, or "".

    Handles the common HTTPS and SSH forms for Azure DevOps, GitHub, GitLab and
    Bitbucket. Falls back to the repository's https URL when the host is not
    recognised, and returns "" when the remote URL cannot be parsed.
    """
    url = remote_url.strip()
    if url.endswith(".git"):
        url = url[:-4]

    host, path = "", ""
    if url.startswith("git@"):
        # SSH short form: git@host:path
        rest = url[len("git@"):]
        if ":" in rest:
            host, path = rest.split(":", 1)
    elif "://" in url:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        path = parsed.path.lstrip("/")
    if not host or not path:
        return ""

    path = path.strip("/")
    branch_q = urllib.parse.quote(branch, safe="/")

    # Azure DevOps SSH: ssh.dev.azure.com with path v3/org/project/repo
    if host == "ssh.dev.azure.com":
        parts = path.split("/")
        if len(parts) >= 4 and parts[0] == "v3":
            org, project, repo = parts[1], parts[2], parts[3]
            return (f"https://dev.azure.com/{org}/{project}/_git/{repo}"
                    f"?version=GB{branch_q}")
        return ""

    # Azure DevOps HTTPS: dev.azure.com/org/project/_git/repo (or *.visualstudio.com)
    if host == "dev.azure.com" or host.endswith(".visualstudio.com"):
        return f"https://{host}/{path}?version=GB{branch_q}"

    if host == "github.com":
        return f"https://github.com/{path}/tree/{branch_q}"

    if host == "bitbucket.org":
        return f"https://bitbucket.org/{path}/branch/{branch_q}"

    if "gitlab" in host:
        return f"https://{host}/{path}/-/tree/{branch_q}"

    # Unknown host: link to the repository over https as a best effort.
    return f"https://{host}/{path}"


def git_branch_url(repo_path, branch, remote="origin"):
    """Return a browser URL to view *branch* on the remote host, or "".

    Returns "" when there is no such remote, no branch, or the URL cannot be
    derived (callers simply omit the link in that case).
    """
    if not branch:
        return ""
    url = git_remote_url(repo_path, remote)
    if not url:
        return ""
    return _branch_web_url(url, branch)


# --------------------------------------------------------------------------- #
# Azure DevOps pull requests
# --------------------------------------------------------------------------- #

def ado_pr_title_from_branch(branch):
    """Build a PR title from a feature branch name.

    ``feature/123_my_description`` becomes ``feature(123) My description``: the
    leading numeric id is shown in braces, underscores become spaces and the
    description's first word is capitalised. Falls back to a best-effort title
    for branches that do not follow the ``<prefix>/<id>_<description>`` shape.
    """
    name = branch.strip()
    if name.startswith("refs/heads/"):
        name = name[len("refs/heads/"):]

    prefix, sep, rest = name.partition("/")
    if not sep:  # no "/" -> treat the whole thing as the rest, no prefix
        prefix, rest = "", name

    first, sep2, tail = rest.partition("_")
    if first.isdigit():
        number, desc = first, tail
    else:
        number, desc = "", rest

    desc = desc.replace("_", " ").strip()
    if desc:
        desc = desc[0].upper() + desc[1:]

    parts = []
    if prefix and number:
        parts.append(f"{prefix}({number})")
    elif prefix:
        parts.append(prefix)
    elif number:
        parts.append(f"({number})")
    if desc:
        parts.append(desc)
    return " ".join(parts).strip() or name


def ado_work_item_id_from_branch(branch):
    """Return the work-item id embedded in a feature branch name, or "".

    ``feature/514231_my_description`` -> ``514231`` (the leading numeric id of
    the description). Returns "" when the branch has no such leading number.
    """
    name = branch.strip()
    if name.startswith("refs/heads/"):
        name = name[len("refs/heads/"):]
    _prefix, _sep, rest = name.partition("/")
    if not _sep:
        rest = name
    first = rest.partition("_")[0]
    return first if first.isdigit() else ""


def parse_ado_remote(remote_url):
    """Return (org, project, repo, host) for an Azure DevOps remote, or None.

    Handles the HTTPS (dev.azure.com and *.visualstudio.com) and SSH
    (git@ssh.dev.azure.com:v3/...) forms. Returns None for non-ADO remotes.
    """
    url = remote_url.strip()
    if url.endswith(".git"):
        url = url[:-4]

    # SSH short form: git@ssh.dev.azure.com:v3/org/project/repo
    if url.startswith("git@ssh.dev.azure.com:"):
        path = url.split(":", 1)[1]
        parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/")]
        if len(parts) >= 4 and parts[0] == "v3":
            return parts[1], parts[2], parts[3], "dev.azure.com"
        return None

    if "://" not in url:
        return None
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    # Decode each path segment so values like "My%20Project" become
    # "My Project" (they are re-encoded when used).
    parts = [
        urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/")
    ]

    # HTTPS: dev.azure.com/org/project/_git/repo
    if host == "dev.azure.com":
        if "_git" in parts:
            i = parts.index("_git")
            if i >= 2 and i + 1 < len(parts):
                return parts[0], parts[i - 1], parts[i + 1], host
        return None

    # HTTPS old form: org.visualstudio.com[/DefaultCollection]/project/_git/repo
    if host.endswith(".visualstudio.com"):
        org = host.split(".")[0]
        if "_git" in parts:
            i = parts.index("_git")
            if i >= 1 and i + 1 < len(parts):
                return org, parts[i - 1], parts[i + 1], host
        return None

    return None


def ado_host_for_path(path):
    """Return the Azure DevOps host for *path*'s origin remote, or ""."""
    parsed = parse_ado_remote(git_remote_url(path))
    return parsed[3] if parsed else ""


def check_ado_connectivity(host, timeout=4):
    """Return (ok, error) after a fast TCP probe of *host* on port 443.

    A quick reachability check so network-dependent batch actions fail fast with
    a clear "check your VPN" message instead of blocking on each request's long
    timeout when there is no route to Azure DevOps (e.g. the VPN is off).
    """
    if not host:
        return False, ""
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return True, ""
    except OSError:
        return False, f"cannot reach {host} - check your VPN / network connection"


def get_git_credential(host, url=None):
    """Return (username, password) Git has stored for *host*, or (None, None).

    Uses ``git credential fill`` so the existing credential (e.g. a PAT managed
    by Git Credential Manager) is reused without prompting the user. When the
    full remote *url* is given it is passed instead of a bare host: Azure DevOps
    stores ``dev.azure.com`` credentials per-organization (``useHttpPath``), so
    the org path must be included or the lookup returns nothing.
    """
    if url:
        query = f"url={url}\n\n"
    else:
        query = f"protocol=https\nhost={host}\n\n"
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input=query,
            capture_output=True, text=True, timeout=20,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if proc.returncode != 0:
        return None, None
    creds = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            creds[key] = value
    return creds.get("username"), creds.get("password")


def create_ado_pr(name, path, title, description="", target="master", draft=False):
    """Create an Azure DevOps pull request for one repo.

    Returns (ok, url_or_err, warning). The PR goes from the repo's current
    branch to *target* (master). When *draft* is true the PR is created as a
    draft. The remote branch must already be pushed.
    Authentication reuses the Git credential already stored for the host, so no
    extra credentials are requested. When the branch name embeds a work-item id
    (e.g. ``feature/514231_...``) that work item is linked to the new PR; a link
    failure does not fail the PR but is returned as *warning*.
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository", ""
    branch = git_current_branch(path)
    if not branch:
        return False, f"{name}: not on a branch (detached HEAD)", ""
    if branch == target:
        return False, f"{name}: on {target}; nothing to create a pull request for", ""

    remote_url = git_remote_url(path)
    parsed = parse_ado_remote(remote_url)
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository", ""
    org, project, repo, host = parsed

    username, password = get_git_credential(host, remote_url)
    if not password:
        return False, f"{name}: no stored Git credential for {host}", ""

    # Project-scoped endpoint; project and repo are re-encoded here (they were
    # decoded by parse_ado_remote, so names with spaces work correctly).
    api_url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/git/repositories/"
        f"{urllib.parse.quote(repo)}/pullrequests?api-version=7.1"
    )
    body = json.dumps({
        "sourceRefName": f"refs/heads/{branch}",
        "targetRefName": f"refs/heads/{target}",
        "title": title,
        "description": description,
        "isDraft": bool(draft),
    }).encode("utf-8")
    auth = base64.b64encode(
        f"{username or ''}:{password}".encode("utf-8")
    ).decode("ascii")

    req = urllib.request.Request(api_url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        try:
            detail = json.loads(detail).get("message", detail)
        except ValueError:
            pass
        if not detail:
            # ADO returns an empty 404 body when the resource path is wrong;
            # include the URL so the org/repo can be verified.
            detail = f"resource not found at {api_url}"
        return False, f"{name}: pull request failed ({exc.code}): {detail}", ""
    except (urllib.error.URLError, OSError) as exc:
        return False, f"{name}: pull request failed: {exc}", ""

    pr_id = data.get("pullRequestId")
    # Prefer the project/repo names returned by the API for an accurate link.
    repo_info = data.get("repository") or {}
    project_name = (repo_info.get("project") or {}).get("name") or project
    repo_name = repo_info.get("name") or repo
    web_url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project_name)}/_git/"
        f"{urllib.parse.quote(repo_name)}/pullrequest/{pr_id}"
    )

    # Best-effort: link the work item referenced by the branch name to the PR.
    work_item_id = ado_work_item_id_from_branch(branch)
    if work_item_id and pr_id is not None:
        project_id = (repo_info.get("project") or {}).get("id")
        repo_id = repo_info.get("id")
        if not (project_id and repo_id):
            warning = (
                f"{name}: PR created but could not link work item "
                f"{work_item_id} (missing project/repo id in API response)"
            )
            return True, web_url, warning
        # The Git credential is usually scoped to Code only, so the Work Items
        # API rejects it. Prefer a dedicated PAT from ADO_PAT (Work Items: write)
        # for the link step, falling back to the Git credential.
        link_token = os.environ.get("ADO_PAT") or password
        ok_link, link_err = _link_work_item_to_pr(
            org, work_item_id, project_id, repo_id, pr_id, username, link_token
        )
        if not ok_link:
            return True, web_url, f"{name}: {link_err}"

    return True, web_url, ""


def get_ado_pr_url(name, path, target="master"):
    """Return (ok, url_or_err) for an existing open PR of the repo's branch.

    Looks up the active Azure DevOps pull request that goes from the repo's
    current branch to *target* (master) and returns a browser link to it.
    Authentication reuses the Git credential already stored for the host (no
    prompts). ok is False - with an explanatory message - when the repo is not
    an ADO repo, has no stored credential, or has no open pull request for its
    current branch.
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository"
    branch = git_current_branch(path)
    if not branch:
        return False, f"{name}: not on a branch (detached HEAD)"

    remote_url = git_remote_url(path)
    parsed = parse_ado_remote(remote_url)
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, repo, host = parsed

    username, password = get_git_credential(host, remote_url)
    if not password:
        return False, f"{name}: no stored Git credential for {host}"

    query = urllib.parse.urlencode({
        "searchCriteria.sourceRefName": f"refs/heads/{branch}",
        "searchCriteria.targetRefName": f"refs/heads/{target}",
        "searchCriteria.status": "active",
        "api-version": "7.1",
    })
    api_url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/git/repositories/"
        f"{urllib.parse.quote(repo)}/pullrequests?{query}"
    )
    auth = base64.b64encode(
        f"{username or ''}:{password}".encode("utf-8")
    ).decode("ascii")

    req = urllib.request.Request(api_url, method="GET")
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        try:
            detail = json.loads(detail).get("message", detail)
        except ValueError:
            pass
        return False, f"{name}: pull request lookup failed ({exc.code}): {detail}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"{name}: pull request lookup failed: {exc}"

    prs = data.get("value") or []
    if not prs:
        return False, f"{name}: no open pull request for branch '{branch}'"

    pr = prs[0]
    pr_id = pr.get("pullRequestId")
    repo_info = pr.get("repository") or {}
    project_name = (repo_info.get("project") or {}).get("name") or project
    repo_name = repo_info.get("name") or repo
    web_url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project_name)}/_git/"
        f"{urllib.parse.quote(repo_name)}/pullrequest/{pr_id}"
    )
    return True, web_url


# Merge strategies accepted by the Azure DevOps PR completion API.
PR_MERGE_STRATEGIES = ("noFastForward", "squash", "rebase", "rebaseMerge")


def _ado_get_json(url, auth, timeout=30):
    """GET *url* with Basic *auth* and return (ok, json_or_error_message)."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        try:
            detail = json.loads(detail).get("message", detail)
        except ValueError:
            pass
        return False, f"({exc.code}) {detail}"
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def _ado_send_json(url, auth, body, method, timeout=30):
    """Send *body* (dict, or None for an empty payload) to *url*.

    Returns (ok, json_or_error_message). Used for the PR PATCH calls and the
    empty-body policy requeue.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Basic {auth}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return True, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        try:
            detail = json.loads(detail).get("message", detail)
        except ValueError:
            pass
        return False, f"({exc.code}) {detail}"
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def _ado_current_user_id(org, auth):
    """Return the authenticated user's identity id (for auto-complete), or None."""
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"_apis/connectionData?api-version=7.1"
    )
    ok, data = _ado_get_json(url, auth)
    if not ok:
        return None
    return (data.get("authenticatedUser") or {}).get("id")


def _pr_policy_state(org, project, project_id, pr_id, auth):
    """Inspect the branch-policy evaluations of a PR.

    Returns (pending, unrun_build_ids, rejected, count) where *pending* is True
    when any required policy is queued or running, *unrun_build_ids* holds the
    evaluation ids of build policies that are queued without a build yet (they
    can be requeued to start the build), *rejected* is True when a policy failed,
    and *count* is the number of evaluations returned.
    """
    artifact = f"vstfs:///CodeReview/CodeReviewId/{project_id}/{pr_id}"
    query = urllib.parse.urlencode({
        "artifactId": artifact,
        "api-version": "7.1",
    })
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/policy/evaluations?{query}"
    )
    ok, data = _ado_get_json(url, auth)
    if not ok:
        return False, [], False, 0

    pending = False
    rejected = False
    unrun_build_ids = []
    evals = data.get("value") or []
    for evaluation in evals:
        status = (evaluation.get("status") or "").lower()
        config = evaluation.get("configuration") or {}
        type_name = ((config.get("type") or {}).get("displayName") or "").lower()
        is_build = "build" in type_name
        if status in ("queued", "running"):
            pending = True
        if status in ("rejected", "broken"):
            rejected = True
        if is_build and status == "queued":
            context = evaluation.get("context") or {}
            if not context.get("buildId"):
                eid = evaluation.get("evaluationId")
                if eid:
                    unrun_build_ids.append(eid)
    return pending, unrun_build_ids, rejected, len(evals)


def _requeue_policy_evaluation(org, project, evaluation_id, auth):
    """Requeue a branch-policy evaluation (e.g. start a build). Returns (ok, err)."""
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/policy/evaluations/"
        f"{urllib.parse.quote(str(evaluation_id))}?api-version=7.1"
    )
    return _ado_send_json(url, auth, None, "PATCH")


def complete_ado_pr(name, path, target="master", merge_strategy="noFastForward",
                    delete_source_branch=True, transition_work_items=True,
                    publish_draft=True, auto_complete_when_not_ready=True,
                    queue_build=True):
    """Complete (merge) the open Azure DevOps PR for the repo's current branch.

    Returns (ok, url_or_err, warning). Looks up the active pull request that
    goes from the repo's current branch to *target* (master) and tries to merge
    it with the given options. Authentication reuses the Git credential stored
    for the host (no prompts).

    The PR state is inspected and remediations are applied when enabled:

    * ``publish_draft`` - when the PR is a draft it is published (unmarked as
      draft) so its branch policies start; without this a draft cannot complete.
    * ``queue_build`` - when a required build policy is queued but has no build
      yet (typical for a freshly published draft) it is requeued so the build
      runs.
    * ``auto_complete_when_not_ready`` - when the PR is not immediately mergeable
      (build/policies still running) it is set to auto-complete so Azure DevOps
      merges it once every policy passes, instead of completing right now.

    ok is False - with an explanatory message - when the repo is not an ADO
    repo, has no stored credential, has no open pull request, a required
    remediation is disabled, a policy was rejected, or the merge is refused.
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository", ""
    branch = git_current_branch(path)
    if not branch:
        return False, f"{name}: not on a branch (detached HEAD)", ""
    if branch == target:
        return False, f"{name}: on {target}; no pull request to complete", ""

    remote_url = git_remote_url(path)
    parsed = parse_ado_remote(remote_url)
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository", ""
    org, project, repo, host = parsed

    username, password = get_git_credential(host, remote_url)
    if not password:
        return False, f"{name}: no stored Git credential for {host}", ""

    auth = base64.b64encode(
        f"{username or ''}:{password}".encode("utf-8")
    ).decode("ascii")

    # 1) Find the active PR for this branch (need its id and merge source commit).
    query = urllib.parse.urlencode({
        "searchCriteria.sourceRefName": f"refs/heads/{branch}",
        "searchCriteria.targetRefName": f"refs/heads/{target}",
        "searchCriteria.status": "active",
        "api-version": "7.1",
    })
    base_url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/git/repositories/"
        f"{urllib.parse.quote(repo)}/pullrequests"
    )
    ok, data = _ado_get_json(f"{base_url}?{query}", auth)
    if not ok:
        return False, f"{name}: pull request lookup failed {data}", ""

    prs = data.get("value") or []
    if not prs:
        return False, f"{name}: no open pull request for branch '{branch}'", ""

    pr = prs[0]
    pr_id = pr.get("pullRequestId")
    is_draft = bool(pr.get("isDraft"))
    merge_commit = (pr.get("lastMergeSourceCommit") or {}).get("commitId")
    repo_info = pr.get("repository") or {}
    project_id = (repo_info.get("project") or {}).get("id")
    project_name = (repo_info.get("project") or {}).get("name") or project
    repo_name = repo_info.get("name") or repo
    web_url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project_name)}/_git/"
        f"{urllib.parse.quote(repo_name)}/pullrequest/{pr_id}"
    )
    pr_url = f"{base_url}/{pr_id}?api-version=7.1"

    if merge_strategy not in PR_MERGE_STRATEGIES:
        merge_strategy = "noFastForward"

    notes = []  # non-fatal actions taken, surfaced together as one warning

    # 2) Publish the PR if it is a draft (draft PRs cannot complete and their
    #    build policies do not run until published).
    if is_draft:
        if not publish_draft:
            return (
                False,
                f"{name}: pull request is a draft; enable 'publish draft PRs' "
                "to complete it",
                "",
            )
        ok, result = _ado_send_json(pr_url, auth, {"isDraft": False}, "PATCH")
        if not ok:
            return False, f"{name}: could not publish draft PR: {result}", ""
        notes.append("published draft")

    # 3) Inspect branch-policy state to decide between an immediate merge and
    #    setting auto-complete.
    pending, unrun_build_ids, rejected, evaluation_count = (False, [], False, 0)
    if project_id:
        pending, unrun_build_ids, rejected, evaluation_count = _pr_policy_state(
            org, project, project_id, pr_id, auth
        )

    if rejected:
        return (
            False,
            f"{name}: a required branch policy was rejected; resolve it in the "
            "PR before completing",
            "",
        )

    # Queue any required build that has not started (common right after a draft
    # is published). Doing so means the PR cannot merge immediately, so it will
    # go down the auto-complete path below.
    if queue_build and unrun_build_ids:
        queued = 0
        for eid in unrun_build_ids:
            ok, _ = _requeue_policy_evaluation(org, project, eid, auth)
            if ok:
                queued += 1
        if queued:
            pending = True
            notes.append(f"queued {queued} build{'s' if queued != 1 else ''}")

    # A freshly published draft may not have its evaluations listed yet; assume
    # policies still need to run so it is not completed prematurely.
    if "published draft" in notes and evaluation_count == 0:
        pending = True

    completion_options = {
        "deleteSourceBranch": bool(delete_source_branch),
        "mergeStrategy": merge_strategy,
        "transitionWorkItems": bool(transition_work_items),
    }

    # 4a) Not ready to merge now -> set auto-complete so ADO merges it later.
    if pending:
        if not auto_complete_when_not_ready:
            extra = f" ({', '.join(notes)})" if notes else ""
            return (
                False,
                f"{name}: pull request is not ready to complete - build or "
                f"policies still running{extra}; enable auto-complete to merge "
                "it automatically",
                "",
            )
        user_id = _ado_current_user_id(org, auth)
        if not user_id:
            return (
                False,
                f"{name}: could not resolve identity to set auto-complete",
                "",
            )
        body = {
            "autoCompleteSetBy": {"id": user_id},
            "completionOptions": completion_options,
        }
        ok, result = _ado_send_json(pr_url, auth, body, "PATCH")
        if not ok:
            return False, f"{name}: could not set auto-complete: {result}", ""
        notes.append("set to auto-complete")
        return True, web_url, f"{name}: {', '.join(notes)} (merges when policies pass)"

    # 4b) Ready now -> complete immediately.
    patch_body = {
        "status": "completed",
        "completionOptions": completion_options,
    }
    if merge_commit:
        patch_body["lastMergeSourceCommit"] = {"commitId": merge_commit}
    ok, result = _ado_send_json(pr_url, auth, patch_body, "PATCH")
    if not ok:
        return False, f"{name}: pull request completion failed {result}", ""

    # ADO may queue the merge asynchronously; a non-completed status here means
    # the merge is still pending (e.g. running policies) rather than done.
    status = result.get("status")
    if status and status != "completed":
        merge_status = result.get("mergeStatus") or "queued"
        notes.append(f"merge {status} (mergeStatus: {merge_status})")
        return True, web_url, f"{name}: {', '.join(notes)}; check the PR"

    if notes:
        return True, web_url, f"{name}: {', '.join(notes)}, then completed"
    return True, web_url, ""



# Serialize every work-item link across threads. When PRs are created in
# parallel and several share the same PBI, concurrent PATCHes to that one work
# item collide (ADO optimistic concurrency -> 409/412), so the links must run
# one at a time even though PR creation itself stays parallel.
_WORK_ITEM_LINK_LOCK = threading.Lock()


def _link_work_item_to_pr(org, work_item_id, project_id, repo_id, pr_id,
                          username, password):
    """Attach a PR ArtifactLink to a work item. Returns (ok, error) (best effort).

    Failures are returned but callers treat them as non-fatal since the PR has
    already been created. All links are serialized through a process-wide lock
    so parallel PR creation never issues concurrent PATCHes to the same PBI.
    """
    # PR artifact id is projectId/repoId/prId with the slashes URL-encoded.
    artifact_id = urllib.parse.quote(
        f"{project_id}/{repo_id}/{pr_id}", safe=""
    )
    artifact_url = f"vstfs:///Git/PullRequestId/{artifact_id}"

    api_url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"_apis/wit/workitems/{urllib.parse.quote(str(work_item_id))}"
        f"?api-version=7.1"
    )
    patch = json.dumps([{
        "op": "add",
        "path": "/relations/-",
        "value": {
            "rel": "ArtifactLink",
            "url": artifact_url,
            "attributes": {"name": "Pull Request"},
        },
    }]).encode("utf-8")

    # Try Basic auth (works for PATs) first, then Bearer (for Azure AD access
    # tokens, which the work-item API may only accept that way). The credential
    # reused from Git can be either form depending on how it was issued.
    basic = base64.b64encode(
        f"{username or ''}:{password}".encode("utf-8")
    ).decode("ascii")
    attempts = [f"Basic {basic}", f"Bearer {password}"]

    # Retry the whole link on transient failures (network blips, throttling,
    # server errors) and on concurrency conflicts (409/412) - the work-item API
    # is occasionally flaky right after a PR is created, and two PRs sharing a
    # PBI can collide when their relations are patched close together.
    max_tries = 5
    retry_delay = 2  # seconds, grows linearly per retry

    def _is_transient(code):
        return code == 429 or code in (409, 412) or code >= 500

    last_code, last_detail = None, ""
    # Serialize the actual HTTP retry loop so concurrent callers do not fight
    # over the same work item; PR creation already happened outside the lock.
    with _WORK_ITEM_LINK_LOCK:
        for try_num in range(1, max_tries + 1):
            transient = False
            for authorization in attempts:
                req = urllib.request.Request(api_url, data=patch, method="PATCH")
                req.add_header("Content-Type", "application/json-patch+json")
                req.add_header("Authorization", authorization)
                try:
                    with urllib.request.urlopen(req, timeout=30):
                        return True, ""
                except urllib.error.HTTPError as exc:
                    last_code = exc.code
                    last_detail = exc.read().decode("utf-8", "replace").strip()
                    # Throttling, server errors and concurrency conflicts are
                    # transient - retry the whole attempt after a wait.
                    if _is_transient(exc.code):
                        transient = True
                        break
                    # Only an auth failure is worth retrying with the other scheme.
                    if exc.code not in (401, 403):
                        return _link_failure(work_item_id, last_code, last_detail)
                except (urllib.error.URLError, OSError) as exc:
                    last_code, last_detail = None, str(exc)
                    transient = True
                    break

            if not transient:
                # Exhausted both auth schemes without a transient error - give up.
                break
            if try_num < max_tries:
                time.sleep(retry_delay * try_num)

    return _link_failure(work_item_id, last_code, last_detail)


def _link_failure(work_item_id, last_code, last_detail):
    """Build the (False, message) tuple for a failed work-item PR link."""
    hint = ""
    if last_code in (401, 403):
        hint = (
            " - the credential lacks Work Items (write) permission; set the "
            "ADO_PAT environment variable to a PAT with that scope, or link the "
            "work item manually"
        )
    if not last_detail:
        last_detail = "(empty response)"
    return False, (
        f"work item {work_item_id} link failed ({last_code}): "
        f"{last_detail}{hint}"
    )




def create_feature_branch(name, path, branch_name, decision):
    """Create one repo's feature branch off updated master. Returns (ok, error).

    *decision* (only set for dirty repos) controls how uncommitted changes are
    handled: "delete", "commit" (savepos on the current branch) or "move"
    (carried onto the new branch via a stash).
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository"

    new_branch = f"feature/{branch_name}"

    # Already on the target branch: nothing to do, skip this repo entirely.
    if git_current_branch(path) == new_branch:
        return True, ""

    # Move: stash everything (including untracked), branch off master, re-apply.
    if decision == "move":
        ok, out = run_git(path, ["stash", "push", "-u", "-m", "move to feature branch"])
        if not ok:
            return False, f"{name}: {out}"
        ok, out = run_git(path, ["checkout", "master"])
        if not ok:
            return False, f"{name}: {out}"
        ok, out = run_git(path, ["pull"])
        if not ok:
            return False, f"{name}: {out}"
        ok, out = run_git(path, ["checkout", "-b", new_branch])
        if not ok:
            return False, f"{name}: {out}"
        ok, out = run_git(path, ["stash", "pop"])
        if not ok:
            return False, (
                f"{name}: changes were moved but applying them caused conflicts. "
                f"manual resolution needed.\n{out}"
            )
        return True, ""

    # Delete: discard all staged/unstaged and untracked changes.
    if decision == "delete":
        ok, out = run_git(path, ["reset", "--hard"])
        if not ok:
            return False, f"{name}: {out}"
        ok, out = run_git(path, ["clean", "-fd"])
        if not ok:
            return False, f"{name}: {out}"

    # Commit: keep the changes as savepos commit(s) on the current branch,
    # preserving the staged/unstaged split so they can be restored later.
    elif decision == "commit":
        ok, out = save_uncommitted(path, SAVEPOS_MSG)
        if not ok:
            return False, f"{name}: {out}"

    ok, out = run_git(path, ["checkout", "master"])
    if not ok:
        return False, f"{name}: {out}"
    ok, out = run_git(path, ["pull"])
    if not ok:
        return False, f"{name}: {out}"
    ok, out = run_git(path, ["checkout", "-b", new_branch])
    if not ok:
        return False, f"{name}: {out}"
    return True, ""


def restore_uncommitted(repo_path, base_msg):
    """Undo savepos commit(s) made by save_uncommitted, restoring exact state.

    Only acts when HEAD is one of our savepos commits, so user commits are
    never touched. Returns (ok, error_message).
    """
    head = git_commit_message(repo_path, "HEAD")
    staged_msg = f"{base_msg} - staged"
    unstaged_msg = f"{base_msg} - unstaged"

    if head == unstaged_msg:
        prev = git_commit_message(repo_path, "HEAD~1")
        if prev == staged_msg:
            # Two commits: rebuild the index from the staged commit's tree while
            # keeping the full working tree, so the staged/unstaged split returns.
            ok, staged_tree = run_git(repo_path, ["rev-parse", "HEAD~1^{tree}"])
            if not ok:
                return False, staged_tree
            ok, out = run_git(repo_path, ["reset", "--soft", "HEAD~2"])
            if not ok:
                return False, out
            ok, out = run_git(repo_path, ["read-tree", staged_tree])
            if not ok:
                return False, out
            return True, ""
        # Single commit, everything was unstaged: put it all back unstaged.
        ok, out = run_git(repo_path, ["reset", "--mixed", "HEAD~1"])
        return (ok, "" if ok else out)

    if head == staged_msg:
        # Single commit, everything was staged: put it all back staged.
        ok, out = run_git(repo_path, ["reset", "--soft", "HEAD~1"])
        return (ok, "" if ok else out)

    return False, "no app savepos commit at HEAD to restore"


def rebase_on_master(name, path, save_msg, decision):
    """Rebase one repo's feature branch onto an updated master. Returns (ok, error).

    *decision* (set only for dirty repos) is one of:
      * "commit" - uncommitted work is committed (staged/unstaged split
        preserved via *save_msg*) and left as commit(s) on the branch after the
        rebase; the working tree is NOT restored.
      * "commit_restore" - same as "commit" but the saved working state is
        restored exactly after a clean rebase.
      * "delete" - uncommitted work is discarded before the rebase.
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository"

    if git_rebase_in_progress(path):
        return False, (
            f"{name}: a rebase is already in progress. cannot start a new "
            f"rebase until it is resolved"
        )

    branch = git_current_branch(path)
    if not branch or branch == "master":
        return False, (
            f"{name}: not on a feature branch (currently '{branch or '?'}'); "
            f"nothing to rebase onto master"
        )

    # Handle uncommitted work per the user's decision. The working tree is only
    # restored afterwards for "commit_restore"; "commit" leaves it committed and
    # "delete" discards it outright.
    restore_after = False
    if git_has_changes(path):
        if decision == "delete":
            ok, out = run_git(path, ["reset", "--hard"])
            if not ok:
                return False, f"{name}: {out}"
            ok, out = run_git(path, ["clean", "-fd"])
            if not ok:
                return False, f"{name}: {out}"
        else:
            ok, out = save_uncommitted(path, save_msg)
            if not ok:
                return False, f"{name}: {out}"
            restore_after = decision == "commit_restore"

    ok, out = run_git(path, ["checkout", "master"])
    if not ok:
        return False, f"{name}: {out}"
    ok, out = run_git(path, ["pull"])
    if not ok:
        return False, f"{name}: {out}"
    ok, out = run_git(path, ["checkout", branch])
    if not ok:
        return False, f"{name}: {out}"

    ok, out = run_git(path, ["rebase", "master"])
    if not ok:
        return False, (
            f"{name}: rebase could not complete automatically. manual "
            f"rebase review is needed.\n{out}"
        )

    # Clean rebase: restore the saved working state (staged/unstaged intact).
    if restore_after:
        ok, out = restore_uncommitted(path, save_msg)
        if not ok:
            return False, f"{name}: {out}"
    return True, ""
