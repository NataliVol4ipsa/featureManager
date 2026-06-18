"""Git and filesystem helpers for Feature Manager.

These functions contain no UI code so they can be reused and tested on their
own. ``run_git`` never raises; it returns ``(ok, combined_output)``.
"""

import os
import re
import json
import subprocess

from config import REPOS_ROOT, NUGETS_ROOT, WORKSPACES_ROOT, EXCLUDED_FOLDERS


# Base message for the generic "commit changes as savepos" commits. Using
# save_uncommitted with this message keeps the staged/unstaged split so the
# work can later be restored exactly.
SAVEPOS_MSG = "savepos"


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
    """Folders for the 'Nugets' tab: sub-folders of D:/Repositories/Shared."""
    return list_subfolders(NUGETS_ROOT)


def list_workspaces():
    """Return the sorted names (without extension) of feature workspace files."""
    if not os.path.isdir(WORKSPACES_ROOT):
        return []
    suffix = ".code-workspace"
    return sorted(
        name[: -len(suffix)]
        for name in os.listdir(WORKSPACES_ROOT)
        if name.endswith(suffix)
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
        try:
            info = os.stat(os.path.join(WORKSPACES_ROOT, fname))
        except OSError:
            continue
        items.append((fname[: -len(suffix)], info.st_ctime, info.st_mtime))
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


def git_commit_message(repo_path, ref):
    """Return the subject line of the commit at *ref*, or '' on failure."""
    ok, out = run_git(repo_path, ["log", "-1", "--format=%s", ref])
    return out if ok else ""


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
