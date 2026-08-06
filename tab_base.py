"""Shared base class for the action tabs (Manual and Workspaces).

Provides the common three-column layout pieces (Actions / Details / Errors)
and a generic background runner that drives the per-repo status indicators.
"""

import threading
import tkinter as tk
import webbrowser
from tkinter import ttk

from widgets import Tooltip, ProgressPanel, ErrorList
from gitutils import (
    is_git_repo, git_current_branch, git_has_changes, commit_all, git_push,
    git_branch_url, create_ado_pr, ado_pr_title_from_branch,
    git_branch_is_empty, open_in_terminal_tabs, get_ado_pr_url,
    remote_branch_exists, ado_work_item_id_from_branch,
)
from dialogs import (
    ask_change_decision, ask_commit_message, ask_branch_warning, ask_pr_details,
    ask_missing_remote_branches, ask_acc_autoapprove,
)
from pipelines import (
    run_pipeline_for_repo_details,
    get_master_pipeline_run_for_merged_branch_details,
    get_work_item_report_details_for_repo,
)
import packages
from parallel import run_in_parallel
from pipeline_monitor import PipelineMonitorWindow


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

        # Links of the most recently created pull requests ({name: url}), kept
        # so a standalone action can copy them after the create-PR run.
        self._last_pr_urls = {}
        # Keep references to floating pipeline monitor windows.
        self._pipeline_monitors = []

    # -- Shared panel builders --------------------------------------------- #
    def build_middle_actions(self, actions):
        """Build the middle 'Actions' column from (label, command, hint) tuples."""
        middle = ttk.LabelFrame(self._top, text="Actions")
        middle.pack(side="left", fill="y", padx=6)
        for label, command, hint in actions:
            button = ttk.Button(middle, text=label, command=command)
            button.pack(fill="x", padx=6, pady=3)
            Tooltip(button, hint)

    def build_middle_sections(self, sections):
        """Build the middle 'Actions' column as several stacked labelled groups.

        *sections* is a list of (title, actions) pairs, where *actions* is the
        usual list of (label, command, hint) tuples. Each section becomes its
        own labelled frame, stacked top-to-bottom in a single column so related
        actions are visually grouped. The whole column lives inside a vertically
        scrollable canvas so it stays usable when there are more actions than
        vertical space.
        """
        container = ttk.Frame(self._top)
        container.pack(side="left", fill="y", padx=6)

        canvas = tk.Canvas(container, highlightthickness=0, width=1)
        scrollbar = ttk.Scrollbar(container, orient="vertical",
                                  command=canvas.yview)
        middle = ttk.Frame(canvas)

        # Keep the scrollregion in sync with the inner frame's size, and match
        # the canvas width to the buttons so nothing is clipped horizontally.
        def _on_configure(_event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.configure(width=middle.winfo_reqwidth())

        middle.bind("<Configure>", _on_configure)
        canvas.create_window((0, 0), window=middle, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Mouse-wheel scrolling while hovering the actions column.
        canvas.bind("<Enter>",
                    lambda _e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        for title, actions in sections:
            frame = ttk.LabelFrame(middle, text=title)
            frame.pack(side="top", fill="x", pady=(0, 6))
            for label, command, hint in actions:
                button = ttk.Button(frame, text=label, command=command)
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
        skipped with no prompt. *skip_branch* may be a fixed branch name or a
        callable ``fn(name) -> branch`` returning each repo's target branch.

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
            # *skip_branch* may be a fixed branch name or a callable returning
            # the per-repo target branch (workspaces can switch each repo to a
            # differently named feature branch). Repos already on their target
            # are skipped (no prompt).
            target = skip_branch(name) if callable(skip_branch) else skip_branch
            if target is not None and branch == target:
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
            parallel=True,
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
        # push straight away with no interaction. When no modal is shown the
        # "skip empty branches" option defaults to on (matching the checkbox).
        skip_empty = True
        branches = {
            git_current_branch(path)
            for _, path in repos
            if is_git_repo(path)
        }
        branches.discard("")  # ignore repos whose branch could not be read
        if len(branches) > 1:
            answer = ask_branch_warning(self, len(repos))
            if not answer["ok"]:
                return
            skip_empty = answer["skip_empty"]

        self.run_repo_action(
            repos,
            git_push,
            "All branches pushed successfully.",
            link_fn=lambda n, p: git_branch_url(p, git_current_branch(p)),
            skip_fn=git_branch_is_empty if skip_empty else None,
            parallel=True,
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
        # column can show them without creating the PR a second time. The same
        # dict is kept on the instance so "Copy PR links" can reuse it later.
        pr_urls = {}
        self._last_pr_urls = pr_urls

        def _create(name, path):
            if options["mode"] == "auto":
                title = ado_pr_title_from_branch(git_current_branch(path))
            else:
                title = options["title"]
            ok, result, warning = create_ado_pr(
                name, path, title, options["description"],
                draft=options["draft"],
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
            skip_fn=git_branch_is_empty if options["skip_empty"] else None,
            parallel=True,
        )


    def copy_pr_links(self, repos):
        """Look up each selected repo's open pull request and copy the links.

        Queries Azure DevOps for the active PR of every repo's current branch
        (no reliance on PRs created in this session), lists a clickable link per
        repo in the Details table, and copies all "repo name - pr link" lines to
        the clipboard. Repos without an open PR are reported in the Errors panel.
        """
        self.errors.clear()
        if not repos:
            return

        self.show_repos_async(repos, with_status=False)

        pr_urls = {}
        self._last_pr_urls = pr_urls

        def _lookup(name, path):
            ok, result = get_ado_pr_url(name, path)
            if ok:
                pr_urls[name] = result
                return True, ""
            return False, result

        def _copy_text(ok_repos):
            return "\n".join(
                f"{name} - {pr_urls[name]}"
                for name, _ in ok_repos if name in pr_urls
            )

        # Copy to the clipboard as soon as the lookups finish, even if some
        # repos had no open PR (the ones that were found are still useful).
        def _on_complete(_all_ok):
            if not pr_urls:
                return
            text = "\n".join(f"{n} - {u}" for n, u in pr_urls.items())
            self.clipboard_clear()
            self.clipboard_append(text)
            self.progress.show_completion(
                "Pull request links copied to clipboard.", text
            )

        self.run_repo_action(
            repos,
            _lookup,
            "Pull request links copied to clipboard.",
            link_fn=lambda n, p: pr_urls.get(n, ""),
            link_text="View PR",
            link_header="Pull request",
            show_branch=False,
            completion_copy_fn=_copy_text,
            on_complete=_on_complete,
        )


    # -- Open Git Bash terminals ------------------------------------------- #
    def open_terminals(self, repos):
        """Open each selected repo as a Git Bash tab in one Windows Terminal window.

        Each tab is titled with the repo name and starts in that repo's folder.
        Falls back to a separate Git Bash window per repo when Windows Terminal
        is unavailable. Nothing happens (no error) when no repos are selected.
        """
        self.errors.clear()
        if not repos:
            return
        ok, message = open_in_terminal_tabs(repos)
        if not ok:
            self.errors.add(message)

    # -- Bump NuGet packages ----------------------------------------------- #
    def bump_packages(self, repos, include_public, include_private, label):
        """Bump each repo's out-of-date package versions for the chosen feeds.

        Repos without any Directory.Packages.props (root, or per sub-solution
        for a multi-repository repo) are marked skipped. When the private feed is
        once up front (on a background thread) with a feed reachability check, so
        a "not logged in" / unreachable failure is reported once, not per repo.
        Each repo's props file is rewritten in place; a per-repo report of the
        applied bumps is offered to copy on completion. *repos* is assumed
        non-empty (callers validate their selection first).
        """
        self.show_repos_async(repos, with_status=True)

        # The private feed needs an Azure CLI token; fetch it once (off the UI
        # thread) so a "not logged in" failure is reported once, not per repo.
        # A reachability healthcheck follows the token (the private feed may need
        # the VPN), so an unreachable feed is reported before any bump is tried.
        if include_private:
            def _prepare():
                token = packages.get_azure_devops_token()
                feed_error = ""
                if token:
                    ok_hc, feed_error = packages.healthcheck_private_feeds(
                        [path for _, path in repos], token
                    )
                    if ok_hc:
                        feed_error = ""
                self.after(0, self._start_bump, repos, include_public,
                           include_private, label, token, feed_error)
            threading.Thread(target=_prepare, daemon=True).start()
        else:
            self._start_bump(repos, include_public, include_private, label, None)

    def _start_bump(self, repos, include_public, include_private, label, token,
                    feed_error=""):
        """Run the actual per-repo bump once any required token/healthcheck passes."""
        if include_private and not token:
            self.progress.show_repos([])
            self.errors.add(
                "could not get an Azure DevOps token - run 'az login' first"
            )
            return
        if include_private and feed_error:
            self.progress.show_repos([])
            self.errors.add(feed_error)
            return

        # Fresh per-batch memo so a package shared across props files / repos is
        # fetched from the feed only once (but a later batch still re-checks).
        packages.reset_version_cache()

        reports = {}
        self._bump_reports = reports

        def _skip(name, path):
            if not packages.find_all_props_files(path):
                return "no Directory.Packages.props found"
            return ""

        def _bump(name, path):
            ok_bump, result = packages.bump_repo_packages(
                path, include_public=include_public,
                include_private=include_private, token=token,
            )
            if not ok_bump:
                return False, result
            reports[name] = result
            return True, ""

        def _report_text(ok_repos):
            lines = []
            for name, _ in ok_repos:
                bumps = reports.get(name, [])
                if bumps:
                    lines.append(name)
                    for package_id, old, new in bumps:
                        lines.append(f"  {package_id}: {old} -> {new}")
                else:
                    lines.append(f"{name} (up to date)")
            return "\n".join(lines)

        self.run_repo_action(
            repos,
            _bump,
            f"Package versions bumped ({label}).",
            skip_fn=_skip,
            completion_copy_fn=_report_text,
            completion_copy_label="Copy report",
            parallel=True,
        )

    # -- Restore packages -------------------------------------------------- #
    def restore_packages(self, repos):
        """Run 'dotnet restore' in each selected repo to refresh restored packages.

        An Azure CLI token is fetched once up front (off the UI thread) and
        passed to each restore so the Azure Artifacts credential provider can
        authenticate against the private feed(s). Repos without any solution file
        (root, or per sub-solution for a multi-repository repo) are marked
        skipped. *repos* is assumed non-empty.
        """
        self.show_repos_async(repos, with_status=True)

        def _prepare():
            token = packages.get_azure_devops_token()
            self.after(0, self._start_restore, repos, token)
        threading.Thread(target=_prepare, daemon=True).start()

    def _start_restore(self, repos, token):
        """Run the actual per-repo restore once the token has been fetched."""
        def _skip(name, path):
            if not packages.find_all_solution_files(path):
                return "no .sln found"
            return ""

        def _restore(name, path):
            return packages.dotnet_restore(path, token)

        self.run_repo_action(
            repos,
            _restore,
            "Packages restored.",
            skip_fn=_skip,
            parallel=True,
        )

    # -- Run pipelines ----------------------------------------------------- #
    def run_pipelines(self, active, environment):
        """Start each repo's pipeline on the given branch for *environment*.

        *active* is a list of (name, path, branch). Each repo's remote branch
        must exist; those that do not are reported in a modal that lets the user
        abort or continue for the rest. The remote check hits the network so it
        runs on a background thread. *active* is assumed non-empty.
        """
        autoapprove_acc = False
        if environment == "acc":
            autoapprove_acc = ask_acc_autoapprove(self)

        self.show_repos_async([(n, p) for n, p, _ in active], with_status=True)

        def _check():
            existing, missing = [], []
            for name, path, branch in active:
                if is_git_repo(path) and remote_branch_exists(path, branch):
                    existing.append((name, path, branch))
                else:
                    missing.append(name)
            self.after(
                0,
                self._on_branches_checked,
                environment,
                autoapprove_acc,
                existing,
                missing,
            )

        threading.Thread(target=_check, daemon=True).start()

    def _on_branches_checked(self, environment, autoapprove_acc, existing, missing):
        """After the remote-branch scan: confirm missing repos, then run."""
        env_label = "Development" if environment == "dev" else "Acceptance"
        if missing:
            if not ask_missing_remote_branches(self, missing, env_label):
                self.progress.show_repos([])
                return
        if not existing:
            self.errors.add(
                "None of the selected repositories have a remote branch to run."
            )
            return

        repos = [(name, path) for name, path, _ in existing]
        branch_of = {name: branch for name, _, branch in existing}
        urls = {}
        run_infos = {}
        self._pipeline_urls = urls

        def _run(name, path):
            ok, result = run_pipeline_for_repo_details(
                name, path, branch_of[name], environment
            )
            if ok:
                urls[name] = result.get("url", "")
                result["environment"] = environment
                result["autoapprove_acc"] = bool(autoapprove_acc)
                run_infos[name] = result
                return True, ""
            return False, result

        def _on_complete(_all_ok):
            monitor_runs = {
                name: info for name, info in run_infos.items()
                if info.get("build_id") is not None
            }
            for name, info in run_infos.items():
                if info.get("build_id") is None:
                    self.errors.add(
                        f"{name}: pipeline started, but build id was unavailable "
                        "for live monitoring"
                    )
            if monitor_runs:
                self._open_pipeline_monitor(monitor_runs)

        self.run_repo_action(
            repos,
            _run,
            f"Pipelines started for the {env_label} environment.",
            link_fn=lambda n, p: urls.get(n, ""),
            link_text="View run",
            link_header="Pipeline run",
            completion_open_fn=lambda ok_repos: [
                urls[n] for n, _ in ok_repos if urls.get(n)
            ],
            completion_open_label="Open pipelines",
            completion_copy_fn=lambda ok_repos: "\n".join(
                f"{n} - {urls[n]}" for n, _ in ok_repos if urls.get(n)
            ),
            completion_copy_label="Copy links",
            on_complete=_on_complete,
            parallel=True,
        )

    def _open_pipeline_monitor(self, run_infos, show_autoapprove_controls=False,
                               pbi_title="", test_reports=None):
        """Create a floating always-on-top window tracking started pipeline runs."""
        monitor = PipelineMonitorWindow(
            self,
            run_infos,
            show_autoapprove_controls=show_autoapprove_controls,
            pbi_title=pbi_title,
            test_reports=test_reports,
        )
        # Drop dead references before storing the new monitor.
        self._pipeline_monitors = [
            win for win in self._pipeline_monitors
            if getattr(win, "winfo_exists", lambda: False)()
        ]
        self._pipeline_monitors.append(monitor)

    def reopen_monitor_session(self, session):
        """Reopen a pipeline monitor from a saved snapshot (see session_state)."""
        run_infos = (session or {}).get("run_infos") or {}
        if not run_infos:
            return
        test_reports = [tuple(item) for item in (session.get("test_reports") or [])]
        self._open_pipeline_monitor(
            run_infos,
            show_autoapprove_controls=bool(session.get("show_autoapprove_controls")),
            pbi_title=session.get("pbi_title", "") or "",
            test_reports=test_reports,
        )

    def show_master_pipeline_monitor_for_merged_prs(self, active):
        """Open monitor window for master runs tied to merged PR commits.

        *active* is a list of (name, path, branch).
        """
        self.errors.clear()
        if not active:
            return

        repos = [(name, path) for name, path, _ in active]
        branch_of = {name: branch for name, _, branch in active}

        self.show_repos_async(repos, with_status=False)
        self.progress.show_completion(
            "Resolving merged-PR master pipeline runs for monitor..."
        )

        def _work():
            run_infos = {}
            errors = []
            for name, path, _ in active:
                ok, result = get_master_pipeline_run_for_merged_branch_details(
                    name, path, branch_of[name]
                )
                if ok:
                    run_infos[name] = result
                else:
                    errors.append(result)
            pbi_title, test_reports, wi_errors = self._resolve_master_pbi_info(active)
            errors.extend(wi_errors)
            self.after(
                0,
                self._on_master_pipeline_monitor_resolved,
                run_infos,
                errors,
                pbi_title,
                test_reports,
            )

        threading.Thread(target=_work, daemon=True).start()

    def _resolve_master_pbi_info(self, active):
        """Return (pbi_title, test_reports, errors) from the branches' work items.

        Work item ids are read from the feature branch names; duplicates (repos
        sharing the same PBI) are resolved once. *test_reports* is a de-duplicated
        list of (name, url) for every "Tested By" linked work item.
        """
        seen_wid = {}
        for name, path, branch in active:
            wid = ado_work_item_id_from_branch(branch)
            if wid and wid not in seen_wid:
                seen_wid[wid] = (name, path)

        titles = []
        test_reports = []
        seen_report_urls = set()
        errors = []
        for wid, (name, path) in seen_wid.items():
            ok, result = get_work_item_report_details_for_repo(name, path, wid)
            if not ok:
                errors.append(result)
                continue
            if result.get("title"):
                titles.append(result["title"])
            for report_name, report_url in result.get("test_reports", []):
                if report_url not in seen_report_urls:
                    seen_report_urls.add(report_url)
                    test_reports.append((report_name, report_url))

        return " | ".join(titles), test_reports, errors

    def _on_master_pipeline_monitor_resolved(self, run_infos, errors,
                                             pbi_title="", test_reports=None):
        """Show lookup errors and open monitor for all resolved merged-PR runs."""
        self.progress.clear_completion()
        for message in errors:
            self.errors.add(message)

        if not run_infos:
            return

        for info in run_infos.values():
            info["is_master_run"] = True

        self._open_pipeline_monitor(
            run_infos,
            show_autoapprove_controls=True,
            pbi_title=pbi_title,
            test_reports=test_reports,
        )
        count = len(run_infos)
        self.progress.show_completion(
            f"Opened monitor for {count} master pipeline run"
            f"{'s' if count != 1 else ''}."
        )

    # -- Open on the remote host in the browser ---------------------------- #
    def open_repos_master(self, repos):
        """Open each repo's master branch on the remote host in the browser."""
        self.errors.clear()
        if not repos:
            return
        self._open_browser_async(
            repos, self._master_url, "master link", "Resolving master links\u2026"
        )

    def open_branches(self, repos):
        """Open each repo's current branch on the remote host in the browser."""
        self.errors.clear()
        if not repos:
            return
        self._open_browser_async(
            repos, self._branch_url, "branch link", "Resolving branch links\u2026"
        )

    def open_prs(self, repos):
        """Open each repo's open pull request on the remote host in the browser."""
        self.errors.clear()
        if not repos:
            return
        self._open_browser_async(
            repos, self._pr_url, "pull request", "Looking up pull requests\u2026"
        )

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
            def _read_one(repo):
                name, path = repo
                if self._branch_scan_token != token:
                    return  # a newer selection started; stop this stale scan
                branch = git_current_branch(path) if is_git_repo(path) else ""
                self.after(0, self._apply_branch, token, name, branch)

            run_in_parallel(repos, _read_one)

        threading.Thread(target=_scan, daemon=True).start()

    def _apply_branch(self, token, name, branch):
        """Apply one lazily-read branch value, unless a newer scan superseded it."""
        if self._branch_scan_token == token:
            self.progress.set_branch(name, branch)

    def run_repo_action(self, repos, per_repo_fn, success_msg, on_complete=None,
                        link_fn=None, link_text="View branch",
                        link_header="Link", show_branch=True,
                        completion_copy_fn=None, skip_fn=None,
                        completion_open_fn=None, completion_open_label="Open all",
                        completion_copy_label="Copy all", parallel=False):
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
        *skip_fn(name, path)*, if given, is called (on the worker thread) before
        each repo's action; if it returns a truthy value the repo is marked
        "skipped" and its action is not run (skipped repos do not affect the
        success banner). When *skip_fn* returns a non-empty string the string
        is attached as a hover tooltip on the skipped status indicator.
        Set *parallel* to True to run *per_repo_fn* across all repos
        concurrently (via a thread pool); status circles still update live as
        each repo finishes. Only enable it for actions that are safe to run in
        parallel (each repo works on its own files, no shared state).
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
                  link_text, show_branch, completion_copy_fn, skip_fn,
                  completion_open_fn, completion_open_label,
                  completion_copy_label, parallel),
            daemon=True,
        ).start()

    def _worker(self, repos, per_repo_fn, success_msg, on_complete=None,
                link_fn=None, link_text="View branch", show_branch=True,
                completion_copy_fn=None, skip_fn=None,
                completion_open_fn=None, completion_open_label="Open all",
                completion_copy_label="Copy all", parallel=False):
        def _process_one(name, path):
            skip_result = skip_fn(name, path) if skip_fn is not None else False
            if skip_result:
                tooltip = skip_result if isinstance(skip_result, str) else None
                self.after(0, self.progress.status, name, "skipped", tooltip)
                if show_branch:
                    branch = git_current_branch(path) if is_git_repo(path) else ""
                    self.after(0, self.progress.set_branch, name, branch)
                return True  # Skipped repos do not affect the success banner.
            self.after(0, self.progress.status, name, "in-progress")
            ok, message = per_repo_fn(name, path)
            if ok:
                self.after(0, self.progress.status, name, "done")
            else:
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
            return ok

        if parallel:
            results = run_in_parallel(repos, lambda rp: _process_one(*rp))
            all_ok = all(results)
        else:
            all_ok = True
            for name, path in repos:
                if not _process_one(name, path):
                    all_ok = False

        if all_ok and success_msg:
            copy_text = completion_copy_fn(repos) if completion_copy_fn else None
            open_urls = completion_open_fn(repos) if completion_open_fn else None
            self.after(0, self.progress.show_completion, success_msg, copy_text,
                       open_urls, completion_open_label, completion_copy_label)
        if on_complete is not None:
            self.after(0, on_complete, all_ok)

