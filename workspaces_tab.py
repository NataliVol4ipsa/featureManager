"""The 'Workspaces' tab: switch all repos of a feature workspace at once."""

import os
import subprocess
import threading
import webbrowser
from datetime import datetime
from tkinter import ttk

from config import WORKSPACES_ROOT, REPOS_ROOT, NUGETS_ROOT
from gitutils import (
    list_workspaces_detailed, read_workspace_repos, write_workspace,
    run_git, is_git_repo, git_branch_exists,
    save_uncommitted, has_savepos, restore_uncommitted,
    git_current_branch, create_feature_branch, rebase_on_master,
    git_branch_url, get_ado_pr_url, open_in_vscode,
    get_nuget_folders,
    workspace_branch_entries, save_branch_overrides, IGNORE_GIT_KEY,
    SAVEPOS_MSG,
)
from widgets import WorkspaceList, Tooltip
from tab_base import ActionTabBase
from dialogs import (
    ask_branch_name, ask_pbi_number, resolve_pbi_repos, edit_branch_overrides,
    ask_include_skipped, ask_workspace_branches,
)
import pbi

# Base message for the savepos commits created when switching workspaces.
SWITCH_SAVE_MSG = "savepos before workspace switch"

# Base message for the rebase savepos commits (see gitutils.save_uncommitted).
REBASE_SAVE_MSG = "save changes before rebase"

# All savepos base messages "Restore state before switch" can put back: the one
# the switch action makes, plus the generic one used when creating feature
# branches / committing savepos (create_feature_branch, "Commit changes (savepos)").
RESTORABLE_SAVE_MSGS = (SWITCH_SAVE_MSG, SAVEPOS_MSG)


def _fmt_time(timestamp):
    """Format an epoch-second timestamp as a short 'YYYY-MM-DD HH:MM' string."""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


class WorkspacesTab(ActionTabBase):
    """Left: single-select workspace list. Middle: actions. Right: progress."""

    def __init__(self, master):
        super().__init__(master)
        self._build_left()
        self.build_middle_sections(self._sections())
        # Let the Details panel absorb the extra width when the window grows, so
        # long branch/PR content on the right has room (the workspace list on
        # the left keeps a fixed width).
        self.build_right_details(expand=True)

    # -- Layout ------------------------------------------------------------ #
    def _build_left(self):
        # Fixed width so widening the window grows the Details panel, not the
        # workspace name column. Sized to fit the Name/Created/Modified columns.
        left = ttk.LabelFrame(self._top, text="Workspaces", width=500)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        self.workspace_list = WorkspaceList(
            left, self._workspace_items(), on_select=self._on_workspace_selected
        )
        self.workspace_list.pack(fill="both", expand=True, padx=4, pady=4)

        ttk.Button(left, text="Refresh", command=self._refresh).pack(
            fill="x", padx=4, pady=(0, 4)
        )
        open_btn = ttk.Button(
            left, text="Open workspaces folder", command=self._open_workspaces_folder
        )
        open_btn.pack(fill="x", padx=4, pady=(0, 4))
        Tooltip(
            open_btn,
            "Opens the workspaces folder "
            f"({WORKSPACES_ROOT}) in Windows File Explorer.",
        )

    def _open_workspaces_folder(self):
        """Open the feature workspaces folder in Windows File Explorer."""
        self.errors.clear()
        if not os.path.isdir(WORKSPACES_ROOT):
            self.errors.add(f"workspaces folder does not exist: {WORKSPACES_ROOT}")
            return
        # 'explorer' expects a backslashed path; normpath gives the native form.
        subprocess.Popen(["explorer", os.path.normpath(WORKSPACES_ROOT)])

    def _on_workspace_selected(self, workspace):
        """Show the workspace's repositories (with branch) in the Details table."""
        self.errors.clear()
        ok, repos = read_workspace_repos(workspace)
        if not ok:
            self.progress.show_repos([])
            self.errors.add(repos)
            return
        self.show_repos_async(repos, with_status=False)

    def _refresh(self):
        """Re-scan the workspaces folder (e.g. after creating a new workspace)."""
        self.workspace_list.set_items(self._workspace_items())

    # Public alias used by the app when the Workspaces tab is opened.
    refresh = _refresh

    def _workspace_items(self):
        """Build (name, created, modified) rows, freshest (most recent) first."""
        items = []
        for name, created, modified in list_workspaces_detailed():
            items.append((name, _fmt_time(created), _fmt_time(modified)))
        return items

    def _sections(self):
        """Two stacked action groups: state-changing actions, then open actions."""
        return [
            ("Manipulate", self._manipulate_actions()),
            ("Open", self._open_actions()),
        ]

    def _manipulate_actions(self):
        return [
            (
                "Create workspace from PBI",
                self._action_create_from_pbi,
                "Downloads an Azure DevOps PBI (work item) by number, reads the "
                "repositories from its WBS section, and creates a feature "
                "workspace for them. You map any unrecognised service to a local "
                "folder (new mappings are remembered) and name the workspace "
                "(pre-filled from the PBI number and title). A matching "
                "'feature/<name>' branch is then created off updated master in "
                "each repository. Requires secrets.json to be configured.",
            ),
            (
                "Switch to selected workspace",
                self._action_switch,
                "Checks out every repository in the selected workspace to its "
                "feature branch (by default 'feature/<workspace>', or a per-repo "
                "name set via 'Manage workspace branches'). Repos flagged as "
                "skipped are still listed but left on their own branch. If a "
                "repo's branch is missing it is marked skipped. Uncommitted "
                "changes are handled per repository (commit, delete, or abort).",
            ),
            (
                "Manage workspace branches",
                self._action_manage_branches,
                "Configures the feature branch used per repository in the "
                "selected workspace. Lists every folder with its branch (default "
                "'feature/<workspace>') and a Skip flag. Set a different branch "
                "name for any repo whose branch differs from the workspace, or "
                "tick Skip to exclude a repo from the branch-modifying actions "
                "(commit, push, rebase, create PR) - it keeps its own branch. "
                "Settings are stored in the .code-workspace file; folders you "
                "add later in VS Code can be configured here too.",
            ),
            (
                "Restore state before switch",
                self._action_restore,
                "For the selected workspace's repositories, undoes the savepos "
                "commit the app made (when switching workspaces or creating a "
                "feature branch) and restores the exact staged/unstaged working "
                "state. User commits are never touched. Skipped repos are offered "
                "as opt-in checkboxes (off by default).",
            ),
            (
                "Rebase current branch on master",
                self._action_rebase_on_master,
                "For the selected workspace's repositories (excluding skipped "
                "repos): updates master (checkout + pull), returns to the "
                "feature branch and rebases it onto master. Uncommitted changes "
                "are committed first (with confirmation) and restored afterwards "
                "(staged/unstaged preserved) on a clean rebase.",
            ),
            (
                "Commit all changes",
                self._action_commit_all,
                "For the selected workspace's repositories (excluding skipped "
                "repos): stages and commits all uncommitted changes using a "
                "commit message you enter. If the repos with changes are on "
                "different branches, a warning is shown before committing.",
            ),
            (
                "Git push",
                self._action_push,
                "For the selected workspace's repositories (excluding skipped "
                "repos): pushes the current branch to origin, creating the "
                "remote branch automatically if it does not exist yet. No "
                "prompts are shown unless the repos are on different branches, "
                "in which case a warning is shown first.",
            ),
            (
                "Create pull request",
                self._action_create_pr,
                "For the selected workspace's repositories (excluding skipped "
                "repos): creates an Azure DevOps pull request to master from the "
                "current branch (it must be pushed first). You choose one custom "
                "title or let each title be auto-generated from its branch name "
                "(feature/123_my_desc \u2192 feature(123) My desc). A link to "
                "each new PR is shown.",
            ),
        ]

    def _open_actions(self):
        return [
            (
                "Open workspace in VS Code",
                self._action_open_workspace,
                "Opens the selected feature workspace (its .code-workspace file) "
                "in VS Code, so every repository of the workspace loads in one "
                "window.",
            ),
            (
                "Open in Git Bash tabs",
                self._action_open_terminals,
                "For the selected workspace's repositories: opens a Git Bash "
                "session in a Windows Terminal tab (one tab per repo, titled "
                "with the repo name, started in that repo's folder). If Windows "
                "Terminal is not available, a separate Git Bash window is opened "
                "per repo.",
            ),
            (
                "Open repositories (master)",
                self._action_open_repos_master,
                "For the selected workspace's repositories: opens each "
                "repository's master branch on the remote host (Azure DevOps / "
                "GitHub / \u2026) in your default web browser, one tab per repo.",
            ),
            (
                "Open branches",
                self._action_open_branches,
                "For the selected workspace's repositories: opens each "
                "repository's current branch on the remote host in your default "
                "web browser, one tab per repo. Branches that have not been "
                "pushed yet may show as not found on the host.",
            ),
            (
                "Open pull requests",
                self._action_open_prs,
                "For the selected workspace's repositories: looks up the open "
                "Azure DevOps pull request for each repo's current branch and "
                "opens it in your default web browser. Repos with no open PR are "
                "reported in the Errors panel.",
            ),
        ]

    # -- Helpers ----------------------------------------------------------- #
    def _selected_repos(self):
        """Return (ok, workspace_name, repos_or_error_message)."""
        workspace = self.workspace_list.get_selected()
        if not workspace:
            return False, None, "no workspace selected"
        ok, repos = read_workspace_repos(workspace)
        if not ok:
            return False, workspace, repos
        return True, workspace, repos

    def _selected_entries(self):
        """Return (ok, workspace_name, entries_or_error).

        *entries* is the per-repo branch map from ``workspace_branch_entries``
        (each a dict with name, path, branch and ignoreGit) - the single source
        of truth for which feature branch each repo of the workspace belongs to.
        """
        workspace = self.workspace_list.get_selected()
        if not workspace:
            return False, None, "no workspace selected"
        ok, entries = workspace_branch_entries(workspace)
        if not ok:
            return False, workspace, entries
        return True, workspace, entries

    def _selected_active_repos(self):
        """Return (ok, workspace_name, repos) with skipped repos excluded.

        Used by the branch-modifying batch actions (commit, push, rebase, create
        PR): a repo flagged "ignore git" keeps its own branch and must be left
        out of operations that would act on the workspace's feature branch.
        """
        ok, workspace, entries = self._selected_entries()
        if not ok:
            return False, workspace, entries
        repos = [(e["name"], e["path"]) for e in entries if not e["ignoreGit"]]
        return True, workspace, repos

    # -- Manage workspace branches ----------------------------------------- #
    def _action_manage_branches(self):
        """Open the per-repo feature-branch editor for the selected workspace."""
        self.errors.clear()
        ok, workspace, entries = self._selected_entries()
        if not ok:
            self.errors.add(entries)
            return
        if not entries:
            self.errors.add("this workspace has no folders to configure")
            return

        overrides = edit_branch_overrides(self, workspace, entries)
        if overrides is None:
            return

        ok_save, message = save_branch_overrides(workspace, overrides)
        if not ok_save:
            self.errors.add(message)
            return
        # Reflect any change in the Details table.
        self._on_workspace_selected(workspace)

    # -- Switch to selected workspace -------------------------------------- #
    def _action_switch(self):
        ok, workspace, entries = self._selected_entries()
        self.errors.clear()
        if not ok:
            if workspace is not None:
                self.errors.add(entries)
            return
        if not entries:
            return

        # Every folder is shown when switching (ignored repos are NOT hidden).
        # A repo flagged "ignore git" keeps its own branch, so it is displayed
        # but not checked out; the rest switch to their effective feature branch
        # (default 'feature/<workspace>', or a per-repo override).
        all_repos = [(e["name"], e["path"]) for e in entries]
        branch_of = {e["name"]: e["branch"] for e in entries}
        ignore_flag = {e["name"]: e["ignoreGit"] for e in entries}
        self.show_repos_async(all_repos, with_status=True)

        # Split into repos we can switch and ones we skip. Workspaces often
        # bundle a docs folder (not a git repo) or repos where the feature
        # branch was only created in some services. Rather than abort the whole
        # batch, we switch what we can and mark the rest "skipped".
        skip_reasons = {}
        switchable = []
        for name, path in all_repos:
            target = branch_of[name]
            if ignore_flag[name]:
                skip_reasons[name] = "ignore git (keeps its own branch)"
            elif not is_git_repo(path):
                skip_reasons[name] = "not a git repository"
            elif not git_branch_exists(path, target):
                skip_reasons[name] = f"branch '{target}' does not exist"
            else:
                switchable.append((name, path))

        # Mark skipped rows upfront so the user sees them before the modal.
        for name, reason in skip_reasons.items():
            self.progress.status(name, "skipped", tooltip=f"Skipped: {reason}")

        if not switchable:
            self.errors.add(
                "No repository in this workspace has its feature branch."
            )
            return

        # Pre-check: decide what to do with uncommitted changes in each repo
        # we're actually going to switch. Repos already on their target branch
        # are skipped entirely (no prompt, no checkout).
        decisions = self.collect_change_decisions(
            switchable, skip_branch=lambda n: branch_of.get(n)
        )
        if decisions is None:
            self.progress.show_repos([])
            return

        self.run_repo_action(
            all_repos,
            lambda n, p: self._switch_one(n, p, branch_of[n], decisions.get(n)),
            f"Switched to workspace '{workspace}'.",
            skip_fn=lambda n, _p: (
                f"Skipped: {skip_reasons[n]}" if n in skip_reasons else False
            ),
        )

    def _switch_one(self, name, path, target, decision):
        """Apply the change decision then check out the target branch."""
        # Already on the target branch: nothing to do, leave the repo untouched.
        if git_current_branch(path) == target:
            return True, ""

        if decision == "delete":
            ok, out = run_git(path, ["reset", "--hard"])
            if not ok:
                return False, f"{name}: {out}"
            ok, out = run_git(path, ["clean", "-fd"])
            if not ok:
                return False, f"{name}: {out}"
        elif decision == "commit":
            ok, out = save_uncommitted(path, SWITCH_SAVE_MSG)
            if not ok:
                return False, f"{name}: {out}"

        ok, out = run_git(path, ["checkout", target])
        if not ok:
            return False, f"{name}: {out}"
        return True, ""

    # -- Restore state before switch --------------------------------------- #
    def _action_restore(self):
        ok, workspace, entries = self._selected_entries()
        self.errors.clear()
        if not ok:
            if workspace is not None:
                self.errors.add(entries)
            return
        if not entries:
            return

        # Non-ignored repos are always restored. Repos flagged "ignore git" keep
        # their own branch, so they are offered as opt-in checkboxes (default
        # off): the user can include a specific ignored repo when its savepos
        # should be restored too.
        repos = [(e["name"], e["path"]) for e in entries if not e["ignoreGit"]]
        ignored = [(e["name"], e["path"]) for e in entries if e["ignoreGit"]]
        if ignored:
            included = ask_include_skipped(
                self, "Restore state before switch", [n for n, _ in ignored]
            )
            if included is None:
                return
            repos += [(n, p) for n, p in ignored if n in included]

        if not repos:
            return

        self.run_repo_action(repos, self._restore_one, "State restored.")

    def _restore_one(self, name, path):
        """Restore the pre-switch working state if the app made a savepos commit."""
        if not is_git_repo(path):
            return False, f"{name}: not a git repository"

        # Restore whichever app-made savepos commit is at HEAD: the switch one,
        # or the generic "savepos" left by creating a feature branch / committing
        # savepos. Skip the repo if HEAD is not one of ours.
        for base_msg in RESTORABLE_SAVE_MSGS:
            if has_savepos(path, base_msg):
                ok, out = restore_uncommitted(path, base_msg)
                if not ok:
                    return False, f"{name}: {out}"
                return True, ""
        return True, ""

    # -- Rebase current branch on master ----------------------------------- #
    def _action_rebase_on_master(self):
        ok, workspace, repos = self._selected_active_repos()
        self.errors.clear()
        if not ok:
            if workspace is not None:
                self.errors.add(repos)
            return
        if not repos:
            return

        self.show_repos_async(repos, with_status=True)

        # Ask per dirty repo what to do with its changes. A rebase needs the
        # changes committed first, so "commit" (leave committed) and
        # "commit & restore" (restore the working state afterwards) are offered.
        decisions = self.collect_change_decisions(
            repos, restore_option=True,
            note="A rebase requires committing the changes first. 'Commit "
                 "changes' leaves them committed on the branch; 'Commit & "
                 "restore' puts the same changes back as uncommitted work after "
                 "the rebase.",
        )
        if decisions is None:
            self.progress.set_repos([])
            return

        self.run_repo_action(
            repos,
            lambda n, p: rebase_on_master(n, p, REBASE_SAVE_MSG, decisions.get(n)),
            "All repositories rebased successfully.",
        )

    # -- Commit all changes (custom message) ------------------------------- #
    def _action_commit_all(self):
        ok, workspace, repos = self._selected_active_repos()
        self.errors.clear()
        if not ok:
            if workspace is not None:
                self.errors.add(repos)
            return
        self.commit_all_changes(repos)

    # -- Git push ---------------------------------------------------------- #
    def _action_push(self):
        ok, workspace, repos = self._selected_active_repos()
        self.errors.clear()
        if not ok:
            if workspace is not None:
                self.errors.add(repos)
            return
        self.push_all(repos)

    # -- Create pull request ----------------------------------------------- #
    def _action_create_pr(self):
        ok, workspace, repos = self._selected_active_repos()
        self.errors.clear()
        if not ok:
            if workspace is not None:
                self.errors.add(repos)
            return
        self.create_prs(repos)

    # -- Open in Git Bash tabs --------------------------------------------- #
    def _action_open_terminals(self):
        ok, workspace, repos = self._selected_repos()
        self.errors.clear()
        if not ok:
            if workspace is not None:
                self.errors.add(repos)
            return
        self.open_terminals(repos)

    # -- Open workspace in VS Code ----------------------------------------- #
    def _action_open_workspace(self):
        """Open the selected workspace's .code-workspace file in VS Code."""
        self.errors.clear()
        workspace = self.workspace_list.get_selected()
        if not workspace:
            self.errors.add("no workspace selected")
            return
        path = os.path.join(WORKSPACES_ROOT, f"{workspace}.code-workspace")
        ok, message = open_in_vscode(path)
        if not ok:
            self.errors.add(message)

    # -- Open repositories (master) in the browser ------------------------- #
    def _action_open_repos_master(self):
        ok, workspace, repos = self._selected_repos()
        self.errors.clear()
        if not ok:
            if workspace is not None:
                self.errors.add(repos)
            return
        if not repos:
            return
        self._open_browser_async(
            repos, self._master_url, "master link", "Resolving master links\u2026"
        )

    # -- Open current branches in the browser ------------------------------ #
    def _action_open_branches(self):
        ok, workspace, repos = self._selected_repos()
        self.errors.clear()
        if not ok:
            if workspace is not None:
                self.errors.add(repos)
            return
        if not repos:
            return
        self._open_browser_async(
            repos, self._branch_url, "branch link", "Resolving branch links\u2026"
        )

    # -- Open pull requests in the browser --------------------------------- #
    def _action_open_prs(self):
        ok, workspace, repos = self._selected_repos()
        self.errors.clear()
        if not ok:
            if workspace is not None:
                self.errors.add(repos)
            return
        if not repos:
            return
        self._open_browser_async(
            repos, self._pr_url, "pull request", "Looking up pull requests\u2026"
        )

    # -- Browser-open helpers ---------------------------------------------- #
    @staticmethod
    def _master_url(name, path):
        """Return (url, error) for *path*'s master branch on the remote host."""
        if not is_git_repo(path):
            return "", f"{name}: not a git repository"
        url = git_branch_url(path, "master")
        if not url:
            return "", f"{name}: could not resolve the remote URL"
        return url, ""

    @staticmethod
    def _branch_url(name, path):
        """Return (url, error) for *path*'s current branch on the remote host."""
        if not is_git_repo(path):
            return "", f"{name}: not a git repository"
        branch = git_current_branch(path)
        url = git_branch_url(path, branch)
        if not url:
            return "", f"{name}: could not resolve the remote URL for '{branch}'"
        return url, ""

    @staticmethod
    def _pr_url(name, path):
        """Return (url, error) for *path*'s open pull request (network lookup)."""
        ok, result = get_ado_pr_url(name, path)
        return (result, "") if ok else ("", result)

    def _open_browser_async(self, repos, url_fn, what, busy_msg):
        """Resolve a URL per repo off the UI thread and open each in the browser.

        *url_fn(name, path)* returns (url, error); a non-empty *url* is opened in
        the default browser and a non-empty *error* is shown in the Errors panel.
        The resolution runs on a background thread (git/network calls can be
        slow) and the browser is opened back on the UI thread. *busy_msg* is
        shown on the completion banner while the lookup runs.
        """
        self.show_repos_async(repos, with_status=False)
        self.progress.show_completion(busy_msg)

        def _work():
            results = [(name, *url_fn(name, path)) for name, path in repos]
            self.after(0, self._on_urls_resolved, results, what)

        threading.Thread(target=_work, daemon=True).start()

    def _on_urls_resolved(self, results, what):
        """Open resolved URLs in the browser and report failures (UI thread)."""
        self.progress.clear_completion()
        opened = 0
        for name, url, error in results:
            if url:
                webbrowser.open(url, new=2)
                opened += 1
            elif error:
                self.errors.add(error)
        if opened:
            self.progress.show_completion(
                f"Opened {opened} {what}{'s' if opened != 1 else ''} in the browser."
            )

    # -- Create workspace from a PBI --------------------------------------- #
    def _action_create_from_pbi(self):
        """Download a PBI, map its WBS repos to folders and build a workspace."""
        self.errors.clear()
        pbi_id = ask_pbi_number(self)
        if not pbi_id:
            return

        # The work item download hits the network, so run it off the UI thread
        # and resume on the UI thread once it returns.
        self.progress.show_repos([])
        self.progress.show_completion(f"Downloading PBI {pbi_id}\u2026")

        def _work():
            ok, result = pbi.fetch_work_item(pbi_id)
            self.after(0, self._on_pbi_downloaded, ok, result)

        threading.Thread(target=_work, daemon=True).start()

    def _on_pbi_downloaded(self, ok, result):
        """Continue the PBI flow on the UI thread after the download finishes."""
        self.progress.clear_completion()
        if not ok:
            self.errors.add(result)
            return

        services = pbi.parse_wbs_services(result["description"])
        if not services:
            self.errors.add(
                f"PBI {result['id']}: no repositories found in the WBS section."
            )
            return

        synonyms = pbi.load_synonyms()
        folders = pbi.available_folders()

        # Shared NuGet folders are offered at the end of every folder dropdown;
        # a set lets us resolve the right root when building the workspace.
        nuget_folders = get_nuget_folders()
        nuget_set = set(nuget_folders)

        # Recognise stored synonyms for both repository and shared NuGet folders
        # so a previously learned NuGet mapping pre-fills its dropdown too.
        all_folders = folders + [f for f in nuget_folders if f not in set(folders)]
        mappings = pbi.map_services(services, synonyms, all_folders)

        resolved = resolve_pbi_repos(
            self, mappings, folders, nuget_folders=nuget_folders
        )
        if resolved is None:
            return

        # Remember every service->folder mapping the user resolved, including
        # shared NuGet folders, so the choice is preserved for next time.
        # Excluded services (folder None) are not learned. Build the list first
        # (not a generator) so add_synonym runs for *all* services - any() over a
        # generator would stop at the first newly added one.
        added = [
            pbi.add_synonym(synonyms, folder, service)
            for service, folder in resolved.items()
            if folder
        ]
        if any(added):
            ok_save, message = pbi.save_synonyms(synonyms)
            if not ok_save:
                self.errors.add(message)

        # Build the repo list (de-duplicated, order preserved). Excluded services
        # (folder None) are left out; shared NuGet folders resolve under
        # NUGETS_ROOT.
        chosen, seen = [], set()
        for folder in resolved.values():
            if not folder or folder in seen:
                continue
            seen.add(folder)
            root = NUGETS_ROOT if folder in nuget_set else REPOS_ROOT
            chosen.append((folder, os.path.join(root, folder)))

        if not chosen:
            self.errors.add(
                f"PBI {result['id']}: no repositories selected for the workspace."
            )
            return

        # Name the workspace and set a per-repo feature branch (pre-filled with
        # the workspace name). Repos ticked "Ignore git" keep their own branch.
        initial = f"{result['id']}_{pbi.slugify_title(result['title'])}"
        info = ask_workspace_branches(
            self, [n for n, _ in chosen], initial=initial
        )
        if info is None:
            return
        name = info["name"]

        # Turn the per-repo choices into branch overrides: an "Ignore git" repo
        # gets a skip override (and no branch); a repo whose branch differs from
        # the workspace name gets a branch override. Matches are left implicit.
        branch_suffix = {}
        overrides, ignore_folders = {}, set()
        for repo_name, _ in chosen:
            if info["ignore_git"].get(repo_name):
                overrides[repo_name] = {IGNORE_GIT_KEY: True}
                ignore_folders.add(repo_name)
                continue
            suffix = info["branches"].get(repo_name, name)
            branch_suffix[repo_name] = suffix
            if suffix != name:
                overrides[repo_name] = {"branch": f"feature/{suffix}"}

        ok_ws, message = write_workspace(name, chosen)
        if not ok_ws:
            self.errors.add(message)
            return

        if overrides:
            ok_ov, msg_ov = save_branch_overrides(name, overrides)
            if not ok_ov:
                self.errors.add(msg_ov)
        self._refresh()

        # Create each repo's feature branch (its own name), except the ignored
        # ones (they keep their own branch). Uncommitted changes are handled per
        # repo; repos already on their branch are skipped without a prompt.
        branch_repos = [(n, p) for n, p in chosen if n not in ignore_folders]
        decisions = self.collect_change_decisions(
            branch_repos, allow_move=True,
            skip_branch=lambda n: f"feature/{branch_suffix.get(n, name)}",
        )
        if decisions is None:
            # User aborted branch creation; the workspace file was still written.
            self.show_repos_async(chosen, with_status=True)
            return

        self.run_repo_action(
            chosen,
            lambda n, p: create_feature_branch(
                n, p, branch_suffix.get(n, name), decisions.get(n)
            ),
            f"{message}\nFeature branches created.",
            skip_fn=lambda n, _p: (
                "Skipped: ignore git (keeps its own branch)"
                if n in ignore_folders else False
            ),
        )

    # -- Create feature branch --------------------------------------------- #
    def _action_create_feature_branch(self):
        """Create a feature branch in every repo of the selected workspace.

        Same flow as the Manual tab's action; only the repo list differs (it
        comes from the workspace file instead of the checkbox selection).
        """
        ok, workspace, repos = self._selected_repos()
        self.errors.clear()
        if not ok:
            if workspace is not None:
                self.errors.add(repos)
            return
        if not repos:
            return

        branch_name = ask_branch_name(self)
        if not branch_name:
            return

        target = f"feature/{branch_name}"

        # Per-repo decisions for dirty repos; repos already on the target branch
        # are skipped (no prompt). Closing any modal aborts the batch.
        decisions = self.collect_change_decisions(
            repos, allow_move=True, skip_branch=target
        )
        if decisions is None:
            return

        self.run_repo_action(
            repos,
            lambda n, p: create_feature_branch(n, p, branch_name, decisions.get(n)),
            "All feature branches created successfully.",
        )
