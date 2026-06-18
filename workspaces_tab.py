"""The 'Workspaces' tab: switch all repos of a feature workspace at once."""

import os
import subprocess
from datetime import datetime
from tkinter import ttk

from config import WORKSPACES_ROOT
from gitutils import (
    list_workspaces_detailed, read_workspace_repos,
    run_git, is_git_repo, git_has_changes, git_branch_exists,
    save_uncommitted, has_savepos, restore_uncommitted,
    git_current_branch, create_feature_branch, rebase_on_master,
)
from widgets import WorkspaceList, Tooltip
from tab_base import ActionTabBase
from dialogs import ask_commit_delete_abort, ask_change_decision, ask_branch_name, ask_commit_or_abort

# Base message for the savepos commits created when switching workspaces.
SWITCH_SAVE_MSG = "savepos before workspace switch"

# Base message for the rebase savepos commits (see gitutils.save_uncommitted).
REBASE_SAVE_MSG = "save changes before rebase"


def _fmt_time(timestamp):
    """Format an epoch-second timestamp as a short 'YYYY-MM-DD HH:MM' string."""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


class WorkspacesTab(ActionTabBase):
    """Left: single-select workspace list. Middle: actions. Right: progress."""

    def __init__(self, master):
        super().__init__(master)
        self._build_left()
        self.build_middle_actions(self._actions())
        # The workspace table needs the room, so keep Details a fixed width and
        # let the left panel absorb the rest of the window.
        self.build_right_details(expand=False, width=300)

    # -- Layout ------------------------------------------------------------ #
    def _build_left(self):
        left = ttk.LabelFrame(self._top, text="Workspaces")
        left.pack(side="left", fill="both", expand=True)

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
        """Show the workspace's repositories in the Details panel immediately."""
        self.errors.clear()
        ok, repos = read_workspace_repos(workspace)
        if not ok:
            self.progress.set_repos([])
            self.errors.add(repos)
            return
        self.progress.set_repos([name for name, _ in repos])

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

    def _actions(self):
        return [
            (
                "Switch to selected workspace",
                self._action_switch,
                "Checks out every repository in the selected workspace to its "
                "'feature/<workspace>' branch. If the branch is missing in any "
                "repo, nothing is switched. Uncommitted changes are handled per "
                "repository (commit, delete, or abort).",
            ),
            (
                "Restore state before switch",
                self._action_restore,
                "For the selected workspace's repositories, undoes the "
                "'savepos before workspace switch' commit made by the app and "
                "restores the exact staged/unstaged working state. User commits "
                "are never touched.",
            ),
            (
                "Rebase current branch on master",
                self._action_rebase_on_master,
                "For the selected workspace's repositories: updates master "
                "(checkout + pull), returns to the feature branch and rebases "
                "it onto master. Uncommitted changes are committed first (with "
                "confirmation) and restored afterwards (staged/unstaged "
                "preserved) on a clean rebase.",
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

    # -- Switch to selected workspace -------------------------------------- #
    def _action_switch(self):
        ok, workspace, repos = self._selected_repos()
        self.errors.clear()
        if not ok:
            if workspace is not None:
                self.errors.add(repos)
            return
        if not repos:
            return

        target = f"feature/{workspace}"
        self.progress.set_repos([name for name, _ in repos])

        # Pre-check 1: every repo must have the target branch. If any is missing,
        # switch none of them.
        problems = []
        for name, path in repos:
            if not is_git_repo(path):
                problems.append(f"{name}: not a git repository")
            elif not git_branch_exists(path, target):
                problems.append(f"{name}: feature '{target}' does not exist")
        if problems:
            for problem in problems:
                self.errors.add(problem)
            for name, _ in repos:
                self.progress.status(name, "error")
            return

        # Pre-check 2: decide what to do with uncommitted changes (per repo).
        # Closing/aborting any modal cancels the whole switch. Repos already on
        # the target branch are skipped entirely (no prompt, no checkout).
        decisions = {}
        for name, path in repos:
            if git_current_branch(path) == target:
                continue
            if git_has_changes(path):
                decision = ask_commit_delete_abort(self, name)
                if decision is None:
                    self.progress.set_repos([])
                    return
                decisions[name] = decision

        self.run_repo_action(
            repos,
            lambda n, p: self._switch_one(n, p, target, decisions.get(n)),
            f"Switched to workspace '{workspace}'.",
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
        ok, workspace, repos = self._selected_repos()
        self.errors.clear()
        if not ok:
            if workspace is not None:
                self.errors.add(repos)
            return
        if not repos:
            return

        self.run_repo_action(repos, self._restore_one, "State restored.")

    def _restore_one(self, name, path):
        """Restore the pre-switch working state if the app made a savepos commit."""
        if not is_git_repo(path):
            return False, f"{name}: not a git repository"

        # Only act on commits the app made during a switch; skip everything else.
        if not has_savepos(path, SWITCH_SAVE_MSG):
            return True, ""

        ok, out = restore_uncommitted(path, SWITCH_SAVE_MSG)
        if not ok:
            return False, f"{name}: {out}"
        return True, ""

    # -- Rebase current branch on master ----------------------------------- #
    def _action_rebase_on_master(self):
        ok, workspace, repos = self._selected_repos()
        self.errors.clear()
        if not ok:
            if workspace is not None:
                self.errors.add(repos)
            return
        if not repos:
            return

        self.progress.set_repos([name for name, _ in repos])

        # Pre-scan: nothing is processed until the user decides about dirty repos.
        dirty = [name for name, path in repos
                 if is_git_repo(path) and git_has_changes(path)]
        if dirty and not ask_commit_or_abort(self, dirty):
            self.progress.set_repos([])
            return

        self.run_repo_action(
            repos,
            lambda n, p: rebase_on_master(n, p, REBASE_SAVE_MSG),
            "All repositories rebased successfully.",
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
        decisions = {}  # repo name -> "delete" | "commit" | "move"
        for name, path in repos:
            if not is_git_repo(path):
                continue
            if git_current_branch(path) == target:
                continue  # already on target branch; skip this repo entirely
            if git_has_changes(path):
                on_master = git_current_branch(path) == "master"
                decision = ask_change_decision(self, name, on_master)
                if decision is None:
                    return
                decisions[name] = decision

        self.run_repo_action(
            repos,
            lambda n, p: create_feature_branch(n, p, branch_name, decisions.get(n)),
            "All feature branches created successfully.",
        )
