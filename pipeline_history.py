"""Pipeline run history.

Records a lightweight history of the pipeline monitor sessions started from the
app, so past runs can be reviewed later. A *session* is everything tracked
within one pipeline monitor window: it stores the trigger time, the workspace it
was started for (if any) and a per-repository list of runs.

Only app-initiated monitor sessions are recorded here - the app never polls
Azure DevOps for runs started by other people. A "Run new" launched from inside
a monitor is appended as a child run of the same session's repository (so the
grouping is per repo); a failed-stage retry is intentionally NOT recorded.

The store is a local, git-ignored JSON file (``pipeline_history.json``). This
module keeps the persistence and the (Toplevel) viewer window together, mirroring
``pipeline_monitor.py``.
"""

import os
import json
import uuid
import calendar
import datetime
import threading
import tkinter as tk
import webbrowser
import urllib.parse
from tkinter import ttk

import theme
from widgets import Tooltip


_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "pipeline_history.json"
)

# Cap the stored history so the file cannot grow without bound.
_MAX_SESSIONS = 200

_lock = threading.Lock()

# Stage key -> short label used to build the run "type" (e.g. build+dev+acc).
_STAGE_SHORT = {
    "build": "build",
    "development": "dev",
    "acceptance": "acc",
    "production": "prod",
}

# Human labels + colours for the per-run state badge.
_STATE_DISPLAY = {
    "run": ("started", "READY"),
    "skipped-completely": ("skipped-completely", "FG_MUTED"),
    "skipped-no-change": ("skipped-no-change", "WARNING"),
}


def _now_iso():
    """Return the current local time as a timezone-aware ISO string."""
    return datetime.datetime.now().astimezone().isoformat()


def _load():
    """Return the stored session list (newest first); [] when unavailable."""
    try:
        with open(_HISTORY_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save(data):
    """Persist the session list; write failures are ignored."""
    try:
        with open(_HISTORY_PATH, "w", encoding="utf-8") as handle:
            json.dump(list(data), handle, indent=2)
    except (OSError, TypeError):
        pass


def stage_type(stages):
    """Return the "build+dev+acc" style type string for a stage-key list."""
    parts = [_STAGE_SHORT[s] for s in (stages or []) if s in _STAGE_SHORT]
    return "+".join(parts)


def _run_state(info):
    """Classify a run info payload into one of the recorded run states."""
    if info.get("skipped") and info.get("build_id") is None:
        return "skipped-completely"
    if info.get("is_previous_run"):
        # Shown instead of a fresh run because the tip commit already deployed.
        return "skipped-no-change"
    return "run"


def _environment_of(info):
    """Best-effort environment label for a run info payload."""
    env = (info.get("environment") or "").strip()
    if env:
        return env
    return "master" if info.get("is_master_run") else ""


def _pipeline_branch(info):
    """Branch the pipeline actually ran on (master for master runs)."""
    if info.get("is_master_run"):
        return "master"
    return info.get("branch", "") or ""


def _run_from_info(info, triggered_at):
    """Build a run record from a monitor run-info payload."""
    stages = list(info.get("visible_stages") or [])
    return {
        "triggered_at": triggered_at,
        "url": info.get("url", "") or "",
        "build_id": info.get("build_id"),
        "commit_id": info.get("commit_id", "") or "",
        "commit_message": info.get("commit_message", "") or "",
        "environment": _environment_of(info),
        "stages": stages,
        "type": stage_type(stages),
        "state": _run_state(info),
    }


def child_run_from_result(info, result):
    """Build a child run record for a "Run new" launched inside the monitor."""
    stages = list(result.get("visible_stages") or info.get("visible_stages") or [])
    return {
        "triggered_at": _now_iso(),
        "url": result.get("url", "") or "",
        "build_id": result.get("build_id"),
        "commit_id": "",
        "commit_message": "",
        "environment": _environment_of(info),
        "stages": stages,
        "type": stage_type(stages),
        "state": "run",
    }


def build_session(run_infos, workspace=None):
    """Return a JSON-serialisable session record for *run_infos*."""
    triggered_at = _now_iso()
    repos = []
    for repo, info in sorted(run_infos.items()):
        repos.append({
            "repo": repo,
            "branch": info.get("branch", "") or "",
            "pipeline_branch": _pipeline_branch(info),
            "runs": [_run_from_info(info, triggered_at)],
        })
    return {
        "id": uuid.uuid4().hex,
        "started_at": triggered_at,
        "workspace": workspace or None,
        "repos": repos,
    }


def record_session(run_infos, workspace=None, monitor_kwargs=None):
    """Persist a new session for *run_infos*; return its id (or None if empty)."""
    if not run_infos:
        return None
    session = build_session(run_infos, workspace)
    # Store a reopen snapshot (full run infos + monitor options) so the viewer
    # can re-open this monitor later, reusing existing infrastructure.
    session["snapshot"] = _build_snapshot(run_infos, monitor_kwargs or {})
    with _lock:
        data = _load()
        data.insert(0, session)
        del data[_MAX_SESSIONS:]
        _save(data)
    return session["id"]


def _serialise_info(info):
    """Return a JSON-safe copy of a run-info payload (drops private keys)."""
    return {key: value for key, value in info.items()
            if not str(key).startswith("_")}


def _build_snapshot(run_infos, monitor_kwargs):
    """Return a reopen snapshot compatible with ActionTabBase.reopen_monitor_session."""
    return {
        "run_infos": {
            repo: _serialise_info(info) for repo, info in run_infos.items()
        },
        "show_autoapprove_controls": bool(
            monitor_kwargs.get("show_autoapprove_controls")
        ),
        "show_prod_control": bool(monitor_kwargs.get("show_prod_control", True)),
        "release_message": bool(monitor_kwargs.get("release_message", True)),
        "pbi_title": monitor_kwargs.get("pbi_title", "") or "",
        "test_reports": [
            list(item) for item in (monitor_kwargs.get("test_reports") or [])
        ],
    }


def add_child_run(session_id, repo, run):
    """Append *run* as a child run of *repo* within *session_id* (grouped)."""
    if not session_id or not run:
        return
    with _lock:
        data = _load()
        for session in data:
            if session.get("id") != session_id:
                continue
            for entry in session.get("repos", []):
                if entry.get("repo") == repo:
                    entry.setdefault("runs", []).append(run)
                    _save(data)
                    return
            session.setdefault("repos", []).append(
                {"repo": repo, "branch": "", "runs": [run]}
            )
            _save(data)
            return


def load_sessions():
    """Return the stored session list (newest first)."""
    with _lock:
        return _load()


def update_snapshot(session_id, snapshot):
    """Replace the reopen snapshot of *session_id* (e.g. on monitor close).

    Keeps the monitor's latest run infos and window geometry so "View monitor"
    reopens it looking as it did before.
    """
    if not session_id or not snapshot:
        return
    with _lock:
        data = _load()
        for session in data:
            if session.get("id") == session_id:
                session["snapshot"] = snapshot
                _save(data)
                return


# --------------------------------------------------------------------------- #
# Viewer window
# --------------------------------------------------------------------------- #

def _format_timestamp(value):
    """Return a local 'YYYY-MM-DD HH:MM' display string from an ISO timestamp."""
    if not value:
        return ""
    try:
        iso = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _session_environments(session):
    """Return the sorted, de-duplicated environments involved in a session."""
    envs = []
    for entry in session.get("repos", []):
        for run in entry.get("runs", []):
            env = (run.get("environment") or "").strip()
            if env and env not in envs:
                envs.append(env)
    return envs


def _parse_run_url(url):
    """Return (org, project, host) parsed from a build results URL, else None."""
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = parsed.netloc
    segments = [seg for seg in parsed.path.split("/") if seg]
    if not host:
        return None
    if host.endswith("dev.azure.com"):
        # https://dev.azure.com/{org}/{project}/_build/results?buildId=...
        if len(segments) >= 2:
            return (urllib.parse.unquote(segments[0]),
                    urllib.parse.unquote(segments[1]), host)
    elif len(segments) >= 1:
        # https://{org}.visualstudio.com/{project}/_build/results?buildId=...
        return (host.split(".")[0], urllib.parse.unquote(segments[0]), host)
    return None


def _reconstruct_run_infos(session):
    """Rebuild monitor run infos from a session's recorded display data.

    Used for older records that predate the reopen snapshot: the Azure DevOps
    context (org/project/host) is parsed from each run's build URL so the
    monitor can poll live again.
    """
    infos = {}
    for entry in session.get("repos", []):
        runs = entry.get("runs") or []
        if not runs:
            continue
        run = runs[-1]
        build_id = run.get("build_id")
        context = _parse_run_url(run.get("url") or "")
        if build_id is None or not context:
            continue
        org, project, host = context
        repo = entry.get("repo", "")
        is_master = any(
            (r.get("environment") or "").lower() == "master" for r in runs
        )
        infos[repo] = {
            "url": run.get("url", "") or "",
            "build_id": build_id,
            "org": org,
            "project": project,
            "host": host,
            "repo": repo,
            "branch": entry.get("branch", "") or "",
            "environment": run.get("environment", "") or "",
            "visible_stages": list(run.get("stages") or []),
            "is_master_run": is_master,
            "is_previous_run": True,
        }
    return infos


def _is_reproducible(session):
    """True if the session can be reopened as a monitor (snapshot or URL data)."""
    if (session.get("snapshot") or {}).get("run_infos"):
        return True
    for entry in session.get("repos", []):
        runs = entry.get("runs") or []
        if runs and runs[-1].get("build_id") is not None and runs[-1].get("url"):
            return True
    return False


def _parse_local(value):
    """Return a timezone-aware local datetime for an ISO string, or None."""
    if not value:
        return None
    try:
        iso = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt.astimezone()


# Preset time ranges offered by the overview dropdown (label, day count).
_RANGE_PRESETS = [
    ("Last 1 day", 1),
    ("Last 3 days", 3),
    ("Last 1 week", 7),
    ("Last 2 weeks", 14),
    ("Last 1 month", 30),
    ("Last 2 months", 60),
]
_DEFAULT_RANGE_DAYS = 3

_WEEKDAY_HEADERS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]


class _CalendarPopup(tk.Toplevel):
    """Borderless month calendar for picking a single date (no time)."""

    def __init__(self, anchor, initial, on_pick):
        super().__init__(anchor.winfo_toplevel())
        self.overrideredirect(True)
        self.configure(background=theme.BORDER)
        theme.enable_dark_titlebar(self)
        self._on_pick = on_pick
        self._selected = initial
        self._year = initial.year
        self._month = initial.month

        self._inner = tk.Frame(self, background=theme.BG_PANEL)
        self._inner.pack(padx=1, pady=1)
        self._render()

        # Position just below the anchoring widget.
        self.update_idletasks()
        self.geometry(
            f"+{anchor.winfo_rootx()}"
            f"+{anchor.winfo_rooty() + anchor.winfo_height() + 2}"
        )
        self.grab_set()
        self.focus_set()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<FocusOut>", lambda _e: self.destroy())

    def _render(self):
        for child in self._inner.winfo_children():
            child.destroy()

        head = tk.Frame(self._inner, background=theme.BG_PANEL)
        head.grid(row=0, column=0, columnspan=7, sticky="ew", padx=4, pady=(4, 2))
        head.grid_columnconfigure(1, weight=1)

        prev = tk.Label(head, text="\u25c4", background=theme.BG_PANEL,
                        foreground=theme.FG, cursor="hand2", padx=6)
        prev.grid(row=0, column=0)
        prev.bind("<Button-1>", lambda _e: self._shift_month(-1))
        title = tk.Label(
            head, text=f"{calendar.month_name[self._month].upper()} {self._year}",
            background=theme.BG_PANEL, foreground=theme.FG, font=("", 9, "bold"),
        )
        title.grid(row=0, column=1)
        nxt = tk.Label(head, text="\u25ba", background=theme.BG_PANEL,
                       foreground=theme.FG, cursor="hand2", padx=6)
        nxt.grid(row=0, column=2)
        nxt.bind("<Button-1>", lambda _e: self._shift_month(1))

        for col, name in enumerate(_WEEKDAY_HEADERS):
            tk.Label(
                self._inner, text=name, background=theme.BG_PANEL,
                foreground=theme.FG_MUTED, font=("", 8, "bold"), width=4,
            ).grid(row=1, column=col, padx=1, pady=1)

        cal = calendar.Calendar(firstweekday=6)  # Sunday first
        today = datetime.date.today()
        for r, week in enumerate(cal.monthdatescalendar(self._year, self._month), 2):
            for c, day in enumerate(week):
                in_month = day.month == self._month
                is_sel = day == self._selected
                is_today = day == today
                fg = theme.FG if in_month else theme.FG_MUTED
                bg = theme.ACCENT if is_sel else theme.BG_PANEL
                if is_today and not is_sel:
                    fg = theme.LINK
                cell = tk.Label(
                    self._inner, text=str(day.day), background=bg,
                    foreground=fg, width=4, cursor="hand2",
                )
                cell.grid(row=r, column=c, padx=1, pady=1)
                cell.bind("<Button-1>", lambda _e, d=day: self._pick(d))
                if not is_sel:
                    cell.bind("<Enter>",
                              lambda _e, w=cell: w.config(background=theme.BG_RAISED))
                    cell.bind("<Leave>",
                              lambda _e, w=cell, b=bg: w.config(background=b))

    def _shift_month(self, delta):
        month = self._month + delta
        year = self._year
        while month < 1:
            month += 12
            year -= 1
        while month > 12:
            month -= 12
            year += 1
        self._month, self._year = month, year
        self._render()

    def _pick(self, day):
        self._on_pick(day)
        self.destroy()


class _RangePanel(tk.Frame):
    """In-window dropdown panel for the overview time range.

    A placed child frame (not a separate window): it appears instantly and moves
    with the history window. *on_close* is called when it should be dismissed.
    """

    def __init__(self, master, current, on_apply, on_close):
        super().__init__(master, background=theme.BORDER)
        self._on_apply = on_apply
        self._on_close = on_close

        today = datetime.date.today()
        self._custom_start = current.get("start") or (
            today - datetime.timedelta(days=_DEFAULT_RANGE_DAYS)
        )
        self._custom_end = current.get("end") or today
        self._choice = tk.StringVar(
            value="custom" if current.get("custom") else str(current.get("days"))
        )

        self._inner = tk.Frame(self, background=theme.BG)
        self._inner.pack(padx=1, pady=1)
        self._build_ui()

    def _build_ui(self):
        body = ttk.Frame(self._inner, padding=12)
        body.pack(fill="both", expand=True)

        for index, (label, days) in enumerate(_RANGE_PRESETS):
            row = index // 2
            col = index % 2
            ttk.Radiobutton(
                body, text=label, value=str(days), variable=self._choice,
                command=self._sync_custom_state,
            ).grid(row=row, column=col, sticky="w", padx=(0, 16), pady=2)

        custom_row = (len(_RANGE_PRESETS) + 1) // 2
        ttk.Radiobutton(
            body, text="Custom", value="custom", variable=self._choice,
            command=self._sync_custom_state,
        ).grid(row=custom_row, column=0, sticky="w", pady=(2, 6))

        self._custom_frame = ttk.Frame(body)
        self._custom_frame.grid(row=custom_row + 1, column=0, columnspan=2,
                                sticky="w")

        ttk.Label(self._custom_frame, text="Start date").grid(
            row=0, column=0, sticky="w", pady=2
        )
        self._start_entry = ttk.Entry(self._custom_frame, width=14,
                                      state="readonly")
        self._start_entry.grid(row=0, column=1, padx=6, pady=2)
        start_btn = ttk.Button(
            self._custom_frame, text="\U0001f4c5", width=3,
            command=lambda: self._open_calendar(True, start_btn),
        )
        start_btn.grid(row=0, column=2)

        ttk.Label(self._custom_frame, text="End date").grid(
            row=1, column=0, sticky="w", pady=2
        )
        self._end_entry = ttk.Entry(self._custom_frame, width=14,
                                    state="readonly")
        self._end_entry.grid(row=1, column=1, padx=6, pady=2)
        end_btn = ttk.Button(
            self._custom_frame, text="\U0001f4c5", width=3,
            command=lambda: self._open_calendar(False, end_btn),
        )
        end_btn.grid(row=1, column=2)

        buttons = ttk.Frame(body)
        buttons.grid(row=custom_row + 2, column=0, columnspan=2,
                     sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Apply", command=self._apply).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(buttons, text="Cancel", command=self._on_close).pack(
            side="left"
        )

        self._refresh_custom_entries()
        self._sync_custom_state()

    def _set_entry(self, entry, value):
        entry.configure(state="normal")
        entry.delete(0, "end")
        entry.insert(0, value)
        entry.configure(state="readonly")

    def _refresh_custom_entries(self):
        self._set_entry(self._start_entry, self._custom_start.strftime("%Y-%m-%d"))
        self._set_entry(self._end_entry, self._custom_end.strftime("%Y-%m-%d"))

    def _sync_custom_state(self):
        # Show the date pickers only when Custom is selected; hide them entirely
        # otherwise.
        if self._choice.get() == "custom":
            self._custom_frame.grid()
        else:
            self._custom_frame.grid_remove()

    def _open_calendar(self, is_start, anchor):
        initial = self._custom_start if is_start else self._custom_end

        def _picked(day):
            if is_start:
                self._custom_start = day
                if self._custom_end < day:
                    self._custom_end = day
            else:
                self._custom_end = day
                if self._custom_start > day:
                    self._custom_start = day
            self._refresh_custom_entries()

        _CalendarPopup(anchor, initial, _picked)

    def _apply(self):
        if self._choice.get() == "custom":
            spec = {
                "custom": True,
                "start": self._custom_start,
                "end": self._custom_end,
                "days": None,
            }
        else:
            spec = {"custom": False, "days": int(self._choice.get()),
                    "start": None, "end": None}
        self._on_apply(spec)
        self._on_close()


class PipelineHistoryWindow(tk.Toplevel):
    """Read-only window listing recorded pipeline monitor sessions."""

    def __init__(self, parent, app=None):
        super().__init__(parent.winfo_toplevel())
        self.title("Pipeline history")
        self.geometry("820x600")
        self.minsize(420, 240)
        self.configure(background=theme.BG)
        theme.apply_window_icon(self)
        theme.enable_dark_titlebar(self)

        # App reference so "View monitor" can reuse / reopen monitor windows.
        self._app = app

        # Overview time range (default: last 3 days). Custom holds date objects.
        self._range = {
            "custom": False, "days": _DEFAULT_RANGE_DAYS,
            "start": None, "end": None,
        }
        self._range_panel = None
        self._range_click_bind = None

        self._build_ui()
        self._reload()
        # Warm the ttk Radiobutton style so the first range-panel open is snappy.
        self.after_idle(self._prewarm_range_style)

    def _prewarm_range_style(self):
        try:
            warm = _RangePanel(
                self, {"custom": False, "days": _DEFAULT_RANGE_DAYS},
                lambda _s: None, lambda: None,
            )
            warm.place(x=-2000, y=-2000)
            warm.update_idletasks()
            warm.destroy()
        except Exception:
            pass

    def _range_label(self):
        """Human label for the current range, shown on the dropdown button."""
        if self._range.get("custom"):
            start = self._range.get("start")
            end = self._range.get("end")
            return f"{start:%Y-%m-%d} \u2192 {end:%Y-%m-%d}"
        days = self._range.get("days")
        for label, preset_days in _RANGE_PRESETS:
            if preset_days == days:
                return label
        return f"Last {days} days"

    def _build_ui(self):
        header = ttk.Frame(self)
        header.pack(side="top", fill="x", padx=10, pady=(10, 6))
        ttk.Label(
            header, text="Recent pipeline monitor sessions",
            font=("", 11, "bold"),
        ).pack(side="left")

        self._range_button = ttk.Button(
            header, text=f"{self._range_label()}  \u25be",
            command=self._open_range_dialog,
        )
        self._range_button.pack(side="left", padx=(16, 0))

        refresh_button = ttk.Button(
            header, text="Refresh", command=self._reload
        )
        refresh_button.pack(side="right")
        Tooltip(
            refresh_button,
            "Reload the pipeline history from disk to pick up sessions recorded "
            "since this window was opened.",
        )

        tk.Frame(self, height=1, background=theme.BORDER).pack(
            side="top", fill="x"
        )

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
        # Mouse-wheel scrolling while the pointer is over the list.
        canvas.bind("<Enter>", lambda _e: self._bind_wheel())
        canvas.bind("<Leave>", lambda _e: self._unbind_wheel())

    def _bind_wheel(self):
        self._canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self):
        self._canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event):
        self._canvas.yview_scroll(int(-event.delta / 120), "units")

    def _open_range_dialog(self):
        # Toggle: a second click on the button closes the open panel.
        if getattr(self, "_range_panel", None) is not None and \
                self._range_panel.winfo_exists():
            self._close_range_panel()
            return

        def _apply(spec):
            self._range = spec
            self._range_button.configure(text=f"{self._range_label()}  \u25be")
            self._reload()

        panel = _RangePanel(
            self, dict(self._range), _apply, self._close_range_panel
        )
        self._range_panel = panel
        # Place just under the range button, relative to this window so it moves
        # with the window (an in-window panel, not a separate popup).
        btn = self._range_button
        x = btn.winfo_rootx() - self.winfo_rootx()
        y = btn.winfo_rooty() - self.winfo_rooty() + btn.winfo_height() + 2
        panel.place(x=max(x, 0), y=max(y, 0))
        panel.lift()
        # Dismiss when clicking anywhere outside the panel or its button.
        self._range_click_bind = self.bind(
            "<Button-1>", self._maybe_close_range_panel, add="+"
        )
        self.bind("<Escape>", lambda _e: self._close_range_panel(), add="+")

    def _close_range_panel(self):
        panel = getattr(self, "_range_panel", None)
        if panel is not None and panel.winfo_exists():
            panel.destroy()
        self._range_panel = None
        bind_id = getattr(self, "_range_click_bind", None)
        if bind_id:
            self.unbind("<Button-1>", bind_id)
            self._range_click_bind = None

    def _maybe_close_range_panel(self, event):
        panel = getattr(self, "_range_panel", None)
        if panel is None or not panel.winfo_exists():
            return
        # Keep the panel open while interacting with it or the range button.
        node = event.widget
        while node is not None:
            if node is panel or node is self._range_button:
                return
            node = getattr(node, "master", None)
        self._close_range_panel()

    def _session_in_range(self, session):
        """True if the session's start time falls within the selected range."""
        started = _parse_local(session.get("started_at"))
        if started is None:
            return True
        if self._range.get("custom"):
            start = self._range.get("start")
            end = self._range.get("end")
            start_dt = datetime.datetime.combine(
                start, datetime.time.min
            ).astimezone()
            end_dt = datetime.datetime.combine(
                end, datetime.time.max
            ).astimezone()
            return start_dt <= started <= end_dt
        cutoff = datetime.datetime.now().astimezone() - datetime.timedelta(
            days=self._range.get("days", _DEFAULT_RANGE_DAYS)
        )
        return started >= cutoff

    def _reload(self):
        for child in self._inner.winfo_children():
            child.destroy()

        sessions = load_sessions()
        if not sessions:
            tk.Label(
                self._inner, text="No pipeline history yet.",
                background=theme.BG, foreground=theme.FG_MUTED,
                padx=8, pady=12,
            ).pack(anchor="w")
            return

        visible = [s for s in sessions if self._session_in_range(s)]
        if not visible:
            tk.Label(
                self._inner,
                text="No pipeline monitor sessions in the selected time range.",
                background=theme.BG, foreground=theme.FG_MUTED,
                padx=8, pady=12,
            ).pack(anchor="w")
            return

        for session in visible:
            self._build_session_card(session)

    def _build_session_card(self, session):
        card = tk.Frame(self._inner, background=theme.BG_PANEL,
                        highlightbackground=theme.BORDER, highlightthickness=1)
        card.pack(fill="x", pady=(0, 8), padx=1)

        head = tk.Frame(card, background=theme.BG_PANEL)
        head.pack(fill="x", padx=10, pady=(8, 4))

        title = _format_timestamp(session.get("started_at"))
        workspace = session.get("workspace")
        if workspace:
            title += f"   \u2022   {workspace}"
        else:
            title += "   \u2022   (no workspace)"
        tk.Label(
            head, text=title, background=theme.BG_PANEL, foreground=theme.FG,
            font=("", 10, "bold"), anchor="w",
        ).pack(side="left")

        # "View monitor" (next to the title) reuses an already-open monitor for
        # this session, or reproduces it (from the snapshot, or older records'
        # URL data) at its latest recorded state.
        if self._app is not None and _is_reproducible(session):
            view_button = ttk.Button(
                head, text="View monitor",
                command=lambda s=session: self._view_monitor(s),
            )
            view_button.pack(side="left", padx=(12, 0))
            Tooltip(
                view_button,
                "Open the pipeline monitor for this session. If it is still open "
                "it is brought to the front; otherwise it is reproduced from the "
                "recorded history at its latest state (then refreshed live).",
            )

        envs = _session_environments(session)
        if envs:
            tk.Label(
                head, text="  ".join(envs), background=theme.BG_PANEL,
                foreground=theme.FG_MUTED, anchor="e",
            ).pack(side="right")

        for entry in session.get("repos", []):
            self._build_repo_row(card, entry)

    def _view_monitor(self, session):
        """Focus the open monitor for this session, or reopen it from snapshot."""
        app = self._app
        if app is None:
            return
        session_id = session.get("id")

        # 1) Reuse a still-open monitor for this session.
        for tab in (app.workspaces_tab, app.manual_tab):
            for win in list(getattr(tab, "_pipeline_monitors", [])):
                if (win.winfo_exists()
                        and getattr(win, "history_session_id", None) == session_id):
                    win.deiconify()
                    win.lift()
                    win.attributes("-topmost", True)
                    win.after(200, lambda w=win: w.attributes("-topmost", False))
                    win.focus_force()
                    return

        # 2) Reproduce from the stored snapshot, or (older records) rebuild the
        # run infos from the recorded URL data. Keep the same history session.
        snapshot = session.get("snapshot") or {}
        run_infos = snapshot.get("run_infos") or _reconstruct_run_infos(session)
        if not run_infos:
            return
        # Older snapshots may have stored URL-encoded org/project (e.g.
        # "Custom%20Software%20Services"); decode so the API does not re-encode
        # them into a 404. unquote is idempotent for already-decoded values.
        for info in run_infos.values():
            for key in ("org", "project"):
                value = info.get(key)
                if isinstance(value, str):
                    info[key] = urllib.parse.unquote(value)
        reopen = dict(snapshot)
        reopen["run_infos"] = run_infos
        reopen["history_session_id"] = session_id
        app.workspaces_tab.reopen_monitor_session(reopen)

    def _build_repo_row(self, card, entry):
        runs = entry.get("runs") or []
        if not runs:
            return
        latest = runs[-1]
        previous = list(reversed(runs[:-1]))

        # The original workspace/feature branch is kept only as a hover on the
        # service name; the pipeline branch is shown once, as the grey
        # environments label in the session header.
        original_branch = entry.get("branch", "") or ""

        row = tk.Frame(card, background=theme.BG_PANEL)
        row.pack(fill="x", padx=10, pady=(0, 4))

        top = tk.Frame(row, background=theme.BG_PANEL)
        top.pack(fill="x")

        name_label = tk.Label(
            top, text=entry.get("repo", ""), background=theme.BG_PANEL,
            foreground=theme.FG, font=("", 9, "bold"), width=26, anchor="w",
        )
        name_label.pack(side="left")
        if original_branch:
            Tooltip(name_label, f"PR into master from: {original_branch}")

        self._pack_run_details(top, latest)

        if previous:
            body = tk.Frame(row, background=theme.BG_PANEL)
            toggle = tk.Label(
                row, text=f"+{len(previous)} more runs",
                background=theme.BG_PANEL, foreground=theme.LINK,
                cursor="hand2", font=("", 8, "underline"), anchor="w",
            )
            toggle.pack(anchor="w", padx=(26 * 7, 0))

            def _toggle(_e=None, body=body, toggle=toggle):
                if body.winfo_ismapped():
                    body.pack_forget()
                    toggle.configure(text=f"+{len(previous)} more runs")
                else:
                    body.pack(fill="x", padx=(26 * 7, 0))
                    toggle.configure(text="hide previous runs")

            toggle.bind("<Button-1>", _toggle)
            for run in previous:
                pr = tk.Frame(body, background=theme.BG_PANEL)
                pr.pack(fill="x")
                self._pack_run_details(pr, run, muted=True)

    def _pack_run_details(self, parent, run, muted=False):
        fg = theme.FG_MUTED if muted else theme.FG
        base = tk.Frame(parent, background=theme.BG_PANEL)
        base.pack(side="left", fill="x", expand=True)

        # Commit number; the full commit message shows as a hover tooltip.
        commit_id = (run.get("commit_id") or "")[:8]
        if commit_id:
            commit_label = tk.Label(
                base, text=commit_id, background=theme.BG_PANEL,
                foreground=fg, anchor="w",
            )
            commit_label.pack(side="left", padx=(12, 0))
            commit_msg = run.get("commit_message") or ""
            if commit_msg:
                Tooltip(commit_label, commit_msg)

        # Type badge.
        type_text = run.get("type") or ""
        if type_text:
            tk.Label(
                base, text=f"[{type_text}]", background=theme.BG_PANEL,
                foreground=theme.FG_MUTED, anchor="w",
            ).pack(side="left", padx=(8, 0))

        # State badge.
        label, color_key = _STATE_DISPLAY.get(
            run.get("state"), (run.get("state") or "", "FG_MUTED")
        )
        if label:
            tk.Label(
                base, text=label, background=theme.BG_PANEL,
                foreground=getattr(theme, color_key, theme.FG_MUTED),
                anchor="w",
            ).pack(side="left", padx=(8, 0))

        # Pipeline link.
        url = run.get("url") or ""
        if url:
            link = tk.Label(
                base, text="Pipeline link", background=theme.BG_PANEL,
                foreground=theme.LINK, cursor="hand2",
                font=("", 8, "underline"), anchor="w",
            )
            link.pack(side="right", padx=(8, 0))
            link.bind("<Button-1>", lambda _e, u=url: webbrowser.open(u, new=2))
