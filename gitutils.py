"""Git and filesystem helpers for Feature Manager.

These functions contain no UI code so they can be reused and tested on their
own. ``run_git`` never raises; it returns ``(ok, combined_output)``.
"""

import data_sources
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
import disk_cache


# Cross-session cache of each org's authenticated-user identity id. The identity
# never changes for the signed-in account, so PR auto-complete need not re-fetch
# connectionData every time.
_IDENTITY_CACHE = disk_cache.JsonDiskCache("ado_identity.json")


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


def rename_workspace(old_name, new_name):
    """Rename a workspace file, returning ``(ok, message)``."""
    if old_name == new_name:
        return True, "Workspace name unchanged."
    source = os.path.join(WORKSPACES_ROOT, f"{old_name}.code-workspace")
    target = os.path.join(WORKSPACES_ROOT, f"{new_name}.code-workspace")
    if os.path.exists(target):
        return False, f"a workspace named '{new_name}' already exists"
    try:
        os.rename(source, target)
        return True, f"Workspace renamed to {new_name}."
    except OSError as exc:
        return False, f"could not rename workspace: {exc}"


def set_workspace_folders(workspace_name, repos):
    """Replace a workspace's folder list, preserving every other block.

    Unlike ``write_workspace`` (which rewrites the whole file from scratch),
    this reads the existing .code-workspace, swaps only its ``folders`` array
    and writes it back, so the ``featureManagerSettings`` block and any VS Code
    ``settings`` survive. *repos* is a list of ``(name, path)``; paths are stored
    relative to WORKSPACES_ROOT to match the existing files. Returns (ok, msg).
    """
    ok, data = _read_workspace_json(workspace_name)
    if not ok:
        return False, data
    if not isinstance(data, dict):
        data = {}

    folders = []
    for _, path in repos:
        rel = os.path.relpath(path, WORKSPACES_ROOT).replace("\\", "/")
        folders.append({"path": rel})
    data["folders"] = folders

    target = os.path.join(WORKSPACES_ROOT, f"{workspace_name}.code-workspace")
    try:
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=4)
        return True, "Workspace folders saved."
    except OSError as exc:
        return False, f"could not save workspace folders: {exc}"


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
    ``os.startfile`` launches it directly; folders use the ``code`` launcher
    to open a separate VS Code window. Returns (ok, error_message).
    """
    if not os.path.exists(path):
        return False, f"path does not exist: {path}"

    startfile = getattr(os, "startfile", None)
    if startfile is not None and not os.path.isdir(path):
        try:
            startfile(path)
            return True, ""
        except OSError as exc:
            return False, f"could not open VS Code: {exc}"

    code = shutil.which("code")
    if not code:
        return False, "VS Code 'code' launcher not found on PATH"
    try:
        subprocess.Popen([code, "--new-window", path], creationflags=NO_WINDOW)
        return True, ""
    except OSError as exc:
        return False, f"could not open VS Code: {exc}"


def _find_devenv():
    """Locate ``devenv.exe`` (the Visual Studio IDE) via vswhere, or None.

    vswhere ships with every VS installer under Program Files (x86); asking it
    for the latest install's ``productPath`` yields the devenv.exe to launch.
    """
    program_files_x86 = (
        os.environ.get("ProgramFiles(x86)")
        or os.environ.get("ProgramFiles", "")
    )
    vswhere = os.path.join(
        program_files_x86, "Microsoft Visual Studio", "Installer", "vswhere.exe"
    )
    if not os.path.isfile(vswhere):
        return None
    try:
        result = subprocess.run(
            [vswhere, "-latest", "-prerelease", "-property", "productPath"],
            capture_output=True, text=True, creationflags=NO_WINDOW,
        )
    except OSError:
        return None
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def list_solutions(repos):
    """Return every openable ``.sln`` as ``(repo_name, sln_path)`` pairs.

    The root ``.sln`` for a single-repository layout, or every sub-solution
    ``.sln`` for a multi-repository repo (e.g. ``InvestableUniverseCreation``).
    *repos* is a list of (name, path) pairs. Pairs are returned so callers can
    show which repo each solution belongs to.
    """
    import packages

    entries = []
    for name, path in repos:
        for sln in packages.find_all_solution_files(path):
            entries.append((name, sln))
    return entries


def open_solutions(solutions):
    """Open each ``.sln`` path in *solutions* in a Visual Studio instance.

    Visual Studio opens one solution per window, so each solution is launched
    separately. Launches ``devenv.exe`` per solution when it can be located (so
    each solution gets its own instance); otherwise falls back to the ``.sln``
    file association via ``os.startfile``. Returns (ok, error_message).
    """
    if not solutions:
        return False, "no .sln selected"

    devenv = _find_devenv()
    startfile = getattr(os, "startfile", None)
    if devenv is None and startfile is None:
        return False, "Visual Studio (devenv.exe) not found"
    try:
        for sln in solutions:
            if devenv is not None:
                subprocess.Popen([devenv, sln], creationflags=NO_WINDOW)
            else:
                startfile(sln)
        return True, ""
    except OSError as exc:
        return False, f"could not open Visual Studio: {exc}"


def open_in_visual_studio(repos):
    """Open every ``.sln`` of each repo in a Visual Studio instance.

    Convenience wrapper that lists every solution of *repos* and opens them all.
    *repos* is a list of (name, path) pairs. Returns (ok, error_message).
    """
    entries = list_solutions(repos)
    if not entries:
        return False, "no .sln found in the selected repositories"
    return open_solutions([sln for _, sln in entries])


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


def git_merge_in_progress(repo_path):
    """Return True if an unfinished (e.g. conflicted) merge exists in the repo."""
    return os.path.isfile(os.path.join(repo_path, ".git", "MERGE_HEAD"))


def git_interrupted_operation(repo_path):
    """Return "rebase", "merge" or None depending on any unfinished git operation.

    A rebase or merge left mid-way (usually because of conflicts) blocks every
    other git action, so callers must resolve it before proceeding.
    """
    if git_rebase_in_progress(repo_path):
        return "rebase"
    if git_merge_in_progress(repo_path):
        return "merge"
    return None


def git_change_counts(repo_path):
    """Return the (staged, unstaged) file counts from the working tree.

    Untracked files count as unstaged. A file that is both staged and modified
    counts in each total. Returns (0, 0) if the status cannot be read.
    """
    # Name-only listings are used instead of 'status --porcelain' because
    # run_git strips leading whitespace, which would corrupt the porcelain
    # index/worktree status columns (a leading-space " M" line would look
    # staged). One file per line, so counting lines is safe.
    def _count(args):
        ok, out = run_git(repo_path, args)
        return len(out.splitlines()) if ok and out else 0

    staged = _count(["diff", "--cached", "--name-only"])
    unstaged = _count(["diff", "--name-only"])
    unstaged += _count(["ls-files", "--others", "--exclude-standard"])
    return staged, unstaged


def _count_file_lines(file_path):
    """Return the number of lines in *file_path*, or None if it can't be read."""
    try:
        with open(file_path, "rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return None


def git_file_changes(repo_path):
    """Return {'staged': [...], 'unstaged': [...]} of per-file diff stats.

    Each entry is ``(relative_path, additions, deletions)``; additions and
    deletions are None for binary files. Untracked files are listed under
    'unstaged' with their line count as additions.
    """
    def _numstat(args):
        ok, out = run_git(repo_path, args)
        entries = []
        if ok and out:
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) != 3:
                    continue
                add, dele, path = parts
                additions = None if add == "-" else int(add)
                deletions = None if dele == "-" else int(dele)
                entries.append((path, additions, deletions))
        return entries

    staged = _numstat(["diff", "--cached", "--numstat"])
    unstaged = _numstat(["diff", "--numstat"])
    # Untracked files are not part of 'diff'; list them with their line count.
    ok, out = run_git(repo_path, ["ls-files", "--others", "--exclude-standard"])
    if ok and out:
        for rel in out.splitlines():
            rel = rel.strip()
            if not rel:
                continue
            additions = _count_file_lines(os.path.join(repo_path, rel))
            unstaged.append((rel, additions, 0))
    return {"staged": staged, "unstaged": unstaged}


def abort_interrupted_operation(repo_path):
    """Abort an in-progress rebase or merge, discarding all uncommitted changes.

    Returns (ok, output). Also runs a hard reset + clean so no staged, unstaged
    or untracked leftovers from the aborted operation remain.
    """
    operation = git_interrupted_operation(repo_path)
    if operation == "rebase":
        ok, out = run_git(repo_path, ["rebase", "--abort"])
    elif operation == "merge":
        ok, out = run_git(repo_path, ["merge", "--abort"])
    else:
        return True, ""
    if not ok:
        return False, out
    # Discard anything the abort left behind (staged/unstaged/untracked).
    run_git(repo_path, ["reset", "--hard"])
    run_git(repo_path, ["clean", "-fd"])
    return True, ""


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


def checkout_branch(repo_path, branch):
    """Check out *branch*, falling back to a new origin-tracking branch."""
    if git_branch_exists(repo_path, branch):
        return run_git(repo_path, ["checkout", branch])

    ok, out = run_git(
        repo_path,
        ["fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}"],
    )
    if not ok:
        return False, out
    ok, out = run_git(repo_path, ["checkout", "-b", branch, f"origin/{branch}"])
    if not ok:
        return False, out
    ok, out = run_git(repo_path, ["config", f"branch.{branch}.remote", "origin"])
    if not ok:
        return False, out
    return run_git(
        repo_path, ["config", f"branch.{branch}.merge", f"refs/heads/{branch}"]
    )


def remote_branch_head(repo_path, branch):
    """Return the commit sha at the tip of origin/*branch*, or '' on failure.

    Hits the network (ls-remote), so call it off the UI thread.
    """
    ok, out = run_git(repo_path, ["ls-remote", "--heads", "origin", branch])
    if not ok or not out.strip():
        return ""
    return out.split()[0].strip()


def git_commit_is_ancestor(repo_path, commit_id, ref):
    """Return True if *commit_id* is *ref* itself or one of its ancestors.

    Exact, using local git (not a guessed/bounded ADO REST query): refreshes
    *ref* from origin first (via FETCH_HEAD - plain ``git fetch origin <ref>``
    does NOT update the ``origin/<ref>`` tracking branch, same gotcha handled
    in git_branch_is_empty), pinned immediately to a concrete sha, falling back
    to an existing tracking/local ref when offline. If *commit_id* isn't already
    known locally it is fetched directly by sha before testing, so this works
    even for a commit older than the last routine fetch. Hits the network, so
    call it off the UI thread. Returns False on any git failure (network error,
    or the commit still unknown after the fetch attempt).
    """
    ok, _ = run_git(repo_path, ["fetch", "--quiet", "origin", ref])
    target = None
    if ok:
        # Pin FETCH_HEAD to a concrete sha NOW: fetching the commit below (when
        # it isn't local yet) repoints FETCH_HEAD to that commit, which would
        # otherwise turn the ancestry test into "<commit> vs <commit>" (always
        # true - the source of the "everything looks like master" bug).
        rok, out = run_git(repo_path, ["rev-parse", "FETCH_HEAD"])
        if rok and out.strip():
            target = out.strip().splitlines()[0].split()[0]
    if target is None:
        for candidate in (f"origin/{ref}", ref):
            rok, out = run_git(repo_path, ["rev-parse", "--verify", candidate])
            if rok and out.strip():
                target = out.strip().splitlines()[0]
                break
    if target is None:
        return False

    known, _ = run_git(repo_path, ["cat-file", "-e", commit_id + "^{commit}"])
    if not known:
        run_git(repo_path, ["fetch", "--quiet", "origin", commit_id])
        known, _ = run_git(repo_path, ["cat-file", "-e", commit_id + "^{commit}"])
        if not known:
            return False

    ok, _ = run_git(repo_path, ["merge-base", "--is-ancestor", commit_id, target])
    return ok


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


def git_branch_is_empty(name, path, target="master"):
    """Return True if *path*'s current branch has no changes versus *target*.

    Signature matches the ``skip_fn(name, path)`` convention so it can be passed
    straight to ``run_repo_action`` as a skip predicate (*name* is unused).
    "Empty" means a pull request from this branch to *target* would show zero
    file changes (no diff against their merge-base, matching what a PR displays).
    Repos that are not git repos, are on a detached HEAD, are already on
    *target*, or whose target ref cannot be resolved are treated as not empty
    (False) so they are never silently skipped.
    """
    if not is_git_repo(path):
        return False
    branch = git_current_branch(path)
    if not branch or branch == target:
        return False
    # Refresh *target* from origin first so the comparison mirrors the true
    # remote state the PR will target, not a stale local tracking ref. Without
    # this, a branch whose changes are already in the remote target still looks
    # ahead of an out-of-date origin/<target> and is never skipped. Fall back to
    # the existing tracking/local ref when offline or the fetch fails.
    ref = None
    ok, _ = run_git(path, ["fetch", "--quiet", "origin", target])
    if ok:
        ref = "FETCH_HEAD"
    else:
        for candidate in (f"origin/{target}", target):
            ok, _ = run_git(
                path, ["rev-parse", "--verify", "--quiet", candidate]
            )
            if ok:
                ref = candidate
                break
    if ref is None:
        return False
    # Three-dot diff compares against the merge-base, exactly like a PR. An exit
    # code of 0 (ok) means there is no diff, i.e. the branch is empty.
    ok, _ = run_git(path, ["diff", "--quiet", f"{ref}...HEAD"])
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

    Repos that are not git repos are reported as errors; repos with nothing to
    commit return the "warning" sentinel so they are shown as a warning rather
    than an error.
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository"
    if not git_has_changes(path):
        return "warning", f"{name}: no changes to commit"

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


def git_ahead_behind(path, branch, remote="origin"):
    """Return (ahead, behind) commit counts for *branch* vs *remote*/*branch*.

    Fetches the remote branch first so the counts are accurate, then compares
    HEAD to the remote tip: *ahead* = local commits not on the remote, *behind*
    = remote commits not in the local branch. Hits the network, so call it off
    the UI thread. Returns (0, 0) when the counts cannot be determined.
    """
    if not branch:
        return (0, 0)
    run_git(path, ["fetch", remote, branch])
    ok, out = run_git(
        path, ["rev-list", "--left-right", "--count", f"HEAD...{remote}/{branch}"]
    )
    if not ok:
        return (0, 0)
    parts = out.split()
    if len(parts) != 2:
        return (0, 0)
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return (0, 0)


def git_force_push(name, path):
    """Force-push one repo's current branch to origin. Returns (ok, error).

    Uses ``push --force-with-lease`` so the overwrite is refused if the remote
    moved again since the last fetch (a safer force push). Call after the user
    has confirmed overwriting the diverged remote branch.
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository"
    branch = git_current_branch(path)
    if not branch:
        return False, f"{name}: not on a branch (detached HEAD)"
    ok, out = run_git(path, ["push", "--force-with-lease", "-u", "origin", "HEAD"])
    if not ok:
        return False, f"{name}: {out}"
    return True, ""


def delete_remote_branch(name, path, branch):
    """Delete *branch* from origin for one repo. Returns (ok, error).

    Uses ``push origin --delete`` so no local checkout is needed. Hits the
    network, so call it off the UI thread.
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository"
    ok, out = run_git(path, ["push", "origin", "--delete", branch])
    if not ok:
        return False, f"{name}: {out}"
    return True, ""


def git_remote_url(repo_path, remote="origin"):
    """Return the configured URL for *remote* (e.g. origin), or "" if none."""
    ok, out = run_git(repo_path, ["remote", "get-url", remote])
    if not ok:
        return ""
    return out.strip()


def find_git_repos(root):
    """Return sorted absolute paths of every git repository under *root*.

    A folder is a repository when it contains a ``.git`` entry; the walk does
    not descend into one (nested repos are uncommon and a parent's checkout
    would recreate them anyway). Returns an empty list for a missing *root*.
    """
    repos = []
    if not os.path.isdir(root):
        return repos
    for dirpath, dirnames, _files in os.walk(root):
        if os.path.exists(os.path.join(dirpath, ".git")):
            repos.append(dirpath)
            dirnames[:] = []  # do not descend into a repository
    return sorted(repos)


def generate_clone_script(root=None):
    """Build a bash script that clones every repo under *root* back into place.

    Walks *root* (defaults to REPOS_ROOT), reads each repository's origin URL,
    and emits a ``git clone <url> "<relative-path>"`` line so the exact folder
    layout can be recreated on another machine by running the script from
    *root*. Hits ``git`` once per repo, so call it off the UI thread. Returns
    the script text.
    """
    root = os.path.abspath(root or REPOS_ROOT)
    header = [
        "#!/usr/bin/env bash",
        "# Generated by Feature Manager.",
        "# Clones every repository back into the exact folder layout under:",
        f"#   {root}",
        "# Run this script from that folder.",
        "set -e",
        "",
    ]
    lines = []
    for repo_path in find_git_repos(root):
        rel = os.path.relpath(repo_path, root).replace(os.sep, "/")
        url = git_remote_url(repo_path)
        if not url:
            lines.append(f"# {rel}: no origin remote - skipped")
            continue
        lines.append(f'git clone {url} "{rel}"')
    if not lines:
        lines.append(f"# No git repositories found under {root}")
    return "\n".join(header + lines) + "\n"


def cleanup_bin_obj(repo_path):
    """Delete every ``bin`` and ``obj`` build folder found under *repo_path*.

    Walks the repository, removing each ``bin``/``obj`` directory (and all its
    content) while never descending into ``.git`` or ``node_modules``. Hits the
    filesystem heavily, so call it off the UI thread. Returns
    ``(removed_count, errors)`` where *errors* is a list of ``(folder, reason)``.
    """
    removed = 0
    errors = []
    if not os.path.isdir(repo_path):
        return removed, errors
    for dirpath, dirnames, _files in os.walk(repo_path):
        base = os.path.basename(dirpath)
        if base in ("bin", "obj"):
            dirnames[:] = []  # do not descend into a folder we are deleting
            try:
                shutil.rmtree(dirpath)
                removed += 1
            except OSError as exc:
                errors.append((dirpath, str(exc)))
        elif base in (".git", "node_modules"):
            dirnames[:] = []  # skip version-control and package folders
    return removed, errors


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


def create_ado_pr(name, path, title, description='', target='master', draft=False, branch=None):
    """Create an Azure DevOps pull request for one repo.

    Returns (ok, url_or_err, warning). The PR goes from *branch* (the repo's
    current branch when not given) to *target* (master). When *draft* is true
    the PR is created as a draft. The remote branch must already be pushed.
    Authentication reuses the Git credential already stored for the host, so no
    extra credentials are requested. When the branch name embeds a work-item id
    (e.g. ``feature/514231_...``) that work item is linked to the new PR; a link
    failure does not fail the PR but is returned as *warning*.
    """
    return data_sources.strategy("repositories").create_pr(name, path, title, description, target, draft, branch)


def get_ado_pr_url(name, path, target='master', branch=None):
    """Return (ok, url_or_err) for an existing open PR of the repo's branch.

    Looks up the active Azure DevOps pull request that goes from *branch* (the
    repo's current branch when not given) to *target* (master) and returns a
    browser link to it. Authentication reuses the Git credential already stored
    for the host (no prompts). ok is False - with an explanatory message - when
    the repo is not an ADO repo, has no stored credential, or has no open pull
    request for its branch.
    """
    return data_sources.strategy("repositories").get_pr_url(name, path, target, branch)


# Azure DevOps reviewer vote -> short human label (see fetch_pr_status).
PR_VOTE_LABELS = {
    10: "approved",
    5: "approved with suggestions",
    0: "no vote",
    -5: "waiting for author",
    -10: "rejected",
}


def fetch_pr_status(name, path, target='master', branch=None):
    """Fetch the latest pull request (any status) for *branch* -> *target*.

    One REST call. Returns (ok, info_or_err). *info* is a dict with the summary
    fields the PR-status table needs and a reusable ``ctx`` (credential + ids)
    so the threads/policy follow-ups make no extra credential lookups:

      pr_id, web_url, title, is_draft, status ('active'|'abandoned'|'completed'),
      merge_status, has_conflicts, reviewers (list of {name, vote, label,
      required, voted}), vote_summary, project_id, repo_id, ctx.

    ``info`` is None (ok True) when the repo simply has no PR for the branch.
    """
    return data_sources.strategy("repositories").fetch_pr_status(name, path, target, branch)


def set_pr_draft(ctx, draft):
    """Set the draft flag on the exact PR identified by a fetched context."""
    return data_sources.strategy("repositories").set_pr_draft(ctx, draft)


def fetch_pr_threads(ctx):
    """Fetch the PR's comment threads using the reusable *ctx* from fetch_pr_status.

    One REST call. Returns (ok, threads_or_err) where each simplified thread is
    ``{id, status, resolved, is_system, file_path, line_range, comments:[...]}``
    and each comment is ``{id, parent_id, author, date, content, is_system}``.
    The same list backs both the summary counts and the row details, so the
    details view makes no further call.
    """
    return data_sources.strategy("repositories").fetch_pr_threads(ctx)


def fetch_pr_policy(ctx):
    """Fetch a PR's branch-policy evaluations using the reusable *ctx*.

    One REST call per PR during the initial status scan. Returns
    (ok, dict_or_err) with: ``build_state`` ('succeeded'|'failed'|'expired'|
    'running'|'none'), ``build_id``, ``build_url``, ``rejected``, ``pending``,
    ``expired_ids`` (evaluation ids of expired builds, for requeueing) and
    ``policies`` (a [{name, status}] list for display). Build-error *text* is a
    separate lazy call (pipelines.get_build_errors) only when build_state failed.
    """
    return data_sources.strategy("repositories").fetch_pr_policy(ctx)


def requeue_pr_policy(ctx, evaluation_id):
    """Requeue one policy evaluation (e.g. re-run an expired build) of a PR."""
    return data_sources.strategy("repositories").requeue_pr_policy(ctx, evaluation_id)


# Merge strategies accepted by the Azure DevOps PR completion API.
PR_MERGE_STRATEGIES = ("noFastForward", "squash", "rebase", "rebaseMerge")


def complete_ado_pr(name, path, target='master', merge_strategy='noFastForward', delete_source_branch=True, transition_work_items=True, publish_draft=True, auto_complete_when_not_ready=True, queue_build=True, branch=None):
    """Complete (merge) the open Azure DevOps PR for the repo's *branch*.

    Returns (ok, url_or_err, warning). Looks up the active pull request that
    goes from *branch* (the repo's current branch when not given) to *target*
    (master) and tries to merge it with the given options. Authentication
    reuses the Git credential stored for the host (no prompts).

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
    return data_sources.strategy("repositories").complete_pr(name, path, target, merge_strategy, delete_source_branch, transition_work_items, publish_draft, auto_complete_when_not_ready, queue_build, branch)


# Serialize every work-item link across threads. When PRs are created in
# parallel and several share the same PBI, concurrent PATCHes to that one work
# item collide (ADO optimistic concurrency -> 409/412), so the links must run
# one at a time even though PR creation itself stays parallel.
_WORK_ITEM_LINK_LOCK = threading.Lock()


def _link_work_item_to_pr(org, work_item_id, project_id, repo_id, pr_id, username, password):
    """Attach a PR ArtifactLink to a work item. Returns (ok, error) (best effort).

    Failures are returned but callers treat them as non-fatal since the PR has
    already been created. All links are serialized through a process-wide lock
    so parallel PR creation never issues concurrent PATCHes to the same PBI.
    """
    return data_sources.strategy("board").link_work_item_to_pr(org, work_item_id, project_id, repo_id, pr_id, username, password)


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


def rebase_on_master(name, path, save_msg, decision, pull_only=False):
    """Rebase one repo's feature branch onto an updated master. Returns (ok, error).

    *decision* (set only for dirty repos) is one of:
      * "commit" - uncommitted work is committed (staged/unstaged split
        preserved via *save_msg*) and left as commit(s) on the branch after the
        rebase; the working tree is NOT restored.
      * "commit_restore" - same as "commit" but the saved working state is
        restored exactly after a clean rebase.
      * "delete" - uncommitted work is discarded before the rebase.

    When *pull_only* is true, the repo must already be on master; its uncommitted
    work is handled as usual, then master is pulled without starting a rebase.
    """
    if not is_git_repo(path):
        return False, f"{name}: not a git repository"

    if git_rebase_in_progress(path):
        return False, (
            f"{name}: a rebase is already in progress. cannot start a new "
            f"rebase until it is resolved"
        )

    branch = git_current_branch(path)
    if pull_only and branch != "master":
        return False, (
            f"{name}: configured for master but currently on "
            f"'{branch or '?'}'; switch to master before pulling"
        )
    if not pull_only and (not branch or branch == "master"):
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
            ok, out = run_git(path, ["clean", "-ffd"])
            if not ok:
                return False, f"{name}: {out}"
            if git_has_changes(path):
                _, remaining = run_git(path, ["status", "--short"])
                return False, (
                    f"{name}: could not delete all uncommitted changes; "
                    f"remaining changes:\n{remaining}"
                )
        else:
            ok, out = save_uncommitted(path, save_msg)
            if not ok:
                return False, f"{name}: {out}"
            restore_after = decision == "commit_restore"

    if pull_only:
        ok, out = run_git(path, ["pull"])
        if not ok:
            return False, f"{name}: {out}"
        if restore_after:
            ok, out = restore_uncommitted(path, save_msg)
            if not ok:
                return False, f"{name}: {out}"
        return True, ""

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


# Compatibility exports for shared helpers and existing offline tests.
from data_sources.ado.repositories import (
    _pr_rest_context,
    _summarise_votes,
    _thread_line_range,
    _ado_get_json,
    _ado_send_json,
    _ado_current_user_id,
    _pr_policy_state,
    _requeue_policy_evaluation,
    _await_pr_merge,
)
from data_sources.ado.board import (
    _link_failure,
)
