"""Modal dialogs used by the action tabs.

Each function is blocking (modal) and returns the user's choice. They take the
parent widget so they can be centred on the main window.
"""

import json
import tkinter as tk
from tkinter import ttk

from gitutils import is_valid_branch_name
from widgets import Tooltip
import pbi
import theme


def _center_over_parent(dialog, parent):
    """Position *dialog* centred over the parent's top-level window."""
    dialog.update_idletasks()  # ensure the dialog has its real size
    theme.enable_dark_titlebar(dialog)  # dark title bar to match the app
    top = parent.winfo_toplevel()
    px, py = top.winfo_rootx(), top.winfo_rooty()
    pw, ph = top.winfo_width(), top.winfo_height()
    dw, dh = dialog.winfo_width(), dialog.winfo_height()
    x = px + (pw - dw) // 2
    y = py + (ph - dh) // 2
    dialog.geometry(f"+{x}+{y}")


# Human-readable button labels for each uncommitted-changes decision key.
_DECISION_LABELS = {
    "commit": "Commit changes",
    "commit_restore": "Commit & restore",
    "delete": "Delete changes",
    "move": "Move to new feature branch",
    "abort": "Abort operation",
}


def ask_change_decision(parent, name, options, on_master=False, note=None):
    """Per-repo modal asking what to do with a repo's uncommitted changes.

    *options* is the ordered list of decision keys to offer, drawn from
    "commit", "commit_restore", "delete", "move" and "abort". Returns the chosen
    key, or None if the user aborts or closes the modal (callers treat None as
    "abort"). The repository name is shown in bold so it stands out. When
    *on_master* is set, a clear warning explains why committing is not offered.
    *note*, if given, is extra explanatory text shown above the buttons (e.g.
    why committing is required for a rebase).
    """
    dialog = tk.Toplevel(parent)
    dialog.title(f"Uncommitted changes - {name}")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    # Message with the repo name highlighted in bold (built from inline labels).
    text = ttk.Frame(dialog)
    text.pack(padx=16, pady=12, anchor="w")
    line = ttk.Frame(text)
    line.pack(anchor="w")
    tk.Label(line, text="Repository ").pack(side="left")
    tk.Label(line, text=name, font=("", 9, "bold")).pack(side="left")
    tk.Label(line, text=" has uncommitted changes.").pack(side="left")

    # On master, make it obvious why there is no Commit button.
    if on_master:
        warn = ttk.Frame(text)
        warn.pack(anchor="w", pady=(8, 0))
        tk.Label(warn, text="\u26A0", foreground=theme.ERROR,
                 font=("", 11, "bold")).pack(side="left", padx=(0, 4))
        master_line = ttk.Frame(warn)
        master_line.pack(side="left")
        tk.Label(master_line, text="This repository is on the ",
                 foreground=theme.ERROR).pack(side="left")
        tk.Label(master_line, text="master", foreground=theme.ERROR,
                 font=("", 9, "bold")).pack(side="left")
        tk.Label(master_line, text=" branch. Committing on master is not allowed.",
                 foreground=theme.ERROR).pack(side="left")

    # Optional explanatory note (e.g. that a rebase requires committing first).
    if note:
        tk.Label(text, text=note, justify="left", wraplength=380).pack(
            anchor="w", pady=(8, 0)
        )

    tk.Label(text, text="What would you like to do with them?").pack(
        anchor="w", pady=(8, 0)
    )

    choice = {"value": None}

    def _set(value):
        choice["value"] = value
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=(0, 12))
    for key in options:
        # "abort" (and a closed window) resolve to None.
        value = None if key == "abort" else key
        ttk.Button(bar, text=_DECISION_LABELS[key],
                   command=lambda v=value: _set(v)).pack(side="left", padx=4)

    dialog.protocol("WM_DELETE_WINDOW", lambda: _set(None))
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return choice["value"]


def ask_branch_name(parent, title="Create feature branch", prefix="feature/",
                    initial=""):
    """Modal with a fixed *prefix* label and a required, validated name field.

    Returns the entered name (without prefix), or None if cancelled. The name
    must be a valid git branch name (no spaces). *initial* pre-fills the entry
    (e.g. a name derived from a PBI id and title).
    """
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    row = ttk.Frame(dialog)
    row.pack(padx=16, pady=(16, 4))
    ttk.Label(row, text=prefix).pack(side="left")
    entry = ttk.Entry(row, width=30)
    entry.pack(side="left")
    if initial:
        entry.insert(0, initial)
    entry.focus_set()

    error_label = tk.Label(dialog, text="", foreground=theme.ERROR)
    error_label.pack(padx=16, anchor="w")

    result = {"name": None}

    def _ok():
        name = entry.get().strip()
        if not name:
            error_label.config(text="Branch name is required.")
            return
        if not is_valid_branch_name(name):
            error_label.config(
                text="Invalid name: no spaces; use letters, digits, . _ / -"
            )
            return
        result["name"] = name
        dialog.destroy()

    def _cancel():
        result["name"] = None
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text="OK", command=_ok).pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    entry.bind("<Return>", lambda _e: _ok())
    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["name"]


def show_report(parent, report_text, title="Report"):
    """Modal showing *report_text* in a wide, read-only scrollable text field.

    A Copy button places the full report on the clipboard. The window is made
    wide so long NuGet package names fit without wrapping.
    """
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.transient(parent.winfo_toplevel())
    dialog.geometry("820x560")
    dialog.minsize(560, 320)

    body = ttk.Frame(dialog)
    body.pack(fill="both", expand=True, padx=12, pady=(12, 6))

    text = tk.Text(body, wrap="none", font=("Consolas", 9),
                   background=theme.BG_INPUT, foreground=theme.FG,
                   insertbackground=theme.FG, borderwidth=1, relief="solid",
                   highlightthickness=0)
    yscroll = ttk.Scrollbar(body, orient="vertical", command=text.yview)
    xscroll = ttk.Scrollbar(body, orient="horizontal", command=text.xview)
    text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
    text.grid(row=0, column=0, sticky="nsew")
    yscroll.grid(row=0, column=1, sticky="ns")
    xscroll.grid(row=1, column=0, sticky="ew")
    body.rowconfigure(0, weight=1)
    body.columnconfigure(0, weight=1)

    text.insert("1.0", report_text)
    text.config(state="disabled")

    bar = ttk.Frame(dialog)
    bar.pack(fill="x", padx=12, pady=(0, 12))

    copy_button = ttk.Button(bar, text="Copy")

    def _copy():
        dialog.clipboard_clear()
        dialog.clipboard_append(report_text)
        copy_button.config(text="Copied!")

    copy_button.config(command=_copy)
    copy_button.pack(side="left")
    ttk.Button(bar, text="Close", command=dialog.destroy).pack(side="right")

    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)


def ask_commit_message(parent, repos, branch_warning=None):
    """Collect a shared and optional per-repository commit message.

    *repos* is a list of ``(name, path)`` pairs. The shared message updates all
    included rows. Once included rows differ, the shared field is disabled until
    their messages match again. Returns ``{repo_name: message}`` for included
    repositories, or None if cancelled.
    """
    dialog = tk.Toplevel(parent)
    dialog.title("Commit all changes")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    tk.Label(
        dialog,
        text=f"Commit changes in {len(repos)} selected "
             f"{'repository' if len(repos) == 1 else 'repositories'}.",
        justify="left",
    ).pack(padx=16, pady=(16, 4), anchor="w")

    # Optional mixed-branch (or similar) warning shown in red.
    if branch_warning:
        warn = ttk.Frame(dialog)
        warn.pack(padx=16, pady=(0, 4), anchor="w")
        tk.Label(warn, text="\u26A0", foreground=theme.ERROR,
                 font=("", 11, "bold")).pack(side="left", padx=(0, 4))
        tk.Label(warn, text=branch_warning, foreground=theme.ERROR,
                 justify="left", wraplength=360).pack(side="left")

    tk.Label(dialog, text="Shared commit message:").pack(padx=16, anchor="w")
    shared_var = tk.StringVar()
    shared_entry = ttk.Entry(dialog, textvariable=shared_var, width=52)
    shared_entry.pack(padx=16, pady=(0, 8), fill="x")
    shared_entry.focus_set()

    body = ttk.Frame(dialog)
    body.pack(padx=16, fill="x")
    body.columnconfigure(0, minsize=260)
    ttk.Label(body, text="Repository", font=("", 9, "bold")).grid(
        row=0, column=0, sticky="w", padx=4, pady=(0, 4)
    )
    ttk.Label(body, text="Commit message", font=("", 9, "bold")).grid(
        row=0, column=1, sticky="w", padx=4, pady=(0, 4)
    )
    ttk.Label(body, text="Exclude", font=("", 9, "bold")).grid(
        row=0, column=2, sticky="w", padx=4, pady=(0, 4)
    )

    message_vars, exclude_vars = {}, {}
    syncing = {"value": False}

    def _included_messages():
        return [
            message_vars[name].get()
            for name, _path in repos
            if not exclude_vars[name].get()
        ]

    def _refresh_shared():
        messages = _included_messages()
        matching = bool(messages) and len(set(messages)) == 1
        shared_entry.configure(state="normal" if matching else "disabled")
        if matching and shared_var.get() != messages[0]:
            syncing["value"] = True
            shared_var.set(messages[0])
            syncing["value"] = False

    def _apply_shared(*_args):
        if syncing["value"]:
            return
        syncing["value"] = True
        for name, _path in repos:
            if not exclude_vars[name].get():
                message_vars[name].set(shared_var.get())
        syncing["value"] = False
        _refresh_shared()

    def _message_changed(*_args):
        if not syncing["value"]:
            _refresh_shared()

    for index, (name, _path) in enumerate(repos, start=1):
        ttk.Label(body, text=name, justify="left").grid(
            row=index, column=0, sticky="w", padx=4, pady=2
        )
        message_var = tk.StringVar()
        message_var.trace_add("write", _message_changed)
        entry = ttk.Entry(body, textvariable=message_var, width=34)
        entry.grid(row=index, column=1, sticky="w", padx=4, pady=2)
        exclude_var = tk.BooleanVar(value=False)

        def _toggle(repo=name, field=entry):
            field.configure(
                state="disabled" if exclude_vars[repo].get() else "normal"
            )
            _refresh_shared()

        ttk.Checkbutton(body, variable=exclude_var, command=_toggle).grid(
            row=index, column=2, padx=4, pady=2
        )
        message_vars[name] = message_var
        exclude_vars[name] = exclude_var

    shared_var.trace_add("write", _apply_shared)

    error_label = tk.Label(dialog, text="", foreground=theme.ERROR)
    error_label.pack(padx=16, anchor="w")

    result = {"messages": None}

    def _ok():
        messages = {}
        for name, _path in repos:
            if exclude_vars[name].get():
                continue
            message = message_vars[name].get().strip()
            if not message:
                error_label.config(text=f"{name}: a commit message is required.")
                return
            messages[name] = message
        if not messages:
            error_label.config(text="Select at least one repository to commit.")
            return
        result["messages"] = messages
        dialog.destroy()

    def _cancel():
        result["messages"] = None
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text="Commit", command=_ok).pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    dialog.bind("<Return>", lambda _e: _ok())
    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["messages"]


def ask_branch_warning(parent, repo_count, title="Push all changes",
                       action_label="Push"):
    """Modal warning that the selected repos are not all on the same branch.

    Used by actions (e.g. push) that otherwise need no input: it is only shown
    when a warning applies. Returns True if the user confirms, False otherwise.
    """
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    tk.Label(
        dialog,
        text=f"{action_label} {repo_count} selected "
             f"{'repository' if repo_count == 1 else 'repositories'}.",
        justify="left",
    ).pack(padx=16, pady=(16, 4), anchor="w")

    warn = ttk.Frame(dialog)
    warn.pack(padx=16, pady=(0, 4), anchor="w")
    tk.Label(warn, text="\u26A0", foreground=theme.ERROR,
             font=("", 11, "bold")).pack(side="left", padx=(0, 4))
    tk.Label(warn, text="The selected repositories are not all on the same branch.",
             foreground=theme.ERROR, justify="left", wraplength=360).pack(side="left")

    skip_empty = tk.BooleanVar(value=True)
    ttk.Checkbutton(
        dialog, variable=skip_empty,
        text="Skip empty branches (no changes vs master)",
    ).pack(padx=16, pady=(4, 0), anchor="w")

    result = {"ok": False}

    def _ok():
        result["ok"] = True
        dialog.destroy()

    def _cancel():
        result["ok"] = False
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text=action_label, command=_ok).pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return {"ok": result["ok"], "skip_empty": bool(skip_empty.get())}


def ask_pr_details(parent, repo_count):
    """Modal collecting pull-request options for every selected repo.

    The user can either auto-generate each PR title from that repo's own branch
    name (e.g. ``feature/123_my_description`` -> ``feature(123) My description``)
    or enter one custom title applied to every repo. A description is optional
    and shared by all. Returns a dict {"mode", "title", "description", "draft"}
    where *mode* is "auto" or "custom", or None if cancelled.
    """
    dialog = tk.Toplevel(parent)
    dialog.title("Create pull requests")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    tk.Label(
        dialog,
        text=f"Create a pull request to master for {repo_count} selected "
             f"{'repository' if repo_count == 1 else 'repositories'}.",
        justify="left",
    ).pack(padx=16, pady=(16, 8), anchor="w")

    mode = tk.StringVar(value="auto")

    # Title entry, only enabled in "custom" mode.
    title_entry = ttk.Entry(dialog, width=46)

    def _sync_title_state(*_args):
        title_entry.configure(
            state="normal" if mode.get() == "custom" else "disabled"
        )

    ttk.Radiobutton(
        dialog, text="Auto-generate each title from its branch name",
        variable=mode, value="auto", command=_sync_title_state,
    ).pack(padx=16, anchor="w")
    tk.Label(
        dialog,
        text="e.g. feature/123_my_description \u2192 feature(123) My description",
        foreground=theme.FG_MUTED,
    ).pack(padx=36, anchor="w")

    ttk.Radiobutton(
        dialog, text="Use a custom title for all", variable=mode,
        value="custom", command=_sync_title_state,
    ).pack(padx=16, pady=(6, 0), anchor="w")
    title_entry.pack(padx=36, pady=(0, 4), anchor="w", fill="x")

    tk.Label(dialog, text="Description (optional):").pack(
        padx=16, pady=(6, 0), anchor="w"
    )
    desc_text = tk.Text(dialog, width=46, height=4, wrap="word")
    desc_text.pack(padx=16, pady=(0, 4), fill="x")

    skip_empty = tk.BooleanVar(value=True)
    ttk.Checkbutton(
        dialog, variable=skip_empty,
        text="Skip empty branches (no changes vs master)",
    ).pack(padx=16, pady=(4, 0), anchor="w")

    draft = tk.BooleanVar(value=False)
    ttk.Checkbutton(
        dialog, variable=draft,
        text="Create as draft",
    ).pack(padx=16, pady=(4, 0), anchor="w")

    error_label = tk.Label(dialog, text="", foreground=theme.ERROR)
    error_label.pack(padx=16, anchor="w")

    _sync_title_state()

    result = {"value": None}

    def _ok():
        chosen = mode.get()
        title = title_entry.get().strip()
        if chosen == "custom" and not title:
            error_label.config(text="A custom title is required.")
            return
        result["value"] = {
            "mode": chosen,
            "title": title,
            "description": desc_text.get("1.0", "end").strip(),
            "skip_empty": bool(skip_empty.get()),
            "draft": bool(draft.get()),
        }
        dialog.destroy()

    def _cancel():
        result["value"] = None
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text="Create", command=_ok).pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["value"]


_MERGE_STRATEGY_LABELS = [
    ("Merge (no fast-forward)", "noFastForward"),
    ("Squash commit", "squash"),
    ("Rebase and fast-forward", "rebase"),
    ("Semi-linear merge (rebase, then merge)", "rebaseMerge"),
]


def ask_complete_pr_details(parent, repo_count):
    """Modal collecting completion (merge) options for the selected repos' PRs.

    Lets the user pick the merge strategy, whether to delete each source branch
    and transition linked work items, and how to remediate PRs that cannot merge
    right away: publish drafts, queue a missing build, and set auto-complete when
    a PR is not ready yet. Returns a dict with those choices or None if cancelled.
    """
    dialog = tk.Toplevel(parent)
    dialog.title("Complete pull requests")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    tk.Label(
        dialog,
        text=f"Complete (merge) the open pull request to master for "
             f"{repo_count} selected "
             f"{'repository' if repo_count == 1 else 'repositories'}.",
        justify="left",
    ).pack(padx=16, pady=(16, 8), anchor="w")

    tk.Label(dialog, text="Merge strategy:").pack(padx=16, anchor="w")
    strategy = tk.StringVar(value="squash")
    for text, value in _MERGE_STRATEGY_LABELS:
        ttk.Radiobutton(
            dialog, text=text, variable=strategy, value=value,
        ).pack(padx=36, anchor="w")

    delete_branch = tk.BooleanVar(value=True)
    ttk.Checkbutton(
        dialog, variable=delete_branch,
        text="Delete source branch after merging",
    ).pack(padx=16, pady=(8, 0), anchor="w")

    transition = tk.BooleanVar(value=False)
    ttk.Checkbutton(
        dialog, variable=transition,
        text="Complete linked work items after merging",
    ).pack(padx=16, pady=(4, 0), anchor="w")

    ttk.Separator(dialog, orient="horizontal").pack(
        fill="x", padx=16, pady=(10, 6)
    )
    tk.Label(
        dialog, text="When a PR is not ready to merge:",
        foreground=theme.FG_MUTED,
    ).pack(padx=16, anchor="w")

    publish_draft = tk.BooleanVar(value=True)
    ttk.Checkbutton(
        dialog, variable=publish_draft,
        text="Publish draft PRs (unmark as draft) before completing",
    ).pack(padx=16, pady=(4, 0), anchor="w")

    queue_build = tk.BooleanVar(value=True)
    ttk.Checkbutton(
        dialog, variable=queue_build,
        text="Queue the required build if a draft PR has none",
    ).pack(padx=16, pady=(4, 0), anchor="w")

    auto_complete = tk.BooleanVar(value=True)
    ttk.Checkbutton(
        dialog, variable=auto_complete,
        text="Set auto-complete when build or policies are still running",
    ).pack(padx=16, pady=(4, 0), anchor="w")

    result = {"value": None}

    def _ok():
        result["value"] = {
            "merge_strategy": strategy.get(),
            "delete_source_branch": bool(delete_branch.get()),
            "transition_work_items": bool(transition.get()),
            "publish_draft": bool(publish_draft.get()),
            "queue_build": bool(queue_build.get()),
            "auto_complete_when_not_ready": bool(auto_complete.get()),
        }
        dialog.destroy()

    def _cancel():
        result["value"] = None
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text="Complete", command=_ok).pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["value"]


def ask_pbi_number(parent):
    """Modal asking for the Azure DevOps PBI (work item) number.

    Returns the numeric id as a string, or None if cancelled. Only digits are
    accepted.
    """
    dialog = tk.Toplevel(parent)
    dialog.title("Create workspace from PBI")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    tk.Label(
        dialog, text="Enter the PBI (work item) number to download:",
        justify="left",
    ).pack(padx=16, pady=(16, 4), anchor="w")

    entry = ttk.Entry(dialog, width=24)
    entry.pack(padx=16, pady=(0, 4), anchor="w")
    entry.focus_set()

    error_label = tk.Label(dialog, text="", foreground=theme.ERROR)
    error_label.pack(padx=16, anchor="w")

    result = {"id": None}

    def _ok():
        value = entry.get().strip()
        if not value:
            error_label.config(text="A PBI number is required.")
            return
        if not value.isdigit():
            error_label.config(text="The PBI number must be digits only.")
            return
        result["id"] = value
        dialog.destroy()

    def _cancel():
        result["id"] = None
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text="Next", command=_ok).pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    entry.bind("<Return>", lambda _e: _ok())
    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["id"]


# Combobox sentinel letting the user leave a PBI service out of the workspace.
_EXCLUDE_LABEL = "\u2014 exclude \u2014"


def resolve_pbi_repos(parent, mappings, folders, nuget_folders=None):
    """Modal mapping each PBI service name to a local repository folder.

    *mappings* is a list of (service_name, folder_or_None) as recognised from
    the synonyms dictionary; *folders* is the list of available local repository
    folder names. Each row shows the service name and a folder picker pre-set to
    the recognised folder (or blank/"unknown" when not recognised). A service can
    be left out of the workspace entirely by choosing "exclude", or included but
    marked to ignore git commands via the per-row "Ignore git" checkbox.

    *nuget_folders*, if given, is the list of shared NuGet folder names; they are
    offered at the *end* of every folder dropdown so a service can be mapped to a
    shared NuGet repository as well.

    The Next button is disabled until every service is either mapped or
    excluded. Returns a ``{service_name: folder_or_None}`` dict on submit (an
    excluded service maps to ``None``), or ``None`` if cancelled.
    """
    nuget_folders = list(nuget_folders or [])

    dialog = tk.Toplevel(parent)
    dialog.title("Create workspace from PBI - map repositories")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    tk.Label(
        dialog,
        text=f"{len(mappings)} service"
             f"{'' if len(mappings) == 1 else 's'} found in the PBI work "
             "breakdown. Map each to a local repository folder.",
        justify="left", wraplength=420,
    ).pack(padx=16, pady=(16, 4), anchor="w")
    tk.Label(
        dialog,
        text="Unrecognised services are marked in red - pick the matching "
             "folder, or choose \"exclude\" to leave it out of the workspace. "
             "Shared NuGet repositories are listed at the end of each dropdown. "
             "New mappings are remembered for next time.",
        foreground=theme.FG_MUTED, justify="left", wraplength=420,
    ).pack(padx=16, pady=(0, 8), anchor="w")

    table = ttk.Frame(dialog)
    table.pack(padx=16, fill="x")
    ttk.Label(table, text="PBI service", font=("", 9, "bold")).grid(
        row=0, column=0, sticky="w", padx=4, pady=(0, 4)
    )
    ttk.Label(table, text="Local folder", font=("", 9, "bold")).grid(
        row=0, column=1, sticky="w", padx=4, pady=(0, 4)
    )

    # Dropdown order: "exclude" first, then repository folders, then the shared
    # NuGet folders at the very end.
    folder_values = [_EXCLUDE_LABEL] + sorted(folders) + sorted(nuget_folders)
    rows = []  # (service_name, combobox, status_label)

    for index, (service, folder) in enumerate(mappings, start=1):
        tk.Label(table, text=service, justify="left", wraplength=200).grid(
            row=index, column=0, sticky="w", padx=4, pady=2
        )
        combo = ttk.Combobox(table, values=folder_values, width=34,
                             state="readonly")
        if folder:
            combo.set(folder)
        combo.grid(row=index, column=1, sticky="w", padx=4, pady=2)
        status = tk.Label(table, text="", width=10)
        status.grid(row=index, column=2, sticky="w", padx=4, pady=2)
        rows.append((service, combo, status))

    result = {"value": None}
    create_button = None  # assigned below; updated by _refresh

    def _refresh(*_args):
        all_resolved = True
        for _service, combo, status in rows:
            value = combo.get()
            if value == _EXCLUDE_LABEL:
                status.config(text="excluded", foreground=theme.FG_MUTED)
            elif value:
                status.config(text="mapped", foreground=theme.SUCCESS)
            else:
                status.config(text="unknown", foreground=theme.ERROR)
                all_resolved = False
        if create_button is not None:
            create_button.config(state="normal" if all_resolved else "disabled")

    for _service, combo, _status in rows:
        combo.bind("<<ComboboxSelected>>", _refresh)

    def _ok():
        resolved = {}
        for service, combo, _status in rows:
            value = combo.get()
            resolved[service] = None if value == _EXCLUDE_LABEL else (value or None)
        result["value"] = resolved
        dialog.destroy()

    def _cancel():
        result["value"] = None
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    create_button = ttk.Button(bar, text="Next", command=_ok)
    create_button.pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    _refresh()
    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["value"]


def ask_workspace_branches(parent, repo_names, initial="", current_branches=None):
    """Modal to name the workspace and set a feature branch per repository.

    The first field is the workspace name; each following field is one repo's
    feature branch, pre-filled with the workspace name and editable. A per-repo
    "Ignore git" checkbox marks a repo that is included in the workspace but
    excluded from git commands (it keeps its own branch). Ticking it shows the
    repo's *current* branch (from *current_branches*, a {repo: branch} map) in
    the now-disabled field; that branch is returned so it can be recorded in the
    workspace file. Unticking restores the value the field held before ticking.

    Returns ``{"name": str, "branches": {repo: suffix},
    "ignore_git": {repo: bool}, "ignore_branches": {repo: current_branch}}``
    (branch suffixes exclude the "feature/" prefix), or None if cancelled.
    """
    dialog = tk.Toplevel(parent)
    dialog.title("Name the feature workspace")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    tk.Label(
        dialog,
        text="Name the workspace, then set each repository's feature branch. "
             "Branches are pre-filled with the workspace name - override any "
             "that differ. Tick \"Ignore git\" to keep a repo on its own branch.",
        justify="left", wraplength=460,
    ).pack(padx=16, pady=(16, 8), anchor="w")

    body = ttk.Frame(dialog)
    body.pack(padx=16, fill="x")

    ttk.Label(body, text="Workspace name", font=("", 9, "bold")).grid(
        row=0, column=0, sticky="w", padx=4, pady=(0, 6)
    )
    name_var = tk.StringVar(value=initial)
    name_entry = ttk.Entry(body, textvariable=name_var, width=40)
    name_entry.grid(row=0, column=1, columnspan=2, sticky="w", padx=4, pady=(0, 6))
    name_entry.focus_set()

    ttk.Label(body, text="Repository", font=("", 9, "bold")).grid(
        row=1, column=0, sticky="w", padx=4, pady=(4, 4)
    )
    ttk.Label(body, text="Feature branch", font=("", 9, "bold")).grid(
        row=1, column=1, sticky="w", padx=4, pady=(4, 4)
    )
    ttk.Label(body, text="Ignore git", font=("", 9, "bold")).grid(
        row=1, column=2, sticky="w", padx=4, pady=(4, 4)
    )

    branch_vars, ignore_vars = {}, {}
    # The value each field held just before "Ignore git" was ticked, so
    # unticking can restore it instead of resetting to the workspace name.
    pre_tick = {}
    current_branches = current_branches or {}

    for index, repo in enumerate(repo_names, start=2):
        ttk.Label(body, text=repo, justify="left", wraplength=180).grid(
            row=index, column=0, sticky="w", padx=4, pady=2
        )
        cell = ttk.Frame(body)
        cell.grid(row=index, column=1, sticky="w", padx=4, pady=2)
        prefix = ttk.Label(cell, text="feature/")
        prefix.pack(side="left")
        branch_var = tk.StringVar(value=initial)
        branch_entry = ttk.Entry(cell, textvariable=branch_var, width=28)
        branch_entry.pack(side="left")
        ignore_var = tk.BooleanVar(value=False)

        # Ticking "Ignore git" shows the repo's current branch (disabled); it is
        # stored in the workspace file. Unticking restores the pre-tick value.
        def _toggle(r=repo, e=branch_entry, p=prefix):
            if ignore_vars[r].get():
                pre_tick[r] = branch_vars[r].get()
                p.pack_forget()  # a full branch is shown, not a 'feature/' suffix
                branch_vars[r].set(current_branches.get(r, ""))
                e.config(state="disabled")
            else:
                p.pack(side="left", before=e)
                e.config(state="normal")
                branch_vars[r].set(pre_tick.get(r, initial))

        ttk.Checkbutton(body, variable=ignore_var, command=_toggle).grid(
            row=index, column=2, padx=4, pady=2
        )
        branch_vars[repo] = branch_var
        ignore_vars[repo] = ignore_var

    error_label = tk.Label(dialog, text="", foreground=theme.ERROR,
                           justify="left", wraplength=460)
    error_label.pack(padx=16, anchor="w")

    result = {"value": None}

    def _ok():
        name = name_var.get().strip()
        if not name:
            error_label.config(text="Workspace name is required.")
            return
        if not is_valid_branch_name(name):
            error_label.config(
                text="Invalid workspace name: no spaces; use letters, digits, "
                     ". _ / -"
            )
            return
        branches, ignore_git, ignore_branches = {}, {}, {}
        for repo in repo_names:
            ignore = ignore_vars[repo].get()
            ignore_git[repo] = ignore
            if ignore:
                # Keep the repo's current branch so it can be recorded verbatim.
                ignore_branches[repo] = branch_vars[repo].get().strip()
                continue
            suffix = branch_vars[repo].get().strip()
            if not suffix:
                error_label.config(text=f"{repo}: a branch name is required.")
                return
            if not is_valid_branch_name(suffix):
                error_label.config(
                    text=f"{repo}: invalid branch name (no spaces; use letters, "
                         "digits, . _ / -)."
                )
                return
            branches[repo] = suffix
        result["value"] = {
            "name": name, "branches": branches, "ignore_git": ignore_git,
            "ignore_branches": ignore_branches,
        }
        dialog.destroy()

    def _cancel():
        result["value"] = None
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text="Create", command=_ok).pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["value"]


def edit_synonyms(parent):
    """Modal editor for the repository synonyms dictionary (repo_synonyms.json).

    Shows the dictionary as formatted JSON the user can edit. On Save the text
    must parse as a JSON object of {folder: [synonym, ...]}; it is then written
    back to disk. Returns True if saved, False if cancelled.
    """
    dialog = tk.Toplevel(parent)
    dialog.title("Repository synonyms")
    dialog.transient(parent.winfo_toplevel())

    tk.Label(
        dialog,
        text="Map each repository folder to the alternative names that may "
             "appear in a PBI. One entry per folder, each a list of synonyms.",
        justify="left", wraplength=440,
    ).pack(padx=16, pady=(16, 6), anchor="w")

    text = tk.Text(dialog, width=58, height=20, wrap="none")
    text.pack(padx=16, fill="both", expand=True)
    text.insert("1.0", json.dumps(pbi.load_synonyms(), indent=4, sort_keys=True))
    text.focus_set()

    error_label = tk.Label(dialog, text="", foreground=theme.ERROR,
                           justify="left", wraplength=440)
    error_label.pack(padx=16, anchor="w")

    result = {"saved": False}

    def _save():
        raw = text.get("1.0", "end").strip()
        try:
            data = json.loads(raw)
        except ValueError as exc:
            error_label.config(text=f"Invalid JSON: {exc}")
            return
        if not isinstance(data, dict) or not all(
            isinstance(v, list) for v in data.values()
        ):
            error_label.config(
                text="Expected an object of \"folder\": [\"synonym\", ...]."
            )
            return
        ok, message = pbi.save_synonyms(data)
        if not ok:
            error_label.config(text=message)
            return
        result["saved"] = True
        dialog.destroy()

    def _cancel():
        result["saved"] = False
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text="Save", command=_save).pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["saved"]


def edit_branch_overrides(parent, workspace_name, entries):
    """Modal editor for a workspace's per-repo feature branch configuration.

    *entries* is the list returned by ``gitutils.workspace_branch_entries``
    (dicts with ``name``, ``path``, ``branch`` and ``ignoreGit``). Each git
    folder in the workspace gets a row where you can edit its feature branch and
    tick "Ignore git" to exclude it from the workspace's git commands (the repo
    then keeps whatever branch it is on). The branch field is pre-filled for
    every git folder, even when it equals the default 'feature/<workspace>'.
    Non-git folders are listed but not editable.

    Returns the ``{folder: {...}}`` override map to persist - only repos that
    ignore git or whose branch differs from the default are included - or None
    if the dialog is cancelled.
    """
    from gitutils import is_git_repo, default_workspace_branch, IGNORE_GIT_KEY

    default = default_workspace_branch(workspace_name)

    dialog = tk.Toplevel(parent)
    dialog.title(f"Manage workspace branches - {workspace_name}")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    header = ttk.Frame(dialog)
    header.pack(padx=16, pady=(16, 8), fill="x")
    tk.Label(
        header,
        text=f"Configure the feature branch for each repository in "
             f"\"{workspace_name}\".",
        justify="left", wraplength=430,
    ).pack(side="left", anchor="w")
    # Details moved into a hover tooltip on the info icon to keep the dialog compact.
    info_icon = tk.Label(
        header, text="\u24d8", font=("Segoe UI", 12),
        foreground=theme.FG_MUTED, cursor="question_arrow",
    )
    info_icon.pack(side="left", anchor="n", padx=(6, 0))
    Tooltip(
        info_icon,
        f"The default branch is \"{default}\". Change it for any repo whose "
        "feature branch has a different name. Tick \"Ignore git\" for a repo "
        "that keeps its own branch and should be left out of the git "
        "commands. Only differences are saved.",
    )

    table = ttk.Frame(dialog)
    table.pack(padx=16, fill="x")
    ttk.Label(table, text="Repository", font=("", 9, "bold")).grid(
        row=0, column=0, sticky="w", padx=4, pady=(0, 4)
    )
    ttk.Label(table, text="Feature branch", font=("", 9, "bold")).grid(
        row=0, column=1, sticky="w", padx=4, pady=(0, 4)
    )
    ttk.Label(table, text="Ignore git", font=("", 9, "bold")).grid(
        row=0, column=2, sticky="w", padx=4, pady=(0, 4)
    )

    rows = []  # (name, is_git, branch_var, ignore_var)

    for index, entry in enumerate(entries, start=1):
        name = entry["name"]
        is_git = is_git_repo(entry["path"])
        ignore_var = tk.BooleanVar(value=bool(entry["ignoreGit"]) and is_git)
        branch_var = tk.StringVar(
            value=entry["branch"] if is_git else "(not a git repository)"
        )

        tk.Label(table, text=name, justify="left", wraplength=220).grid(
            row=index, column=0, sticky="w", padx=4, pady=2
        )
        branch_entry = ttk.Entry(table, textvariable=branch_var, width=34)
        branch_entry.grid(row=index, column=1, sticky="w", padx=4, pady=2)
        ignore_check = ttk.Checkbutton(table, variable=ignore_var)
        ignore_check.grid(row=index, column=2, padx=4, pady=2)

        if not is_git:
            branch_entry.config(state="disabled")
            ignore_check.config(state="disabled")
        else:
            # Ignored repos keep their own branch, so disable the branch field
            # while "Ignore git" is ticked.
            def _toggle(e=branch_entry, v=ignore_var):
                e.config(state="disabled" if v.get() else "normal")

            ignore_check.config(command=_toggle)
            _toggle()

        rows.append((name, is_git, branch_var, ignore_var))

    error_label = tk.Label(dialog, text="", foreground=theme.ERROR,
                           justify="left", wraplength=460)
    error_label.pack(padx=16, anchor="w")

    result = {"value": None}

    def _save():
        overrides = {}
        for name, is_git, branch_var, ignore_var in rows:
            if not is_git:
                continue
            if ignore_var.get():
                overrides[name] = {IGNORE_GIT_KEY: True}
                continue
            branch = branch_var.get().strip()
            if not branch:
                error_label.config(text=f"{name}: a branch name is required.")
                return
            if not is_valid_branch_name(branch):
                error_label.config(
                    text=f"{name}: invalid branch name (no spaces; use letters, "
                         "digits, . _ / -)."
                )
                return
            if branch != default:
                overrides[name] = {"branch": branch}
        result["value"] = overrides
        dialog.destroy()

    def _cancel():
        result["value"] = None
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text="Save", command=_save).pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["value"]


def ask_include_skipped(parent, action_label, names):
    """Modal offering each skipped repo an "Include" checkbox (default off).

    *names* is the list of repo folder names flagged as skipped. The repos are
    normally left out of *action_label*; ticking a checkbox opts that repo back
    in for this run only. Returns the set of names the user chose to include, or
    None if the dialog is cancelled.
    """
    dialog = tk.Toplevel(parent)
    dialog.title(f"Include skipped repositories - {action_label}")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    tk.Label(
        dialog,
        text=f"These repositories are flagged as skipped and are normally left "
             f"out of \"{action_label}\". Tick any you want to include this time.",
        justify="left", wraplength=420,
    ).pack(padx=16, pady=(16, 8), anchor="w")

    box = ttk.Frame(dialog)
    box.pack(padx=16, fill="x")
    checks = {}
    for name in names:
        var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text=name, variable=var).pack(anchor="w", pady=1)
        checks[name] = var

    result = {"value": None}

    def _ok():
        result["value"] = {name for name, var in checks.items() if var.get()}
        dialog.destroy()

    def _cancel():
        result["value"] = None
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text="Continue", command=_ok).pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["value"]


def ask_missing_remote_branches(parent, names, environment_label):
    """Modal warning that some repos have no remote feature branch.

    *names* is the list of repo folder names whose feature branch does not exist
    on origin (so no pipeline can be started for them). Returns True to continue
    for the repositories that do have a remote branch, or False to abort the
    whole run. *environment_label* (e.g. "Development") is shown for context.
    """
    dialog = tk.Toplevel(parent)
    dialog.title("Missing remote branches")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    tk.Label(
        dialog,
        text=f"These repositories do not have a remote branch, so no "
             f"{environment_label} pipeline can be started for them:",
        justify="left", wraplength=420,
    ).pack(padx=16, pady=(16, 8), anchor="w")

    box = ttk.Frame(dialog)
    box.pack(padx=16, fill="x")
    for name in names:
        tk.Label(box, text=f"\u2022 {name}", font=("", 9, "bold")).pack(
            anchor="w", pady=1
        )

    tk.Label(
        dialog,
        text="Do you want to abort, or continue and run pipelines only for the "
             "repositories that do have a remote branch?",
        justify="left", wraplength=420,
    ).pack(padx=16, pady=(8, 0), anchor="w")

    result = {"value": False}

    def _continue():
        result["value"] = True
        dialog.destroy()

    def _abort():
        result["value"] = False
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text="Continue for existing branches",
               command=_continue).pack(side="left", padx=4)
    ttk.Button(bar, text="Abort", command=_abort).pack(side="left", padx=4)

    dialog.protocol("WM_DELETE_WINDOW", _abort)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["value"]


def ask_deploy_selection(parent, entries, environment_label):
    """Modal to choose which repositories to deploy to an environment.

    *entries* is a list of ``(repo_name, already_deployed, last_commit)`` where
    ``last_commit`` is a ``(short_hash, subject)`` tuple. A repo whose latest
    branch commit already deployed successfully to the environment is unticked
    by default; ticking it shows an inline note that it will be redeployed with
    no changes. The last commit is always shown. Returns a
    ``{repo_name: deploy_bool}`` dict on confirm, or ``None`` if cancelled.
    """
    dialog = tk.Toplevel(parent)
    dialog.title(f"Run {environment_label} deployments")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    tk.Label(
        dialog,
        text=f"Select the repositories to deploy to {environment_label}.",
        justify="left", wraplength=480,
    ).pack(padx=16, pady=(16, 4), anchor="w")
    tk.Label(
        dialog,
        text="Repositories whose latest branch commit was already deployed "
             "successfully are unticked. Tick one to redeploy it anyway.",
        foreground=theme.FG_MUTED, justify="left", wraplength=480,
    ).pack(padx=16, pady=(0, 8), anchor="w")

    table = ttk.Frame(dialog)
    table.pack(padx=16, fill="x")
    ttk.Label(table, text="Deploy", font=("", 9, "bold")).grid(
        row=0, column=0, sticky="w", padx=4, pady=(0, 4)
    )
    ttk.Label(table, text="Repository", font=("", 9, "bold")).grid(
        row=0, column=1, sticky="w", padx=4, pady=(0, 4)
    )

    rows = []  # (name, already_deployed, var, note_label)
    for index, (name, already, *rest) in enumerate(entries, start=1):
        short, subject = (rest[0] if rest else ("", "")) or ("", "")
        var = tk.BooleanVar(value=not already)
        ttk.Checkbutton(table, variable=var).grid(
            row=index, column=0, sticky="w", padx=4, pady=2
        )
        tk.Label(table, text=name, font=("", 9, "bold")).grid(
            row=index, column=1, sticky="w", padx=4, pady=2
        )
        note = tk.Label(table, text="", justify="left", wraplength=260)
        note.grid(row=index, column=2, sticky="w", padx=6, pady=2)
        rows.append((name, already, var, note, short, subject))

    def _commit_text(short, subject):
        if not short and not subject:
            return ""
        return f"{short}  {subject}".strip()

    def _refresh(*_args):
        for _name, already, var, note, short, subject in rows:
            commit = _commit_text(short, subject)
            if already and var.get():
                extra = ("earlier successful deployment already exists - repo "
                         "will be redeployed with no changes")
                note.config(
                    text=f"{commit}\n{extra}" if commit else extra,
                    foreground=theme.WARNING,
                )
            elif already:
                extra = "already deployed - will be skipped"
                note.config(
                    text=f"{commit}\n{extra}" if commit else extra,
                    foreground=theme.FG_MUTED,
                )
            else:
                note.config(text=commit, foreground=theme.FG_MUTED)

    for _name, _already, var, _note, _short, _subject in rows:
        var.trace_add("write", _refresh)

    result = {"value": None}

    def _ok():
        result["value"] = {name: var.get() for name, _a, var, _n, _s, _su in rows}
        dialog.destroy()

    def _cancel():
        result["value"] = None
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text="Run deployments", command=_ok).pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    _refresh()
    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["value"]


def ask_acc_autoapprove(parent):
    """Modal asking whether ACC pipeline acceptance approvals should auto-approve.

    Returns True for auto-approve, False for manual approval.
    """
    dialog = tk.Toplevel(parent)
    dialog.title("Acceptance approval")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    tk.Label(
        dialog,
        text=(
            "Do you want Feature Manager to auto-approve Acceptance when the "
            "ACC pipeline reaches the approval gate?"
        ),
        justify="left", wraplength=440,
    ).pack(padx=16, pady=(16, 8), anchor="w")
    tk.Label(
        dialog,
        text=(
            "If you choose No, approval remains manual in Azure DevOps."
        ),
        justify="left", wraplength=440, foreground=theme.FG_MUTED,
    ).pack(padx=16, pady=(0, 8), anchor="w")

    result = {"value": False}

    def _yes():
        result["value"] = True
        dialog.destroy()

    def _no():
        result["value"] = False
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text="Yes, auto-approve", command=_yes).pack(
        side="left", padx=4
    )
    ttk.Button(bar, text="No, I will approve manually", command=_no).pack(
        side="left", padx=4
    )

    dialog.protocol("WM_DELETE_WINDOW", _no)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["value"]


def confirm_force_close(parent, monitor_count):
    """Modal shown on app close while pipeline monitors are still open.

    Returns True to force-close the app, False to abort and keep it running.
    """
    plural = "s" if monitor_count != 1 else ""
    dialog = tk.Toplevel(parent)
    dialog.title("Pipeline monitors still open")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    tk.Label(
        dialog,
        text=(
            f"{monitor_count} pipeline monitor window{plural} still open. "
            "Closing the app now stops monitoring those pipelines."
        ),
        justify="left", wraplength=440,
    ).pack(padx=16, pady=(16, 8), anchor="w")

    result = {"value": False}

    def _force():
        result["value"] = True
        dialog.destroy()

    def _abort():
        result["value"] = False
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text="Force close", command=_force).pack(side="left", padx=4)
    ttk.Button(bar, text="Abort", command=_abort).pack(side="left", padx=4)

    dialog.protocol("WM_DELETE_WINDOW", _abort)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["value"]


def ask_pipeline_poll_seconds(parent, current, minimum, maximum):
    """Modal asking for pipeline monitor poll frequency in seconds.

    Returns the chosen integer value, or None if cancelled.
    """
    dialog = tk.Toplevel(parent)
    dialog.title("Pipeline monitor polling")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    tk.Label(
        dialog,
        text=(
            "Set how often the floating pipeline monitor refreshes statuses."
        ),
        justify="left", wraplength=420,
    ).pack(padx=16, pady=(16, 6), anchor="w")
    tk.Label(
        dialog,
        text=(
            f"Frequency in seconds (integer, min {minimum}, max {maximum})."
        ),
        foreground=theme.FG_MUTED,
        justify="left",
    ).pack(padx=16, pady=(0, 8), anchor="w")

    row = ttk.Frame(dialog)
    row.pack(padx=16, pady=(0, 4), anchor="w")
    ttk.Label(row, text="Poll every").pack(side="left")
    entry = ttk.Entry(row, width=10)
    entry.pack(side="left", padx=(6, 6))
    ttk.Label(row, text="seconds").pack(side="left")
    entry.insert(0, str(current))
    entry.focus_set()

    error_label = tk.Label(dialog, text="", foreground=theme.ERROR)
    error_label.pack(padx=16, anchor="w")

    result = {"value": None}

    def _ok():
        raw = entry.get().strip()
        if not raw:
            error_label.config(text="Polling frequency is required.")
            return
        if not raw.isdigit():
            error_label.config(text="Polling frequency must be an integer.")
            return
        value = int(raw)
        if value < minimum or value > maximum:
            error_label.config(
                text=(
                    f"Polling frequency must be between {minimum} and {maximum} seconds."
                )
            )
            return
        result["value"] = value
        dialog.destroy()

    def _cancel():
        result["value"] = None
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text="Save", command=_ok).pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    entry.bind("<Return>", lambda _e: _ok())
    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["value"]

