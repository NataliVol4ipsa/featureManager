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
        self.build_middle_sections(self._sections())
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
        Branches are filled in lazily so a large selection stays responsive.
        """
        self.show_repos_async(self._all_selected_repos(), with_status=False)

    def _sections(self):
        """Stacked action groups mirroring the Workspaces tab (minus the
        workspace-only actions), driven by the checked repositories."""
        return [
            ("Local", self._local_actions()),
            ("Remote", self._remote_actions()),
            ("Packages", self._package_actions()),
            ("Pipelines", self._pipeline_actions()),
            ("Open", self._open_actions()),
        ]

    def _local_actions(self):
        return [
            (
                "Checkout & Pull master",
                self._action_checkout_pull_master,
                "For every selected repository: checks out the 'master' branch "
                "and pulls the latest changes from the remote.",
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
                "Rebase current branch on master",
                self._action_rebase_on_master,
                "For every selected repository: updates master (checkout + pull), "
                "returns to the feature branch and rebases it onto master. "
                "Uncommitted changes are committed first (with confirmation) and "
                "restored afterwards (staged/unstaged preserved) on a clean rebase.",
            ),
            (
                "Commit all changes",
                self._action_commit_all,
                "For every selected repository: stages and commits all "
                "uncommitted changes using a commit message you enter. If the "
                "selected repos with changes are on different branches, a "
                "warning is shown before committing.",
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

    def _remote_actions(self):
        return [
            (
                "Git push",
                self._action_push,
                "For every selected repository: pushes the current branch to "
                "origin, creating the remote branch automatically if it does "
                "not exist yet. No prompts are shown unless the selected repos "
                "are on different branches, in which case a warning is shown "
                "first.",
            ),
            (
                "Create pull request",
                self._action_create_pr,
                "For every selected repository: creates an Azure DevOps pull "
                "request to master from the current branch (it must be pushed "
                "first). You choose one custom title or let each title be "
                "auto-generated from its branch name (feature/123_my_desc "
                "\u2192 feature(123) My desc). A link to each new PR is shown.",
            ),
            (
                "Copy PR links",
                self._action_copy_pr_links,
                "For every selected repository: looks up each repo's open Azure "
                "DevOps pull request (current branch \u2192 master) and copies "
                "all 'repo name - pr link' lines to the clipboard. Repos without "
                "an open PR are listed in the Errors panel.",
            ),
        ]

    def _package_actions(self):
        return [
            (
                "Bump NuGet packages (public)",
                self._action_bump_public,
                "For every selected repository: reads each repo's root "
                "Directory.Packages.props (Central Package Management), checks "
                "every PackageVersion against the public NuGet feed (nuget.org) "
                "and rewrites any that have a newer stable release. Prerelease "
                "versions are ignored; packages not found on the public feed "
                "(e.g. private-feed packages) are left untouched. The file is "
                "edited in place - review, restore and commit the changes "
                "yourself. A per-repo report of the bumps is offered to copy "
                "when the run finishes.",
            ),
            (
                "Bump NuGet packages (private)",
                self._action_bump_private,
                "For every selected repository: bumps only packages hosted on "
                "the private Azure DevOps Artifacts feed(s) declared in each "
                "repo's nuget.config (sources starting with "
                "https://pkgs.dev.azure.com). The feed is queried with an Azure "
                "CLI token, so run 'az login' first. Packages only on the public "
                "feed are left untouched. The props file is edited in place; a "
                "per-repo report of the bumps is offered to copy when the run "
                "finishes.",
            ),
            (
                "Bump all NuGet packages",
                self._action_bump_all,
                "For every selected repository: bumps every PackageVersion to "
                "the highest stable release found across both the public NuGet "
                "feed (nuget.org) and the private Azure DevOps Artifacts feed(s) "
                "from each repo's nuget.config. Requires 'az login' for the "
                "private feed. Prerelease versions are ignored; the props file "
                "is edited in place and a per-repo report of the bumps is "
                "offered to copy when the run finishes.",
            ),
            (
                "Restore NuGet packages",
                self._action_restore,
                "For every selected repository: runs 'dotnet restore' on the "
                "repo's solution to refresh the restored packages (e.g. after a "
                "bump). An Azure CLI token is fetched once so the Azure Artifacts "
                "credential provider can authenticate against the private "
                "feed(s) - run 'az login' first, and the credential provider "
                "must be installed. Repos without a .sln in their root are "
                "skipped; any restore error (e.g. a 401 or a missing package "
                "version) is shown in the Errors list.",
            ),
        ]

    def _pipeline_actions(self):
        return [
            (
                "Run dev pipelines",
                self._action_run_dev_pipeline,
                "For every selected repository: starts each repository's Azure "
                "DevOps pipeline on its current branch, deploying to the "
                "Development environment only (infrastructure + Development on; "
                "Acceptance and Production off). Repositories whose branch is not "
                "on the remote are reported first, letting you abort or continue "
                "for the rest. Needs an ADO_PAT with Build (Read & execute) "
                "permission.",
            ),
            (
                "Run acc pipelines",
                self._action_run_acc_pipeline,
                "For every selected repository: starts each repository's Azure "
                "DevOps pipeline on its current branch, deploying to the "
                "Acceptance environment only (infrastructure + Acceptance on; "
                "Development and Production off). Repositories whose branch is "
                "not on the remote are reported first, letting you abort or "
                "continue for the rest. Needs an ADO_PAT with Build (Read & "
                "execute) permission.",
            ),
            (
                "View merged master pipelines",
                self._action_show_master_pipelines_merged_pr,
                "For every selected repository: finds the latest completed "
                "Azure DevOps pull request from that repository's current "
                "branch to master, resolves the matching master pipeline run "
                "for that exact merge commit, and opens a live monitor. "
                "Auto-approve controls are shown only in this master-pipeline "
                "monitor.",
            ),
        ]

    def _open_actions(self):
        return [
            (
                "Open in Git Bash tabs",
                self._action_open_terminals,
                "For every selected repository: opens a Git Bash session in a "
                "Windows Terminal tab (one tab per repo, titled with the repo "
                "name, started in that repo's folder). If Windows Terminal is "
                "not available, a separate Git Bash window is opened per repo.",
            ),
            (
                "Open repositories (master)",
                self._action_open_repos_master,
                "For every selected repository: opens each repository's master "
                "branch on the remote host (Azure DevOps / GitHub / \u2026) in "
                "your default web browser, one tab per repo.",
            ),
            (
                "Open remote branches",
                self._action_open_branches,
                "For every selected repository: opens each repository's current "
                "branch on the remote host in your default web browser, one tab "
                "per repo. Branches that have not been pushed yet may show as not "
                "found on the host.",
            ),
            (
                "Open pull requests",
                self._action_open_prs,
                "For every selected repository: looks up the open Azure DevOps "
                "pull request for each repo's current branch and opens it in your "
                "default web browser. Repos with no open PR are reported in the "
                "Errors panel.",
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

    # -- Commit all changes (custom message) ------------------------------- #
    def _action_commit_all(self):
        self.commit_all_changes(self._all_selected_repos())

    # -- Git push ---------------------------------------------------------- #
    def _action_push(self):
        self.push_all(self._all_selected_repos())

    # -- Create pull request ----------------------------------------------- #
    def _action_create_pr(self):
        self.create_prs(self._all_selected_repos())

    # -- Open in Git Bash tabs --------------------------------------------- #
    def _action_open_terminals(self):
        self.open_terminals(self._all_selected_repos())

    # -- Copy PR links ----------------------------------------------------- #
    def _action_copy_pr_links(self):
        self.copy_pr_links(self._all_selected_repos())

    # -- Bump NuGet packages ----------------------------------------------- #
    def _action_bump_public(self):
        self._bump_packages(include_public=True, include_private=False,
                            label="public feed")

    def _action_bump_private(self):
        self._bump_packages(include_public=False, include_private=True,
                            label="private feed")

    def _action_bump_all(self):
        self._bump_packages(include_public=True, include_private=True,
                            label="all feeds")

    def _bump_packages(self, include_public, include_private, label):
        """Bump the selected repos' out-of-date package versions for *label* feeds."""
        self.errors.clear()
        repos = self._all_selected_repos()
        if not repos:
            return
        self.bump_packages(repos, include_public, include_private, label)

    # -- Restore NuGet packages -------------------------------------------- #
    def _action_restore(self):
        self.errors.clear()
        repos = self._all_selected_repos()
        if not repos:
            return
        self.restore_packages(repos)

    # -- Run pipelines ----------------------------------------------------- #
    def _action_run_dev_pipeline(self):
        self._run_pipelines("dev")

    def _action_run_acc_pipeline(self):
        self._run_pipelines("acc")

    def _run_pipelines(self, environment):
        """Start each selected repo's pipeline on its current branch."""
        self.errors.clear()
        repos = self._all_selected_repos()
        if not repos:
            return
        active = [(name, path, git_current_branch(path)) for name, path in repos]
        self.run_pipelines(active, environment)

    def _action_show_master_pipelines_merged_pr(self):
        repos = self._all_selected_repos()
        if not repos:
            return
        active = [(name, path, git_current_branch(path)) for name, path in repos]
        self.show_master_pipeline_monitor_for_merged_prs(active)

    # -- Open on the remote host ------------------------------------------- #
    def _action_open_repos_master(self):
        self.open_repos_master(self._all_selected_repos())

    def _action_open_branches(self):
        self.open_branches(self._all_selected_repos())

    def _action_open_prs(self):
        self.open_prs(self._all_selected_repos())

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
        self.show_repos_async(repos, with_status=True)
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
