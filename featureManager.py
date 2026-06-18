"""
Feature Manager
================
A small desktop tool to manage repositories located under D:/Repositories.

Layout (left -> right):
  * Left half  : Notebook with two tabs ("Services" and "Nugets"), each
                 showing a scrollable, checkbox-based folder list plus
                 Select All / Deselect All buttons. Selections are kept in
                 memory so switching tabs never loses state.
  * Middle     : A vertical list of action buttons (one per row).
  * Right      : A placeholder section to be filled in later.

The code is intentionally split into small, reusable pieces so new tabs,
actions or panels can be added later with minimal effort.
"""

import os
import subprocess
import threading
import tkinter as tk
from tkinter import ttk

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Root folder that is scanned for repositories.
REPOS_ROOT = r"D:/Repositories"

# Sub-folder used to populate the "Nugets" tab.
NUGETS_ROOT = os.path.join(REPOS_ROOT, "Shared")

# Folder names that must never appear in the "Services" tab (case-insensitive).
EXCLUDED_FOLDERS = {"ibs", "shared", "wiki"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def list_subfolders(path):
    """Return a sorted list of immediate sub-folder names inside *path*.

    Files in the root are ignored (only directories are returned). Returns an
    empty list if the path does not exist so the UI can still render.
    """
    if not os.path.isdir(path):
        return []
    return sorted(
        name for name in os.listdir(path)
        if os.path.isdir(os.path.join(path, name))
    )


def get_service_folders():
    """Folders for the 'Services' tab: every repo folder except the excluded ones."""
    return [
        name for name in list_subfolders(REPOS_ROOT)
        if name.lower() not in EXCLUDED_FOLDERS
    ]


def get_nuget_folders():
    """Folders for the 'Nugets' tab: sub-folders of D:/Repositories/Shared."""
    return list_subfolders(NUGETS_ROOT)


def run_git(repo_path, args):
    """Run a git command inside *repo_path* and return (ok, output).

    *args* is the list of git arguments (e.g. ["pull"]). The combined
    stdout/stderr is returned as text so callers can log or display it.
    Never raises: a non-zero exit just yields ok=False.
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True, text=True,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output.strip()
    except FileNotFoundError:
        return False, "git executable not found on PATH"
    except OSError as exc:
        return False, str(exc)


def git_current_branch(repo_path):
    """Return the current branch name, or '' if it cannot be determined."""
    ok, out = run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    return out if ok else ""


def git_has_changes(repo_path):
    """Return True if the working tree has uncommitted (tracked/untracked) changes."""
    ok, out = run_git(repo_path, ["status", "--porcelain"])
    return ok and bool(out)


# --------------------------------------------------------------------------- #
# Reusable UI components
# --------------------------------------------------------------------------- #

class Tooltip:
    """Lightweight hover tooltip for any widget (used for action button hints)."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self._tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _event=None):
        if self._tip or not self.text:
            return
        # Position the tooltip just below-right of the widget.
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)  # no window border/title bar
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._tip, text=self.text, justify="left",
            background="#ffffe0", relief="solid", borderwidth=1,
            padx=6, pady=3, wraplength=260,
        ).pack()

    def _hide(self, _event=None):
        if self._tip:
            self._tip.destroy()
            self._tip = None


# Visual indicators for each repository row, keyed by status.
STATUS_STYLES = {
    "pending":     ("\u25CB", "gray"),    # hollow circle
    "in-progress": ("\u25CF", "#d98c00"),  # filled circle, amber
    "done":        ("\u25CF", "#1a9e1a"),  # filled circle, green
    "error":       ("\u25CF", "#c0392b"),  # filled circle, red
}


class ProgressPanel(ttk.Frame):
    """Right-side panel: a completion banner plus a live per-repo status list.

    Call set_repos() at the start of an action, then status() as each repo
    progresses. show_completion() reveals the green banner when everything
    finished successfully.
    """

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)

        # Green completion banner, shown above the repo list (hidden by default).
        self._banner = tk.Label(self, text="", foreground="#1a9e1a",
                                font=("", 10, "bold"), anchor="w")

        # Scrollable list area for repo rows.
        self._canvas = tk.Canvas(self, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(self, orient="vertical",
                                        command=self._canvas.yview)
        self._inner = ttk.Frame(self._canvas)
        self._inner.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._canvas.configure(yscrollcommand=self._scrollbar.set)

        self._canvas.pack(side="left", fill="both", expand=True)
        self._scrollbar.pack(side="right", fill="y")

        # Maps repo name -> the Label widget showing its status indicator.
        self._indicators = {}

    def set_repos(self, names):
        """Build one row per repository, all starting in the 'pending' state."""
        self.clear_completion()
        for child in self._inner.winfo_children():
            child.destroy()
        self._indicators = {}

        for name in names:
            row = ttk.Frame(self._inner)
            row.pack(fill="x", anchor="w", padx=4, pady=1)
            symbol, color = STATUS_STYLES["pending"]
            indicator = tk.Label(row, text=symbol, foreground=color, width=2)
            indicator.pack(side="left")
            ttk.Label(row, text=name).pack(side="left")
            self._indicators[name] = indicator

    def status(self, name, state):
        """Update a single repo row to the given state (see STATUS_STYLES)."""
        indicator = self._indicators.get(name)
        if indicator is None:
            return
        symbol, color = STATUS_STYLES.get(state, STATUS_STYLES["pending"])
        indicator.config(text=symbol, foreground=color)

    def show_completion(self, text):
        """Reveal the green completion banner above the repo list."""
        self._banner.config(text=text)
        self._banner.pack(fill="x", padx=6, pady=(4, 6), before=self._canvas)

    def clear_completion(self):
        """Hide the completion banner (e.g. when a new action starts)."""
        self._banner.config(text="")
        self._banner.pack_forget()


class ErrorList(ttk.Frame):
    """Full-width bottom area that lists errors from the most recent action.

    Reused across actions: call clear() before an action and add() per error.
    """

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)

        self._text = tk.Text(self, height=6, wrap="word", state="disabled",
                             foreground="#c0392b")
        scrollbar = ttk.Scrollbar(self, orient="vertical",
                                  command=self._text.yview)
        self._text.configure(yscrollcommand=scrollbar.set)
        self._text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def clear(self):
        """Remove all previously listed errors."""
        self._text.config(state="normal")
        self._text.delete("1.0", "end")
        self._text.config(state="disabled")

    def add(self, message):
        """Append a single error line."""
        self._text.config(state="normal")
        self._text.insert("end", message.rstrip() + "\n")
        self._text.config(state="disabled")
        self._text.see("end")


class CheckboxList(ttk.Frame):
    """A scrollable list of checkboxes.

    Each item keeps its own tk.BooleanVar so selection state survives even when
    the widget is hidden (e.g. while another notebook tab is shown).
    """

    def __init__(self, master, items, **kwargs):
        super().__init__(master, **kwargs)

        # Maps folder name -> BooleanVar holding its checked state.
        self.vars = {}

        # Canvas + inner frame + scrollbar give us a vertically scrollable area.
        self._canvas = tk.Canvas(self, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(
            self, orient="vertical", command=self._canvas.yview
        )
        self._inner = ttk.Frame(self._canvas)

        self._inner.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._canvas.configure(yscrollcommand=self._scrollbar.set)

        self._canvas.pack(side="left", fill="both", expand=True)
        self._scrollbar.pack(side="right", fill="y")

        # Mouse-wheel scrolling while hovering the list.
        self._canvas.bind("<Enter>", self._bind_mousewheel)
        self._canvas.bind("<Leave>", self._unbind_mousewheel)

        self.set_items(items)

    def set_items(self, items):
        """(Re)build the checkbox rows from *items* while preserving prior state."""
        for child in self._inner.winfo_children():
            child.destroy()

        new_vars = {}
        for name in items:
            # Reuse an existing BooleanVar so a rescan keeps the user's choices.
            var = self.vars.get(name, tk.BooleanVar(value=False))
            new_vars[name] = var
            ttk.Checkbutton(self._inner, text=name, variable=var).pack(
                anchor="w", padx=4, pady=1
            )
        self.vars = new_vars

    def set_all(self, value):
        """Check (True) or uncheck (False) every item."""
        for var in self.vars.values():
            var.set(value)

    def get_selected(self):
        """Return the list of currently checked item names."""
        return [name for name, var in self.vars.items() if var.get()]

    # -- internal mouse-wheel handling ------------------------------------- #
    def _bind_mousewheel(self, _event):
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event):
        self._canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        self._canvas.yview_scroll(int(-event.delta / 120), "units")


class FolderTab(ttk.Frame):
    """A single notebook tab: Select-All / Deselect-All buttons + a CheckboxList.

    Each tab owns an independent CheckboxList, so actions on one tab never
    affect the other.
    """

    def __init__(self, master, items, root_path, **kwargs):
        super().__init__(master, **kwargs)

        # Folder that the displayed items live under; used to build full paths.
        self.root_path = root_path

        # Top row: per-tab selection buttons.
        button_bar = ttk.Frame(self)
        button_bar.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Button(
            button_bar, text="Select All",
            command=lambda: self.checkbox_list.set_all(True),
        ).pack(side="left", padx=(0, 4))
        ttk.Button(
            button_bar, text="Deselect All",
            command=lambda: self.checkbox_list.set_all(False),
        ).pack(side="left")

        # The visible, bordered area that contains the checkbox list.
        container = ttk.LabelFrame(self, text="Folders")
        container.pack(fill="both", expand=True, padx=4, pady=(2, 4))

        self.checkbox_list = CheckboxList(container, items)
        self.checkbox_list.pack(fill="both", expand=True, padx=2, pady=2)

    def get_selected(self):
        """Convenience pass-through to the underlying checkbox list."""
        return self.checkbox_list.get_selected()

    def get_selected_paths(self):
        """Return (name, full_path) pairs for every checked folder."""
        return [
            (name, os.path.join(self.root_path, name))
            for name in self.get_selected()
        ]


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #

class FeatureManagerApp(ttk.Frame):
    """Top-level application frame wiring the three panels together."""

    def __init__(self, master):
        super().__init__(master, padding=6)
        self.pack(fill="both", expand=True)

        # Top region holds the three side-by-side panels; the error list sits
        # full-width below it and is shared by every action.
        self._top = ttk.Frame(self)
        self._top.pack(side="top", fill="both", expand=True)

        self._build_left_panel()
        self._build_middle_panel()
        self._build_right_panel()
        self._build_error_panel()

    # -- Left: tabs with folder checkbox lists ----------------------------- #
    def _build_left_panel(self):
        left = ttk.Frame(self._top)
        left.pack(side="left", fill="both", expand=True)

        self.notebook = ttk.Notebook(left)
        self.notebook.pack(fill="both", expand=True)

        # Tabs are kept as attributes so actions can read their selections.
        self.services_tab = FolderTab(self.notebook, get_service_folders(), REPOS_ROOT)
        self.nugets_tab = FolderTab(self.notebook, get_nuget_folders(), NUGETS_ROOT)

        self.notebook.add(self.services_tab, text="Services")
        self.notebook.add(self.nugets_tab, text="Nugets")

    # -- Middle: vertical list of action buttons --------------------------- #
    def _build_middle_panel(self):
        middle = ttk.LabelFrame(self._top, text="Actions")
        middle.pack(side="left", fill="y", padx=6)

        # Register new repository actions here as (label, callback, hint).
        # The hint is shown as a hover tooltip on the button.
        actions = [
            (
                "Checkout & Pull master",
                self._action_checkout_pull_master,
                "For every selected repository: checks out the 'master' branch "
                "and pulls the latest changes from the remote.",
            ),
        ]
        for label, command, hint in actions:
            button = ttk.Button(middle, text=label, command=command)
            button.pack(fill="x", padx=6, pady=3)
            Tooltip(button, hint)

    # -- Right: live progress for the running action ----------------------- #
    def _build_right_panel(self):
        right = ttk.LabelFrame(self._top, text="Details")
        right.pack(side="left", fill="both", expand=True)

        self.progress = ProgressPanel(right)
        self.progress.pack(fill="both", expand=True, padx=4, pady=4)

    # -- Bottom: shared, full-width error list ----------------------------- #
    def _build_error_panel(self):
        errors = ttk.LabelFrame(self, text="Errors")
        errors.pack(side="bottom", fill="x")

        self.errors = ErrorList(errors)
        self.errors.pack(fill="x", expand=False, padx=4, pady=4)

    # -- Selection helpers ------------------------------------------------- #
    def _all_selected_repos(self):
        """Return (name, path) pairs for every checked repo across both tabs."""
        return (
            self.services_tab.get_selected_paths()
            + self.nugets_tab.get_selected_paths()
        )

    # -- Action handlers --------------------------------------------------- #
    def _action_checkout_pull_master(self):
        """Checkout 'master' and pull latest for all selected repositories."""
        repos = self._all_selected_repos()
        if not repos:
            return

        # Reset the shared UI areas for a fresh run.
        self.errors.clear()
        self.progress.set_repos([name for name, _ in repos])

        # Run git work off the UI thread so the window stays responsive.
        threading.Thread(
            target=self._run_checkout_pull_master, args=(repos,), daemon=True
        ).start()

    def _run_checkout_pull_master(self, repos):
        """Worker: perform checkout + pull for each repo, updating the UI live."""
        all_ok = True
        for name, path in repos:
            self.after(0, self.progress.status, name, "in-progress")

            ok, message = self._checkout_and_pull(name, path)
            if ok:
                self.after(0, self.progress.status, name, "done")
            else:
                all_ok = False
                self.after(0, self.progress.status, name, "error")
                self.after(0, self.errors.add, message)

        # Only celebrate when every repository succeeded.
        if all_ok:
            self.after(0, self.progress.show_completion,
                       "All repositories updated successfully.")

    def _checkout_and_pull(self, name, path):
        """Run checkout master + pull for one repo. Returns (ok, error_message).

        Handling of uncommitted changes:
          * On a non-master branch, local changes are committed as "savepos"
            so they are preserved before switching to master.
          * On master with local changes, the pull is unsafe, so the repo is
            skipped with an error (no commit, no checkout, no pull).
        """
        if not os.path.isdir(os.path.join(path, ".git")):
            return False, f"{name}: not a git repository"

        if git_has_changes(path):
            if git_current_branch(path) == "master":
                return False, (
                    f"{name} is already on master and has unsaved changes. "
                    f"cannot perform pull"
                )
            # Preserve work on the current branch before checking out master.
            ok, out = run_git(path, ["add", "-A"])
            if not ok:
                return False, f"{name}: {out}"
            ok, out = run_git(path, ["commit", "-m", "savepos"])
            if not ok:
                return False, f"{name}: {out}"

        ok, out = run_git(path, ["checkout", "master"])
        if not ok:
            return False, f"{name}: {out}"

        ok, out = run_git(path, ["pull"])
        if not ok:
            return False, f"{name}: {out}"

        return True, ""


def main():
    root = tk.Tk()
    root.title("Feature Manager")
    root.geometry("900x600")
    FeatureManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
