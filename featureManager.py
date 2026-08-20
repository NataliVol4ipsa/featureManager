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
import threading
import tkinter as tk
from tkinter import ttk

from manual_tab import ManualTab
from workspaces_tab import WorkspacesTab
from dialogs import edit_synonyms, ask_pipeline_poll_seconds, confirm_force_close
from toolbar import build_action_toolbar
import packages
import pipeline_estimates
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

    # Text button on the far right that re-execs the process (picks up code
    # changes). Packed into the existing menu bar so nothing else shifts.
    restart_item = tk.Label(menubar, text="Restart", padx=10, pady=3,
                            background=theme.BG_PANEL, foreground=theme.FG)
    restart_item.pack(side="right")

    def _relaunch():
        """Re-exec the Python process, preserving any open pipeline monitors."""
        sessions = []
        for tab in (app.workspaces_tab, app.manual_tab):
            for win in getattr(tab, "_pipeline_monitors", []):
                if win.winfo_exists():
                    sessions.append(win.session_state())
        theme.save_monitor_session(sessions)
        os.execv(sys.executable, [sys.executable, *sys.argv])

    def _toggle_theme():
        theme.save_dark_preference(not theme.load_dark_preference())
        # Re-launch so every widget is rebuilt cleanly with the new palette.
        _relaunch()

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

    def _toggle_pipeline_estimates():
        enabled = not theme.load_pipeline_estimates_enabled()
        theme.save_pipeline_estimates_enabled(enabled)
        if not enabled:
            return
        # Turning it on: refresh now only when the cache is missing/expired;
        # a still-valid cache is kept as-is and we just report the next refresh.
        if pipeline_estimates.needs_refresh():
            threading.Thread(
                target=lambda: pipeline_estimates.refresh_all(log=_log_estimate),
                daemon=True,
            ).start()
        else:
            _log_estimate(pipeline_estimates.status_message())

    def _settings_entries():
        return [
            ("Repository synonyms\u2026", lambda: edit_synonyms(root), False),
            (
                f"Pipeline monitor polling ({theme.load_pipeline_poll_seconds()}s)\u2026",
                _set_pipeline_poll_seconds,
                False,
            ),
            (
                "Estimate pipeline time left",
                _toggle_pipeline_estimates,
                theme.load_pipeline_estimates_enabled(),
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

    restart_item.bind("<Button-1>", lambda _e: _relaunch())
    restart_item.bind(
        "<Enter>", lambda _e: restart_item.config(background=theme.BG_RAISED)
    )
    restart_item.bind(
        "<Leave>", lambda _e: restart_item.config(background=theme.BG_PANEL)
    )

    # Top action toolbar: mirrors the active tab's action buttons as icons.
    # Packed before the notebook so it sits under the menu bar; populated once
    # the tabs exist and rebuilt whenever the active tab changes.
    toolbar_host = tk.Frame(root, background=theme.BG_PANEL)
    toolbar_host.pack(side="top", fill="x")
    tk.Frame(root, height=1, background=theme.BORDER).pack(side="top", fill="x")

    app = FeatureManagerApp(root)

    def _rebuild_toolbar(_event=None):
        active = app.nametowidget(app.select())
        build_action_toolbar(toolbar_host, getattr(active, "action_sections", []))

    _rebuild_toolbar()
    app.bind("<<NotebookTabChanged>>", _rebuild_toolbar, add="+")

    def _log_estimate(message):
        """Append a gray info line to the active tab's log (thread-safe)."""
        def _append():
            active = app.nametowidget(app.select())
            panel = getattr(active, "errors", None)
            if panel is not None:
                panel.add(message, info=True)
        root.after(0, _append)

    def _on_close():
        open_monitors = [
            win
            for tab in (app.workspaces_tab, app.manual_tab)
            for win in getattr(tab, "_pipeline_monitors", [])
            if win.winfo_exists()
        ]
        if open_monitors and not confirm_force_close(root, len(open_monitors)):
            return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)

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

    # Pull the window to the front and give it focus - a re-exec'd process
    # (Restart / theme toggle) otherwise starts behind the terminal.
    def _grab_focus():
        root.lift()
        root.attributes("-topmost", True)
        root.after(200, lambda: root.attributes("-topmost", False))
        root.focus_force()

    root.after(0, _grab_focus)

    # Warm the Azure DevOps token cache in the background so the first
    # bump/restore does not pay the slow ``az`` cold-start latency.
    packages.prewarm_azure_devops_token()

    # When enabled, refresh the cached pipeline time-left estimates in the
    # background (only stale/missing repos actually hit Azure DevOps).
    if theme.load_pipeline_estimates_enabled():
        threading.Thread(
            target=lambda: pipeline_estimates.refresh_all(log=_log_estimate),
            daemon=True,
        ).start()

    # Reopen any pipeline monitors that were open before a theme-change relaunch.
    root.after(0, lambda: [
        app.workspaces_tab.reopen_monitor_session(session)
        for session in theme.pop_monitor_session()
    ])

    root.mainloop()


if __name__ == "__main__":
    main()
