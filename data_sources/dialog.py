"""Data-source settings and main-thread-only placeholder notifications."""

import tkinter as tk
from tkinter import messagebox, ttk

import data_sources
from . import settings as source_settings
import theme
from dialogs import _center_over_parent
from widgets import Tooltip


class DataSourcesDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.withdraw()
        self.title("Data sources")
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.configure(background=theme.BG)
        theme.apply_window_icon(self)
        self.saved = False
        self.variables = {}

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        for column, text in enumerate(("Source", "Actual source")):
            ttk.Label(body, text=text, font=("", 10, "bold")).grid(
                row=0, column=column, sticky="w", padx=(0, 24), pady=(0, 8))
        for row, (source, label) in enumerate(source_settings.SOURCES.items(), 1):
            ttk.Label(body, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 24), pady=5)
            variable = tk.StringVar(value=source_settings.current()[source])
            self.variables[source] = variable
            selector = ttk.Combobox(
                body, textvariable=variable, values=source_settings.PROVIDERS,
                state="readonly", width=16,
            )
            selector.grid(row=row, column=1, sticky="ew", pady=5)
            Tooltip(selector, f"Choose the provider for {label.lower()}. "
                    "GitHub is a placeholder and will not make requests.")
        ttk.Label(
            body, text="GitHub is not implemented yet.\n"
            "Saving restarts the app to apply sources safely.",
            foreground=theme.FG_MUTED,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(16, 0))
        cancel = ttk.Button(buttons, text="Cancel", command=self.destroy)
        cancel.pack(side="left", padx=(0, 8))
        Tooltip(cancel, "Close without changing any data sources.")
        save = ttk.Button(buttons, text="Save and restart", command=self._save)
        save.pack(side="left")
        Tooltip(save, "Save these data sources and restart Feature Manager. "
                "Open monitors are restored using the selected sources.")
        self.bind("<Escape>", lambda _event: self.destroy())
        _center_over_parent(self, parent)
        self.deiconify()
        self.grab_set()

    def _save(self):
        selection = {source: variable.get()
                     for source, variable in self.variables.items()}
        try:
            source_settings.save(selection)
        except (OSError, ValueError) as error:
            messagebox.showerror("Data sources", str(error), parent=self)
            return
        self.saved = True
        self.destroy()


def edit_data_sources(parent):
    dialog = DataSourcesDialog(parent)
    parent.wait_window(dialog)
    return dialog.saved


def install_notifications(root):
    """Use a Tk timer so workers never call Tk, even to schedule callbacks."""
    def drain():
        notices = data_sources.pending_notices()
        if notices:
            labels = ", ".join(source_settings.SOURCES[source]
                               for source in notices)
            messagebox.showinfo(
                "Data source", f"not implemented yet\n\nGitHub: {labels}\n"
                "No requests were made for these sources. Select ADO in "
                "Settings > Data sources to use the existing integration.",
                parent=root,
            )
        root.after(100, drain)
    root.after(100, drain)
