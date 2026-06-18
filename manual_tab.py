"""The 'Manual' tab: per-repo git actions driven by the Services/Nugets lists."""

import os
from tkinter import ttk

from config import REPOS_ROOT, NUGETS_ROOT
from gitutils import (
    get_service_folders, get_nuget_folders, write_workspace,
    run_git, is_git_repo, git_current_branch, git_has_changes,
    git_rebase_in_progress, save_uncommitted, restore_uncommitted,
    create_feature_branch,
)
from widgets import FolderTab
from tab_base import ActionTabBase
from dialogs import ask_commit_or_abort, ask_change_decision, ask_branch_name

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
        left = ttk.Frame(self._top)
        left.pack(side="left", fill="both", expand=True)

        self.notebook = ttk.Notebook(left)
        self.notebook.pack(fill="both", expand=True)

        self.services_tab = FolderTab(self.notebook, get_service_folders(), REPOS_ROOT)
        self.nugets_tab = FolderTab(self.notebook, get_nuget_folders(), NUGETS_ROOT)
        self.notebook.add(self.services_tab, text="Services")
        self.notebook.add(self.nugets_tab, text="Nugets")

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
                "Create feature workspace",
                self._action_create_workspace,
                "Creates a VS Code '.code-workspace' file from the selected "
                "repositories, named after the feature name you enter.",
            ),
            (
                "Create feature workspace and branches",
                self._action_create_workspace_and_branches,
                "Combination of 'Create feature branch' and 'Create feature "
                "workspace': first creates a 'feature/<name>' branch in every "
                "selected repository (with the same uncommitted-change handling), "
                "then writes a workspace file named after the same feature name.",
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
        self.run_repo_action(
            repos, self._checkout_and_pull,
            "All repositories updated successfully.",
        )

    def _checkout_and_pull(self, name, path):
        """Checkout master + pull for one repo. Returns (ok, error_message).

        On a non-master branch, local changes are committed as "savepos" first.
        On master with local changes the pull is unsafe, so the repo is skipped.
        """
        if not is_git_repo(path):
            return False, f"{name}: not a git repository"

        if git_has_changes(path):
            if git_current_branch(path) == "master":
                return False, (
                    f"{name} is already on master and has unsaved changes. "
                    f"cannot perform pull"
                )
            ok, out = run_git(path, ["add", "-A"])
            if not ok:
                return False, f"{name}: {out}"
            ok, out = run_git(path, ["commit", "-m", "savepos"])
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
        self.progress.set_repos([name for name, _ in repos])

        # Pre-scan: nothing is processed until the user decides about dirty repos.
        dirty = [name for name, path in repos
                 if is_git_repo(path) and git_has_changes(path)]
        if dirty and not ask_commit_or_abort(self, dirty):
            self.progress.set_repos([])
            return

        self.run_repo_action(
            repos, self._rebase_one,
            "All repositories rebased successfully.",
        )

    def _rebase_one(self, name, path):
        """Rebase one repo's feature branch onto master. Returns (ok, error_message).

        Uncommitted work is saved as commit(s) preserving the staged/unstaged
        split, then restored after a clean rebase so the working tree comes back
        exactly as it was.
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

        saved = git_has_changes(path)
        if saved:
            ok, out = save_uncommitted(path, REBASE_SAVE_MSG)
            if not ok:
                return False, f"{name}: {out}"

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
        if saved:
            ok, out = restore_uncommitted(path, REBASE_SAVE_MSG)
            if not ok:
                return False, f"{name}: {out}"
        return True, ""

    # -- Create feature branch --------------------------------------------- #
    def _action_create_feature_branch(self):
        repos = self._all_selected_repos()
        if not repos:
            return

        branch_name = ask_branch_name(self)
        if not branch_name:
            return

        decisions = self._collect_change_decisions(repos, f"feature/{branch_name}")
        if decisions is None:
            return  # user aborted

        self.run_repo_action(
            repos,
            lambda n, p: create_feature_branch(n, p, branch_name, decisions.get(n)),
            "All feature branches created successfully.",
        )

    def _collect_change_decisions(self, repos, target_branch):
        """Ask per-repo how to handle uncommitted changes before branching.

        Repos already on *target_branch* are skipped (no prompt). Returns a
        {name: decision} dict, or None if the user aborts any modal (so the
        whole batch is cancelled).
        """
        decisions = {}  # repo name -> "delete" | "commit" | "move"
        for name, path in repos:
            if not is_git_repo(path):
                continue
            if git_current_branch(path) == target_branch:
                continue  # already on target branch; skip this repo entirely
            if git_has_changes(path):
                on_master = git_current_branch(path) == "master"
                decision = ask_change_decision(self, name, on_master)
                if decision is None:
                    return None
                decisions[name] = decision
        return decisions

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
        """Commit one repo's changes as 'savepos'. Returns (ok, error_message)."""
        if not is_git_repo(path):
            return False, f"{name}: not a git repository"
        if git_current_branch(path) == "master":
            return False, f"{name} is on master. cannot commit changes on master"
        if not git_has_changes(path):
            return False, f"{name}: no changes to commit"

        ok, out = run_git(path, ["add", "-A"])
        if not ok:
            return False, f"{name}: {out}"
        ok, out = run_git(path, ["commit", "-m", "savepos"])
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
        self.progress.set_repos([name for name, _ in repos])
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

        decisions = self._collect_change_decisions(repos, f"feature/{feature_name}")
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
