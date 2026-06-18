"""Reusable Tkinter widgets for Feature Manager (no business logic)."""

import os
import tkinter as tk
from tkinter import ttk


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
    "pending":     ("\u25CB", "gray"),     # hollow circle
    "in-progress": ("\u25CF", "#d98c00"),  # filled circle, amber
    "done":        ("\u25CF", "#1a9e1a"),  # filled circle, green
    "error":       ("\u25CF", "#c0392b"),  # filled circle, red
}


class ProgressPanel(ttk.Frame):
    """A completion banner plus a live per-repo status list.

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


class WorkspaceList(ttk.Frame):
    """A scrollable, single-selection list of feature workspaces.

    *on_select*, if given, is called with the selected workspace name whenever
    the selection changes.
    """

    def __init__(self, master, items, on_select=None, **kwargs):
        super().__init__(master, **kwargs)

        self._on_select = on_select
        self.listbox = tk.Listbox(self, selectmode="browse", exportselection=False,
                                  activestyle="dotbox")
        scrollbar = ttk.Scrollbar(self, orient="vertical",
                                  command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        if on_select is not None:
            self.listbox.bind("<<ListboxSelect>>", self._handle_select)

        self.set_items(items)

    def _handle_select(self, _event=None):
        name = self.get_selected()
        if name is not None and self._on_select is not None:
            self._on_select(name)

    def set_items(self, items):
        """Replace the list contents (keeps no prior selection)."""
        self.listbox.delete(0, "end")
        for name in items:
            self.listbox.insert("end", name)

    def get_selected(self):
        """Return the selected workspace name, or None if nothing is selected."""
        selection = self.listbox.curselection()
        if not selection:
            return None
        return self.listbox.get(selection[0])
