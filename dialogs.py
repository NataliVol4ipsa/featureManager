"""Modal dialogs used by the action tabs.

Each function is blocking (modal) and returns the user's choice. They take the
parent widget so they can be centred on the main window.
"""

import json
import tkinter as tk
from tkinter import ttk

from gitutils import is_valid_branch_name
import pbi


def _center_over_parent(dialog, parent):
    """Position *dialog* centred over the parent's top-level window."""
    dialog.update_idletasks()  # ensure the dialog has its real size
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
        tk.Label(warn, text="\u26A0", foreground="#c0392b",
                 font=("", 11, "bold")).pack(side="left", padx=(0, 4))
        master_line = ttk.Frame(warn)
        master_line.pack(side="left")
        tk.Label(master_line, text="This repository is on the ",
                 foreground="#c0392b").pack(side="left")
        tk.Label(master_line, text="master", foreground="#c0392b",
                 font=("", 9, "bold")).pack(side="left")
        tk.Label(master_line, text=" branch. Committing on master is not allowed.",
                 foreground="#c0392b").pack(side="left")

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

    error_label = tk.Label(dialog, text="", foreground="#c0392b")
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


def ask_commit_message(parent, repo_count, branch_warning=None):
    """Modal asking for a commit message applied to every selected repo.

    *repo_count* is shown for context. *branch_warning*, if given, is shown as a
    red warning (e.g. when the repos are not all on the same branch). Returns the
    entered message, or None if cancelled.
    """
    dialog = tk.Toplevel(parent)
    dialog.title("Commit all changes")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    tk.Label(
        dialog,
        text=f"Commit all changes in {repo_count} selected "
             f"{'repository' if repo_count == 1 else 'repositories'}.",
        justify="left",
    ).pack(padx=16, pady=(16, 4), anchor="w")

    # Optional mixed-branch (or similar) warning shown in red.
    if branch_warning:
        warn = ttk.Frame(dialog)
        warn.pack(padx=16, pady=(0, 4), anchor="w")
        tk.Label(warn, text="\u26A0", foreground="#c0392b",
                 font=("", 11, "bold")).pack(side="left", padx=(0, 4))
        tk.Label(warn, text=branch_warning, foreground="#c0392b",
                 justify="left", wraplength=360).pack(side="left")

    tk.Label(dialog, text="Commit message:").pack(padx=16, anchor="w")
    entry = ttk.Entry(dialog, width=44)
    entry.pack(padx=16, pady=(0, 4), fill="x")
    entry.focus_set()

    error_label = tk.Label(dialog, text="", foreground="#c0392b")
    error_label.pack(padx=16, anchor="w")

    result = {"message": None}

    def _ok():
        message = entry.get().strip()
        if not message:
            error_label.config(text="Commit message is required.")
            return
        result["message"] = message
        dialog.destroy()

    def _cancel():
        result["message"] = None
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    ttk.Button(bar, text="Commit", command=_ok).pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    entry.bind("<Return>", lambda _e: _ok())
    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["message"]


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
    tk.Label(warn, text="\u26A0", foreground="#c0392b",
             font=("", 11, "bold")).pack(side="left", padx=(0, 4))
    tk.Label(warn, text="The selected repositories are not all on the same branch.",
             foreground="#c0392b", justify="left", wraplength=360).pack(side="left")

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
    and shared by all. Returns a dict {"mode", "title", "description"} where
    *mode* is "auto" or "custom", or None if cancelled.
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
        foreground="#666666",
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

    error_label = tk.Label(dialog, text="", foreground="#c0392b")
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

    error_label = tk.Label(dialog, text="", foreground="#c0392b")
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
    ttk.Button(bar, text="Download", command=_ok).pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    entry.bind("<Return>", lambda _e: _ok())
    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    _center_over_parent(dialog, parent)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["id"]


# Combobox sentinel letting the user leave a PBI service out of the workspace.
_SKIP_LABEL = "\u2014 skip this repo \u2014"


def resolve_pbi_repos(parent, mappings, folders, nuget_folders=None):
    """Modal mapping each PBI service name to a local repository folder.

    *mappings* is a list of (service_name, folder_or_None) as recognised from
    the synonyms dictionary; *folders* is the list of available local repository
    folder names. Each row shows the service name and a folder picker pre-set to
    the recognised folder (or blank/"unknown" when not recognised). A service can
    also be left out of the workspace by choosing "skip this repo".

    *nuget_folders*, if given, is the list of shared NuGet folder names; they are
    offered at the *end* of every folder dropdown so a service can be mapped to a
    shared NuGet repository as well.

    The Create button is disabled until every service is either mapped or
    skipped. Returns a ``{service_name: folder_or_None}`` dict on submit (a
    skipped service maps to ``None``), or ``None`` if cancelled.
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
             "folder, or choose \"skip this repo\" to leave it out. Shared "
             "NuGet repositories are listed at the end of each dropdown. New "
             "mappings are remembered for next time.",
        foreground="#666666", justify="left", wraplength=420,
    ).pack(padx=16, pady=(0, 8), anchor="w")

    table = ttk.Frame(dialog)
    table.pack(padx=16, fill="x")
    ttk.Label(table, text="PBI service", font=("", 9, "bold")).grid(
        row=0, column=0, sticky="w", padx=4, pady=(0, 4)
    )
    ttk.Label(table, text="Local folder", font=("", 9, "bold")).grid(
        row=0, column=1, sticky="w", padx=4, pady=(0, 4)
    )

    # Dropdown order: "skip" first, then repository folders, then the shared
    # NuGet folders at the very end.
    folder_values = [_SKIP_LABEL] + sorted(folders) + sorted(nuget_folders)
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
            if value == _SKIP_LABEL:
                status.config(text="skipped", foreground="#666666")
            elif value:
                status.config(text="mapped", foreground="#1a9e1a")
            else:
                status.config(text="unknown", foreground="#c0392b")
                all_resolved = False
        if create_button is not None:
            create_button.config(state="normal" if all_resolved else "disabled")

    for _service, combo, _status in rows:
        combo.bind("<<ComboboxSelected>>", _refresh)

    def _ok():
        resolved = {}
        for service, combo, _ in rows:
            value = combo.get()
            resolved[service] = None if value == _SKIP_LABEL else value
        result["value"] = resolved
        dialog.destroy()

    def _cancel():
        result["value"] = None
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=12)
    create_button = ttk.Button(bar, text="Create", command=_ok)
    create_button.pack(side="left", padx=4)
    ttk.Button(bar, text="Cancel", command=_cancel).pack(side="left", padx=4)

    _refresh()
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

    error_label = tk.Label(dialog, text="", foreground="#c0392b",
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


