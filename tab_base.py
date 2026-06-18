"""Shared base class for the action tabs (Manual and Workspaces).

Provides the common three-column layout pieces (Actions / Details / Errors)
and a generic background runner that drives the per-repo status indicators.
"""

import threading
from tkinter import ttk

from widgets import Tooltip, ProgressPanel, ErrorList
from gitutils import (
    is_git_repo, git_current_branch, git_has_changes, commit_all, git_push,
    git_branch_url, create_ado_pr, ado_pr_title_from_branch,
)
from dialogs import (
    ask_change_decision, ask_commit_message, ask_branch_warning, ask_pr_details,
)


class ActionTabBase(ttk.Frame):
    """Base tab with a top row (for left/middle/right panels) and a bottom error list.

    Subclasses build their own left panel, then call build_middle_actions() and
    build_right_details(). Long-running work goes through run_repo_action().
    """

    def __init__(self, master, **kwargs):
        super().__init__(master, padding=6, **kwargs)

        # Top region holds the side-by-side panels; the error list sits
        # full-width below it.
        self._top = ttk.Frame(self)
        self._top.pack(side="top", fill="both", expand=True)

        errors_frame = ttk.LabelFrame(self, text="Errors")
        errors_frame.pack(side="bottom", fill="x")
        self.errors = ErrorList(errors_frame)
        self.errors.pack(fill="x", expand=False, padx=4, pady=4)

    # -- Shared panel builders --------------------------------------------- #
    def build_middle_actions(self, actions):
        """Build the middle 'Actions' column from (label, command, hint) tuples."""
        middle = ttk.LabelFrame(self._top, text="Actions")
        middle.pack(side="left", fill="y", padx=6)
        for label, command, hint in actions:
            button = ttk.Button(middle, text=label, command=command)
            button.pack(fill="x", padx=6, pady=3)
            Tooltip(button, hint)

    def build_right_details(self, expand=True, width=None):
        """Build the right 'Details' column holding the live progress panel.

        *expand* controls whether the panel absorbs spare horizontal space.
        *width*, if given, fixes the panel width (pixels) regardless of content
        so a tab can keep its left panel wider.
        """
        right = ttk.LabelFrame(self._top, text="Details")
        if width is not None:
            right.configure(width=width)
            right.pack_propagate(False)  # keep the fixed width
        right.pack(side="left", fill="both", expand=expand)
        self.progress = ProgressPanel(right)
        self.progress.pack(fill="both", expand=True, padx=4, pady=4)

    # -- Shared uncommitted-changes handling ------------------------------- #
    def collect_change_decisions(self, repos, allow_move=False, skip_branch=None,
                                 restore_option=False, note=None):
        """Ask, per repo, what to do with its uncommitted changes.

        The same modal is used by every action. Buttons are chosen per repo:
        always "delete" and "abort"; "commit" only when the repo is not on
        master (committing on master is disallowed); "commit_restore" alongside
        "commit" when *restore_option* is set (used by rebase, where one variant
        only commits and the other also restores the working state afterwards);
        "move" only when *allow_move* is set (i.e. a new branch is being
        created). *note* is extra explanatory text shown in each modal. Repos
        that are not git repos, are clean, or are already on *skip_branch* are
        skipped with no prompt.

        Returns a {repo_name: decision} dict (decision is "commit",
        "commit_restore", "delete" or "move"), or None if the user aborts/closes
        any modal so the whole batch is cancelled. Wherever a commit decision is
        later applied, the change set is committed preserving its staged/unstaged
        split so it can be restored.
        """
        decisions = {}
        for name, path in repos:
            if not is_git_repo(path):
                continue
            branch = git_current_branch(path)
            if skip_branch is not None and branch == skip_branch:
                continue
            if not git_has_changes(path):
                continue

            options = []
            if branch != "master":
                options.append("commit")
                if restore_option:
                    options.append("commit_restore")
            options.append("delete")
            if allow_move:
                options.append("move")
            options.append("abort")

            decision = ask_change_decision(
                self, name, options, on_master=(branch == "master"), note=note
            )
            if decision is None:
                return None
            decisions[name] = decision
        return decisions

    def commit_all_changes(self, repos):
        """Commit all changes in *repos* with a single user-supplied message.

        Shows the Details table for the repos, warns if the selected repos are
        not all on the same branch, asks for a commit message, then commits each
        repo on a background thread. Repos that are clean or not git repos are
        reported as errors during the run.
        """
        self.errors.clear()
        if not repos:
            return

        self.show_repos_async(repos, with_status=False)

        # Warn if the selected git repositories are not all on the same branch,
        # since the one commit message would land on different branches.
        branches = {
            git_current_branch(path)
            for _, path in repos
            if is_git_repo(path)
        }
        branches.discard("")  # ignore repos whose branch could not be read
        warning = None
        if len(branches) > 1:
            warning = (
                "The selected repositories are not all on the same branch."
            )

        message = ask_commit_message(self, len(repos), branch_warning=warning)
        if not message:
            return

        self.run_repo_action(
            repos,
            lambda n, p: commit_all(n, p, message),
            "All changes committed successfully.",
        )

    def push_all(self, repos):
        """Push every selected repo's current branch to origin.

        Shows the Details table for the repos. If the selected repos are not all
        on the same branch a warning modal is shown to confirm; otherwise the
        push runs with no modals. Remote branches that do not exist yet are
        created automatically without any interaction.
        """
        self.errors.clear()
        if not repos:
            return

        self.show_repos_async(repos, with_status=False)

        # Only show a modal when the repos are on different branches; otherwise
        # push straight away with no interaction.
        branches = {
            git_current_branch(path)
            for _, path in repos
            if is_git_repo(path)
        }
        branches.discard("")  # ignore repos whose branch could not be read
        if len(branches) > 1:
            if not ask_branch_warning(self, len(repos)):
                return

        self.run_repo_action(
            repos,
            git_push,
            "All branches pushed successfully.",
            link_fn=lambda n, p: git_branch_url(p, git_current_branch(p)),
        )

    def create_prs(self, repos):
        """Create an Azure DevOps pull request for every selected repo.

        Asks once whether to auto-generate each PR title from its branch name or
        use one custom title, then creates the PRs on a background thread. Each
        successful PR adds a clickable link to it in the Details table.
        """
        self.errors.clear()
        if not repos:
            return

        self.show_repos_async(repos, with_status=False)

        options = ask_pr_details(self, len(repos))
        if options is None:
            return

        # PR URLs are produced by the action itself; stash them so the link
        # column can show them without creating the PR a second time.
        pr_urls = {}

        def _create(name, path):
            if options["mode"] == "auto":
                title = ado_pr_title_from_branch(git_current_branch(path))
            else:
                title = options["title"]
            ok, result, warning = create_ado_pr(
                name, path, title, options["description"]
            )
            if ok:
                pr_urls[name] = result
                # A work-item link failure is non-fatal: the PR still succeeds,
                # but surface the reason so it is not silently lost.
                if warning:
                    self.after(0, self.errors.add, warning)
                return True, ""
            return False, result

        # On full success, offer to copy every "repo name - pr link" line.
        def _copy_text(ok_repos):
            return "\n".join(
                f"{name} - {pr_urls[name]}"
                for name, _ in ok_repos if name in pr_urls
            )

        self.run_repo_action(
            repos,
            _create,
            "All pull requests created successfully.",
            link_fn=lambda n, p: pr_urls.get(n, ""),
            link_text="View PR",
            link_header="Pull request",
            show_branch=False,
            completion_copy_fn=_copy_text,
        )


    # -- Generic background runner ----------------------------------------- #
    def repo_rows(self, repos):
        """Return [(name, branch), ...], querying each repo's current branch."""
        rows = []
        for name, path in repos:
            branch = git_current_branch(path) if is_git_repo(path) else ""
            rows.append((name, branch))
        return rows

    def show_repos_async(self, repos, with_status=False):
        """Show *repos* in the Details table at once, filling branches lazily.

        The table is drawn immediately with a "..." branch placeholder (querying
        every repo's branch up front is slow for large selections), then a
        background thread fills in each repo's real branch as it is read.
        """
        # Draw straight away with placeholders so the UI stays responsive.
        self.progress.show_repos(
            [(name, "...") for name, _ in repos], with_status=with_status
        )

        # A token guards against a newer selection overwriting an older scan's
        # late results: only the most recent call's updates are applied.
        token = getattr(self, "_branch_scan_token", 0) + 1
        self._branch_scan_token = token

        def _scan():
            for name, path in repos:
                if self._branch_scan_token != token:
                    return  # a newer selection started; stop this stale scan
                branch = git_current_branch(path) if is_git_repo(path) else ""
                self.after(0, self._apply_branch, token, name, branch)

        threading.Thread(target=_scan, daemon=True).start()

    def _apply_branch(self, token, name, branch):
        """Apply one lazily-read branch value, unless a newer scan superseded it."""
        if self._branch_scan_token == token:
            self.progress.set_branch(name, branch)

    def run_repo_action(self, repos, per_repo_fn, success_msg, on_complete=None,
                        link_fn=None, link_text="View branch",
                        link_header="Link", show_branch=True,
                        completion_copy_fn=None):
        """Run *per_repo_fn(name, path)* for each repo off the UI thread.

        *per_repo_fn* must return (ok, error_message). The table shows each
        repo's status circle and current branch; both update live (the branch is
        re-read after each repo in case the action changed it). The green banner
        shows only when every repo succeeds. *on_complete(all_ok)*, if given,
        runs on the UI thread afterwards (used to chain a follow-up step such as
        writing a workspace file). *link_fn(name, path)*, if given, returns a URL
        shown as a clickable link (labelled *link_text*, under the *link_header*
        column) after the repo's action succeeds. Set *show_branch* to False to
        hide the Branch column (e.g. when creating pull requests).
        *completion_copy_fn(ok_repos)*, if given, returns text shown behind a
        "Copy all" button on the success banner (ok_repos is the (name, path)
        list, used to build e.g. "repo name - pr link" lines).
        """
        self.errors.clear()
        self.progress.show_repos(
            [(name, "...") for name, _ in repos], with_status=True,
            with_link=link_fn is not None, show_branch=show_branch,
            link_header=link_header,
        )
        threading.Thread(
            target=self._worker,
            args=(repos, per_repo_fn, success_msg, on_complete, link_fn,
                  link_text, show_branch, completion_copy_fn),
            daemon=True,
        ).start()

    def _worker(self, repos, per_repo_fn, success_msg, on_complete=None,
                link_fn=None, link_text="View branch", show_branch=True,
                completion_copy_fn=None):
        all_ok = True
        for name, path in repos:
            self.after(0, self.progress.status, name, "in-progress")
            ok, message = per_repo_fn(name, path)
            if ok:
                self.after(0, self.progress.status, name, "done")
            else:
                all_ok = False
                self.after(0, self.progress.status, name, "error")
                self.after(0, self.errors.add, message)
            # The action may have changed the branch (e.g. checkout); refresh it
            # (no-op when the Branch column is hidden).
            if show_branch:
                branch = git_current_branch(path) if is_git_repo(path) else ""
                self.after(0, self.progress.set_branch, name, branch)
            # Add a clickable link once the action has succeeded.
            if ok and link_fn is not None:
                url = link_fn(name, path)
                if url:
                    self.after(0, self.progress.set_link, name, url, link_text)

        if all_ok and success_msg:
            copy_text = completion_copy_fn(repos) if completion_copy_fn else None
            self.after(0, self.progress.show_completion, success_msg, copy_text)
        if on_complete is not None:
            self.after(0, on_complete, all_ok)

