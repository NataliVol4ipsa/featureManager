"""Floating pipeline monitor window.

Shows per-repository stage progress for started pipeline runs and refreshes
statuses on a configurable interval.
"""

import threading
import datetime
import re
import tkinter as tk
import webbrowser
from tkinter import ttk

import theme
import icons
from widgets import Tooltip
import pipeline_history
from parallel import run_in_parallel
from pipelines import (
    get_pipeline_stage_statuses,
    rerun_failed_stage,
    rerun_pipeline_from_latest_commit,
)
import pipeline_estimates


# Amber "previous run" marker (Lucide undo-2), matching the toolbar icon style.
# Cached module-level so Tk doesn't garbage-collect the PhotoImage.
_previous_run_img = None


def _previous_run_icon():
    global _previous_run_img
    if _previous_run_img is None:
        table = (icons.MISC_ICON_DARK if theme.load_dark_preference()
                 else icons.MISC_ICON_LIGHT)
        _previous_run_img = tk.PhotoImage(data=table["previous-run"])
    return _previous_run_img


_STAGE_ORDER = [
    ("build", "Build"),
    ("development", "Development"),
    ("acceptance", "Acceptance"),
    ("production", "Production"),
]

# Row geometry for the two view modes. Compact drops the per-circle status and
# environment labels and packs the rows closer together while keeping columns
# (and therefore circle x-positions) unchanged.
_GRAPH_HEIGHT_FULL = 62
_GRAPH_HEIGHT_COMPACT = 26
_ROW_MINSIZE_FULL = 58
_ROW_MINSIZE_COMPACT = 24
_CIRCLE_Y_FULL = 25
_CIRCLE_Y_COMPACT = 13

_STAGE_STYLE = {
    "waiting": {
        "fill": theme.BG_INPUT,
        "outline": theme.BORDER,
        "text": theme.FG_MUTED,
        "label": "waiting",
    },
    "skipped": {
        "fill": theme.BG_PANEL,
        "outline": theme.BORDER,
        "text": theme.FG_MUTED,
        "label": "skipped",
    },
    "running": {
        "fill": theme.ACCENT_HOVER,
        "outline": theme.ACCENT_HOVER,
        "text": theme.FG,
        "label": "running",
    },
    "ready": {
        "fill": theme.READY,
        "outline": theme.READY,
        "text": theme.FG,
        "label": "ready",
    },
    "approval": {
        "fill": theme.WARNING,
        "outline": theme.WARNING,
        "text": theme.FG,
        "label": "approval",
    },
    "failed": {
        "fill": theme.ERROR,
        "outline": theme.ERROR,
        "text": theme.FG,
        "label": "failed",
    },
    "canceled": {
        "fill": theme.FG_MUTED,
        "outline": theme.FG_MUTED,
        "text": theme.FG,
        "label": "canceled",
    },
    "done": {
        "fill": theme.SUCCESS,
        "outline": theme.SUCCESS,
        "text": theme.FG,
        "label": "done",
    },
}


def _format_local_time(value):
    """Return a local HH:MM:SS display string from an ISO timestamp."""
    if not value:
        return ""
    try:
        iso = value
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%H:%M:%S")
    except ValueError:
        # Fallback for unexpected formats.
        if "T" in value:
            value = value.split("T", 1)[1]
        for marker in ("+", "-"):
            if marker in value:
                value = value.split(marker, 1)[0]
        return value[:8]


class PipelineMonitorWindow(tk.Toplevel):
    """Always-on-top floating monitor for pipeline stage statuses."""

    def __init__(self, parent, run_infos, show_autoapprove_controls=False,
                 pbi_title="", test_reports=None, show_prod_control=True,
                 release_message=True, restore_geometry=None):
        super().__init__(parent.winfo_toplevel())
        self.title("Pipeline monitor")
        # Actual size is fitted to the content once the UI is built; this is only
        # a placeholder to avoid a visible flash before that runs.
        self.geometry("760x250")
        # When reopened after a relaunch, restore the previous position/size so
        # the window does not jump to a random spot; applied early to avoid a
        # flash and re-asserted after the content fit.
        self._restore_geometry = restore_geometry or None
        if self._restore_geometry:
            self.geometry(self._clamp_geometry(self._restore_geometry))
        self.minsize(200, 80)
        self.attributes("-topmost", True)
        self.configure(background=theme.BG)
        theme.apply_window_icon(self)
        theme.enable_dark_titlebar(self)

        self._closed = False
        self._poll_in_progress = False
        self._next_poll_token = None
        # Time-left estimates are hidden until the first live poll has run, so a
        # freshly opened monitor never shows a guess before real stage data.
        self._first_poll_done = False
        # Per-poll aggregation, filled as each repo's result arrives so the
        # window title/summary can be finalised once all repos are done.
        self._poll_any_error = False
        self._poll_latest_timestamp = ""
        self._poll_autoapproved = []
        self._poll_autoapprove_errors = []
        self._run_infos = dict(run_infos)
        # Owning tab (has the Errors panel); used to report poll errors.
        self._tab = parent
        # Last reported poll error per repo, so the same error is not repeated.
        self._reported_poll_errors = {}
        self._show_autoapprove_controls = bool(show_autoapprove_controls)
        self._show_prod_control = bool(show_prod_control)
        self._release_message = bool(release_message)
        self._estimates_enabled = bool(theme.load_pipeline_estimates_enabled())
        self._compact = bool(theme.load_pipeline_monitor_compact())
        self._pbi_title = (pbi_title or "").strip()
        self._test_reports = list(test_reports or [])
        self._rows = {}
        # History session id (set by the tab after construction) so a "Run new"
        # can be grouped into the same recorded pipeline-history session.
        self.history_session_id = None
        self._scrollbar_visible = True
        self._pan_anchor = None
        self._progress_tip = None
        self._progress_tip_target = None
        self._acc_locked_by_master = False
        self._prod_locked_by_master = False
        self._autoapprove_acceptance = any(
            bool(info.get("autoapprove_acceptance")) or (
                info.get("environment") == "acc"
                and bool(info.get("autoapprove_acc"))
            )
            for info in self._run_infos.values()
        )
        self._autoapprove_production = any(
            bool(info.get("autoapprove_production"))
            for info in self._run_infos.values()
        )

        self._build_ui()
        self._apply_autoapprove_flags()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after_idle(self._fit_to_content)
        self.after(150, self._poll_once)
        # Live-update the estimated time-left column once a second (no network).
        if self._estimates_enabled:
            self.after(1000, self._tick_estimates)

    def _clamp_geometry(self, geometry):
        """Keep a restored 'WxH+X+Y' geometry within the visible screen.

        A saved position can land off-screen (e.g. the window was on a second
        display that is now gone); clamp the offset so it always opens on-screen.
        """
        match = re.match(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$", geometry.strip())
        if not match:
            return geometry
        width, height = int(match.group(1)), int(match.group(2))
        x, y = int(match.group(3)), int(match.group(4))
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(width, screen_w - 80)
        height = min(height, screen_h - 120)
        x = max(0, min(x, screen_w - width))
        y = max(0, min(y, screen_h - height))
        return f"{width}x{height}+{x}+{y}"

    def _fit_to_content(self):
        if self._closed:
            return
        self.update_idletasks()
        # Give the scrolling canvas the size of its content so the window's
        # requested size accounts for every row.
        self._canvas.configure(
            width=self._inner.winfo_reqwidth(),
            height=self._inner.winfo_reqheight(),
        )
        self.update_idletasks()
        self._update_scrollregion_and_scrollbar()
        self.update_idletasks()
        # A restored geometry keeps the user's previous position/size as-is.
        if self._restore_geometry:
            self.geometry(self._clamp_geometry(self._restore_geometry))
            self._restore_geometry = None
            return
        # Never open larger than the screen; overflow falls back to scroll/pan.
        width = min(self.winfo_reqwidth(), self.winfo_screenwidth() - 80)
        height = min(self.winfo_reqheight(), self.winfo_screenheight() - 120)
        self.geometry(f"{width}x{height}")

    def _build_ui(self):
        # Master monitors show the PBI title centered above the controls row.
        if self._show_autoapprove_controls and self._pbi_title:
            title_bar = ttk.Frame(self)
            title_bar.pack(side="top", fill="x", padx=10, pady=(8, 0))
            ttk.Label(
                title_bar, text=self._pbi_title, anchor="center",
                font=("", 10, "bold"),
            ).pack(fill="x")

        controls = ttk.Frame(self)
        controls.pack(side="top", fill="x", padx=10, pady=(8, 0))
        self._controls = controls
        self._flash_label = None

        self._acc_button = None
        self._prod_button = None
        if self._show_autoapprove_controls:
            self._acc_button = ttk.Button(
                controls,
                command=self._toggle_autoapprove_acceptance,
            )
            self._acc_button.pack(side="left")
            Tooltip(
                self._acc_button,
                "Toggle auto-approval of the Acceptance (ACC) deployment gate "
                "for the tracked master runs.",
            )

            if self._show_prod_control:
                self._prod_button = ttk.Button(
                    controls,
                    command=self._toggle_autoapprove_production,
                )
                self._prod_button.pack(side="left", padx=(6, 0))
                Tooltip(
                    self._prod_button,
                    "Toggle auto-approval of the Production (PRD) deployment "
                    "gate for the tracked master runs.",
                )

        if self._show_autoapprove_controls and self._release_message:
            self._copy_links_button = ttk.Button(
                controls,
                text="Generate release message",
                command=self._generate_release_message,
            )
            Tooltip(
                self._copy_links_button,
                "Copy a release message to the clipboard: the feature name, "
                "then a '<service>: run link' line for every tracked "
                "repository, then each 'Tested By' work item as "
                "'test name: link'.",
            )
        else:
            self._copy_links_button = ttk.Button(
                controls,
                text="Copy all links",
                command=self._copy_all_links,
            )
            Tooltip(
                self._copy_links_button,
                "Copy a '<service> - run link' line for every tracked "
                "repository to the clipboard.",
            )
        self._copy_links_button.pack(side="left", padx=(12, 0))

        # Compact/full view toggle, right-aligned so it never crowds the
        # left-hand action buttons.
        self._compact_button = ttk.Button(
            controls,
            text="Full view" if self._compact else "Compact view",
            command=self._toggle_compact,
        )
        self._compact_button.pack(side="right")
        Tooltip(
            self._compact_button,
            "Switch between the full view (each stage circle labelled with its "
            "status and environment) and the compact view (labels hidden, rows "
            "packed closer together).",
        )

        self._sync_control_labels()

        table_shell = ttk.Frame(self)
        table_shell.pack(side="top", fill="both", expand=True, padx=10, pady=(8, 10))

        canvas = tk.Canvas(table_shell, highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(table_shell, orient="vertical", command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scroll.set)
        self._canvas = canvas
        self._vscroll = scroll

        self._inner = ttk.Frame(canvas)
        self._inner.bind("<Configure>", self._on_canvas_layout_changed)
        canvas.bind("<Configure>", self._on_canvas_layout_changed)
        canvas.create_window((0, 0), window=self._inner, anchor="nw")

        # No horizontal scrollbar: pan the canvas by dragging.
        self._bind_pan_widget(canvas)
        self._bind_pan_widget(self._inner)

        for index, (repo, info) in enumerate(sorted(self._run_infos.items())):
            self._inner.grid_rowconfigure(index, minsize=self._row_minsize())

            # Column 0 (left of the name): amber "previous run" marker on a row
            # that shows an existing run instead of a freshly started one.
            rewind_icon = None
            if info.get("is_previous_run"):
                rewind_icon = tk.Label(
                    self._inner, image=_previous_run_icon(), background=theme.BG,
                )
                rewind_icon.grid(row=index, column=0, sticky="w",
                                 padx=(4, 0), pady=0)
                Tooltip(rewind_icon, "previous run")
                self._bind_pan_widget(rewind_icon)

            repo_label = ttk.Label(self._inner, text=repo)
            repo_label.grid(row=index, column=1, sticky="w", padx=(4, 6), pady=0)
            self._bind_pan_widget(repo_label)

            # Skipped repositories have no run to track: show a placeholder and
            # keep the row out of polling/drawing.
            if info.get("skipped") and info.get("build_id") is None:
                skipped = ttk.Label(
                    self._inner, text="-- skipped --", foreground=theme.FG_MUTED
                )
                skipped.grid(row=index, column=2, sticky="w", padx=6, pady=0)
                self._bind_pan_widget(skipped)
                self._rows[repo] = {"skipped": True, "index": index}
                continue
            configured_stages = list(info.get("visible_stages") or [
                "build", "development", "acceptance", "production"
            ])
            stage_count = max(1, len(configured_stages))
            graph_width = max(120, 68 + (stage_count - 1) * 88)

            graph = tk.Canvas(self._inner, width=graph_width,
                              height=self._graph_height(),
                              background=theme.BG, highlightthickness=0)
            graph.grid(row=index, column=2, sticky="w", padx=6, pady=0)
            self._bind_pan_widget(graph)
            # Hover/click on a failed stage circle to rerun its failed jobs.
            graph.bind("<Motion>", lambda e, r=repo: self._on_stage_motion(e, r),
                       add="+")
            graph.bind("<Leave>", lambda e, r=repo: self._on_stage_leave(e, r),
                       add="+")
            graph.bind("<ButtonPress-1>",
                       lambda e, r=repo: self._on_stage_press(e, r), add="+")

            # Keep a direct clickable link to the run details page.
            link = tk.Label(
                self._inner,
                text=f"Build {info.get('build_id', '?')}",
                foreground=theme.LINK,
                cursor="hand2",
                font=("", 9, "underline"),
            )
            link.grid(row=index, column=3, sticky="w", padx=6, pady=0)
            url = info.get("url")
            if url:
                link.bind("<Button-1>", lambda _e, u=url: webbrowser.open(u, new=2))
            else:
                link.configure(foreground=theme.FG_MUTED, cursor="", font=("", 9))

            # Dev/acc monitors get a per-row action to queue a fresh run from the
            # latest commit of the branch (master runs don't - they are tied to
            # a specific merge commit).
            rerun_button = None
            if not info.get("is_master_run"):
                rerun_button = ttk.Button(
                    self._inner,
                    text="Run new",
                    width=11,
                    command=lambda r=repo: self._rerun_from_latest(r),
                )
                rerun_button.grid(row=index, column=4, sticky="w",
                                  padx=(6, 4), pady=0)
                Tooltip(
                    rerun_button,
                    "Queue a brand-new pipeline run from the latest commit of "
                    "the branch, using exactly the same parameters as this run. "
                    "This row then follows the new run.",
                )

            # Rightmost column: estimated total time left (excluding Production),
            # shown only when the estimate feature is on.
            estimate_label = None
            estimate = None
            if self._estimates_enabled:
                estimate = pipeline_estimates.get_estimate(repo)
                estimate_label = ttk.Label(
                    self._inner, text="", foreground=theme.FG_MUTED, width=8,
                )
                estimate_label.grid(row=index, column=5, sticky="w",
                                    padx=(10, 6), pady=0)
                Tooltip(
                    estimate_label,
                    "Estimated total time left for this run, based on the "
                    "average of the last successful master runs. This is an "
                    "estimation and excludes Production.",
                )
                self._bind_pan_widget(estimate_label)

            self._rows[repo] = {
                "graph": graph,
                "link": link,
                "link_url": url,
                "build_id": info.get("build_id"),
                "configured_stages": configured_stages,
                "stages": {
                    "build": "waiting",
                    "development": "waiting",
                    "acceptance": "waiting",
                    "production": "waiting",
                },
                "stage_identifiers": {},
                "retry_hitboxes": [],
                "hover_stage": None,
                "retry_in_progress": False,
                "rerun_button": rerun_button,
                "rerun_launch_in_progress": False,
                "rewind_icon": rewind_icon,
                "stage_progress": {},
                "running_hitboxes": [],
                "stage_times": {},
                "estimate": estimate,
                "estimate_label": estimate_label,
                "environment": info.get("environment"),
                "index": index,
            }
            # Seed the last recorded stage states (history reproduction) so the
            # row shows how it looked before, prior to the first live poll.
            restored_stages = info.get("restored_stages")
            if restored_stages:
                self._rows[repo]["stages"].update({
                    key: value for key, value in restored_stages.items()
                    if key in self._rows[repo]["stages"]
                })
            self._draw_row(repo)
            self._update_estimate_label(repo)

        self.after_idle(self._update_scrollregion_and_scrollbar)

    def _sync_control_labels(self):
        """Refresh monitor control labels to reflect current toggle states."""
        if not self._show_autoapprove_controls:
            return

        if self._acc_locked_by_master:
            acc_text = "Auto-approve ACC: DISABLED"
        else:
            acc_state = "ON" if self._autoapprove_acceptance else "OFF"
            acc_text = f"Auto-approve ACC: {acc_state}"

        if self._prod_locked_by_master:
            prod_text = "Auto-approve PRD: DISABLED"
        else:
            prod_state = "ON" if self._autoapprove_production else "OFF"
            prod_text = f"Auto-approve PRD: {prod_state}"

        self._acc_button.configure(
            text=acc_text,
            state=("disabled" if self._acc_locked_by_master else "normal"),
        )
        if self._prod_button is not None:
            self._prod_button.configure(
                text=prod_text,
                state=("disabled" if self._prod_locked_by_master else "normal"),
            )

    def _apply_autoapprove_flags(self):
        """Propagate monitor toggle state into each tracked run info payload."""
        for info in self._run_infos.values():
            info["autoapprove_acceptance"] = bool(self._autoapprove_acceptance)
            info["autoapprove_production"] = bool(self._autoapprove_production)

    def _toggle_autoapprove_acceptance(self):
        if self._acc_locked_by_master:
            return
        self._autoapprove_acceptance = not self._autoapprove_acceptance
        self._apply_autoapprove_flags()
        self._sync_control_labels()

    def _toggle_autoapprove_production(self):
        if self._prod_locked_by_master:
            return
        self._autoapprove_production = not self._autoapprove_production
        self._apply_autoapprove_flags()
        self._sync_control_labels()

    def _refresh_autoapprove_locks(self):
        """Lock autoapprove controls when a tracked master run is already deployed."""
        if not self._show_autoapprove_controls:
            return

        master_repos = [
            repo for repo, info in self._run_infos.items()
            if info.get("is_master_run")
        ]
        if master_repos:
            # A gate no longer needs auto-approval once every tracked run has
            # deployed it ("done") or already has my approval in ("ready").
            acc_locked = all(
                ((self._rows.get(repo) or {}).get("stages") or {}).get("acceptance")
                in ("done", "ready")
                for repo in master_repos
            )
            prod_locked = all(
                ((self._rows.get(repo) or {}).get("stages") or {}).get("production")
                in ("done", "ready")
                for repo in master_repos
            )
        else:
            acc_locked = False
            prod_locked = False

        flags_changed = False
        if acc_locked and self._autoapprove_acceptance:
            self._autoapprove_acceptance = False
            flags_changed = True
        if prod_locked and self._autoapprove_production:
            self._autoapprove_production = False
            flags_changed = True

        lock_changed = (
            acc_locked != self._acc_locked_by_master
            or prod_locked != self._prod_locked_by_master
        )
        self._acc_locked_by_master = acc_locked
        self._prod_locked_by_master = prod_locked

        if flags_changed:
            self._apply_autoapprove_flags()
        if lock_changed or flags_changed:
            self._sync_control_labels()

    def session_state(self):
        """Return a JSON-serialisable snapshot for restoring after a relaunch."""
        infos = {}
        for repo, info in self._run_infos.items():
            serial = {
                key: value for key, value in info.items()
                if not str(key).startswith("_")
            }
            # Keep the latest stage states so the history viewer can reproduce
            # the monitor as it looked (before the reopened monitor re-polls).
            stages = (self._rows.get(repo) or {}).get("stages")
            if stages:
                serial["restored_stages"] = dict(stages)
            infos[repo] = serial
        return {
            "show_autoapprove_controls": self._show_autoapprove_controls,
            "show_prod_control": self._show_prod_control,
            "release_message": self._release_message,
            "pbi_title": self._pbi_title,
            "test_reports": [list(item) for item in self._test_reports],
            "run_infos": infos,
            "history_session_id": self.history_session_id,
            "geometry": self.geometry(),
        }

    def _copy_all_links(self):
        """Copy multiline '<service> - <link>' output for all rows with URLs."""
        lines = []
        for repo in sorted(self._rows):
            url = self._rows[repo].get("link_url") or ""
            if url:
                lines.append(f"{repo} - {url}")
        if not lines:
            return
        text = "\n".join(lines)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.title("Pipeline monitor - links copied")
        self._flash_copied()

    def _generate_release_message(self):
        """Copy the feature name, run links and linked test report links."""
        lines = []
        # Feature name is the part after '|' in the PBI title (fallback: title).
        feature_name = (self._pbi_title.split("|")[-1]).strip()
        if feature_name:
            lines.append(feature_name)
        for repo in sorted(self._rows):
            url = self._rows[repo].get("link_url") or ""
            if url:
                lines.append(f"{repo}: {url}")
        if self._test_reports:
            for name, url in self._test_reports:
                lines.append(f"{name}: {url}")
        if not lines:
            return
        text = "\n".join(lines)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.title("Pipeline monitor - release message copied")
        self._flash_copied()

    def _flash_copied(self, message="Copied!"):
        """Show a short 'Copied!' label next to the button that fades out."""
        if self._flash_label is not None:
            self._flash_label.destroy()
            self._flash_label = None

        label = tk.Label(self._controls, text=message, background=theme.BG,
                         font=("", 9, "bold"))
        label.pack(side="left", padx=(10, 0))
        self._flash_label = label

        def _hex(color):
            return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))

        start, end = _hex(theme.SUCCESS), _hex(theme.BG)
        steps, hold = 14, 6  # frames of fade, preceded by a brief solid hold

        def _step(frame):
            if self._closed or not label.winfo_exists():
                return
            if frame < hold:
                self.after(60, _step, frame + 1)
                return
            t = (frame - hold) / steps
            if t >= 1:
                label.destroy()
                if self._flash_label is label:
                    self._flash_label = None
                return
            rgb = tuple(round(s + (e - s) * t) for s, e in zip(start, end))
            label.config(foreground="#%02x%02x%02x" % rgb)
            self.after(45, _step, frame + 1)

        label.config(foreground=theme.SUCCESS)
        self.after(45, _step, 0)

    def _on_canvas_layout_changed(self, _event=None):
        self._update_scrollregion_and_scrollbar()

    def _update_scrollregion_and_scrollbar(self):
        """Refresh scrollregion and show vertical scrollbar only when needed."""
        canvas = getattr(self, "_canvas", None)
        scroll = getattr(self, "_vscroll", None)
        if canvas is None or scroll is None:
            return
        canvas.configure(scrollregion=canvas.bbox("all"))
        bbox = canvas.bbox("all")
        if not bbox:
            return
        content_height = bbox[3] - bbox[1]
        viewport_height = max(1, canvas.winfo_height())
        needed = content_height > viewport_height + 1
        if needed and not self._scrollbar_visible:
            scroll.pack(side="right", fill="y")
            self._scrollbar_visible = True
        elif not needed and self._scrollbar_visible:
            scroll.pack_forget()
            self._scrollbar_visible = False

    def _bind_pan_widget(self, widget):
        """Enable drag-to-pan gestures on a widget."""
        widget.bind("<ButtonPress-1>", self._on_pan_start)
        widget.bind("<B1-Motion>", self._on_pan_drag)

    def _on_pan_start(self, event):
        self._pan_anchor = (event.x_root, event.y_root)

    def _on_pan_drag(self, event):
        if self._pan_anchor is None:
            return
        canvas = getattr(self, "_canvas", None)
        if canvas is None:
            return
        dx = event.x_root - self._pan_anchor[0]
        dy = event.y_root - self._pan_anchor[1]
        self._pan_anchor = (event.x_root, event.y_root)
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        canvas.xview_scroll(int(-dx / 2), "units")
        canvas.yview_scroll(int(-dy / 2), "units")

    def _graph_height(self):
        return _GRAPH_HEIGHT_COMPACT if self._compact else _GRAPH_HEIGHT_FULL

    def _row_minsize(self):
        return _ROW_MINSIZE_COMPACT if self._compact else _ROW_MINSIZE_FULL

    def _toggle_compact(self):
        """Switch between the full and compact view and re-layout every row."""
        self._compact = not self._compact
        self._apply_view_mode()

    def _apply_view_mode(self):
        """Apply the current view mode to row heights, canvases and the window."""
        if self._closed:
            return
        self._compact_button.configure(
            text="Full view" if self._compact else "Compact view"
        )
        minsize = self._row_minsize()
        height = self._graph_height()
        for repo, row in self._rows.items():
            index = row.get("index")
            if index is not None:
                self._inner.grid_rowconfigure(index, minsize=minsize)
            graph = row.get("graph")
            if graph is not None and graph.winfo_exists():
                graph.configure(height=height)
            if not row.get("skipped"):
                self._draw_row(repo)
        self.after_idle(self._fit_to_content)

    def _draw_row(self, repo):
        row = self._rows[repo]
        canvas = row["graph"]
        stages = row["stages"]
        canvas.delete("all")
        retry_hitboxes = []
        running_hitboxes = []

        stage_order = [
            (key, title)
            for key, title in _STAGE_ORDER
            if key in row.get("configured_stages", [])
        ]
        if not stage_order:
            stage_order = [("build", "Build")]

        # Every configured stage keeps a fixed column; a skipped stage leaves its
        # slot empty and the connector spans it (Build --- Acceptance keeps
        # Acceptance under the Acceptance column instead of sliding left).
        drawn = [
            (slot, key, title)
            for slot, (key, title) in enumerate(stage_order)
            if stages.get(key) != "skipped"
        ]
        if not drawn:
            drawn = [(0, stage_order[0][0], stage_order[0][1])]

        start_x = 34
        gap = 88
        y = _CIRCLE_Y_COMPACT if self._compact else _CIRCLE_Y_FULL

        # Connector line between consecutive drawn stages (spans skipped columns).
        for i in range(len(drawn) - 1):
            x1 = start_x + drawn[i][0] * gap + 20
            x2 = start_x + drawn[i + 1][0] * gap - 20
            canvas.create_line(x1, y, x2, y, fill=theme.BORDER, width=2)

        for slot, key, title in drawn:
            x = start_x + slot * gap
            state = stages.get(key, "waiting")
            style = _STAGE_STYLE.get(state, _STAGE_STYLE["waiting"])
            # A running stage with known progress is drawn as a circular bar: a
            # blue pie sweeping clockwise from 12 o'clock over a track, filling
            # by the stage's completion percent. Everything else is a plain dot.
            percent = None
            if state == "running":
                percent = ((row.get("stage_progress") or {}).get(key) or {}).get(
                    "percent"
                )
            if state == "running" and percent is not None and percent < 100:
                canvas.create_oval(
                    x - 10, y - 10, x + 10, y + 10,
                    fill=theme.BG_INPUT, outline=style["outline"], width=2,
                )
                sweep = max(0.0, min(100.0, float(percent))) / 100.0 * 360.0
                if sweep > 0:
                    canvas.create_arc(
                        x - 10, y - 10, x + 10, y + 10,
                        start=90, extent=-sweep, style="pieslice",
                        fill=style["fill"], outline=style["fill"],
                    )
                canvas.create_oval(
                    x - 10, y - 10, x + 10, y + 10,
                    fill="", outline=style["outline"], width=2,
                )
            else:
                canvas.create_oval(
                    x - 10, y - 10, x + 10, y + 10,
                    fill=style["fill"], outline=style["outline"], width=2,
                )
            # In compact view the per-circle environment/status labels are
            # hidden; circle x-positions (columns) stay identical either way.
            if not self._compact:
                canvas.create_text(
                    x, y + 24, text=title, fill=theme.FG, font=("", 8)
                )
                # Live theme colour so the state label stays visible in light theme.
                label_color = (theme.FG_MUTED if state in ("waiting", "skipped")
                               else theme.FG)
                canvas.create_text(
                    x, y - 18, text=style["label"], fill=label_color, font=("", 8)
                )
            # A failed stage is retryable: remember its circle and, while it is
            # hovered, draw a white rerun icon on top of the red circle.
            if state == "failed":
                retry_hitboxes.append((key, x, y))
                if row.get("hover_stage") == key:
                    canvas.create_text(
                        x, y, text="\u21bb", fill="white", font=("", 13, "bold"),
                    )
            # A running stage shows its current step + completion % on hover.
            if state == "running":
                running_hitboxes.append((key, x, y))
        row["retry_hitboxes"] = retry_hitboxes
        row["running_hitboxes"] = running_hitboxes

    def _stage_at(self, row, px, py):
        """Return the failed-stage key whose circle contains (px, py), or None."""
        for key, cx, cy in row.get("retry_hitboxes") or []:
            if (px - cx) ** 2 + (py - cy) ** 2 <= 12 ** 2:
                return key
        return None

    def _update_estimate_label(self, repo):
        """Refresh a row's estimated total time-left column (excludes Production)."""
        row = self._rows.get(repo)
        if not row:
            return
        label = row.get("estimate_label")
        if label is None or not label.winfo_exists():
            return
        # Nothing to estimate before the first live poll has fetched real data.
        if not self._first_poll_done:
            label.configure(text="")
            return
        estimate = row.get("estimate")
        if not estimate:
            label.configure(text="")
            return
        total = pipeline_estimates.total_time_left(
            estimate,
            row.get("stages") or {},
            row.get("stage_times") or {},
            environment=row.get("environment"),
            visible_stages=row.get("configured_stages"),
        )
        if total and total > 0:
            label.configure(text="~" + pipeline_estimates.fmt_mmss(total))
        else:
            label.configure(text="")

    def _tick_estimates(self):
        """Re-render the estimated time-left columns once a second."""
        if self._closed:
            return
        for repo in list(self._rows):
            self._update_estimate_label(repo)
        self.after(1000, self._tick_estimates)

    def _on_stage_motion(self, event, repo):
        """Show the rerun icon while hovering a failed stage circle."""
        row = self._rows.get(repo)
        if not row:
            return
        self._update_progress_tip(repo, row, event.x, event.y)
        hit = self._stage_at(row, event.x, event.y)
        if hit == row.get("hover_stage"):
            return
        row["hover_stage"] = hit
        row["graph"].configure(cursor="hand2" if hit else "")
        if hit:
            stage_label = dict(_STAGE_ORDER).get(hit, hit)
            self.title(
                f"Pipeline monitor - click to rerun failed {stage_label} jobs"
            )
        else:
            self.title("Pipeline monitor")
        self._draw_row(repo)

    def _on_stage_leave(self, _event, repo):
        self._hide_progress_tip()
        row = self._rows.get(repo)
        if not row or row.get("hover_stage") is None:
            return
        row["hover_stage"] = None
        row["graph"].configure(cursor="")
        self._draw_row(repo)

    def _running_stage_at(self, row, px, py):
        """Return the running-stage key whose circle contains (px, py), or None."""
        for key, cx, cy in row.get("running_hitboxes") or []:
            if (px - cx) ** 2 + (py - cy) ** 2 <= 12 ** 2:
                return key
        return None

    def _update_progress_tip(self, repo, row, px, py):
        """Show/hide the step-progress tooltip for a hovered running stage."""
        key = self._running_stage_at(row, px, py)
        target = (repo, key) if key else None
        if target == self._progress_tip_target:
            return
        self._progress_tip_target = target
        self._hide_progress_tip()
        if not key:
            return
        progress = (row.get("stage_progress") or {}).get(key)
        if not progress:
            return
        current = progress.get("current") or ""
        percent = progress.get("percent")
        text = f"{percent}% {current}".strip() if percent is not None else current
        # Append the estimated time left for this stage (never for Production).
        if self._estimates_enabled and key != "production":
            estimate = row.get("estimate")
            if estimate:
                avg = (estimate.get("stages") or {}).get(key)
                start = ((row.get("stage_times") or {}).get(key) or {}).get("start")
                left = pipeline_estimates.stage_time_left(avg, "running", start)
                if left is not None:
                    extra = (
                        "Est. time left: "
                        + pipeline_estimates.fmt_mmss(left)
                    )
                    text = f"{text}\n{extra}" if text else extra
        if not text:
            return
        canvas = row["graph"]
        center = next(
            ((cx, cy) for k, cx, cy in row.get("running_hitboxes") or []
             if k == key),
            None,
        )
        if center is None:
            return
        x = canvas.winfo_rootx() + center[0] + 14
        y = canvas.winfo_rooty() + center[1] + 14
        self._show_progress_tip(text, x, y)

    def _show_progress_tip(self, text, x, y):
        self._hide_progress_tip()
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        tip.wm_attributes("-topmost", True)
        tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tip, text=text, justify="left", background=theme.TOOLTIP_BG,
            foreground=theme.TOOLTIP_FG, relief="solid", borderwidth=1,
            padx=6, pady=3, wraplength=320,
        ).pack()
        self._progress_tip = tip

    def _hide_progress_tip(self):
        if self._progress_tip is not None:
            self._progress_tip.destroy()
            self._progress_tip = None

    def _on_stage_press(self, event, repo):
        row = self._rows.get(repo)
        if not row:
            return
        key = self._stage_at(row, event.x, event.y)
        if key:
            self._retry_stage(repo, key)

    def _retry_stage(self, repo, key):
        """Rerun the failed jobs of *key* stage for *repo* on a worker thread."""
        row = self._rows.get(repo)
        info = self._run_infos.get(repo)
        if not row or not info or row.get("retry_in_progress"):
            return
        stage_id = (row.get("stage_identifiers") or {}).get(key)
        stage_label = dict(_STAGE_ORDER).get(key, key)
        row["retry_in_progress"] = True
        self.title(f"Pipeline monitor - retrying {repo} {stage_label}...")

        def _work():
            ok, err = rerun_failed_stage(info, stage_id)
            self.after(0, self._on_retry_done, repo, key, ok, err)

        threading.Thread(target=_work, daemon=True).start()

    def _on_retry_done(self, repo, key, ok, err):
        if self._closed:
            return
        row = self._rows.get(repo)
        if row:
            row["retry_in_progress"] = False
        stage_label = dict(_STAGE_ORDER).get(key, key)
        if ok:
            if row:
                row["stages"][key] = "running"
                row["hover_stage"] = None
                row["graph"].configure(cursor="")
                self._draw_row(repo)
            self.title(f"Pipeline monitor - {repo} {stage_label} rerun queued")
        else:
            self.title(f"Pipeline monitor - rerun failed: {err}")

    def _rerun_from_latest(self, repo):
        """Queue a fresh run from the branch tip and follow it in this row."""
        row = self._rows.get(repo)
        info = self._run_infos.get(repo)
        if not row or not info or row.get("rerun_launch_in_progress"):
            return
        row["rerun_launch_in_progress"] = True
        button = row.get("rerun_button")
        if button is not None:
            button.configure(state="disabled")
        self.title(f"Pipeline monitor - starting new {repo} run...")

        def _work():
            ok, result = rerun_pipeline_from_latest_commit(info)
            self.after(0, self._on_rerun_launched, repo, ok, result)

        threading.Thread(target=_work, daemon=True).start()

    def _on_rerun_launched(self, repo, ok, result):
        if self._closed:
            return
        row = self._rows.get(repo)
        if row:
            row["rerun_launch_in_progress"] = False
            button = row.get("rerun_button")
            if button is not None:
                button.configure(state="normal")
        if not ok:
            self.title(f"Pipeline monitor - new run failed: {result}")
            return

        info = self._run_infos.get(repo)
        # Record the fresh run as a child of this repo in the history session
        # (a full "Run new"; failed-stage retries are deliberately not recorded).
        if info is not None and self.history_session_id:
            pipeline_history.add_child_run(
                self.history_session_id,
                repo,
                pipeline_history.child_run_from_result(info, result),
            )
        if info is not None:
            # Follow the new run: refresh identity, drop stale approval flags.
            info["url"] = result.get("url", "")
            info["build_id"] = result.get("build_id")
            info["pipeline_id"] = result.get("pipeline_id")
            info["visible_stages"] = result.get("visible_stages") or []
            info["template_parameters"] = result.get("template_parameters") or {}
            info["is_previous_run"] = False
            for stale in ("_autoapprove_acceptance_done",
                          "_autoapprove_production_done"):
                info.pop(stale, None)

        if row:
            # A fresh run is no longer a "previous" one - drop the rewind marker.
            rewind_icon = row.get("rewind_icon")
            if rewind_icon is not None:
                rewind_icon.grid_forget()
                row["rewind_icon"] = None
            new_url = result.get("url", "")
            new_build = result.get("build_id")
            row["link_url"] = new_url
            row["build_id"] = new_build
            row["stage_identifiers"] = {}
            row["hover_stage"] = None
            row["stages"] = {
                "build": "waiting",
                "development": "waiting",
                "acceptance": "waiting",
                "production": "waiting",
            }
            link = row["link"]
            link.unbind("<Button-1>")
            link.configure(
                text=f"Build {new_build if new_build is not None else '?'}",
                foreground=theme.LINK if new_url else theme.FG_MUTED,
                cursor="hand2" if new_url else "",
                font=("", 9, "underline") if new_url else ("", 9),
            )
            if new_url:
                link.bind("<Button-1>",
                          lambda _e, u=new_url: webbrowser.open(u, new=2))
            self._draw_row(repo)
        self.title(f"Pipeline monitor - new {repo} run queued")

    def _report_error(self, message):
        """Report a message to the owning tab's Errors panel (if available)."""
        panel = getattr(self._tab, "errors", None)
        if panel is None:
            return
        try:
            panel.add(message)
        except Exception:
            pass

    def _poll_once(self):
        if self._closed:
            return
        if self._poll_in_progress:
            self._schedule_next_poll()
            return

        self._poll_in_progress = True
        self.title("Pipeline monitor - polling...")

        # Reset the per-poll aggregation used by _apply_single_poll_result.
        self._poll_any_error = False
        self._poll_latest_timestamp = ""
        self._poll_autoapproved = []
        self._poll_autoapprove_errors = []

        # Skipped placeholder rows have nothing to poll.
        pollable = [
            (repo, info) for repo, info in self._run_infos.items()
            if not (info.get("skipped") or info.get("build_id") is None)
        ]

        def _fetch(item):
            repo, info = item
            ok, payload = get_pipeline_stage_statuses(info)
            # Push each repo's result to the UI as soon as it is fetched, so
            # rows update progressively instead of all at the very end.
            self.after(0, self._apply_single_poll_result, repo, ok, payload)

        def _work():
            # Fan the per-repo status calls out over a thread pool - each is an
            # independent network round-trip, so many repos no longer poll
            # one-by-one.
            run_in_parallel(pollable, _fetch)
            self.after(0, self._finish_poll)

        threading.Thread(target=_work, daemon=True).start()

    def _apply_single_poll_result(self, repo, ok, payload):
        if self._closed or repo not in self._rows:
            return

        if ok:
            self._rows[repo]["stages"].update(payload.get("stages") or {})
            self._rows[repo]["stage_identifiers"] = (
                payload.get("stage_identifiers") or {}
            )
            self._rows[repo]["stage_progress"] = (
                payload.get("stage_progress") or {}
            )
            self._rows[repo]["stage_times"] = (
                payload.get("stage_times") or {}
            )
            self._poll_latest_timestamp = payload.get(
                "updated_at", self._poll_latest_timestamp
            )
            if payload.get("autoapproved"):
                target = payload.get("autoapproved_target")
                if target:
                    self._poll_autoapproved.append(f"{repo}({target})")
                else:
                    self._poll_autoapproved.append(repo)
            if payload.get("autoapprove_error"):
                self._poll_autoapprove_errors.append(
                    f"{repo}: {payload.get('autoapprove_error')}"
                )
            build_id = self._rows[repo].get("build_id")
            link_url = self._rows[repo].get("link_url")
            link = self._rows[repo]["link"]
            link.configure(
                text=f"Build {build_id if build_id is not None else '?'}",
                foreground=theme.LINK,
                cursor="hand2" if link_url else "",
                font=("", 9, "underline") if link_url else ("", 9),
            )
            link.unbind("<Button-1>")
            if link_url:
                link.bind("<Button-1>", lambda _e, u=link_url: webbrowser.open(u, new=2))
            self._draw_row(repo)
            self._update_estimate_label(repo)
            # Recovered: allow a future error for this repo to be reported.
            self._reported_poll_errors.pop(repo, None)
        else:
            self._poll_any_error = True
            self._rows[repo]["link"].configure(
                text="poll error", foreground=theme.ERROR, cursor=""
            )
            self._rows[repo]["link"].unbind("<Button-1>")
            # Surface the actual error in the tab's Errors panel (once per
            # distinct message, so repeated polls do not spam it).
            message = str(payload)
            if self._reported_poll_errors.get(repo) != message:
                self._reported_poll_errors[repo] = message
                self._report_error(f"{repo}: pipeline poll failed - {message}")

        self._refresh_autoapprove_locks()

    def _finish_poll(self):
        if self._closed:
            return

        self._poll_in_progress = False
        self._first_poll_done = True
        latest_timestamp = self._poll_latest_timestamp
        if latest_timestamp:
            latest_time = _format_local_time(latest_timestamp)
            title = f"Pipeline monitor - last updated: {latest_timestamp}"
            if latest_time:
                title = f"Pipeline monitor - last updated: {latest_time}"
            if self._poll_autoapproved:
                title += " | auto-approved: " + ",".join(self._poll_autoapproved)
            if self._poll_autoapprove_errors:
                title += " | approval error"
            self.title(title)
        elif self._poll_any_error:
            self.title("Pipeline monitor - last updated: error while polling")
        else:
            self.title("Pipeline monitor")
        self._update_scrollregion_and_scrollbar()
        self._schedule_next_poll()

    def _schedule_next_poll(self):
        if self._closed:
            return
        seconds = theme.load_pipeline_poll_seconds()
        if self._next_poll_token is not None:
            try:
                self.after_cancel(self._next_poll_token)
            except Exception:
                pass
        self._next_poll_token = self.after(seconds * 1000, self._poll_once)

    def _on_close(self):
        self._closed = True
        self._hide_progress_tip()
        # Persist the latest run infos + window geometry so the history viewer
        # can reopen this monitor looking as it did before.
        if self.history_session_id:
            try:
                pipeline_history.update_snapshot(
                    self.history_session_id, self.session_state()
                )
            except Exception:
                pass
        if self._next_poll_token is not None:
            try:
                self.after_cancel(self._next_poll_token)
            except Exception:
                pass
            self._next_poll_token = None
        self.destroy()
