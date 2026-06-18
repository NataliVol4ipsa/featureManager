"""The 'Manual' tab: per-repo git actions driven by the Services/Nugets lists."""

import os
from tkinter import ttk

from config import REPOS_ROOT, NUGETS_ROOT
from gitutils import (
    get_service_folders, get_nuget_folders, write_workspace,
    run_git, is_git_repo, git_current_branch, git_has_changes,
    save_uncommitted, create_feature_branch, rebase_on_master, SAVEPOS_MSG,
)
from widgets import FolderTab
from tab_base import ActionTabBase
from dialogs import ask_branch_name

# Base message for the rebase savepos commits (see gitutils.save_uncommitted).
REBASE_SAVE_MSG = "save changes before rebase"


class ManualTab(ActionTabBase):
    """Left: Services/Nugets checkbox tabs. Middle: actions. Right: progress."""

    def __init__(self, master):
        super().__init__(master)
        self._build_left()
        self.build_middle_actions(self._actions())
        self.build_right_details()

    # -- Layout ------------------------------------------------------------ #
    def _build_left(self):
        # Fixed (slightly narrow) width so the Details table gets more room.
        left = ttk.Frame(self._top, width=230)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        self.notebook = ttk.Notebook(left)
        self.notebook.pack(fill="both", expand=True)

        self.services_tab = FolderTab(
            self.notebook, get_service_folders(), REPOS_ROOT,
            on_change=self._on_selection_changed,
        )
        self.nugets_tab = FolderTab(
            self.notebook, get_nuget_folders(), NUGETS_ROOT,
            on_change=self._on_selection_changed,
        )
        self.notebook.add(self.services_tab, text="Services")
        self.notebook.add(self.nugets_tab, text="Nugets")

    def _on_selection_changed(self):
        """Show the currently selected repos (with branch) in the Details table.

        Status circles stay blank here - they only appear while an action runs.
        """
        repos = self._all_selected_repos()
        self.progress.show_repos(self.repo_rows(repos), with_status=False)

    def _actions(self):
        """Return the (label, command, hint) tuples for the middle column."""
        return [
            (
                "Checkout & Pull master",
                self._action_checkout_pull_master,
                "For every selected repository: checks out the 'master' branch "
                "and pulls the latest changes from the remote.",
            ),
            (
                "Rebase current branch on master",
                self._action_rebase_on_master,
                "For every selected repository: updates master (checkout + pull), "
                "returns to the feature branch and rebases it onto master. "
                "Uncommitted changes are committed first (with confirmation) and "
                "restored afterwards (staged/unstaged preserved) on a clean rebase.",
            ),
            (
                "Create feature branch",
                self._action_create_feature_branch,
                "For every selected repository: updates master (checkout + pull) "
                "and creates a new 'feature/<name>' branch. Uncommitted changes "
                "are handled per repository (delete, commit, or move to the new "
                "branch) before the branch is created.",
            ),
            (
                "Commit all changes as savepos",
                self._action_commit_savepos,
                "For every selected repository: commits all uncommitted changes "
                "as a 'savepos' commit on the current branch. Repositories on "
                "master are skipped with an error.",
            ),
            (
                "Create feature workspace and branches",
                self._action_create_workspace_and_branches,
                "For every selected repository: first creates a 'feature/<name>' "
                "branch (updating master, then branching off it, with per-repo "
                "handling of uncommitted changes - delete, commit, or move), "
                "then writes a VS Code '.code-workspace' file named after the "
                "same feature name.",
            ),
        ]

    # -- Selection helper -------------------------------------------------- #
    def _all_selected_repos(self):
        """Return (name, path) pairs for every checked repo across both tabs."""
        return (
            self.services_tab.get_selected_paths()
            + self.nugets_tab.get_selected_paths()
        )

    # -- Checkout & Pull master -------------------------------------------- #
    def _action_checkout_pull_master(self):
        repos = self._all_selected_repos()
        if not repos:
            return

        # Ask per dirty repo what to do with its changes before switching away.
        decisions = self.collect_change_decisions(repos)
        if decisions is None:
            return  # user aborted

        self.run_repo_action(
            repos,
            lambda n, p: self._checkout_and_pull(n, p, decisions.get(n)),
            "All repositories updated successfully.",
        )

    def _checkout_and_pull(self, name, path, decision):
        """Checkout master + pull for one repo. Returns (ok, error_message).

        Uncommitted changes are handled per the user's *decision*: "delete"
        discards them; "commit" saves them as restorable savepos commit(s) on
        the current branch (staged/unstaged split preserved).
        """
        if not is_git_repo(path):
            return False, f"{name}: not a git repository"

        if git_has_changes(path):
            if decision == "delete":
                ok, out = run_git(path, ["reset", "--hard"])
                if not ok:
                    return False, f"{name}: {out}"
                ok, out = run_git(path, ["clean", "-fd"])
                if not ok:
                    return False, f"{name}: {out}"
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
        return True, ""

    # -- Rebase current branch on master ----------------------------------- #
    def _action_rebase_on_master(self):
        repos = self._all_selected_repos()
        if not repos:
            return

        self.errors.clear()
        self.progress.show_repos(self.repo_rows(repos), with_status=True)

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
            self.progress.show_repos([])
            return

        self.run_repo_action(
            repos,
            lambda n, p: rebase_on_master(n, p, REBASE_SAVE_MSG, decisions.get(n)),
            "All repositories rebased successfully.",
        )

    # -- Create feature branch --------------------------------------------- #
    def _action_create_feature_branch(self):
        repos = self._all_selected_repos()
        if not repos:
            return

        branch_name = ask_branch_name(self)
        if not branch_name:
            return

        decisions = self.collect_change_decisions(
            repos, allow_move=True, skip_branch=f"feature/{branch_name}"
        )
        if decisions is None:
            return  # user aborted

        self.run_repo_action(
            repos,
            lambda n, p: create_feature_branch(n, p, branch_name, decisions.get(n)),
            "All feature branches created successfully.",
        )

    # -- Commit all changes as savepos ------------------------------------- #
    def _action_commit_savepos(self):
        repos = self._all_selected_repos()
        if not repos:
            return
        self.run_repo_action(
            repos, self._commit_savepos_one,
            "All changes committed successfully.",
        )

    def _commit_savepos_one(self, name, path):
        """Commit one repo's changes as savepos. Returns (ok, error_message).

        Uses the staged/unstaged-preserving save so the commit can be restored.
        """
        if not is_git_repo(path):
            return False, f"{name}: not a git repository"
        if git_current_branch(path) == "master":
            return False, f"{name} is on master. cannot commit changes on master"
        if not git_has_changes(path):
            return False, f"{name}: no changes to commit"

        ok, out = save_uncommitted(path, SAVEPOS_MSG)
        if not ok:
            return False, f"{name}: {out}"
        return True, ""

    # -- Create feature workspace ------------------------------------------ #
    def _action_create_workspace(self):
        repos = self._all_selected_repos()
        if not repos:
            return

        feature_name = ask_branch_name(
            self, title="Create feature workspace", prefix="feature/"
        )
        if not feature_name:
            return

        # Writing a file is fast, so this runs inline (no background thread).
        self.errors.clear()
        self.progress.show_repos(self.repo_rows(repos), with_status=True)
        ok, message = write_workspace(feature_name, repos)
        if ok:
            for name, _ in repos:
                self.progress.status(name, "done")
            self.progress.show_completion(message)
        else:
            self.errors.add(message)

    # -- Create feature workspace and branches ----------------------------- #
    def _action_create_workspace_and_branches(self):
        """Combine create-feature-branch then create-feature-workspace.

        Branches are created first (same flow/handling as the standalone
        action); once they finish the workspace file is written for the same
        feature name.
        """
        repos = self._all_selected_repos()
        if not repos:
            return

        feature_name = ask_branch_name(self)
        if not feature_name:
            return

        decisions = self.collect_change_decisions(
            repos, allow_move=True, skip_branch=f"feature/{feature_name}"
        )
        if decisions is None:
            return  # user aborted

        # After the branches finish, write the workspace. The combined green
        # banner appears only when every branch succeeded and the file was saved.
        def _then(all_ok):
            ok, message = write_workspace(feature_name, repos)
            if not ok:
                self.errors.add(message)
                return
            if all_ok:
                self.progress.show_completion(
                    f"Feature branches created. {message}"
                )

        self.run_repo_action(
            repos,
            lambda n, p: create_feature_branch(n, p, feature_name, decisions.get(n)),
            None,  # completion handled by _then so we get a combined message
            on_complete=_then,
        )
