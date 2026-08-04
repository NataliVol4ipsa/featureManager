"""Feature Manager - entry point.

Run with:  python featureManager.py

The application is split across sibling modules (no build step needed - they
are imported directly). This file only wires the two top-level tabs together:

  * "Workspaces"   - switch every repo of a feature workspace at once
                     (modules: workspaces_tab).
  * "Repositories" - per-repo git actions driven by Services/Nugets checkbox
                     lists (modules: manual_tab, widgets, dialogs).

Shared building blocks live in: config, gitutils, widgets, tab_base, dialogs.
"""

import os
import sys
import tkinter as tk
from tkinter import ttk

from manual_tab import ManualTab
from workspaces_tab import WorkspacesTab
from dialogs import edit_synonyms, ask_pipeline_poll_seconds
import theme


class FeatureManagerApp(ttk.Notebook):
    """Top-level notebook holding the Workspaces and Repositories tabs."""

    def __init__(self, master):
        super().__init__(master, padding=6)
        self.pack(fill="both", expand=True)

        self.workspaces_tab = WorkspacesTab(self)
        self.manual_tab = ManualTab(self)
        self.add(self.workspaces_tab, text="Workspaces")
        self.add(self.manual_tab, text="Repositories")

        # Open on the Workspaces tab.
        self.select(self.workspaces_tab)

        # Refresh the workspace list every time the Workspaces tab is opened, so
        # newly created/modified workspaces show up without a manual refresh.
        self.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _on_tab_changed(self, _event=None):
        if self.nametowidget(self.select()) is self.workspaces_tab:
            self.workspaces_tab.refresh()


def main():
    root = tk.Tk()
    root.title("Feature Manager")
    root.geometry("1160x740")
    theme.apply_window_icon(root)

    # Theme (dark or light). Persisted preference; must run before any widgets.
    theme.apply_theme(root)

    # Custom menu bar. The native Windows menu bar paints its empty strip with
    # the system brush, and a native popup menu draws a white window frame -
    # neither is themeable. So both the bar and its dropdown are hand-built.
    menubar = tk.Frame(root, background=theme.BG_PANEL)
    menubar.pack(side="top", fill="x")
    settings_item = tk.Label(menubar, text="Settings", padx=10, pady=3,
                             background=theme.BG_PANEL, foreground=theme.FG)
    settings_item.pack(side="left")

    def _toggle_theme():
        theme.save_dark_preference(not theme.load_dark_preference())
        # Re-launch so every widget is rebuilt cleanly with the new palette.
        os.execv(sys.executable, [sys.executable, *sys.argv])

    def _set_pipeline_poll_seconds():
        current = theme.load_pipeline_poll_seconds()
        value = ask_pipeline_poll_seconds(
            root,
            current,
            theme.PIPELINE_POLL_MIN_SECONDS,
            theme.PIPELINE_POLL_MAX_SECONDS,
        )
        if value is not None:
            theme.save_pipeline_poll_seconds(value)

    def _settings_entries():
        return [
            ("Repository synonyms\u2026", lambda: edit_synonyms(root), False),
            (
                f"Pipeline monitor polling ({theme.load_pipeline_poll_seconds()}s)\u2026",
                _set_pipeline_poll_seconds,
                False,
            ),
            ("Dark theme", _toggle_theme, theme.load_dark_preference()),
        ]

    def _post_settings(_event=None):
        popup = tk.Toplevel(root)
        popup.overrideredirect(True)  # no OS title bar / border
        popup.configure(background=theme.BORDER)  # shows as a 1px border
        popup.geometry(
            f"+{settings_item.winfo_rootx()}"
            f"+{settings_item.winfo_rooty() + settings_item.winfo_height()}"
        )
        inner = tk.Frame(popup, background=theme.BG_PANEL)
        inner.pack(padx=1, pady=1)

        def _dismiss(_e=None):
            if popup.winfo_exists():
                popup.destroy()

        for label, command, checked in _settings_entries():
            entry = tk.Label(
                inner, text=("\u2713  " if checked else "     ") + label,
                anchor="w", background=theme.BG_PANEL, foreground=theme.FG,
                padx=12, pady=5,
            )
            entry.pack(fill="x")
            entry.bind("<Enter>",
                       lambda _e, w=entry: w.config(background=theme.ACCENT))
            entry.bind("<Leave>",
                       lambda _e, w=entry: w.config(background=theme.BG_PANEL))
            entry.bind("<Button-1>",
                       lambda _e, c=command: (_dismiss(), c()))

        # A click anywhere else (grabbed) or losing focus closes the menu.
        popup.bind("<Button-1>", _dismiss)
        inner.bind("<Button-1>", _dismiss)
        popup.bind("<Escape>", _dismiss)
        popup.bind("<FocusOut>", _dismiss)
        popup.grab_set()
        popup.focus_set()

    settings_item.bind("<Button-1>", _post_settings)
    settings_item.bind(
        "<Enter>", lambda _e: settings_item.config(background=theme.BG_RAISED)
    )
    settings_item.bind(
        "<Leave>", lambda _e: settings_item.config(background=theme.BG_PANEL)
    )

    FeatureManagerApp(root)

    # Author credit footer - always visible at the bottom of the window.
    footer = ttk.Label(
        root,
        text="Feature Manager  -  by Nataliia Kolosova",
        anchor="e",
        padding=(8, 2),
        foreground=theme.FG_MUTED,
    )
    footer.pack(side="bottom", fill="x")

    # Dark Windows title bar (matches Explorer's dark header). Applied after the
    # window is mapped so it has a real HWND to set the DWM attribute on.
    root.after(0, lambda: theme.apply_window_icon(root))
    root.after(0, lambda: theme.enable_dark_titlebar(root))

    root.mainloop()


if __name__ == "__main__":
    main()
