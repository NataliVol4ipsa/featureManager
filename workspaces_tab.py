"""The 'Workspaces' tab: switch all repos of a feature workspace at once."""

import os
from tkinter import ttk

from gitutils import (
    list_workspaces, read_workspace_repos,
    run_git, is_git_repo, git_has_changes, git_branch_exists,
    save_uncommitted, has_savepos, restore_uncommitted,
    git_current_branch, create_feature_branch,
)
from widgets import WorkspaceList
from tab_base import ActionTabBase
from dialogs import ask_commit_delete_abort, ask_change_decision, ask_branch_name

# Base message for the savepos commits created when switching workspaces.
SWITCH_SAVE_MSG = "savepos before workspace switch"


class WorkspacesTab(ActionTabBase):
    """Left: single-select workspace list. Middle: actions. Right: progress."""

    def __init__(self, master):
        super().__init__(master)
        self._build_left()
        self.build_middle_actions(self._actions())
        self.build_right_details()

    # -- Layout ------------------------------------------------------------ #
    def _build_left(self):
        left = ttk.LabelFrame(self._top, text="Workspaces")
        left.pack(side="left", fill="both", expand=True)

        self.workspace_list = WorkspaceList(
            left, list_workspaces(), on_select=self._on_workspace_selected
        )
        self.workspace_list.pack(fill="both", expand=True, padx=4, pady=4)

        ttk.Button(left, text="Refresh", command=self._refresh).pack(
            fill="x", padx=4, pady=(0, 4)
        )

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
        self.workspace_list.set_items(list_workspaces())

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
                "Create feature branch",
                self._action_create_feature_branch,
                "For the selected workspace's repositories: updates master "
                "(checkout + pull) and creates a new 'feature/<name>' branch. "
                "Uncommitted changes are handled per repository (delete, commit, "
                "or move to the new branch) before the branch is created.",
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
