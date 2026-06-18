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
import tkinter as tk
from tkinter import ttk, messagebox

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


# --------------------------------------------------------------------------- #
# Reusable UI components
# --------------------------------------------------------------------------- #

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

    def __init__(self, master, items, **kwargs):
        super().__init__(master, **kwargs)

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


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #

class FeatureManagerApp(ttk.Frame):
    """Top-level application frame wiring the three panels together."""

    def __init__(self, master):
        super().__init__(master, padding=6)
        self.pack(fill="both", expand=True)

        self._build_left_panel()
        self._build_middle_panel()
        self._build_right_panel()

    # -- Left: tabs with folder checkbox lists ----------------------------- #
    def _build_left_panel(self):
        left = ttk.Frame(self)
        left.pack(side="left", fill="both", expand=True)

        self.notebook = ttk.Notebook(left)
        self.notebook.pack(fill="both", expand=True)

        # Tabs are kept as attributes so actions can read their selections.
        self.services_tab = FolderTab(self.notebook, get_service_folders())
        self.nugets_tab = FolderTab(self.notebook, get_nuget_folders())

        self.notebook.add(self.services_tab, text="Services")
        self.notebook.add(self.nugets_tab, text="Nugets")

    # -- Middle: vertical list of action buttons --------------------------- #
    def _build_middle_panel(self):
        middle = ttk.LabelFrame(self, text="Actions")
        middle.pack(side="left", fill="y", padx=6)

        # Add new repository actions here: (label, callback).
        actions = [
            ("Action 1", lambda: self._placeholder_action("Action 1")),
            ("Action 2", lambda: self._placeholder_action("Action 2")),
            ("Action 3", lambda: self._placeholder_action("Action 3")),
        ]
        for label, command in actions:
            ttk.Button(middle, text=label, command=command).pack(
                fill="x", padx=6, pady=3
            )

    # -- Right: placeholder section (to be defined later) ------------------ #
    def _build_right_panel(self):
        right = ttk.LabelFrame(self, text="Details")
        right.pack(side="left", fill="both", expand=True)

        ttk.Label(
            right, text="(reserved for future content)",
            foreground="gray",
        ).pack(padx=10, pady=10)

    # -- Action handlers --------------------------------------------------- #
    def _placeholder_action(self, name):
        """Temporary handler showing which folders are currently selected."""
        services = self.services_tab.get_selected()
        nugets = self.nugets_tab.get_selected()
        messagebox.showinfo(
            name,
            f"Services selected: {services or 'none'}\n"
            f"Nugets selected: {nugets or 'none'}",
        )


def main():
    root = tk.Tk()
    root.title("Feature Manager")
    root.geometry("900x600")
    FeatureManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
