"""Feature Manager - entry point.

Run with:  python featureManager.py

The application is split across sibling modules (no build step needed - they
are imported directly). This file only wires the two top-level tabs together:

  * "Manual"     - per-repo git actions driven by Services/Nugets checkbox lists
                   (modules: manual_tab, widgets, dialogs).
  * "Workspaces" - switch every repo of a feature workspace at once
                   (modules: workspaces_tab).

Shared building blocks live in: config, gitutils, widgets, tab_base, dialogs.
"""

import tkinter as tk
from tkinter import ttk

from manual_tab import ManualTab
from workspaces_tab import WorkspacesTab
from dialogs import edit_synonyms


class FeatureManagerApp(ttk.Notebook):
    """Top-level notebook holding the Manual and Workspaces tabs."""

    def __init__(self, master):
        super().__init__(master, padding=6)
        self.pack(fill="both", expand=True)

        self.manual_tab = ManualTab(self)
        self.workspaces_tab = WorkspacesTab(self)
        self.add(self.manual_tab, text="Manual")
        self.add(self.workspaces_tab, text="Workspaces")

        # Refresh the workspace list every time the Workspaces tab is opened, so
        # newly created/modified workspaces show up without a manual refresh.
        self.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _on_tab_changed(self, _event=None):
        if self.nametowidget(self.select()) is self.workspaces_tab:
            self.workspaces_tab.refresh()


def main():
    root = tk.Tk()
    root.title("Feature Manager")
    root.geometry("1160x740")

    # Menu bar with a Settings menu for editing the repository synonyms used by
    # the "Create workspace from PBI" action.
    menubar = tk.Menu(root)
    settings_menu = tk.Menu(menubar, tearoff=0)
    settings_menu.add_command(
        label="Repository synonyms\u2026",
        command=lambda: edit_synonyms(root),
    )
    menubar.add_cascade(label="Settings", menu=settings_menu)
    root.config(menu=menubar)

    FeatureManagerApp(root)

    # Author credit footer - always visible at the bottom of the window.
    footer = ttk.Label(
        root,
        text="Feature Manager  -  by Nataliia Kolosova",
        anchor="e",
        padding=(8, 2),
        foreground="gray40",
    )
    footer.pack(side="bottom", fill="x")

    root.mainloop()


if __name__ == "__main__":
    main()
