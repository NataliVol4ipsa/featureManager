""""View deployment status" window: latest commit deployed per pipeline stage.

Shows, per selected repository, the newest commit that completed each ticked
pipeline stage (Build/Development/Acceptance/Production). The underlying scan
spans every branch of the pipeline, not just master - Development/Acceptance
are routinely deployed straight from a feature branch, so restricting to
master would silently report a stale commit. Reading this instead of the plain
Runs list matters because an inline "Rerun" in Azure DevOps updates a stage's
completion time without changing the run's position in that list.

Each commit cell is also colour-coded against the repository's exact
workspace/feature branch (a workspace can override the branch per repo, so the
caller must pass the exact branch, not assume ``feature/<workspace>``):
  * blue    - the latest commit on master
  * green   - the latest commit on the feature branch
  * yellow  - an older commit that is still on the feature branch (not shared
              with master, so it is never confused with a master commit)
  * grey    - an older commit on master (not the tip, but still master history)
  * red     - unrecognised (e.g. a stale build from a different feature branch)

Clicking a commit opens the repository's pipeline (run history overview), not
a one-off build page; the URL is built from the pipeline id already resolved
while fetching the commit, so no extra HTTP call is made on click.

Self-contained (owns its own fetching, like pipeline_monitor.py): callers only
need to build the (name, path, branch) entries and the ticked stage keys.
"""

import re
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk

import theme
from widgets import Tooltip
from parallel import run_in_parallel
from pipelines import (
    get_latest_deployed_artifacts, get_deployment_commit_context,
    classify_deployed_commit,
)

_STAGE_LABELS = {
    "development": "Dev",
    "acceptance": "Acc",
    "production": "Prod",
}

# commit classification -> (theme colour name, short explanation appended to
# the tooltip).
_CLASSIFICATION = {
    "master": ("LINK", "latest commit on master"),
    "branch_latest": ("SUCCESS", "latest commit on the feature branch"),
    "branch_older": ("WARNING", "an older commit on the feature branch"),
    "master_older": ("FG_MUTED", "an older commit on master"),
    "unknown": ("ERROR", "not on master or the feature branch (a different/stale branch?)"),
}

_NAME_WIDTH = 26
_STAGE_WIDTH = 14

# Windows currently open, tracked so a relaunch (theme toggle / Restart) can
# snapshot and later reopen them - see session_state()/reopen_session().
_open_windows = []


def open_windows():
    """Return the list of still-open DeploymentStatusWindow instances."""
    global _open_windows
    _open_windows = [win for win in _open_windows if win.winfo_exists()]
    return list(_open_windows)


def reopen_session(parent, session):
    """Reopen a window from a session_state() snapshot (see theme.pop_deployment_status_session)."""
    entries = [tuple(entry) for entry in (session or {}).get("entries") or []]
    stage_keys = list((session or {}).get("stage_keys") or [])
    if not entries or not stage_keys:
        return None
    return DeploymentStatusWindow(
        parent, entries, stage_keys, restore_geometry=session.get("geometry"),
    )


class DeploymentStatusWindow(tk.Toplevel):
    """Non-modal window with a borderless service x stage grid."""

    def __init__(self, parent, entries, stage_keys, restore_geometry=None,
                 on_progress=None):
        super().__init__(parent.winfo_toplevel())
        self.title("Deployment status")
        self.geometry(restore_geometry or theme.load_deployment_status_geometry() or "640x360")
        self.minsize(360, 200)
        self.configure(background=theme.BG)
        theme.apply_window_icon(self)
        theme.enable_dark_titlebar(self)
        _open_windows.append(self)

        self._entries = entries
        self._stage_keys = stage_keys
        # Optional callback(name, state, tooltip=None) mirroring
        # widgets.ProgressPanel.status, so the caller's own Details table can
        # show live per-repo progress while this window fetches its data.
        self._on_progress = on_progress

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._load(force=False)

        # Raise + focus so the window doesn't open behind the main window.
        self.lift()
        self.attributes("-topmost", True)
        self.after(
            400, lambda: self.winfo_exists() and self.attributes("-topmost", False)
        )
        self.focus_force()

    def _build_ui(self):
        header = ttk.Frame(self)
        header.pack(side="top", fill="x", padx=10, pady=(10, 6))
        ttk.Label(
            header, text="Latest commit deployed per stage",
            font=("", 11, "bold"),
        ).pack(side="left")
        self._refresh_button = ttk.Button(
            header, text="Refresh", command=self._refresh
        )
        self._refresh_button.pack(side="right")
        Tooltip(
            self._refresh_button,
            "Re-check Azure DevOps now, bypassing the short-lived cache.",
        )

        tk.Frame(self, height=1, background=theme.BORDER).pack(
            side="top", fill="x"
        )

        # Two rows of two so long explanations never get clipped off the
        # window's right edge (a single packed row would overflow).
        legend = ttk.Frame(self)
        legend.pack(side="top", fill="x", padx=10, pady=(4, 0))
        kinds = ("master", "branch_latest", "branch_older", "master_older", "unknown")
        for i in range(0, len(kinds), 2):
            row = ttk.Frame(legend)
            row.pack(side="top", fill="x", anchor="w")
            for kind in kinds[i:i + 2]:
                color_name, explanation = _CLASSIFICATION[kind]
                tk.Label(
                    row, text="\u25cf", background=theme.BG,
                    foreground=getattr(theme, color_name),
                ).pack(side="left", padx=(0, 3))
                tk.Label(
                    row, text=explanation.capitalize(), background=theme.BG,
                    foreground=theme.FG_MUTED, font=("", 8),
                ).pack(side="left", padx=(0, 12))

        shell = ttk.Frame(self)
        shell.pack(side="top", fill="both", expand=True, padx=10, pady=(6, 10))
        canvas = tk.Canvas(shell, highlightthickness=0, background=theme.BG)
        canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scroll.set)
        self._canvas = canvas
        self._inner = tk.Frame(canvas, background=theme.BG)
        self._inner_id = canvas.create_window(
            (0, 0), window=self._inner, anchor="nw"
        )
        self._inner.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfigure(self._inner_id, width=e.width),
        )
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", self._on_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

    def _on_wheel(self, event):
        self._canvas.yview_scroll(int(-event.delta / 120), "units")

    def _refresh(self):
        self._load(force=True)

    def _clear_inner(self):
        for child in self._inner.winfo_children():
            child.destroy()

    def _load(self, force):
        self._refresh_button.configure(state="disabled")
        self._clear_inner()

        # Draw the full service list up front (header + one row per repo with
        # placeholder cells), then fill each row in as its data arrives - so the
        # user sees every service immediately instead of a blank "Loading...".
        self._row_index = {}
        self._row_cells = {}
        self._build_header()
        for row, (name, _path, _branch) in enumerate(self._entries, start=2):
            tk.Label(
                self._inner, text=name, background=theme.BG, foreground=theme.FG,
                width=_NAME_WIDTH, anchor="w",
            ).grid(row=row, column=0, padx=(4, 8), pady=2, sticky="w")
            self._row_index[name] = row
            self._row_cells[name] = []
            for col in range(1, len(self._stage_keys) + 1):
                cell = tk.Label(
                    self._inner, text="\u2026", background=theme.BG,
                    foreground=theme.FG_MUTED, width=_STAGE_WIDTH, anchor="w",
                )
                cell.grid(row=row, column=col, padx=(0, 8), pady=2, sticky="w")
                self._row_cells[name].append(cell)
        self.after_idle(self._fit_to_content)

        def _work():
            def _fetch(entry):
                name, path, branch = entry
                if self._on_progress:
                    self.after(0, self._on_progress, name, "in-progress")
                ok, data = get_latest_deployed_artifacts(name, path, force=force)
                # Best-effort: a failed context lookup just disables colouring
                # for this repo's row (still shows the commit ids/messages).
                ctx_ok, context = get_deployment_commit_context(name, path, branch)
                context = context if ctx_ok else None
                # Classification does its own network calls (ancestry checks) -
                # run it HERE, on this repo's own background thread (parallel
                # across repos via run_in_parallel), never on the UI thread.
                kinds = {}
                if ok:
                    for key in self._stage_keys:
                        stage_entry = data.get(key)
                        if stage_entry and stage_entry.get("commit"):
                            kinds[key] = classify_deployed_commit(context, stage_entry["commit"])
                if self._on_progress:
                    self.after(
                        0, self._on_progress, name, "done" if ok else "error",
                        None if ok else str(data),
                    )
                # Fill this repo's row as soon as its data is ready (do not wait
                # for the slowest repo).
                self.after(0, self._render_row, name, ok, data, kinds)
                return name

            run_in_parallel(self._entries, _fetch)
            self.after(0, self._finish_load)

        threading.Thread(target=_work, daemon=True).start()

    def _build_header(self):
        tk.Label(
            self._inner, text="Service", background=theme.BG, foreground=theme.FG,
            font=("", 9, "bold"), width=_NAME_WIDTH, anchor="w",
        ).grid(row=0, column=0, padx=(4, 8), pady=4, sticky="w")
        for col, key in enumerate(self._stage_keys, start=1):
            tk.Label(
                self._inner, text=_STAGE_LABELS.get(key, key), background=theme.BG,
                foreground=theme.FG, font=("", 9, "bold"), width=_STAGE_WIDTH,
                anchor="w",
            ).grid(row=0, column=col, padx=(0, 8), pady=4, sticky="w")
        tk.Frame(self._inner, height=1, background=theme.BORDER).grid(
            row=1, column=0, columnspan=len(self._stage_keys) + 1,
            sticky="ew", pady=(0, 2),
        )

    def _render_row(self, name, ok, data, kinds):
        """Fill in one repo's stage cells (replacing its placeholders)."""
        if not self.winfo_exists():
            return
        row = self._row_index.get(name)
        if row is None:
            return
        for cell in self._row_cells.get(name, []):
            if cell.winfo_exists():
                cell.destroy()
        self._row_cells[name] = []

        if not ok:
            cell = tk.Label(
                self._inner, text="error", background=theme.BG,
                foreground=theme.ERROR, width=_STAGE_WIDTH, anchor="w",
            )
            cell.grid(row=row, column=1, padx=(0, 8), pady=2, sticky="w")
            Tooltip(cell, str(data))
            self._row_cells[name].append(cell)
            self.after_idle(self._fit_to_content)
            return

        pipeline_url = data.get("_pipeline_url") or ""
        for col, key in enumerate(self._stage_keys, start=1):
            entry = data.get(key)
            has_commit = bool(entry and entry.get("commit"))
            text = entry["commit"][:8] if has_commit else "\u2013"
            fg = theme.FG_MUTED
            if has_commit:
                color_name, _explanation = _CLASSIFICATION[kinds[key]]
                fg = getattr(theme, color_name)
            link = pipeline_url or (entry.get("url") if has_commit else "")
            cell = tk.Label(
                self._inner, text=text, background=theme.BG, foreground=fg,
                width=_STAGE_WIDTH, anchor="w",
                cursor="hand2" if has_commit and link else "",
            )
            cell.grid(row=row, column=col, padx=(0, 8), pady=2, sticky="w")
            if has_commit:
                Tooltip(cell, _cell_tooltip(kinds[key], entry))
            if has_commit and link:
                cell.bind("<Button-1>", lambda _e, u=link: webbrowser.open(u))
            self._row_cells[name].append(cell)
        self.after_idle(self._fit_to_content)

    def _finish_load(self):
        """Re-enable the refresh button once every repo's row is rendered."""
        if not self.winfo_exists():
            return
        self._refresh_button.configure(state="normal")
        self.after_idle(self._fit_to_content)


    def _fit_to_content(self):
        """Grow/shrink the window to the table's content, capped to the screen.

        Beyond the screen cap the canvas's own scrollbar takes over (content
        height still exceeds the window, so it becomes scrollable).
        """
        if not self.winfo_exists():
            return
        self.update_idletasks()
        content_height = self._inner.winfo_reqheight()
        self._canvas.configure(
            width=self._inner.winfo_reqwidth(), height=content_height,
        )
        self.update_idletasks()
        width = min(self.winfo_reqwidth(), self.winfo_screenwidth() - 80)
        height = min(self.winfo_reqheight(), self.winfo_screenheight() - 120)
        # Keep the current position, only the size is re-fitted.
        match = re.search(r"(\+-?\d+\+-?\d+)$", self.geometry())
        self.geometry(f"{width}x{height}{match.group(1) if match else ''}")

    def session_state(self):
        """Return a JSON-serialisable snapshot for restoring after a relaunch."""
        return {
            "entries": [list(entry) for entry in self._entries],
            "stage_keys": list(self._stage_keys),
            "geometry": self.geometry(),
        }

    def _on_close(self):
        theme.save_deployment_status_geometry(self.geometry())
        self.destroy()


def _cell_tooltip(kind, entry):
    """Build the cell tooltip: commit message, then origin + who triggered it.

    Extends (never overwrites) the existing commit-message tooltip. *kind* is
    the classification already computed on the background fetch thread (never
    recomputed here - it needs network calls).
    """
    lines = []
    if entry.get("message"):
        lines.append(f"- {entry['message']}")
    _color_name, explanation = _CLASSIFICATION[kind]
    lines.append(f"- {explanation}")
    if entry.get("triggered_by"):
        lines.append(f"- Triggered by: {entry['triggered_by']}")
    return "\n".join(lines)
