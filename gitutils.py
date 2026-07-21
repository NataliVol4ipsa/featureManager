"""Git and filesystem helpers for Feature Manager.

These functions contain no UI code so they can be reused and tested on their
own. ``run_git`` never raises; it returns ``(ok, combined_output)``.
"""

import os
import re
import json
import base64
import shutil
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


def git_commit_message(repo_path, ref):
    """Return the subject line of the commit at *ref*, or '' on failure."""
    ok, out = run_git(repo_path, ["log", "-1", "--format=%s", ref])
    return out if ok else ""


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


def get_git_credential(host):
    """Return (username, password) Git has stored for *host*, or (None, None).

    Uses ``git credential fill`` so the existing credential (e.g. a PAT managed
    by Git Credential Manager) is reused without prompting the user.
    """
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input=f"protocol=https\nhost={host}\n\n",
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


def create_ado_pr(name, path, title, description="", target="master"):
    """Create an Azure DevOps pull request for one repo.

    Returns (ok, url_or_err, warning). The PR goes from the repo's current
    branch to *target* (master). The remote branch must already be pushed.
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

    parsed = parse_ado_remote(git_remote_url(path))
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository", ""
    org, project, repo, host = parsed

    username, password = get_git_credential(host)
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

    parsed = parse_ado_remote(git_remote_url(path))
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, repo, host = parsed

    username, password = get_git_credential(host)
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


def _link_work_item_to_pr(org, work_item_id, project_id, repo_id, pr_id,
                          username, password):
    """Attach a PR ArtifactLink to a work item. Returns (ok, error) (best effort).

    Failures are returned but callers treat them as non-fatal since the PR has
    already been created.
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

    last_code, last_detail = None, ""
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
            # Only an auth failure is worth retrying with the other scheme.
            if exc.code not in (401, 403):
                break
        except (urllib.error.URLError, OSError) as exc:
            return False, f"work item {work_item_id} link failed: {exc}"

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
