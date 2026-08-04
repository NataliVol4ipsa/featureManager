"""Floating pipeline monitor window.

Shows per-repository stage progress for started pipeline runs and refreshes
statuses on a configurable interval.
"""

import threading
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


class PipelineMonitorWindow(tk.Toplevel):
    """Always-on-top floating monitor for pipeline stage statuses."""

    def __init__(self, parent, run_infos):
        super().__init__(parent.winfo_toplevel())
        self.title("Pipeline monitor")
        self.geometry("760x250")
        self.minsize(310, 90)
        self.attributes("-topmost", True)
        self.configure(background=theme.BG)
        theme.enable_dark_titlebar(self)

        self._closed = False
        self._poll_in_progress = False
        self._next_poll_token = None
        self._run_infos = dict(run_infos)
        self._rows = {}

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

        self._inner = ttk.Frame(canvas)
        self._inner.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self._inner, anchor="nw")

        for index, (repo, info) in enumerate(sorted(self._run_infos.items())):
            self._inner.grid_rowconfigure(index, minsize=58)
            repo_label = ttk.Label(self._inner, text=repo)
            repo_label.grid(row=index, column=0, sticky="w", padx=(4, 6), pady=0)

            graph = tk.Canvas(self._inner, width=340, height=62,
                              background=theme.BG, highlightthickness=0)
            graph.grid(row=index, column=1, sticky="w", padx=6, pady=0)

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
                "stages": {
                    "build": "waiting",
                    "development": "waiting",
                    "acceptance": "waiting",
                    "production": "waiting",
                },
            }
            self._draw_row(repo)

    def _draw_row(self, repo):
        row = self._rows[repo]
        canvas = row["graph"]
        stages = row["stages"]
        canvas.delete("all")

        start_x = 34
        gap = 88
        y = 25

        # Connector line between stages.
        for idx in range(len(_STAGE_ORDER) - 1):
            x1 = start_x + idx * gap + 20
            x2 = start_x + (idx + 1) * gap - 20
            canvas.create_line(x1, y, x2, y, fill=theme.BORDER, width=2)

        for idx, (key, title) in enumerate(_STAGE_ORDER):
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

        for repo, (ok, payload) in results.items():
            if repo not in self._rows:
                continue
            if ok:
                self._rows[repo]["stages"].update(payload.get("stages") or {})
                latest_timestamp = payload.get("updated_at", latest_timestamp)
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
            self.title(f"Pipeline monitor - last updated: {latest_timestamp}")
        elif any_error:
            self.title("Pipeline monitor - last updated: error while polling")
        else:
            self.title("Pipeline monitor")
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
