"""Owned remote feature branches and open pull requests, with cleanup actions."""

import os
import queue
import threading
import time
import urllib.parse
import webbrowser
import tkinter as tk
from datetime import datetime, timedelta, timezone
from tkinter import ttk, messagebox

import ado_auth
import gitutils
import theme
from parallel import run_in_parallel
from widgets import Tooltip


_CACHE = {}
_LOCK = threading.Lock()
_IDENTITY_LOCK = threading.Lock()
_CACHE_SECONDS = 300
_ZERO = "0" * 40


def _date(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _get(url, auth, params=None, response_headers=None):
    query = dict(params or {}, **{"api-version": "7.1"})
    options = {"response_headers": response_headers} if response_headers is not None else {}
    ok, data = gitutils._ado_get_json(
        url + "?" + urllib.parse.urlencode(query), auth, **options
    )
    if not ok:
        raise RuntimeError(data)
    return data


def _pr_state(pr):
    if pr.get("isDraft"):
        return "Draft"
    if pr.get("mergeStatus") == "conflicts":
        return "Merge conflicts"
    reviewers = pr.get("reviewers") or []
    votes = [int(reviewer.get("vote") or 0) for reviewer in reviewers]
    if -10 in votes:
        return "Rejected"
    if -5 in votes:
        return "Waiting for author"
    if pr.get("autoCompleteSetBy"):
        return "Auto-complete pending"
    required = [reviewer for reviewer in reviewers if reviewer.get("isRequired")]
    if any(int(reviewer.get("vote") or 0) < 5 for reviewer in required):
        return "Waiting for approvals"
    if any(vote >= 5 for vote in votes):
        return "Approved (policies not checked)"
    return "Waiting for approvals"


def _creation_date(base, auth, ref):
    offset = 0
    while True:
        pushes = _get(base + "/pushes", auth, {
            "searchCriteria.refName": ref,
            "searchCriteria.includeRefUpdates": "true",
            "$top": 100, "$skip": offset,
        }).get("value", [])
        for push in pushes:
            for update in push.get("refUpdates") or []:
                if update.get("name") == ref and update.get("oldObjectId") == _ZERO:
                    return push["date"]
        if len(pushes) < 100:
            return None
        offset += len(pushes)


def scan_repo(entry, force=False, now=None):
    """Return branches, PRs and errors; never infer ownership from commit author."""
    name, path = entry
    result = {"branches": [], "prs": [], "errors": []}
    try:
        remote = gitutils.git_remote_url(path)
        parsed = gitutils.parse_ado_remote(remote)
        if not parsed:
            raise RuntimeError("origin is not an Azure DevOps repository")
        org, project, repo, host = parsed
        header, error = ado_auth.auth_header(host, org=org, cred_url=remote)
        if not header:
            raise RuntimeError(error)
        auth = header.removeprefix("Basic ")
        with _IDENTITY_LOCK:
            user_id = gitutils._ado_current_user_id(org, auth)
        if not user_id:
            raise RuntimeError("Cannot resolve your Azure DevOps identity")
        key = (org.lower(), project.lower(), repo.lower(), user_id.lower())
        with _LOCK:
            cached = _CACHE.get(key)
            if not force and cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
                return cached[1]
        base = (
            "https://dev.azure.com/" + urllib.parse.quote(org, safe="") + "/"
            + urllib.parse.quote(project, safe="") + "/_apis/git/repositories/"
            + urllib.parse.quote(repo, safe="")
        )
        web = (
            "https://dev.azure.com/" + urllib.parse.quote(org, safe="") + "/"
            + urllib.parse.quote(project, safe="") + "/_git/"
            + urllib.parse.quote(repo, safe="")
        )
        context = {"base": base, "path": path, "repo": name, "key": key}
        try:
            offset = 0
            while True:
                prs = _get(base + "/pullrequests", auth, {
                    "searchCriteria.status": "active",
                    "searchCriteria.creatorId": user_id,
                    "$top": 100, "$skip": offset,
                }).get("value", [])
                for pr in prs:
                    if (pr.get("status") != "active" or
                            (pr.get("createdBy") or {}).get("id", "").lower() != user_id.lower()):
                        continue
                    result["prs"].append(dict(
                        context, title=f"#{pr['pullRequestId']} {pr.get('title', '')}",
                        pr_id=pr["pullRequestId"], date=pr["creationDate"],
                        link=web + f"/pullrequest/{pr['pullRequestId']}",
                        state=_pr_state(pr),
                    ))
                if len(prs) < 100:
                    break
                offset += len(prs)
        except Exception as exc:
            result["errors"].append(f"{name} PRs: {exc}")
        try:
            refs = []
            continuation = None
            while True:
                params = {"filter": "heads/feature/", "$top": 1000}
                if continuation:
                    params["continuationToken"] = continuation
                headers = {}
                page = _get(base + "/refs", auth, params, headers).get("value", [])
                refs.extend(page)
                continuation = next((value for key, value in headers.items()
                                     if key.lower() == "x-ms-continuationtoken"), None)
                if not continuation:
                    break
            owned = [ref for ref in refs if
                     (ref.get("creator") or {}).get("id", "").lower() == user_id.lower()]
            stats = {}
            if owned:
                metadata = _get(base, auth)
                target = metadata.get("defaultBranch") or "refs/heads/master"
                stats = {stat["name"]: stat for stat in _get(
                    base + "/stats/branches", auth,
                    {"baseVersionDescriptor.version": target.removeprefix("refs/heads/"),
                     "baseVersionDescriptor.versionType": "branch"},
                ).get("value", [])}
            cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=7)
            for ref in owned:
                try:
                    created = _creation_date(base, auth, ref["name"])
                    if not created:
                        raise RuntimeError("branch creation date unavailable")
                    if _date(created) >= cutoff:
                        continue
                    branch = ref["name"].removeprefix("refs/heads/")
                    behind = stats.get(branch, {}).get("behindCount")
                    result["branches"].append(dict(
                        context, title=branch, ref=ref["name"],
                        object_id=ref["objectId"], date=created,
                        link=web + "?" + urllib.parse.urlencode({"version": "GB" + branch}),
                        state=str(behind) if behind is not None else "Unknown",
                        target=target.removeprefix("refs/heads/"),
                    ))
                except Exception as exc:
                    result["errors"].append(f"{name} {ref['name']}: {exc}")
        except Exception as exc:
            result["errors"].append(f"{name} branches: {exc}")
        if not result["errors"]:
            with _LOCK:
                _CACHE[key] = (time.monotonic(), result)
    except Exception as exc:
        result["errors"].append(f"{name}: {exc}")
    return result


def remove_item(item, kind, context=None):
    """Use repository Git credentials for writes, as existing PR actions do."""
    try:
        ctx = context
        if ctx is None:
            ok, ctx = gitutils._pr_rest_context(item["repo"], item["path"], branch="master")
            if not ok:
                return False, ctx
        if kind == "branches":
            url = item["base"] + "/refs?api-version=7.1"
            body = [{"name": item["ref"], "oldObjectId": item["object_id"],
                     "newObjectId": _ZERO}]
            ok, data = gitutils._ado_send_json(url, ctx["auth"], body, "POST")
            if ok:
                updates = data.get("value") or []
                ok = bool(updates) and all(update.get("success") for update in updates)
                if not ok:
                    data = "; ".join(str(update.get("customMessage") or
                                         update.get("updateStatus")) for update in updates)
        else:
            url = item["base"] + f"/pullrequests/{item['pr_id']}?api-version=7.1"
            ok, data = gitutils._ado_send_json(url, ctx["auth"], {"status": "abandoned"}, "PATCH")
        with _LOCK:
            _CACHE.pop(item["key"], None)
        return ok, "" if ok else str(data)
    except Exception as exc:
        return False, str(exc)


def repository_entries():
    paths = set()
    for root in (gitutils.REPOS_ROOT, gitutils.NUGETS_ROOT):
        paths.update(os.path.normcase(os.path.abspath(path))
                     for path in gitutils.find_git_repos(root))
    return [(os.path.basename(path), path) for path in sorted(paths)]


class StaleItemsWindow(tk.Toplevel):
    """Modal cleanup table; background workers communicate through a queue."""

    def __init__(self, parent):
        super().__init__(parent.winfo_toplevel())
        self.title("Stale branches and PRs")
        self.geometry("1180x620")
        self.minsize(800, 360)
        self.configure(background=theme.BG)
        theme.apply_window_icon(self)
        theme.enable_dark_titlebar(self)
        self.transient(parent.winfo_toplevel())
        self._events = queue.Queue()
        self._rows = {"branches": [], "prs": []}
        self._sort = {"branches": (None, False), "prs": (None, False)}
        self._pages = {}
        self._errors = []
        self._busy = False
        self._closed = threading.Event()
        self.protocol("WM_DELETE_WINDOW", self._close)

        header = ttk.Frame(self)
        header.pack(fill="x", padx=10, pady=10)
        self._status = ttk.Label(header, text="Scanning repositories...")
        self._status.pack(side="left", fill="x", expand=True)
        self._refresh = ttk.Button(header, text="Refresh", command=lambda: self._load(True))
        self._refresh.pack(side="right")
        Tooltip(self._refresh, "Scan all Services and NuGet repositories again, bypassing scan results cached for five minutes.")
        self._error_button = ttk.Button(header, text="Errors", command=self._show_errors)
        self._error_button.pack(side="right", padx=6)
        Tooltip(self._error_button, "Show repositories or branches that could not be scanned or changed.")
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        for kind, title in (("branches", "Branches"), ("prs", "PRs")):
            page = ttk.Frame(self._notebook)
            self._notebook.add(page, text=title)
            controls = ttk.Frame(page)
            controls.pack(fill="x", pady=6)
            select = tk.BooleanVar(value=False)
            select_button = ttk.Checkbutton(
                controls, text="Select all", variable=select,
                command=lambda selected_kind=kind: self._select_all(selected_kind),
            )
            select_button.pack(side="left")
            Tooltip(select_button, "Select or clear every listed row on this tab.")
            verb = "Delete" if kind == "branches" else "Abandon"
            bulk = ttk.Button(controls, text=verb + " selected",
                              command=lambda selected_kind=kind: self._act(selected_kind))
            bulk.pack(side="right")
            Tooltip(bulk, verb + " the selected remote items after confirmation. Local branches are not removed.")
            shell = ttk.Frame(page)
            shell.pack(fill="both", expand=True)
            canvas = tk.Canvas(shell, background=theme.BG, highlightthickness=0)
            vertical = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
            horizontal = ttk.Scrollbar(page, orient="horizontal", command=canvas.xview)
            vertical.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            horizontal.pack(fill="x")
            canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
            table = ttk.Frame(canvas)
            table_id = canvas.create_window((0, 0), window=table, anchor="nw")
            table.bind("<Configure>", lambda event, view=canvas: view.configure(scrollregion=view.bbox("all")))
            canvas.bind("<Configure>", lambda event, view=canvas, inner=table, ident=table_id:
                        view.itemconfigure(ident, width=max(event.width, inner.winfo_reqwidth())))
            canvas.bind("<MouseWheel>", lambda event, view=canvas:
                        view.yview_scroll(int(-event.delta / 120), "units"))
            self._pages[kind] = {"table": table, "bulk": bulk, "select": select,
                                 "select_button": select_button, "title": title, "page": page}
        self.grab_set()
        self.after(60, self._drain)
        self._load(False)

    def _close(self):
        self._closed.set()
        self.grab_release()
        self.destroy()

    def _show_errors(self):
        from dialogs import show_report
        show_report(self, "\n".join(self._errors) or "No errors.", title="Stale item errors")

    def _set_busy(self, busy):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self._refresh.configure(state=state)
        for kind, page in self._pages.items():
            page["bulk"].configure(state=state)
            page["select_button"].configure(state=state)
            for row in self._rows[kind]:
                row["button"].configure(state=state)
                row["check"].configure(state=state)

    def _load(self, force):
        if self._busy:
            return
        self._errors = []
        self._set_busy(True)
        self._status.configure(text="Scanning repositories...")
        for kind in self._rows:
            self._rows[kind] = []
            self._pages[kind]["select"].set(False)
            self._render(kind)

        def worker():
            try:
                entries = repository_entries()
                def scan(entry):
                    if self._closed.is_set():
                        return
                    result = scan_repo(entry, force=force)
                    self._events.put(("repo", result))
                run_in_parallel(entries, scan)
                self._events.put(("loaded", len(entries)))
            except Exception as exc:
                self._events.put(("failed", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _drain(self):
        if self._closed.is_set():
            return
        try:
            while True:
                event, payload = self._events.get_nowait()
                if event == "repo":
                    self._errors.extend(payload["errors"])
                    for kind in self._rows:
                        for item in payload[kind]:
                            self._rows[kind].append({"item": item, "selected": tk.BooleanVar(value=False)})
                        self._render(kind)
                    self._status.configure(text=self._summary("Scanning"))
                elif event == "loaded":
                    self._set_busy(False)
                    self._status.configure(text=self._summary(f"{payload} repositories"))
                elif event == "failed":
                    self._errors.append(payload)
                    self._set_busy(False)
                    self._status.configure(text=self._summary("Scan failed"))
                elif event == "removed":
                    kind, outcomes = payload
                    for row, ok, error in outcomes:
                        if ok:
                            self._rows[kind].remove(row)
                        else:
                            self._errors.append(f"{row['item']['repo']} {row['item']['title']}: {error}")
                    self._render(kind)
                    self._set_busy(False)
                    self._status.configure(text=self._summary("Cleanup finished"))
        except queue.Empty:
            pass
        self.after(60, self._drain)

    def _summary(self, prefix):
        return (f"{prefix}: {len(self._rows['branches'])} branches, "
                f"{len(self._rows['prs'])} PRs; {len(self._errors)} errors")

    def _sort_table(self, kind, key):
        current, descending = self._sort[kind]
        self._sort[kind] = (key, not descending if current == key else False)
        self._render(kind)

    def _render(self, kind):
        page = self._pages[kind]
        table = page["table"]
        sort_key, descending = self._sort[kind]
        if sort_key:
            def value(row):
                item = row["item"]
                if sort_key == "date":
                    return _date(item["date"])
                if kind == "branches" and sort_key == "state":
                    return int(item["state"])
                if kind == "prs" and sort_key == "title":
                    return item["pr_id"]
                return item[sort_key].casefold()
            unknown = []
            rows = self._rows[kind]
            if kind == "branches" and sort_key == "state":
                unknown = [row for row in rows if not row["item"]["state"].isdigit()]
                rows = [row for row in rows if row["item"]["state"].isdigit()]
            self._rows[kind] = sorted(rows, key=value, reverse=descending) + unknown
        for widget in table.winfo_children():
            widget.destroy()
        columns = ("", "Repo", "Branch name" if kind == "branches" else "PR",
                   "Link", "Created", "Commits behind" if kind == "branches" else "Status",
                   "Delete" if kind == "branches" else "Abandon")
        sort_keys = {1: "repo", 2: "title", 3: "link", 4: "date", 5: "state"}
        for column, title in enumerate(columns):
            key = sort_keys.get(column)
            indicator = (" v" if descending else " ^") if key and key == sort_key else "  "
            header = ttk.Label(table, text=title + indicator if key else title,
                               font=("", 9, "bold"), cursor="hand2" if key else "")
            header.grid(row=0, column=column, padx=6, pady=6, sticky="w")
            if key:
                header.bind("<Button-1>", lambda event, selected_key=key: self._sort_table(kind, selected_key))
                Tooltip(header, f"Sort by {title.lower()}; click again to reverse the order.")
        table.columnconfigure(2, weight=1)
        for index, row in enumerate(self._rows[kind], start=1):
            item = row["item"]
            check = ttk.Checkbutton(table, variable=row["selected"])
            check.grid(row=index, column=0, padx=6, pady=3)
            row["check"] = check
            Tooltip(check, "Select this item for the bulk action.")
            ttk.Label(table, text=item["repo"], wraplength=170).grid(
                row=index, column=1, padx=6, pady=3, sticky="w")
            ttk.Label(table, text=item["title"], wraplength=340).grid(
                row=index, column=2, padx=6, pady=3, sticky="w")
            link = ttk.Label(table, text="Open", foreground=theme.ACCENT, cursor="hand2")
            link.grid(row=index, column=3, padx=6, pady=3, sticky="w")
            link.bind("<Button-1>", lambda event, url=item["link"]: webbrowser.open(url))
            Tooltip(link, item["link"])
            ttk.Label(table, text=_date(item["date"]).astimezone().strftime("%Y-%m-%d %H:%M")).grid(
                row=index, column=4, padx=6, pady=3, sticky="w")
            status = ttk.Label(table, text=item["state"], wraplength=180)
            status.grid(row=index, column=5, padx=6, pady=3, sticky="w")
            if kind == "branches":
                Tooltip(status, "Compared with " + item["target"])
            button = ttk.Button(table, text=columns[-1],
                                command=lambda selected=row: self._act(kind, [selected]))
            button.grid(row=index, column=6, padx=6, pady=3)
            Tooltip(button, ("Delete this remote branch" if kind == "branches" else "Abandon this open PR")
                    + " after confirmation.")
            row["button"] = button
            if self._busy:
                button.configure(state="disabled")
                check.configure(state="disabled")
        self._notebook.tab(page["page"], text=f"{page['title']} ({len(self._rows[kind])})")

    def _select_all(self, kind):
        for row in self._rows[kind]:
            row["selected"].set(self._pages[kind]["select"].get())

    def _act(self, kind, rows=None):
        if self._busy:
            return
        rows = rows if rows is not None else [row for row in self._rows[kind] if row["selected"].get()]
        if not rows:
            return
        verb = "Delete remote branches" if kind == "branches" else "Abandon PRs"
        details = "\n".join(f"{row['item']['repo']}: {row['item']['title']}" for row in rows)
        if not messagebox.askyesno(verb, f"{verb} ({len(rows)})?\n\n{details}", parent=self):
            return
        self._set_busy(True)
        self._status.configure(text=verb + "...")

        def worker():
            def remove_group(group):
                outcomes = []
                item = group[0]["item"]
                ok, context = gitutils._pr_rest_context(item["repo"], item["path"], branch="master")
                if not ok:
                    return [(row, False, context) for row in group]
                for row in group:
                    if self._closed.is_set():
                        outcomes.append((row, False, "Cancelled"))
                    else:
                        ok, error = remove_item(row["item"], kind, context=context)
                        outcomes.append((row, ok, error))
                return outcomes
            groups = {}
            for row in rows:
                groups.setdefault(row["item"]["path"], []).append(row)
            try:
                batches = run_in_parallel(groups.values(), remove_group)
                self._events.put(("removed", (kind, [outcome for batch in batches for outcome in batch])))
            except Exception as exc:
                self._events.put(("failed", str(exc)))
        threading.Thread(target=worker, daemon=True).start()