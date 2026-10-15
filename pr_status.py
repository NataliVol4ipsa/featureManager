""""View PR status" window: pull-request state + comments per repository.

Mirrors the fetch philosophy of :mod:`deployment_status` - each repository is
scanned on its own background thread (fanned out with ``run_in_parallel``) and
its row is filled in as soon as its data arrives, so the UI never blocks.

Per repo the initial parallel scan fetches the PR, its comment threads, and
branch-policy evaluations. Failed builds also fetch one timeline for error
details. The fetched data is reused by the table and details windows.

  1. ``gitutils.fetch_pr_status``  - the latest PR for the branch (any status):
     draft flag, reviewer votes, merge conflicts, aborted/abandoned.
  2. ``gitutils.fetch_pr_threads`` - the comment threads: human thread count,
     unresolved count, and the full thread tree for the details view.

Clicking anywhere in a service row opens a modeless details window: reviewer
votes (who voted what), comment threads, and build/merge readiness at the end.
"""

import re
import threading
import webbrowser
import tkinter as tk
from datetime import datetime
from tkinter import ttk, messagebox

import messages
import theme
from widgets import Tooltip
from parallel import run_in_parallel
from gitutils import fetch_pr_status, fetch_pr_threads, fetch_pr_policy, set_pr_draft
import pipelines


# (key, header, width) for every table column, in display order.
_COLUMNS = (
    ("service", "Service", 24),
    ("draft", "Draft", 7),
    ("build", "Build", 7),
    ("vote", "Vote", 0),
    ("ready", "Ready", 9),
    ("comments", "Comments", 9),
    ("conflicts", "Conflicts", 9),
    ("complete", "", 9),
    ("prlink", "", 8),
)


def _fmt_date(value):
    """Format an Azure DevOps ISO timestamp as 'YYYY-MM-DD HH:MM' (local-ish)."""
    if not value:
        return ""
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return value[:16].replace("T", " ")
    return parsed.strftime("%Y-%m-%d %H:%M")


def _vote_color(summary):
    """Theme colour name for an overall vote summary cell."""
    if summary in ("rejected", "waiting for author"):
        return "ERROR"
    if summary == "approved":
        return "SUCCESS"
    if summary == "approved (partial)":
        return "READY"
    return "FG_MUTED"


class PrStatusWindow(tk.Toplevel):
    """Non-modal window listing each repo's pull-request state."""

    def __init__(self, parent, entries, on_progress=None, restore_geometry=None):
        super().__init__(parent.winfo_toplevel())
        self.withdraw()
        self.title("Pull request status")
        self.geometry(restore_geometry or theme.load_pr_status_geometry() or "820x360")
        self.minsize(520, 200)
        self.configure(background=theme.BG)
        theme.apply_window_icon(self)
        theme.enable_dark_titlebar(self)

        self._entries = entries
        self._on_progress = on_progress
        self._tab = parent
        self._path_by_name = {name: path for name, path, _branch in entries}
        self._branch_by_name = {name: branch for name, _path, branch in entries}
        self._show_complete = True
        self._row_index = {}
        self._row_data = {}        # name -> fetched PR, policy and comment data
        self._service_cells = {}
        self._row_exclude = {}     # name -> widgets that keep their own click
        self._detail_windows = {}  # name -> open PrDetailWindow
        self._draft_updates = set()
        self._loading = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._load(force=False)

        self.update_idletasks()
        self.deiconify()
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
            header, text="Pull request state & comments",
            font=("", 11, "bold"),
        ).pack(side="left")
        self._refresh_button = ttk.Button(
            header, text="Refresh", command=self._refresh
        )
        self._refresh_button.pack(side="right")
        Tooltip(self._refresh_button, "Re-check Azure DevOps now.")
        self._copy_links_button = ttk.Button(
            header, text="Copy PR links", command=self._copy_pr_links
        )
        self._copy_links_button.pack(side="right", padx=(0, 6))
        Tooltip(
            self._copy_links_button,
            "Copy the loaded pull requests as 'repo name - pr link' lines "
            "to the clipboard.",
        )
        self._complete_all_button = ttk.Button(
            header, text="Complete all", command=self._complete_all
        )
        Tooltip(
            self._complete_all_button,
            "Complete (merge) every listed pull request - shown only when they "
            "are all ready to merge.",
        )

        tk.Frame(self, height=1, background=theme.BORDER).pack(side="top", fill="x")

        hint = ttk.Label(
            self, text="Click anywhere on a service row to open its details.",
            foreground=theme.FG_MUTED, font=("", 8),
        )
        hint.pack(side="top", anchor="w", padx=10, pady=(4, 0))

        shell = ttk.Frame(self)
        shell.pack(side="top", fill="both", expand=True, padx=10, pady=(6, 10))
        canvas = tk.Canvas(shell, highlightthickness=0, background=theme.BG)
        canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scroll.set)
        self._canvas = canvas
        self._inner = tk.Frame(canvas, background=theme.BG)
        self._inner_id = canvas.create_window((0, 0), window=self._inner, anchor="nw")
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

    def _copy_pr_links(self):
        items = []
        for name, _path, _branch in self._entries:
            data = self._row_data.get(name)
            if data and data["ok"] and data["info"]:
                url = data["info"].get("web_url")
                if url:
                    items.append((name, url))
        messages.copy_message(self, messages.build_message(items))

    def _clear_inner(self):
        for child in self._inner.winfo_children():
            child.destroy()

    def _columns(self):
        if self._show_complete:
            return _COLUMNS
        return tuple(col for col in _COLUMNS if col[0] != "complete")

    def _build_header(self):
        cols = self._columns()
        for col, (_key, label, width) in enumerate(cols):
            tk.Label(
                self._inner, text=label, background=theme.BG, foreground=theme.FG,
                font=("", 9, "bold"), width=width, anchor="w",
            ).grid(row=0, column=col, padx=4, pady=4, sticky="w")
        tk.Frame(self._inner, height=1, background=theme.BORDER).grid(
            row=1, column=0, columnspan=len(cols), sticky="ew", pady=(0, 2),
        )

    def _load(self, force):
        self._loading = True
        self._show_complete = True
        self._refresh_button.configure(state="disabled")
        self._clear_inner()
        self._row_index = {}
        self._row_data = {}
        self._service_cells = {}
        self._row_exclude = {}
        self._complete_all_button.pack_forget()
        self._build_header()
        cols = self._columns()
        for row, (name, _path, _branch) in enumerate(self._entries, start=2):
            service = tk.Frame(self._inner, background=theme.BG, cursor="hand2")
            service.grid(row=row, column=0, padx=4, pady=2, sticky="ew")
            tk.Label(
                service, text=name, background=theme.BG, foreground=theme.FG,
                anchor="w", cursor="hand2",
            ).pack(side="left")
            self._service_cells[name] = service
            self._row_index[name] = row
            for col, (_key, _label, width) in enumerate(cols[1:], start=1):
                tk.Label(
                    self._inner, text="\u2026", background=theme.BG,
                    foreground=theme.FG_MUTED, width=width, anchor="w",
                ).grid(row=row, column=col, padx=4, pady=2, sticky="w")
        self.after_idle(self._fit_to_content)

        def _work():
            def _fetch(entry):
                name, path, branch = entry
                if self._on_progress:
                    self.after(0, self._on_progress, name, "in-progress")
                ok, info = fetch_pr_status(name, path, branch=branch)
                threads, terr = [], None
                policy_ok, policy, build_errors = False, None, None
                if ok and info:
                    tok, tdata = fetch_pr_threads(info["ctx"])
                    if tok:
                        threads = tdata
                    else:
                        terr = tdata
                    policy_ok, policy = fetch_pr_policy(info["ctx"])
                    if (policy_ok and policy.get("build_state") == "failed"
                            and policy.get("build_id")):
                        eok, edata = pipelines.get_build_errors(
                            info["ctx"]["org"], info["ctx"]["project"],
                            info["ctx"]["host"], policy["build_id"],
                        )
                        build_errors = edata if eok else [
                            f"(could not read build log: {edata})"
                        ]
                if self._on_progress:
                    state = "done" if ok else "error"
                    self.after(0, self._on_progress, name, state,
                               None if ok else str(info))
                self.after(
                    0, self._render_row, name, ok, info, threads, terr,
                    policy_ok, policy, build_errors,
                )
                return name

            run_in_parallel(self._entries, _fetch)
            self.after(0, self._finish_load)

        threading.Thread(target=_work, daemon=True).start()

    def _render_row(self, name, ok, info, threads, terr,
                    policy_ok, policy, build_errors):
        if not self.winfo_exists():
            return
        row = self._row_index.get(name)
        if row is None:
            return

        human_threads = [t for t in threads if not t["is_system"]] if threads else []
        resolved = sum(1 for t in human_threads if t["resolved"])
        unresolved = len(human_threads) - resolved
        self._row_data[name] = {
            "ok": ok, "info": info, "threads": threads, "unresolved": unresolved,
            "resolved": resolved, "policy_ok": policy_ok,
            "policy": policy, "build_errors": build_errors,
            "thread_error": terr,
        }

        # Clear every cell except the service name (column 0) before refilling.
        self._row_exclude[name] = []
        for child in self._inner.grid_slaves(row=row):
            if int(child.grid_info()["column"]) != 0:
                child.destroy()

        self._fill_row_cells(name, row)
        self.after_idle(self._fit_to_content)

    def _fill_row_cells(self, name, row):
        data = self._row_data.get(name)
        if not data:
            return
        cols = self._columns()

        if not data["ok"]:
            cell = tk.Label(
                self._inner, text="error", background=theme.BG,
                foreground=theme.ERROR, width=cols[1][2], anchor="w",
            )
            cell.grid(row=row, column=1, padx=4, pady=2, sticky="w")
            Tooltip(cell, str(data["info"]))
            self._bind_row(name)
            return

        info = data["info"]
        if not info:
            cell = tk.Label(
                self._inner, text="no PR", background=theme.BG,
                foreground=theme.FG_MUTED, width=cols[1][2], anchor="w",
            )
            cell.grid(row=row, column=1, padx=4, pady=2, sticky="w")
            self._bind_row(name)
            return

        threads = data["threads"]
        human_threads = [t for t in threads if not t["is_system"]] if threads else []
        resolved = data["resolved"]
        unresolved = data["unresolved"]
        terr = data["thread_error"]
        policy_ok = data["policy_ok"]
        policy = data["policy"]

        service = self._service_cells.get(name)
        if service:
            for child in service.winfo_children()[1:]:
                child.destroy()
            if info["status"] == "abandoned":
                status_label = "aborted"
            else:
                status_label = ""
            if status_label:
                tk.Label(
                    service, text=status_label, background=theme.BG,
                    foreground=theme.FG_MUTED, font=("", 8, "italic"),
                    cursor="hand2",
                ).pack(side="left", padx=(6, 0))

        approver_count = sum(
            reviewer["vote"] >= 5
            for reviewer in info.get("reviewers", [])
            if not reviewer["is_container"]
        )
        values = {
            "vote": (f"{info['vote_summary']}({approver_count})",
                     _vote_color(info["vote_summary"])),
            "build": self._build_summary(policy_ok, policy),
            "ready": self._ready_summary(
                info, policy_ok, policy, unresolved, threads_ok=terr is None,
            ),
            "conflicts": ("conflicts" if info["has_conflicts"] else "", "ERROR"),
        }
        for col, (key, _label, width) in enumerate(cols):
            if key == "service":
                continue
            if key == "draft":
                self._build_draft_cell(name, row, col, width, info)
                continue
            if key == "complete":
                if info["status"] == "active":
                    btn = ttk.Button(
                        self._inner, text="complete", width=width,
                        command=lambda n=name: self._complete_one(n),
                        state="normal" if self._is_ready(data)
                        and name not in self._draft_updates else "disabled",
                    )
                    btn.grid(row=row, column=col, padx=4, pady=1, sticky="w")
                    Tooltip(btn, "Complete (merge) this pull request once it is ready.")
                    self._row_exclude.setdefault(name, []).append(btn)
                continue
            if key == "prlink":
                link = tk.Label(
                    self._inner, text=f"#{info['pr_id']}", background=theme.BG,
                    foreground=theme.LINK, cursor="hand2", width=width, anchor="w",
                )
                link.grid(row=row, column=col, padx=4, pady=2, sticky="w")
                link.bind("<Button-1>",
                          lambda _e, u=info["web_url"]: webbrowser.open(u, new=2))
                Tooltip(link, info["web_url"])
                self._row_exclude.setdefault(name, []).append(link)
                continue
            if key == "comments":
                count_cell = tk.Frame(self._inner, background=theme.BG, cursor="hand2")
                count_cell.grid(row=row, column=col, padx=4, pady=2, sticky="ew")
                if terr:
                    comment_counts = ("?", "?", theme.FG_MUTED)
                else:
                    resolved_color = (
                        theme.SUCCESS if resolved == len(human_threads)
                        else theme.WARNING
                    )
                    comment_counts = (str(resolved), str(len(human_threads)), resolved_color)
                tk.Label(
                    count_cell, text=comment_counts[0], background=theme.BG,
                    foreground=comment_counts[2], cursor="hand2", padx=0,
                ).pack(side="left")
                tk.Label(
                    count_cell, text=f"/{comment_counts[1]}", background=theme.BG,
                    foreground=theme.FG_MUTED, cursor="hand2", padx=0,
                ).pack(side="left")
                continue
            text, color_name = values[key]
            cell = tk.Label(
                self._inner, text=text, background=theme.BG,
                foreground=getattr(theme, color_name), width=width, anchor="w",
                cursor="hand2",
            )
            cell.grid(row=row, column=col, padx=4, pady=2, sticky="w")
            if key == "vote":
                Tooltip(cell, info["vote_summary"])
        self._bind_row(name)

    def _build_draft_cell(self, name, row, col, width, info):
        cell = tk.Frame(self._inner, background=theme.BG)
        cell.grid(row=row, column=col, padx=4, pady=1, sticky="w")
        pending = name in self._draft_updates
        label = tk.Label(
            cell, text="toggling...", font=("", 8, "italic"),
            background=theme.BG, foreground=theme.FG_MUTED, anchor="w",
        )
        button = ttk.Button(
            cell, text="toggle", width=width,
            command=lambda: self._toggle_draft(name),
            state="disabled" if name in self._draft_updates else "normal",
        )
        Tooltip(
            button,
            f"Publish the draft pull request for {name}." if info["is_draft"]
            else f"Mark the pull request for {name} as draft.",
        )
        cell.configure(
            width=max(label.winfo_reqwidth(), button.winfo_reqwidth()),
            height=max(label.winfo_reqheight(), button.winfo_reqheight()),
        )
        if not pending:
            label.configure(
                text="draft" if info["is_draft"] else "-", font=("", 9),
            )
        label.place(x=0, y=0, relwidth=1, relheight=1)
        if info["status"] != "active":
            return
        self._row_exclude.setdefault(name, []).append(cell)
        if pending:
            return

        def show(_event):
            button.place(x=0, y=0, relwidth=1, relheight=1)
            button.lift()

        def hide(_event):
            button.place_forget()

        label.bind("<Enter>", show)
        button.bind("<Leave>", hide, add="+")

    def _toggle_draft(self, name):
        data = self._row_data.get(name)
        if (not data or not data["info"]
                or data["info"]["status"] != "active"
                or name in self._draft_updates):
            return
        draft = not data["info"]["is_draft"]
        self._draft_updates.add(name)
        self._render_row(
            name, data["ok"], data["info"], data["threads"], data["thread_error"],
            data["policy_ok"], data["policy"], data["build_errors"],
        )
        self._update_actions()

        def work():
            try:
                ok, result = set_pr_draft(data["info"]["ctx"], draft)
            except Exception as exc:
                ok, result = False, str(exc)
            policy_ok, policy = False, None
            if ok:
                try:
                    policy_ok, policy = fetch_pr_policy(data["info"]["ctx"])
                except Exception as exc:
                    policy = str(exc)
            self.after(
                0, self._draft_toggled, name, data, draft, ok, result,
                policy_ok, policy,
            )

        threading.Thread(target=work, daemon=True).start()

    def _draft_toggled(self, name, data, draft, ok, result, policy_ok, policy):
        if not self.winfo_exists():
            return
        self._draft_updates.discard(name)
        if ok:
            data["info"]["is_draft"] = draft
            data["policy_ok"], data["policy"] = policy_ok, policy
            detail = self._detail_windows.pop(name, None)
            if detail is not None and detail.winfo_exists():
                detail.destroy()
        self._render_row(
            name, data["ok"], data["info"], data["threads"], data["thread_error"],
            data["policy_ok"], data["policy"], data["build_errors"],
        )
        self._update_actions()
        if not ok:
            messagebox.showerror("Could not toggle draft", str(result), parent=self)

    def _redraw_table(self):
        self._clear_inner()
        self._row_index = {}
        self._service_cells = {}
        self._row_exclude = {}
        self._build_header()
        for row, (name, _path, _branch) in enumerate(self._entries, start=2):
            self._row_index[name] = row
            service = tk.Frame(self._inner, background=theme.BG, cursor="hand2")
            service.grid(row=row, column=0, padx=4, pady=2, sticky="ew")
            tk.Label(
                service, text=name, background=theme.BG, foreground=theme.FG,
                anchor="w", cursor="hand2",
            ).pack(side="left")
            self._service_cells[name] = service
            self._fill_row_cells(name, row)

    def _open_details(self, name):
        data = self._row_data.get(name)
        if not data:
            return
        existing = self._detail_windows.get(name)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            return
        win = PrDetailWindow(self, name, data)
        self._detail_windows[name] = win
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_detail(name, win))

    def _close_detail(self, name, win):
        self._detail_windows.pop(name, None)
        if win.winfo_exists():
            win.destroy()

    def _bind_row(self, name):
        """Make each visible widget in a PR row open the same details window."""
        row = self._row_index.get(name)
        if row is None:
            return
        exclude = set(self._row_exclude.get(name, []))
        widgets = list(self._inner.grid_slaves(row=row))
        service = self._service_cells.get(name)
        if service:
            widgets.append(service)
        pending = list(widgets)
        while pending:
            widget = pending.pop()
            if widget in exclude:
                continue
            pending.extend(widget.winfo_children())
            widget.configure(cursor="hand2")
            widget.bind("<Button-1>", lambda _e, n=name: self._open_details(n))

    def _completable(self):
        """(name, path, data) for every repo with an active pull request."""
        items = []
        for name in self._row_index:
            data = self._row_data.get(name)
            if data and data["info"] and data["info"]["status"] == "active":
                items.append((name, self._path_by_name.get(name), data))
        return items

    def _is_ready(self, data):
        text, _color = self._ready_summary(
            data["info"], data["policy_ok"], data["policy"],
            data["unresolved"], threads_ok=data["thread_error"] is None,
        )
        return text == "yes"

    def _all_ready(self):
        """True when every PR is ready to merge or already completed, and at
        least one is still completable (an all-completed set hides the button).
        """
        if self._loading or self._draft_updates:
            return False
        prs = [self._row_data.get(n) for n in self._row_index]
        prs = [d for d in prs if d and d["info"]]
        if not prs:
            return False
        to_complete = 0
        for data in prs:
            status = data["info"]["status"]
            if status == "completed":
                continue
            if status == "active" and self._is_ready(data):
                to_complete += 1
                continue
            return False
        return to_complete > 0

    def _complete_one(self, name):
        data = self._row_data.get(name)
        if not data or name in self._draft_updates or not self._is_ready(data):
            return
        path = self._path_by_name.get(name)
        if path:
            self._tab.complete_prs(
                [(name, path)], branches={name: self._branch_by_name.get(name)}
            )

    def _complete_all(self):
        if not self._all_ready():
            return
        repos = [(name, path) for name, path, _d in self._completable()]
        if repos:
            branches = {name: self._branch_by_name.get(name) for name, _p in repos}
            self._tab.complete_prs(repos, branches=branches)

    @staticmethod
    def _build_summary(policy_ok, policy):
        if not policy_ok:
            return "error", "ERROR"
        return {
            "succeeded": ("passed", "SUCCESS"),
            "failed": ("failed", "ERROR"),
            "running": ("running", "WARNING"),
            "none": ("-", "FG_MUTED"),
        }.get(policy.get("build_state"), ("?", "FG_MUTED"))

    @staticmethod
    def _ready_summary(info, policy_ok, policy, unresolved, threads_ok=True):
        if info.get("status") == "completed":
            return "completed", "COMPLETED"
        if not policy_ok or not threads_ok:
            return "?", "FG_MUTED"
        ready = (
            not info["is_draft"]
            and info["status"] != "abandoned"
            and not info["has_conflicts"]
            and not policy.get("rejected")
            and not policy.get("pending")
            and policy.get("build_state") in ("succeeded", "none")
            and info["vote_summary"] not in ("rejected", "waiting for author")
            and unresolved == 0
        )
        return ("yes" if ready else "no", "SUCCESS" if ready else "FG_MUTED")

    def _finish_load(self):
        if not self.winfo_exists():
            return
        self._loading = False
        has_any_complete = any(
            d and d.get("info") and d["info"].get("status") == "active"
            for d in self._row_data.values()
        )
        if not has_any_complete and self._show_complete:
            self._show_complete = False
            self._redraw_table()

        self._update_actions()
        self.after_idle(self._fit_to_content)

    def _update_actions(self):
        self._refresh_button.configure(
            state="disabled" if self._loading or self._draft_updates else "normal"
        )
        if self._all_ready():
            self._complete_all_button.pack(side="right", padx=(0, 6))
        else:
            self._complete_all_button.pack_forget()

    def _fit_to_content(self):
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
        match = re.search(r"(\+-?\d+\+-?\d+)$", self.geometry())
        self.geometry(f"{width}x{height}{match.group(1) if match else ''}")

    def _on_close(self):
        theme.save_pr_status_geometry(self.geometry())
        for win in list(self._detail_windows.values()):
            if win.winfo_exists():
                win.destroy()
        self._detail_windows.clear()
        self.destroy()


class PrDetailWindow(tk.Toplevel):
    """Modeless per-repo details: votes, comments, and build readiness."""

    def __init__(self, parent, name, data):
        super().__init__(parent.winfo_toplevel())
        self.withdraw()
        self.transient(parent.winfo_toplevel())
        self._name = name
        self._info = data["info"]
        self._threads = data["threads"]
        self._unresolved = data["unresolved"]
        self._policy_ok = data["policy_ok"]
        self._policy = data["policy"]
        self._build_errors = data["build_errors"]
        self._thread_error = data["thread_error"]
        self.title(f"Pull request - {name}")
        self.geometry("680x560")
        self.minsize(420, 320)
        self.configure(background=theme.BG)
        theme.apply_window_icon(self)
        theme.enable_dark_titlebar(self)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.update_idletasks()
        parent_top = parent.winfo_toplevel()
        parent_x, parent_y = parent_top.winfo_rootx(), parent_top.winfo_rooty()
        parent_width, parent_height = parent_top.winfo_width(), parent_top.winfo_height()
        width, height = self.winfo_width(), self.winfo_height()
        self.geometry(
            f"+{parent_x + (parent_width - width) // 2}"
            f"+{parent_y + (parent_height - height) // 2}"
        )
        self.deiconify()
        self.lift()
        self.focus_force()

    def _build_ui(self):
        info = self._info
        header = ttk.Frame(self)
        header.pack(side="top", fill="x", padx=12, pady=(10, 4))
        title = info.get("title") or self._name
        ttk.Label(header, text=title, font=("", 11, "bold")).pack(side="left")
        link = tk.Label(
            header, text="open in browser", background=theme.BG,
            foreground=theme.LINK, cursor="hand2",
        )
        link.pack(side="right")
        link.bind("<Button-1>", lambda _e: webbrowser.open(info["web_url"]))

        sub = []
        if info["is_draft"]:
            sub.append("draft")
        if info["status"] == "abandoned":
            sub.append("abandoned")
        if info["has_conflicts"]:
            sub.append("merge conflicts")
        sub.append(f"{self._unresolved} unresolved thread(s)")
        ttk.Label(
            self, text="  \u2022  ".join(sub), foreground=theme.FG_MUTED,
            font=("", 8),
        ).pack(side="top", anchor="w", padx=12)

        tk.Frame(self, height=1, background=theme.BORDER).pack(
            side="top", fill="x", pady=(6, 0)
        )

        shell = ttk.Frame(self)
        shell.pack(side="top", fill="both", expand=True, padx=0, pady=0)
        canvas = tk.Canvas(shell, highlightthickness=0, background=theme.BG)
        canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scroll.set)
        self._canvas = canvas
        body = tk.Frame(canvas, background=theme.BG)
        body_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(body_id, width=e.width))
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", self._on_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        self._body = body

        self._build_votes(body)
        self._build_threads(body)
        self._policy_frame = self._section(body, "Build & merge readiness")
        self._policy_body = tk.Label(
            self._policy_frame, text="Checking branch policies\u2026",
            background=theme.BG, foreground=theme.FG_MUTED, anchor="w", justify="left",
        )
        self._policy_body.pack(side="top", fill="x", anchor="w")
        self._render_policy(self._policy_ok, self._policy, self._build_errors)

    def _on_wheel(self, event):
        self._canvas.yview_scroll(int(-event.delta / 120), "units")

    def _section(self, parent, title):
        """A titled section frame with a heading and a thin divider."""
        wrap = tk.Frame(parent, background=theme.BG)
        wrap.pack(side="top", fill="x", padx=12, pady=(10, 2))
        tk.Label(
            wrap, text=title, background=theme.BG, foreground=theme.FG,
            font=("", 10, "bold"), anchor="w",
        ).pack(side="top", fill="x", anchor="w")
        tk.Frame(wrap, height=1, background=theme.BORDER).pack(
            side="top", fill="x", pady=(2, 4)
        )
        return wrap

    def _build_votes(self, parent):
        wrap = self._section(parent, "Reviewer votes")
        reviewers = [r for r in self._info["reviewers"] if not r["is_container"]]
        if not reviewers:
            tk.Label(
                wrap, text="No reviewers.", background=theme.BG,
                foreground=theme.FG_MUTED, anchor="w",
            ).pack(side="top", fill="x", anchor="w")
            return
        for reviewer in reviewers:
            color = {
                "approved": theme.SUCCESS,
                "approved with suggestions": theme.READY,
                "waiting for author": theme.ERROR,
                "rejected": theme.ERROR,
            }.get(reviewer["label"], theme.FG_MUTED)
            required = " (required)" if reviewer["required"] else ""
            line = tk.Frame(wrap, background=theme.BG)
            line.pack(side="top", fill="x", anchor="w")
            tk.Label(
                line, text=f"{reviewer['name']}{required}", background=theme.BG,
                foreground=theme.FG, anchor="w",
            ).pack(side="left")
            tk.Label(
                line, text=f"  -  {reviewer['label']}", background=theme.BG,
                foreground=color, anchor="w",
            ).pack(side="left")

    def _build_threads(self, parent):
        human = [t for t in self._threads if not t["is_system"]]
        wrap = self._section(parent, f"Comment threads ({len(human)})")
        if self._thread_error:
            tk.Label(
                wrap, text=f"Could not load comment threads: {self._thread_error}",
                background=theme.BG, foreground=theme.ERROR, anchor="w",
                justify="left", wraplength=600,
            ).pack(side="top", fill="x", anchor="w")
            return
        if not human:
            tk.Label(
                wrap, text="No comment threads.", background=theme.BG,
                foreground=theme.FG_MUTED, anchor="w",
            ).pack(side="top", fill="x", anchor="w")
            return
        for thread in human:
            ThreadView(wrap, thread).pack(side="top", fill="x", anchor="w", pady=(0, 6))

    def _render_policy(self, ok, policy, errors=None):
        if not self.winfo_exists():
            return
        self._policy_body.destroy()
        container = self._policy_frame
        if not ok:
            tk.Label(
                container, text=f"Policy lookup failed: {policy}",
                background=theme.BG, foreground=theme.ERROR, anchor="w",
                justify="left", wraplength=600,
            ).pack(side="top", fill="x", anchor="w")
            return

        info = self._info
        build_state = policy.get("build_state", "none")
        ready_text, ready_color = PrStatusWindow._ready_summary(
            info, True, policy, self._unresolved,
            threads_ok=self._thread_error is None,
        )
        ready = ready_text == "yes"

        build_label = {
            "succeeded": ("Build: passed", theme.SUCCESS),
            "failed": ("Build: failed", theme.ERROR),
            "running": ("Build: running", theme.WARNING),
            "none": ("Build: no build policy", theme.FG_MUTED),
        }.get(build_state, ("Build: unknown", theme.FG_MUTED))
        build_line = tk.Label(
            container, text=build_label[0], background=theme.BG,
            foreground=build_label[1], anchor="w",
            cursor="hand2" if policy.get("build_url") else "",
        )
        build_line.pack(side="top", fill="x", anchor="w")
        if policy.get("build_url"):
            build_line.bind("<Button-1>",
                            lambda _e: webbrowser.open(policy["build_url"]))

        if ready_text == "completed":
            ready_label = "Ready to merge: completed"
        else:
            ready_label = "Ready to merge: yes" if ready else "Ready to merge: no"
        tk.Label(
            container, text=ready_label,
            background=theme.BG, foreground=getattr(theme, ready_color),
            anchor="w",
        ).pack(side="top", fill="x", anchor="w")

        for policy_item in policy.get("policies") or []:
            status = policy_item["status"]
            color = theme.ERROR if status in ("rejected", "broken") else (
                theme.WARNING if status in ("queued", "running") else theme.FG_MUTED
            )
            tk.Label(
                container, text=f"  \u2022 {policy_item['name']}: {status}",
                background=theme.BG, foreground=color, anchor="w",
            ).pack(side="top", fill="x", anchor="w")

        if errors:
            tk.Label(
                container, text="Build errors:", background=theme.BG,
                foreground=theme.ERROR, anchor="w", font=("", 9, "bold"),
            ).pack(side="top", fill="x", anchor="w", pady=(4, 0))
            for message in errors[:20]:
                tk.Label(
                    container, text=f"  {message}", background=theme.BG,
                    foreground=theme.FG, anchor="w", justify="left", wraplength=600,
                ).pack(side="top", fill="x", anchor="w")


class ThreadView(tk.Frame):
    """One comment thread: a clickable header that expands to its full replies."""

    def __init__(self, parent, thread):
        super().__init__(parent, background=theme.BG)
        self._thread = thread
        self._expanded = False

        comments = thread["comments"]
        first = comments[0]
        resolved = thread["resolved"]
        status_color = theme.FG_MUTED if resolved else theme.WARNING
        status_text = thread["status"] or ("resolved" if resolved else "active")

        head = tk.Frame(self, background=theme.BG)
        head.pack(side="top", fill="x", anchor="w")
        self._toggle = tk.Label(
            head, text="\u25b8", background=theme.BG, foreground=theme.FG_MUTED,
            cursor="hand2", width=2,
        )
        self._toggle.pack(side="left")
        meta = f"{first['author']}  \u2022  {_fmt_date(first['date'])}"
        if len(comments) > 1:
            meta += f"  \u2022  {len(comments) - 1} repl" + (
                "y" if len(comments) - 1 == 1 else "ies"
            )
        tk.Label(
            head, text=meta, background=theme.BG, foreground=theme.FG,
            anchor="w", cursor="hand2",
        ).pack(side="left")
        tk.Label(
            head, text=f"  [{status_text}]", background=theme.BG,
            foreground=status_color, anchor="w",
        ).pack(side="left")

        anchor = thread["file_path"]
        if anchor:
            loc = anchor + (f" {thread['line_range']}" if thread["line_range"] else "")
            tk.Label(
                self, text=loc, background=theme.BG, foreground=theme.LINK,
                anchor="w", font=("", 8),
            ).pack(side="top", fill="x", anchor="w", padx=(22, 0))

        self._preview = tk.Label(
            self, text=_one_line(first["content"]), background=theme.BG,
            foreground=theme.FG_MUTED, anchor="w", justify="left", wraplength=600,
        )
        self._preview.pack(side="top", fill="x", anchor="w", padx=(22, 0))

        self._detail = tk.Frame(self, background=theme.BG)

        for widget in (self._toggle, head):
            widget.bind("<Button-1>", lambda _e: self._toggle_expand())
        for child in head.winfo_children():
            child.bind("<Button-1>", lambda _e: self._toggle_expand())

    def _toggle_expand(self):
        self._expanded = not self._expanded
        if self._expanded:
            self._toggle.configure(text="\u25be")
            self._preview.pack_forget()
            for index, comment in enumerate(self._thread["comments"]):
                indent = 22 + (18 if comment["parent_id"] else 0)
                block = tk.Frame(self._detail, background=theme.BG)
                block.pack(side="top", fill="x", anchor="w")
                tk.Label(
                    block,
                    text=f"{comment['author']}  \u2022  {_fmt_date(comment['date'])}",
                    background=theme.BG, foreground=theme.FG, anchor="w",
                    font=("", 8, "bold"),
                ).pack(side="top", fill="x", anchor="w", padx=(indent, 0))
                tk.Label(
                    block, text=comment["content"], background=theme.BG,
                    foreground=theme.FG, anchor="w", justify="left", wraplength=600,
                ).pack(side="top", fill="x", anchor="w", padx=(indent, 0))
            self._detail.pack(side="top", fill="x", anchor="w", pady=(2, 0))
        else:
            self._toggle.configure(text="\u25b8")
            for child in self._detail.winfo_children():
                child.destroy()
            self._detail.pack_forget()
            self._preview.pack(side="top", fill="x", anchor="w", padx=(22, 0))


def _one_line(text, limit=120):
    """Collapse a comment to a single truncated preview line."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "\u2026"
