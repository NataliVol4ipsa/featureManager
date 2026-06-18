"""Shared base class for the action tabs (Manual and Workspaces).

Provides the common three-column layout pieces (Actions / Details / Errors)
and a generic background runner that drives the per-repo status indicators.
"""

import threading
from tkinter import ttk

from widgets import Tooltip, ProgressPanel, ErrorList


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

    # -- Generic background runner ----------------------------------------- #
    def run_repo_action(self, repos, per_repo_fn, success_msg, on_complete=None):
        """Run *per_repo_fn(name, path)* for each repo off the UI thread.

        *per_repo_fn* must return (ok, error_message). Status dots and the error
        list update live; the green banner shows only when every repo succeeds.
        *on_complete(all_ok)*, if given, runs on the UI thread afterwards (used
        to chain a follow-up step such as writing a workspace file).
        """
        self.errors.clear()
        self.progress.set_repos([name for name, _ in repos])
        threading.Thread(
            target=self._worker, args=(repos, per_repo_fn, success_msg, on_complete),
            daemon=True,
        ).start()

    def _worker(self, repos, per_repo_fn, success_msg, on_complete=None):
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

        if all_ok and success_msg:
            self.after(0, self.progress.show_completion, success_msg)
        if on_complete is not None:
            self.after(0, on_complete, all_ok)
