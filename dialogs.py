"""Modal dialogs used by the action tabs.

Each function is blocking (modal) and returns the user's choice. They take the
parent widget so they can be centred on the main window.
"""

import tkinter as tk
from tkinter import ttk

from gitutils import is_valid_branch_name


def ask_commit_or_abort(parent, repo_names):
    """Modal asking whether to commit & rebase or abort. Returns True=commit."""
    dialog = tk.Toplevel(parent)
    dialog.title("Uncommitted changes")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    message = (
        "There are uncommitted changes in following repositories:\n"
        f"{', '.join(repo_names)}.\n\n"
        "Do you want to commit and rebase, or to abort operation?"
    )
    tk.Label(dialog, text=message, justify="left", wraplength=380).pack(
        padx=16, pady=12
    )

    choice = {"commit": False}

    def _commit():
        choice["commit"] = True
        dialog.destroy()

    def _abort():
        choice["commit"] = False
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=(0, 12))
    ttk.Button(bar, text="Commit and rebase", command=_commit).pack(
        side="left", padx=4
    )
    ttk.Button(bar, text="Abort operation", command=_abort).pack(
        side="left", padx=4
    )

    dialog.protocol("WM_DELETE_WINDOW", _abort)
    dialog.grab_set()
    parent.wait_window(dialog)
    return choice["commit"]


def ask_change_decision(parent, name, on_master):
    """Per-repo modal for handling uncommitted changes when creating a branch.

    Returns "delete", "commit" or "move"; None if the user closes the modal.
    Committing is not offered when the repo is on master.
    """
    dialog = tk.Toplevel(parent)
    dialog.title(f"Uncommitted changes - {name}")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    message = (
        f"Repository '{name}' has uncommitted changes.\n"
        "What would you like to do with them?"
    )
    if on_master:
        message += "\n\n(On master, committing is not allowed.)"
    tk.Label(dialog, text=message, justify="left", wraplength=400).pack(
        padx=16, pady=12
    )

    choice = {"value": None}

    def _set(value):
        choice["value"] = value
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=(0, 12))
    ttk.Button(bar, text="Delete changes",
               command=lambda: _set("delete")).pack(side="left", padx=4)
    if not on_master:
        ttk.Button(bar, text="Commit changes",
                   command=lambda: _set("commit")).pack(side="left", padx=4)
    ttk.Button(bar, text="Move to new feature branch",
               command=lambda: _set("move")).pack(side="left", padx=4)

    dialog.protocol("WM_DELETE_WINDOW", lambda: _set(None))
    dialog.grab_set()
    parent.wait_window(dialog)
    return choice["value"]


def ask_commit_delete_abort(parent, name):
    """Per-repo modal for handling uncommitted changes before a workspace switch.

    Returns "commit", "delete"; None if the user aborts/closes the modal.
    """
    dialog = tk.Toplevel(parent)
    dialog.title(f"Uncommitted changes - {name}")
    dialog.transient(parent.winfo_toplevel())
    dialog.resizable(False, False)

    message = (
        f"Repository '{name}' has uncommitted changes.\n"
        "Commit them, delete them, or abort the switch?"
    )
    tk.Label(dialog, text=message, justify="left", wraplength=400).pack(
        padx=16, pady=12
    )

    choice = {"value": None}

    def _set(value):
        choice["value"] = value
        dialog.destroy()

    bar = ttk.Frame(dialog)
    bar.pack(padx=16, pady=(0, 12))
    ttk.Button(bar, text="Commit changes",
               command=lambda: _set("commit")).pack(side="left", padx=4)
    ttk.Button(bar, text="Delete changes",
               command=lambda: _set("delete")).pack(side="left", padx=4)
    ttk.Button(bar, text="Abort operation",
               command=lambda: _set(None)).pack(side="left", padx=4)

    dialog.protocol("WM_DELETE_WINDOW", lambda: _set(None))
    dialog.grab_set()
    parent.wait_window(dialog)
    return choice["value"]


def ask_branch_name(parent, title="Create feature branch", prefix="feature/"):
    """Modal with a fixed *prefix* label and a required, validated name field.

    Returns the entered name (without prefix), or None if cancelled. The name
    must be a valid git branch name (no spaces).
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
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["name"]
