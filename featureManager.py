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
from dialogs import (
    edit_synonyms, edit_pipeline_ids, edit_ado_identity_cache,
    edit_nuget_feed_cache, ask_pipeline_poll_seconds, confirm_force_close,
    show_report, ask_repositories_to_cleanup,
)
from toolbar import build_action_toolbar
import deployment_status
import gitutils
import packages
import pipeline_estimates
import pipeline_history
import theme


def _reload_persisted_pat():
    """Refresh ADO_PAT from the persisted Windows environment.

    ``setx`` writes the token to the registry, but a running process keeps the
    value it started with - so an in-app Restart (``os.execv`` reuses the current
    environment) would otherwise never see a newly set or rotated PAT. Machine
    scope is read first so a User-scoped value takes precedence.
    """
    try:
        import winreg
    except ImportError:
        return
    for root_key, sub in (
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(root_key, sub) as key:
                value, _ = winreg.QueryValueEx(key, "ADO_PAT")
        except OSError:
            continue
        if value:
            os.environ["ADO_PAT"] = value


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
    theme.enable_dpi_awareness()  # before Tk() - fixes popdown/geometry glitches
    root = tk.Tk()
    root.title("Feature Manager")
    root.geometry("1160x740")
    # After a relaunch, reopen at the previous position/size instead of a
    # window-manager-chosen (random) spot.
    _restart_geometry = theme.pop_restart_geometry()
    if _restart_geometry:
        root.geometry(_restart_geometry)
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

    # "Pipeline history" opens a read-only viewer of recorded monitor sessions.
    history_item = tk.Label(menubar, text="Pipeline history", padx=10, pady=3,
                            background=theme.BG_PANEL, foreground=theme.FG)
    history_item.pack(side="left")

    # "Tools" opens a dropdown of one-off utilities (e.g. generate a clone
    # script for every local repository).
    tools_item = tk.Label(menubar, text="Tools", padx=10, pady=3,
                          background=theme.BG_PANEL, foreground=theme.FG)
    tools_item.pack(side="left")

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
        theme.save_deployment_status_session(
            [win.session_state() for win in deployment_status.open_windows()]
        )
        # Remember where the window is so the restarted app reopens in place.
        theme.save_restart_geometry(root.geometry())
        # Reopen the pipeline history window too if it is currently open.
        history_win = getattr(app, "_history_window", None)
        history_open = bool(history_win is not None and history_win.winfo_exists())
        theme.save_history_window_open(
            history_open, history_win.geometry() if history_open else ""
        )
        # Pick up a PAT set via setx after this process started (execv keeps the
        # current environment, so re-read the persisted value first).
        _reload_persisted_pat()
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
                target=lambda: pipeline_estimates.refresh_all(
                    log=_log_estimate, error_log=_log_estimate_error,
                ),
                daemon=True,
            ).start()
        else:
            _log_estimate(pipeline_estimates.status_message())

    def _toggle_pipeline_monitor_compact():
        theme.save_pipeline_monitor_compact(
            not theme.load_pipeline_monitor_compact()
        )

    def _settings_entries():
        # A nested entry is (label, submenu_list, False); the popup drills into
        # the list in place. The cache editors are grouped under "Caches".
        cache_items = [
            ("Pipeline id cache\u2026", lambda: edit_pipeline_ids(root), False),
            ("ADO identity cache\u2026", lambda: edit_ado_identity_cache(root), False),
            ("NuGet feed cache\u2026", lambda: edit_nuget_feed_cache(root), False),
        ]
        top = [
            ("Repository synonyms\u2026", lambda: edit_synonyms(root), False),
            ("Caches", None, False),  # replaced with its submenu below
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
            (
                "Compact pipeline monitor by default",
                _toggle_pipeline_monitor_compact,
                theme.load_pipeline_monitor_compact(),
            ),
            ("Dark theme", _toggle_theme, theme.load_dark_preference()),
        ]
        # "\u2039 Back" is itself a nested entry whose submenu is the top menu.
        cache_menu = [("\u2039  Back", top, False)] + cache_items
        top[1] = ("Caches", cache_menu, False)
        return top

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

        def _render(entries):
            for child in inner.winfo_children():
                child.destroy()
            for label, command, checked in entries:
                submenu = command if isinstance(command, list) else None
                # Right chevron for a drill-in, but not for "\u2039 Back".
                arrow = (" \u203a" if submenu is not None
                         and not label.lstrip().startswith("\u2039") else "")
                entry = tk.Label(
                    inner, text=("\u2713  " if checked else "     ") + label + arrow,
                    anchor="w", background=theme.BG_PANEL, foreground=theme.FG,
                    padx=12, pady=5,
                )
                entry.pack(fill="x")
                entry.bind("<Enter>",
                           lambda _e, w=entry: w.config(background=theme.ACCENT))
                entry.bind("<Leave>",
                           lambda _e, w=entry: w.config(background=theme.BG_PANEL))
                if submenu is not None:
                    entry.bind("<Button-1>",
                               lambda _e, sub=submenu: _render(sub))
                else:
                    entry.bind("<Button-1>",
                               lambda _e, c=command: (_dismiss(), c()))
            popup.update_idletasks()  # re-fit the borderless window to content

        _render(_settings_entries())

        # A click anywhere else (grabbed) or losing focus closes the menu.
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

    history_item.bind(
        "<Button-1>",
        lambda _e: _open_history(),
    )
    history_item.bind(
        "<Enter>", lambda _e: history_item.config(background=theme.BG_RAISED)
    )
    history_item.bind(
        "<Leave>", lambda _e: history_item.config(background=theme.BG_PANEL)
    )

    def _generate_clone_script():
        """Build the clone-all script off the UI thread, then show it."""
        _log_estimate("Generating clone script for all local repositories\u2026")

        def _worker():
            script = gitutils.generate_clone_script()
            root.after(
                0,
                lambda: show_report(app, script, title="Clone all repositories"),
            )

        threading.Thread(target=_worker, daemon=True).start()

    def _cleanup_repositories():
        """Wipe bin/obj folders of the repositories the user selects."""
        selected = ask_repositories_to_cleanup(app)
        if not selected:
            return
        _log_estimate(
            f"Cleaning bin/obj folders of {len(selected)} repositor"
            f"{'y' if len(selected) == 1 else 'ies'}\u2026"
        )

        def _worker():
            lines = []
            total_removed = 0
            for name, path in selected:
                removed, errors = gitutils.cleanup_bin_obj(path)
                total_removed += removed
                if errors:
                    lines.append(
                        f"{name}: removed {removed} folder(s), "
                        f"{len(errors)} error(s)"
                    )
                    lines.extend(f"    ! {folder}: {reason}"
                                 for folder, reason in errors)
                else:
                    lines.append(f"{name}: removed {removed} folder(s)")
            summary = (
                f"Deleted {total_removed} bin/obj folder(s) across "
                f"{len(selected)} repositor"
                f"{'y' if len(selected) == 1 else 'ies'}.\n\n"
            )
            report = summary + "\n".join(lines) + "\n"
            root.after(
                0,
                lambda: show_report(app, report, title="Cleanup repositories"),
            )

        threading.Thread(target=_worker, daemon=True).start()

    def _tools_entries():
        # Flat list of (label, command) utilities. Add new tools here.
        return [
            ("Generate clone script\u2026", _generate_clone_script),
            ("Cleanup repositories\u2026", _cleanup_repositories),
        ]

    def _post_tools(_event=None):
        popup = tk.Toplevel(root)
        popup.overrideredirect(True)  # no OS title bar / border
        popup.configure(background=theme.BORDER)  # shows as a 1px border
        popup.geometry(
            f"+{tools_item.winfo_rootx()}"
            f"+{tools_item.winfo_rooty() + tools_item.winfo_height()}"
        )
        inner = tk.Frame(popup, background=theme.BG_PANEL)
        inner.pack(padx=1, pady=1)

        def _dismiss(_e=None):
            if popup.winfo_exists():
                popup.destroy()

        for label, command in _tools_entries():
            entry = tk.Label(
                inner, text="     " + label, anchor="w",
                background=theme.BG_PANEL, foreground=theme.FG, padx=12, pady=5,
            )
            entry.pack(fill="x")
            entry.bind("<Enter>",
                       lambda _e, w=entry: w.config(background=theme.ACCENT))
            entry.bind("<Leave>",
                       lambda _e, w=entry: w.config(background=theme.BG_PANEL))
            entry.bind("<Button-1>",
                       lambda _e, c=command: (_dismiss(), c()))
        popup.update_idletasks()  # re-fit the borderless window to content

        # A click anywhere else (grabbed) or losing focus closes the menu.
        popup.bind("<Escape>", _dismiss)
        popup.bind("<FocusOut>", _dismiss)
        popup.grab_set()
        popup.focus_set()

    tools_item.bind("<Button-1>", _post_tools)
    tools_item.bind(
        "<Enter>", lambda _e: tools_item.config(background=theme.BG_RAISED)
    )
    tools_item.bind(
        "<Leave>", lambda _e: tools_item.config(background=theme.BG_PANEL)
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

    def _open_history(geometry=None):
        """Open the single pipeline history window (focus it if already open)."""
        existing = getattr(app, "_history_window", None)
        if existing is not None and existing.winfo_exists():
            win = existing
        else:
            win = pipeline_history.PipelineHistoryWindow(root, app)
            app._history_window = win
        # Restore the previous position/size after a restart.
        if geometry:
            win.geometry(geometry)
        # Raise above the main window (which grabs focus on a restart).
        win.deiconify()
        win.lift()
        win.attributes("-topmost", True)
        win.after(
            400,
            lambda: win.winfo_exists() and win.attributes("-topmost", False),
        )
        win.focus_force()

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

    def _log_estimate_error(message):
        """Append a cache-refresh failure to the active tab's Errors area."""
        def _append():
            active = app.nametowidget(app.select())
            panel = getattr(active, "errors", None)
            if panel is not None:
                panel.add(message)
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
            target=lambda: pipeline_estimates.refresh_all(
                log=_log_estimate, error_log=_log_estimate_error,
            ),
            daemon=True,
        ).start()

    # Reopen any pipeline monitors that were open before a theme-change relaunch.
    root.after(0, lambda: [
        app.workspaces_tab.reopen_monitor_session(session)
        for session in theme.pop_monitor_session()
    ])

    # Reopen any "View deployment status" windows that were open before a
    # theme-change relaunch (parent just needs to be some live widget under
    # root - the window reparents to the toplevel regardless of which tab).
    root.after(0, lambda: [
        deployment_status.reopen_session(app.workspaces_tab, session)
        for session in theme.pop_deployment_status_session()
    ])

    # Reopen the pipeline history window too if it was open at relaunch. Delayed
    # so it lands after the main window's focus grab and ends up on top.
    _history_geometry = theme.pop_history_window_open()
    if _history_geometry is not None:
        root.after(350, lambda: _open_history(_history_geometry))

    root.mainloop()


if __name__ == "__main__":
    main()
