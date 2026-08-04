"""Floating pipeline monitor window.

Shows per-repository stage progress for started pipeline runs and refreshes
statuses on a configurable interval.
"""

import threading
import datetime
import tkinter as tk
import webbrowser
from tkinter import ttk

import theme
from pipelines import get_pipeline_stage_statuses


_STAGE_ORDER = [
    ("build", "Build"),
    ("development", "Development"),
    ("acceptance", "Acceptance"),
    ("production", "Production"),
]

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

    def __init__(self, parent, run_infos):
        super().__init__(parent.winfo_toplevel())
        self.title("Pipeline monitor")
        self.geometry("760x250")
        self.minsize(310, 90)
        self.attributes("-topmost", True)
        self.configure(background=theme.BG)
        theme.apply_window_icon(self)
        theme.enable_dark_titlebar(self)

        self._closed = False
        self._poll_in_progress = False
        self._next_poll_token = None
        self._run_infos = dict(run_infos)
        self._rows = {}
        self._scrollbar_visible = True
        self._pan_anchor = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._poll_once)

    def _build_ui(self):
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
            self._inner.grid_rowconfigure(index, minsize=58)
            repo_label = ttk.Label(self._inner, text=repo)
            repo_label.grid(row=index, column=0, sticky="w", padx=(4, 6), pady=0)
            self._bind_pan_widget(repo_label)

            configured_stages = list(info.get("visible_stages") or [
                "build", "development", "acceptance", "production"
            ])
            stage_count = max(1, len(configured_stages))
            graph_width = max(120, 68 + (stage_count - 1) * 88)

            graph = tk.Canvas(self._inner, width=graph_width, height=62,
                              background=theme.BG, highlightthickness=0)
            graph.grid(row=index, column=1, sticky="w", padx=6, pady=0)
            self._bind_pan_widget(graph)

            # Keep a direct clickable link to the run details page.
            link = tk.Label(
                self._inner,
                text=f"Build {info.get('build_id', '?')}",
                foreground=theme.LINK,
                cursor="hand2",
                font=("", 9, "underline"),
            )
            link.grid(row=index, column=2, sticky="w", padx=6, pady=0)
            url = info.get("url")
            if url:
                link.bind("<Button-1>", lambda _e, u=url: webbrowser.open(u, new=2))
            else:
                link.configure(foreground=theme.FG_MUTED, cursor="", font=("", 9))

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
            }
            self._draw_row(repo)

        self.after_idle(self._update_scrollregion_and_scrollbar)

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

    def _draw_row(self, repo):
        row = self._rows[repo]
        canvas = row["graph"]
        stages = row["stages"]
        canvas.delete("all")

        stage_order = [
            (key, title)
            for key, title in _STAGE_ORDER
            if key in row.get("configured_stages", [])
            and stages.get(key) != "skipped"
        ]
        if not stage_order:
            stage_order = [("build", "Build")]

        start_x = 34
        gap = 88
        y = 25

        # Connector line between stages.
        for idx in range(len(stage_order) - 1):
            x1 = start_x + idx * gap + 20
            x2 = start_x + (idx + 1) * gap - 20
            canvas.create_line(x1, y, x2, y, fill=theme.BORDER, width=2)

        for idx, (key, title) in enumerate(stage_order):
            x = start_x + idx * gap
            state = stages.get(key, "waiting")
            style = _STAGE_STYLE.get(state, _STAGE_STYLE["waiting"])
            canvas.create_oval(
                x - 10, y - 10, x + 10, y + 10,
                fill=style["fill"], outline=style["outline"], width=2,
            )
            canvas.create_text(x, y + 24, text=title, fill=theme.FG, font=("", 8))
            canvas.create_text(
                x, y - 18, text=style["label"], fill=style["text"], font=("", 8)
            )

    def _poll_once(self):
        if self._closed:
            return
        if self._poll_in_progress:
            self._schedule_next_poll()
            return

        self._poll_in_progress = True
        self.title("Pipeline monitor - polling...")

        def _work():
            results = {}
            for repo, info in self._run_infos.items():
                ok, payload = get_pipeline_stage_statuses(info)
                results[repo] = (ok, payload)
            self.after(0, self._apply_poll_results, results)

        threading.Thread(target=_work, daemon=True).start()

    def _apply_poll_results(self, results):
        if self._closed:
            return

        self._poll_in_progress = False
        any_error = False
        latest_timestamp = ""
        autoapproved_repos = []
        autoapprove_errors = []

        for repo, (ok, payload) in results.items():
            if repo not in self._rows:
                continue
            if ok:
                self._rows[repo]["stages"].update(payload.get("stages") or {})
                latest_timestamp = payload.get("updated_at", latest_timestamp)
                if payload.get("autoapproved"):
                    autoapproved_repos.append(repo)
                if payload.get("autoapprove_error"):
                    autoapprove_errors.append(f"{repo}: {payload.get('autoapprove_error')}")
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
            else:
                any_error = True
                self._rows[repo]["link"].configure(
                    text="poll error", foreground=theme.ERROR, cursor=""
                )
                self._rows[repo]["link"].unbind("<Button-1>")

        if latest_timestamp:
            latest_time = _format_local_time(latest_timestamp)
            title = f"Pipeline monitor - last updated: {latest_timestamp}"
            if latest_time:
                title = f"Pipeline monitor - last updated: {latest_time}"
            if autoapproved_repos:
                title += " | auto-approved: " + ",".join(autoapproved_repos)
            if autoapprove_errors:
                title += " | approval error"
            self.title(title)
        elif any_error:
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
        if self._next_poll_token is not None:
            try:
                self.after_cancel(self._next_poll_token)
            except Exception:
                pass
            self._next_poll_token = None
        self.destroy()
