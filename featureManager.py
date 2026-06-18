"""
Feature Manager
================
A small desktop tool to manage repositories located under D:/Repositories.

Layout (left -> right):
  * Left half  : Notebook with two tabs ("Services" and "Nugets"), each
                 showing a scrollable, checkbox-based folder list plus
                 Select All / Deselect All buttons. Selections are kept in
                 memory so switching tabs never loses state.
  * Middle     : A vertical list of action buttons (one per row).
  * Right      : A placeholder section to be filled in later.

The code is intentionally split into small, reusable pieces so new tabs,
actions or panels can be added later with minimal effort.
"""

import os
import json
import subprocess
import threading
import tkinter as tk
from tkinter import ttk

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Root folder that is scanned for repositories.
REPOS_ROOT = r"D:/Repositories"

# Sub-folder used to populate the "Nugets" tab.
NUGETS_ROOT = os.path.join(REPOS_ROOT, "Shared")

# Folder where generated VS Code workspace files are written.
WORKSPACES_ROOT = r"D:/Workspaces/features"

# Folder names that must never appear in the "Services" tab (case-insensitive).
EXCLUDED_FOLDERS = {"ibs", "shared", "wiki"}


# --------------------------------------------------------------------------- #
# Helpers
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


def git_current_branch(repo_path):
    """Return the current branch name, or '' if it cannot be determined."""
    ok, out = run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    return out if ok else ""


def git_has_changes(repo_path):
    """Return True if the working tree has uncommitted (tracked/untracked) changes."""
    ok, out = run_git(repo_path, ["status", "--porcelain"])
    return ok and bool(out)


def git_rebase_in_progress(repo_path):
    """Return True if an unfinished rebase already exists in the repo."""
    git_dir = os.path.join(repo_path, ".git")
    return (
        os.path.isdir(os.path.join(git_dir, "rebase-merge"))
        or os.path.isdir(os.path.join(git_dir, "rebase-apply"))
    )


# --------------------------------------------------------------------------- #
# Reusable UI components
# --------------------------------------------------------------------------- #

class Tooltip:
    """Lightweight hover tooltip for any widget (used for action button hints)."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self._tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _event=None):
        if self._tip or not self.text:
            return
        # Position the tooltip just below-right of the widget.
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)  # no window border/title bar
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._tip, text=self.text, justify="left",
            background="#ffffe0", relief="solid", borderwidth=1,
            padx=6, pady=3, wraplength=260,
        ).pack()

    def _hide(self, _event=None):
        if self._tip:
            self._tip.destroy()
            self._tip = None


# Visual indicators for each repository row, keyed by status.
STATUS_STYLES = {
    "pending":     ("\u25CB", "gray"),    # hollow circle
    "in-progress": ("\u25CF", "#d98c00"),  # filled circle, amber
    "done":        ("\u25CF", "#1a9e1a"),  # filled circle, green
    "error":       ("\u25CF", "#c0392b"),  # filled circle, red
}


class ProgressPanel(ttk.Frame):
    """Right-side panel: a completion banner plus a live per-repo status list.

    Call set_repos() at the start of an action, then status() as each repo
    progresses. show_completion() reveals the green banner when everything
    finished successfully.
    """

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)

        # Green completion banner, shown above the repo list (hidden by default).
        self._banner = tk.Label(self, text="", foreground="#1a9e1a",
                                font=("", 10, "bold"), anchor="w")

        # Scrollable list area for repo rows.
        self._canvas = tk.Canvas(self, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(self, orient="vertical",
                                        command=self._canvas.yview)
        self._inner = ttk.Frame(self._canvas)
        self._inner.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._canvas.configure(yscrollcommand=self._scrollbar.set)

        self._canvas.pack(side="left", fill="both", expand=True)
        self._scrollbar.pack(side="right", fill="y")

        # Maps repo name -> the Label widget showing its status indicator.
        self._indicators = {}

    def set_repos(self, names):
        """Build one row per repository, all starting in the 'pending' state."""
        self.clear_completion()
        for child in self._inner.winfo_children():
            child.destroy()
        self._indicators = {}

        for name in names:
            row = ttk.Frame(self._inner)
            row.pack(fill="x", anchor="w", padx=4, pady=1)
            symbol, color = STATUS_STYLES["pending"]
            indicator = tk.Label(row, text=symbol, foreground=color, width=2)
            indicator.pack(side="left")
            ttk.Label(row, text=name).pack(side="left")
            self._indicators[name] = indicator

    def status(self, name, state):
        """Update a single repo row to the given state (see STATUS_STYLES)."""
        indicator = self._indicators.get(name)
        if indicator is None:
            return
        symbol, color = STATUS_STYLES.get(state, STATUS_STYLES["pending"])
        indicator.config(text=symbol, foreground=color)

    def show_completion(self, text):
        """Reveal the green completion banner above the repo list."""
        self._banner.config(text=text)
        self._banner.pack(fill="x", padx=6, pady=(4, 6), before=self._canvas)

    def clear_completion(self):
        """Hide the completion banner (e.g. when a new action starts)."""
        self._banner.config(text="")
        self._banner.pack_forget()


class ErrorList(ttk.Frame):
    """Full-width bottom area that lists errors from the most recent action.

    Reused across actions: call clear() before an action and add() per error.
    """

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)

        self._text = tk.Text(self, height=6, wrap="word", state="disabled",
                             foreground="#c0392b")
        scrollbar = ttk.Scrollbar(self, orient="vertical",
                                  command=self._text.yview)
        self._text.configure(yscrollcommand=scrollbar.set)
        self._text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def clear(self):
        """Remove all previously listed errors."""
        self._text.config(state="normal")
        self._text.delete("1.0", "end")
        self._text.config(state="disabled")

    def add(self, message):
        """Append a single error line."""
        self._text.config(state="normal")
        self._text.insert("end", message.rstrip() + "\n")
        self._text.config(state="disabled")
        self._text.see("end")


class CheckboxList(ttk.Frame):
    """A scrollable list of checkboxes.

    Each item keeps its own tk.BooleanVar so selection state survives even when
    the widget is hidden (e.g. while another notebook tab is shown).
    """

    def __init__(self, master, items, **kwargs):
        super().__init__(master, **kwargs)

        # Maps folder name -> BooleanVar holding its checked state.
        self.vars = {}

        # Canvas + inner frame + scrollbar give us a vertically scrollable area.
        self._canvas = tk.Canvas(self, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(
            self, orient="vertical", command=self._canvas.yview
        )
        self._inner = ttk.Frame(self._canvas)

        self._inner.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._canvas.configure(yscrollcommand=self._scrollbar.set)

        self._canvas.pack(side="left", fill="both", expand=True)
        self._scrollbar.pack(side="right", fill="y")

        # Mouse-wheel scrolling while hovering the list.
        self._canvas.bind("<Enter>", self._bind_mousewheel)
        self._canvas.bind("<Leave>", self._unbind_mousewheel)

        self.set_items(items)

    def set_items(self, items):
        """(Re)build the checkbox rows from *items* while preserving prior state."""
        for child in self._inner.winfo_children():
            child.destroy()

        new_vars = {}
        for name in items:
            # Reuse an existing BooleanVar so a rescan keeps the user's choices.
            var = self.vars.get(name, tk.BooleanVar(value=False))
            new_vars[name] = var
            ttk.Checkbutton(self._inner, text=name, variable=var).pack(
                anchor="w", padx=4, pady=1
            )
        self.vars = new_vars

    def set_all(self, value):
        """Check (True) or uncheck (False) every item."""
        for var in self.vars.values():
            var.set(value)

    def get_selected(self):
        """Return the list of currently checked item names."""
        return [name for name, var in self.vars.items() if var.get()]

    # -- internal mouse-wheel handling ------------------------------------- #
    def _bind_mousewheel(self, _event):
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event):
        self._canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        self._canvas.yview_scroll(int(-event.delta / 120), "units")


class FolderTab(ttk.Frame):
    """A single notebook tab: Select-All / Deselect-All buttons + a CheckboxList.

    Each tab owns an independent CheckboxList, so actions on one tab never
    affect the other.
    """

    def __init__(self, master, items, root_path, **kwargs):
        super().__init__(master, **kwargs)

        # Folder that the displayed items live under; used to build full paths.
        self.root_path = root_path

        # Top row: per-tab selection buttons.
        button_bar = ttk.Frame(self)
        button_bar.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Button(
            button_bar, text="Select All",
            command=lambda: self.checkbox_list.set_all(True),
        ).pack(side="left", padx=(0, 4))
        ttk.Button(
            button_bar, text="Deselect All",
            command=lambda: self.checkbox_list.set_all(False),
        ).pack(side="left")

        # The visible, bordered area that contains the checkbox list.
        container = ttk.LabelFrame(self, text="Folders")
        container.pack(fill="both", expand=True, padx=4, pady=(2, 4))

        self.checkbox_list = CheckboxList(container, items)
        self.checkbox_list.pack(fill="both", expand=True, padx=2, pady=2)

    def get_selected(self):
        """Convenience pass-through to the underlying checkbox list."""
        return self.checkbox_list.get_selected()

    def get_selected_paths(self):
        """Return (name, full_path) pairs for every checked folder."""
        return [
            (name, os.path.join(self.root_path, name))
            for name in self.get_selected()
        ]


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #

class FeatureManagerApp(ttk.Frame):
    """Top-level application frame wiring the three panels together."""

    def __init__(self, master):
        super().__init__(master, padding=6)
        self.pack(fill="both", expand=True)

        # Top region holds the three side-by-side panels; the error list sits
        # full-width below it and is shared by every action.
        self._top = ttk.Frame(self)
        self._top.pack(side="top", fill="both", expand=True)

        self._build_left_panel()
        self._build_middle_panel()
        self._build_right_panel()
        self._build_error_panel()

    # -- Left: tabs with folder checkbox lists ----------------------------- #
    def _build_left_panel(self):
        left = ttk.Frame(self._top)
        left.pack(side="left", fill="both", expand=True)

        self.notebook = ttk.Notebook(left)
        self.notebook.pack(fill="both", expand=True)

        # Tabs are kept as attributes so actions can read their selections.
        self.services_tab = FolderTab(self.notebook, get_service_folders(), REPOS_ROOT)
        self.nugets_tab = FolderTab(self.notebook, get_nuget_folders(), NUGETS_ROOT)

        self.notebook.add(self.services_tab, text="Services")
        self.notebook.add(self.nugets_tab, text="Nugets")

    # -- Middle: vertical list of action buttons --------------------------- #
    def _build_middle_panel(self):
        middle = ttk.LabelFrame(self._top, text="Actions")
        middle.pack(side="left", fill="y", padx=6)

        # Register new repository actions here as (label, callback, hint).
        # The hint is shown as a hover tooltip on the button.
        actions = [
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
                "restored afterwards on a clean rebase.",
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
                "Creates a VS Code '.code-workspace' file in D:/Workspaces from "
                "the selected repositories, named after the feature name you "
                "enter.",
            ),
        ]
        for label, command, hint in actions:
            button = ttk.Button(middle, text=label, command=command)
            button.pack(fill="x", padx=6, pady=3)
            Tooltip(button, hint)

    # -- Right: live progress for the running action ----------------------- #
    def _build_right_panel(self):
        right = ttk.LabelFrame(self._top, text="Details")
        right.pack(side="left", fill="both", expand=True)

        self.progress = ProgressPanel(right)
        self.progress.pack(fill="both", expand=True, padx=4, pady=4)

    # -- Bottom: shared, full-width error list ----------------------------- #
    def _build_error_panel(self):
        errors = ttk.LabelFrame(self, text="Errors")
        errors.pack(side="bottom", fill="x")

        self.errors = ErrorList(errors)
        self.errors.pack(fill="x", expand=False, padx=4, pady=4)

    # -- Selection helpers ------------------------------------------------- #
    def _all_selected_repos(self):
        """Return (name, path) pairs for every checked repo across both tabs."""
        return (
            self.services_tab.get_selected_paths()
            + self.nugets_tab.get_selected_paths()
        )

    # -- Action handlers --------------------------------------------------- #
    def _action_checkout_pull_master(self):
        """Checkout 'master' and pull latest for all selected repositories."""
        repos = self._all_selected_repos()
        if not repos:
            return

        # Reset the shared UI areas for a fresh run.
        self.errors.clear()
        self.progress.set_repos([name for name, _ in repos])

        # Run git work off the UI thread so the window stays responsive.
        threading.Thread(
            target=self._run_checkout_pull_master, args=(repos,), daemon=True
        ).start()

    def _run_checkout_pull_master(self, repos):
        """Worker: perform checkout + pull for each repo, updating the UI live."""
        all_ok = True
        for name, path in repos:
            self.after(0, self.progress.status, name, "in-progress")

            ok, message = self._checkout_and_pull(name, path)
            if ok:
                self.after(0, self.progress.status, name, "done")
            else:
                all_ok = False
                self.after(0, self.progress.status, name, "error")
                self.after(0, self.errors.add, message)

        # Only celebrate when every repository succeeded.
        if all_ok:
            self.after(0, self.progress.show_completion,
                       "All repositories updated successfully.")

    def _checkout_and_pull(self, name, path):
        """Run checkout master + pull for one repo. Returns (ok, error_message).

        Handling of uncommitted changes:
          * On a non-master branch, local changes are committed as "savepos"
            so they are preserved before switching to master.
          * On master with local changes, the pull is unsafe, so the repo is
            skipped with an error (no commit, no checkout, no pull).
        """
        if not os.path.isdir(os.path.join(path, ".git")):
            return False, f"{name}: not a git repository"

        if git_has_changes(path):
            if git_current_branch(path) == "master":
                return False, (
                    f"{name} is already on master and has unsaved changes. "
                    f"cannot perform pull"
                )
            # Preserve work on the current branch before checking out master.
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
        """Rebase each selected repo's feature branch onto an updated master."""
        repos = self._all_selected_repos()
        if not repos:
            return

        # Reset shared UI areas for a fresh run.
        self.errors.clear()
        self.progress.set_repos([name for name, _ in repos])

        # Pre-scan: find repos with uncommitted changes. Nothing is processed
        # until the user decides how to handle them, so the whole batch waits.
        dirty = [name for name, path in repos
                 if os.path.isdir(os.path.join(path, ".git"))
                 and git_has_changes(path)]

        if dirty:
            if not self._ask_commit_or_abort(dirty):
                # User aborted: leave every repo untouched.
                self.progress.set_repos([])
                return

        # At this point uncommitted changes (if any) are approved for commit.
        threading.Thread(
            target=self._run_rebase_on_master, args=(repos,), daemon=True
        ).start()

    def _ask_commit_or_abort(self, repo_names):
        """Modal asking whether to commit & rebase or abort. Returns True=commit."""
        dialog = tk.Toplevel(self)
        dialog.title("Uncommitted changes")
        dialog.transient(self.winfo_toplevel())
        dialog.resizable(False, False)

        message = (
            "There are uncommitted changes in following repositories:\n"
            f"{', '.join(repo_names)}.\n\n"
            "Do you want to commit and rebase, or to abort operation?"
        )
        tk.Label(dialog, text=message, justify="left", wraplength=380).pack(
            padx=16, pady=12
        )

        # Holds the user's choice; default is abort (safe) if window is closed.
        choice = {"commit": False}

        def _commit():
            choice["commit"] = True
            dialog.destroy()

        def _abort():
            choice["commit"] = False
            dialog.destroy()

        button_bar = ttk.Frame(dialog)
        button_bar.pack(padx=16, pady=(0, 12))
        ttk.Button(button_bar, text="Commit and rebase", command=_commit).pack(
            side="left", padx=4
        )
        ttk.Button(button_bar, text="Abort operation", command=_abort).pack(
            side="left", padx=4
        )

        dialog.protocol("WM_DELETE_WINDOW", _abort)
        dialog.grab_set()              # make it modal
        self.wait_window(dialog)       # block until a choice is made
        return choice["commit"]

    def _run_rebase_on_master(self, repos):
        """Worker: rebase each repo onto master, updating the UI live."""
        all_ok = True
        for name, path in repos:
            self.after(0, self.progress.status, name, "in-progress")

            ok, message = self._rebase_one(name, path)
            if ok:
                self.after(0, self.progress.status, name, "done")
            else:
                all_ok = False
                self.after(0, self.progress.status, name, "error")
                self.after(0, self.errors.add, message)

        if all_ok:
            self.after(0, self.progress.show_completion,
                       "All repositories rebased successfully.")

    def _rebase_one(self, name, path):
        """Rebase one repo's feature branch onto master. Returns (ok, error_message).

        Flow: optionally commit local changes, update master, return to the
        feature branch and rebase. On a clean rebase any pre-rebase commit is
        undone so the changes return to their uncommitted state.
        """
        if not os.path.isdir(os.path.join(path, ".git")):
            return False, f"{name}: not a git repository"

        # A rebase already underway must be finished/aborted by hand.
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

        # Preserve uncommitted work so master can be checked out safely. The
        # commit is undone again after a clean rebase.
        committed = False
        if git_has_changes(path):
            ok, out = run_git(path, ["add", "-A"])
            if not ok:
                return False, f"{name}: {out}"
            ok, out = run_git(path, ["commit", "-m", "save changes before rebase"])
            if not ok:
                return False, f"{name}: {out}"
            committed = True

        # Update master.
        ok, out = run_git(path, ["checkout", "master"])
        if not ok:
            return False, f"{name}: {out}"
        ok, out = run_git(path, ["pull"])
        if not ok:
            return False, f"{name}: {out}"

        # Back to the feature branch and rebase it onto the fresh master.
        ok, out = run_git(path, ["checkout", branch])
        if not ok:
            return False, f"{name}: {out}"

        ok, out = run_git(path, ["rebase", "master"])
        if not ok:
            # Conflicts (or any rebase failure) need a human; leave the repo
            # mid-rebase for review and keep processing the other repos.
            return False, (
                f"{name}: rebase could not complete automatically. manual "
                f"rebase review is needed.\n{out}"
            )

        # Clean rebase: drop the temporary commit so changes go back to being
        # uncommitted (only our "save changes before rebase" commit, never the
        # user's own commits).
        if committed:
            ok, out = run_git(path, ["reset", "HEAD~1"])
            if not ok:
                return False, f"{name}: {out}"

        return True, ""

    # -- Create feature branch --------------------------------------------- #
    def _action_create_feature_branch(self):
        """Create a new feature branch (off updated master) for each selected repo."""
        repos = self._all_selected_repos()
        if not repos:
            return

        # Pre-scan: for each dirty repo, ask (one modal per repo) what to do with
        # its uncommitted changes. Closing any modal aborts the whole operation,
        # so nothing is processed until every decision is made.
        decisions = {}  # repo name -> "delete" | "commit" | "move"
        for name, path in repos:
            if not os.path.isdir(os.path.join(path, ".git")):
                continue
            if git_has_changes(path):
                on_master = git_current_branch(path) == "master"
                decision = self._ask_change_decision(name, on_master)
                if decision is None:
                    return  # user aborted
                decisions[name] = decision

        # Ask for the (required) feature branch name; "feature/" is fixed.
        branch_name = self._ask_branch_name()
        if not branch_name:
            return

        self.errors.clear()
        self.progress.set_repos([name for name, _ in repos])
        threading.Thread(
            target=self._run_create_feature,
            args=(repos, branch_name, decisions), daemon=True,
        ).start()

    def _ask_change_decision(self, name, on_master):
        """Per-repo modal for handling uncommitted changes.

        Returns "delete", "commit" or "move"; None if the user closes the modal.
        Committing is not offered when the repo is on master.
        """
        dialog = tk.Toplevel(self)
        dialog.title(f"Uncommitted changes - {name}")
        dialog.transient(self.winfo_toplevel())
        dialog.resizable(False, False)

        message = (
            f"Repository '{name}' has uncommitted changes.\n"
            "What would you like to do with them?"
        )
        if on_master:
            message += "\n\n(On master, committing is not allowed.)"
        tk.Label(dialog, text=message, justify="left", wraplength=400).pack(
            padx=16, pady=12
        )

        choice = {"value": None}

        def _set(value):
            choice["value"] = value
            dialog.destroy()

        bar = ttk.Frame(dialog)
        bar.pack(padx=16, pady=(0, 12))
        ttk.Button(bar, text="Delete changes",
                   command=lambda: _set("delete")).pack(side="left", padx=4)
        if not on_master:
            ttk.Button(bar, text="Commit changes",
                       command=lambda: _set("commit")).pack(side="left", padx=4)
        ttk.Button(bar, text="Move to new feature branch",
                   command=lambda: _set("move")).pack(side="left", padx=4)

        dialog.protocol("WM_DELETE_WINDOW", lambda: _set(None))
        dialog.grab_set()
        self.wait_window(dialog)
        return choice["value"]

    def _ask_branch_name(self, title="Create feature branch", prefix="feature/"):
        """Modal with a fixed *prefix* label and a required name field.

        Returns the entered name (without prefix), or None if cancelled.
        """
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self.winfo_toplevel())
        dialog.resizable(False, False)

        row = ttk.Frame(dialog)
        row.pack(padx=16, pady=(16, 4))
        ttk.Label(row, text=prefix).pack(side="left")
        entry = ttk.Entry(row, width=30)
        entry.pack(side="left")
        entry.focus_set()

        error_label = tk.Label(dialog, text="", foreground="#c0392b")
        error_label.pack(padx=16, anchor="w")

        result = {"name": None}

        def _ok():
            name = entry.get().strip()
            if not name:
                error_label.config(text="Branch name is required.")
                return
            result["name"] = name
            dialog.destroy()

        def _cancel():
            result["name"] = None
            dialog.destroy()

        bar = ttk.Frame(dialog)
        bar.pack(padx=16, pady=12)
        ttk.Button(bar, text="OK", command=_ok).pack(side="left", padx=4)
        ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

        entry.bind("<Return>", lambda _e: _ok())
        dialog.protocol("WM_DELETE_WINDOW", _cancel)
        dialog.grab_set()
        self.wait_window(dialog)
        return result["name"]

    def _run_create_feature(self, repos, branch_name, decisions):
        """Worker: create the feature branch for each repo, updating the UI live."""
        all_ok = True
        for name, path in repos:
            self.after(0, self.progress.status, name, "in-progress")

            ok, message = self._create_feature_one(
                name, path, branch_name, decisions.get(name)
            )
            if ok:
                self.after(0, self.progress.status, name, "done")
            else:
                all_ok = False
                self.after(0, self.progress.status, name, "error")
                self.after(0, self.errors.add, message)

        if all_ok:
            self.after(0, self.progress.show_completion,
                       "All feature branches created successfully.")

    def _create_feature_one(self, name, path, branch_name, decision):
        """Create one repo's feature branch. Returns (ok, error_message).

        *decision* (only set for dirty repos) controls how uncommitted changes
        are handled: "delete", "commit" (savepos on the current branch) or
        "move" (carried onto the new branch via a stash).
        """
        if not os.path.isdir(os.path.join(path, ".git")):
            return False, f"{name}: not a git repository"

        new_branch = f"feature/{branch_name}"

        # Move: stash everything (including untracked), branch off master, then
        # re-apply. Conflicts are surfaced without aborting other repos.
        if decision == "move":
            ok, out = run_git(
                path, ["stash", "push", "-u", "-m", "move to feature branch"]
            )
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
                    f"{name}: changes were moved but applying them caused "
                    f"conflicts. manual resolution needed.\n{out}"
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

        # Commit: keep the changes as a savepos commit on the current branch.
        elif decision == "commit":
            ok, out = run_git(path, ["add", "-A"])
            if not ok:
                return False, f"{name}: {out}"
            ok, out = run_git(path, ["commit", "-m", "savepos"])
            if not ok:
                return False, f"{name}: {out}"

        # Update master and branch off it.
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

    # -- Commit all changes as savepos ------------------------------------- #
    def _action_commit_savepos(self):
        """Commit uncommitted changes as 'savepos' for each selected repo."""
        repos = self._all_selected_repos()
        if not repos:
            return

        self.errors.clear()
        self.progress.set_repos([name for name, _ in repos])
        threading.Thread(
            target=self._run_commit_savepos, args=(repos,), daemon=True
        ).start()

    def _run_commit_savepos(self, repos):
        """Worker: commit savepos for each repo, updating the UI live."""
        all_ok = True
        for name, path in repos:
            self.after(0, self.progress.status, name, "in-progress")

            ok, message = self._commit_savepos_one(name, path)
            if ok:
                self.after(0, self.progress.status, name, "done")
            else:
                all_ok = False
                self.after(0, self.progress.status, name, "error")
                self.after(0, self.errors.add, message)

        if all_ok:
            self.after(0, self.progress.show_completion,
                       "All changes committed successfully.")

    def _commit_savepos_one(self, name, path):
        """Commit one repo's changes as 'savepos'. Returns (ok, error_message).

        Repos on master are rejected; repos with nothing to commit are an error
        so the user can see which ones had no changes.
        """
        if not os.path.isdir(os.path.join(path, ".git")):
            return False, f"{name}: not a git repository"

        if git_current_branch(path) == "master":
            return False, (
                f"{name} is on master. cannot commit changes on master"
            )

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
        """Create a VS Code workspace file from the selected repositories."""
        repos = self._all_selected_repos()
        if not repos:
            return

        feature_name = self._ask_branch_name(
            title="Create feature workspace", prefix="feature/"
        )
        if not feature_name:
            return

        self.errors.clear()
        self.progress.set_repos([name for name, _ in repos])

        ok, message = self._write_workspace(feature_name, repos)
        if ok:
            for name, _ in repos:
                self.progress.status(name, "done")
            self.progress.show_completion(message)
        else:
            self.errors.add(message)

    def _write_workspace(self, feature_name, repos):
        """Write the .code-workspace file. Returns (ok, message).

        Folder paths are stored relative to WORKSPACES_ROOT (e.g.
        "../Repositories/<repo>") to match the existing workspace files.
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


def main():
    root = tk.Tk()
    root.title("Feature Manager")
    root.geometry("900x600")
    FeatureManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
