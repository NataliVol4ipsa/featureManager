"""Reusable Tkinter widgets for Feature Manager (no business logic)."""

import os
import tkinter as tk
import webbrowser
from datetime import datetime
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


class SelectableLabel(tk.Entry):
    """A label whose text the user can select and copy.

    Implemented as a read-only ``Entry`` styled to look like a flat label (no
    border, frame-coloured background), so the text can be highlighted and
    copied while still being non-editable.
    """

    def __init__(self, master, text="", font=None, **kwargs):
        self._var = tk.StringVar(value=text)
        # Blend into the surrounding ttk.Frame background.
        background = ttk.Style().lookup("TFrame", "background") or "SystemButtonFace"
        super().__init__(
            master, textvariable=self._var, state="readonly",
            relief="flat", borderwidth=0, highlightthickness=0,
            readonlybackground=background, cursor="xterm",
            width=max(len(text) + 1, 1), **kwargs,
        )
        if font is not None:
            self.configure(font=font)

    def set_text(self, text):
        """Replace the displayed text (and resize to fit it)."""
        self._var.set(text)
        self.configure(width=max(len(text) + 1, 1))


# Visual indicators for each repository row, keyed by status.
STATUS_STYLES = {
    "pending":     ("\u25CB", "gray"),     # hollow circle
    "in-progress": ("\u25CF", "#d98c00"),  # filled circle, amber
    "done":        ("\u25CF", "#1a9e1a"),  # filled circle, green
    "error":       ("\u25CF", "#c0392b"),  # filled circle, red
    "skipped":     ("\u2013", "#888888"),  # en dash, gray (no-op / skipped)
}


class ProgressPanel(ttk.Frame):
    """A completion banner plus a per-repo table (status / name / branch).

    Call show_repos() whenever the selection changes to (re)build the table.
    Status circles are only drawn while *with_status* is set (i.e. an action is
    running); on plain selection the status column stays blank. status() updates
    a single repo's circle and set_branch() updates its branch cell live.
    """

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)

        # Green completion banner (label + optional copy link), shown above the
        # repo list and hidden by default.
        self._banner_frame = ttk.Frame(self)
        self._banner = tk.Label(self._banner_frame, text="",
                                foreground="#1a9e1a", font=("", 10, "bold"),
                                anchor="w")
        self._banner.pack(side="left")
        self._copy_button = ttk.Button(self._banner_frame, text="Copy all",
                                       command=self._do_copy)
        # The text to place on the clipboard when the copy button is clicked.
        self._copy_payload = ""
        self._open_button = ttk.Button(self._banner_frame, text="Open all",
                                       command=self._do_open)
        # The URLs opened in the browser when the open button is clicked.
        self._open_payload = []

        # Scrollable table area for repo rows.
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

        # Per repo name: the status indicator Label and the branch Label, so
        # both can be updated live during an action.
        self._indicators = {}
        self._branch_labels = {}
        self._link_labels = {}
        self._with_status = False
        self._with_link = False

    def show_repos(self, rows, with_status=False, with_link=False,
                   show_branch=True, link_header="Link"):
        """(Re)build the table from *rows* (each a (name, branch) pair).

        When *with_status* is True the status column shows a 'pending' circle
        per row; otherwise it is left blank (plain selection view). When
        *with_link* is True an extra link column (titled *link_header*) is added
        whose cells stay empty until set_link() fills them with a clickable
        link. Set *show_branch* to False to hide the Branch column (e.g. when
        creating pull requests, where only the PR link is relevant).
        """
        self.clear_completion()
        for child in self._inner.winfo_children():
            child.destroy()
        self._indicators = {}
        self._branch_labels = {}
        self._link_labels = {}
        self._with_status = with_status
        self._with_link = with_link

        # Assign column indices dynamically so hidden columns leave no gap.
        branch_col = 2 if show_branch else None
        link_col = (3 if show_branch else 2) if with_link else None

        # Let the visible name and trailing columns share the spare width.
        self._inner.columnconfigure(1, weight=1)
        if branch_col is not None:
            self._inner.columnconfigure(branch_col, weight=1)
        if link_col is not None:
            self._inner.columnconfigure(link_col, weight=1)

        # Header row.
        ttk.Label(self._inner, text="", width=2).grid(
            row=0, column=0, padx=(4, 2), pady=(2, 4)
        )
        ttk.Label(self._inner, text="Repository", font=("", 9, "bold")).grid(
            row=0, column=1, sticky="w", padx=4, pady=(2, 4)
        )
        if branch_col is not None:
            ttk.Label(self._inner, text="Branch", font=("", 9, "bold")).grid(
                row=0, column=branch_col, sticky="w", padx=4, pady=(2, 4)
            )
        if link_col is not None:
            ttk.Label(self._inner, text=link_header, font=("", 9, "bold")).grid(
                row=0, column=link_col, sticky="w", padx=4, pady=(2, 4)
            )

        for index, (name, branch) in enumerate(rows, start=1):
            symbol, color = STATUS_STYLES["pending"]
            indicator = tk.Label(
                self._inner, text=(symbol if with_status else ""),
                foreground=color, width=2,
            )
            indicator.grid(row=index, column=0, padx=(4, 2), pady=1)
            SelectableLabel(self._inner, text=name).grid(
                row=index, column=1, sticky="w", padx=4, pady=1
            )
            self._indicators[name] = indicator
            if branch_col is not None:
                branch_label = SelectableLabel(self._inner, text=branch)
                branch_label.grid(
                    row=index, column=branch_col, sticky="w", padx=4, pady=1
                )
                self._branch_labels[name] = branch_label
            if link_col is not None:
                # Empty placeholder cell; set_link() turns it into a link.
                link_label = tk.Label(self._inner, text="")
                link_label.grid(
                    row=index, column=link_col, sticky="w", padx=4, pady=1
                )
                self._link_labels[name] = link_label

    # Backwards-compatible alias: a plain name list with no branch/status.
    def set_repos(self, names):
        """Build a status-less table from bare repo *names* (no branch info)."""
        self.show_repos([(name, "") for name in names], with_status=False)

    def status(self, name, state, tooltip=None):
        """Update a single repo row to the given state (see STATUS_STYLES).

        If *tooltip* is given, attach a hover tooltip to the status indicator
        explaining the state (used e.g. to show why a repo was skipped).
        """
        indicator = self._indicators.get(name)
        if indicator is None:
            return
        symbol, color = STATUS_STYLES.get(state, STATUS_STYLES["pending"])
        indicator.config(text=symbol, foreground=color)
        if tooltip:
            # Replace any previous tooltip on the indicator so the reason stays
            # accurate if the status changes across a batch.
            existing = getattr(indicator, "_status_tip", None)
            if existing is not None:
                existing.text = tooltip
            else:
                indicator._status_tip = Tooltip(indicator, tooltip)

    def set_branch(self, name, branch):
        """Update a single repo's branch cell (e.g. after a checkout changes it)."""
        label = self._branch_labels.get(name)
        if label is not None:
            label.set_text(branch)

    def set_link(self, name, url, text="View branch"):
        """Turn a repo's Link cell into a clickable link that opens *url*.

        Only works when the table was built with with_link=True. A falsy *url*
        leaves the cell blank (e.g. when no remote URL could be derived).
        """
        label = self._link_labels.get(name)
        if label is None or not url:
            return
        label.config(
            text=text, foreground="#0a6cff", cursor="hand2",
            font=("", 9, "underline"),
        )
        # Rebind to the latest URL (avoid stacking handlers if called twice).
        label.unbind("<Button-1>")
        label.bind("<Button-1>", lambda _e, u=url: webbrowser.open(u))

    def show_completion(self, text, copy_text=None, open_urls=None,
                        open_label="Open all"):
        """Reveal the green completion banner above the repo list.

        When *copy_text* is given, a "Copy all" button is shown next to the
        banner that copies that text to the clipboard (used to copy every repo's
        PR link as "repo name - pr link" lines). When *open_urls* is given, an
        *open_label* button is shown that opens every URL in the browser (used to
        open every started pipeline run).
        """
        self._banner.config(text=text)
        if copy_text:
            self._copy_payload = copy_text
            self._copy_button.config(text="Copy all")
            self._copy_button.pack(side="left", padx=(10, 0))
        else:
            self._copy_payload = ""
            self._copy_button.pack_forget()
        if open_urls:
            self._open_payload = list(open_urls)
            self._open_button.config(text=open_label)
            self._open_button.pack(side="left", padx=(10, 0))
        else:
            self._open_payload = []
            self._open_button.pack_forget()
        self._banner_frame.pack(fill="x", padx=6, pady=(4, 6), before=self._canvas)

    def _do_copy(self, _event=None):
        """Copy the stored payload to the clipboard and confirm on the button."""
        self.clipboard_clear()
        self.clipboard_append(self._copy_payload)
        self._copy_button.config(text="Copied!")

    def _do_open(self, _event=None):
        """Open every stored URL in the default web browser."""
        for url in self._open_payload:
            if url:
                webbrowser.open(url)

    def clear_completion(self):
        """Hide the completion banner (e.g. when a new action starts)."""
        self._banner.config(text="")
        self._copy_payload = ""
        self._copy_button.pack_forget()
        self._open_payload = []
        self._open_button.pack_forget()
        self._banner_frame.pack_forget()


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
        """Append an error, prefixing each line with the current time (HH:MM:SS)."""
        stamp = datetime.now().strftime("%H:%M:%S")
        self._text.config(state="normal")
        # Multi-line messages (e.g. git output) get a timestamp on every row.
        for line in message.rstrip().splitlines():
            self._text.insert("end", f"[{stamp}] {line}\n")
        self._text.config(state="disabled")
        self._text.see("end")


class CheckboxList(ttk.Frame):
    """A scrollable list of checkboxes.

    Each item keeps its own tk.BooleanVar so selection state survives even when
    the widget is hidden (e.g. while another notebook tab is shown).
    """

    def __init__(self, master, items, on_change=None, **kwargs):
        super().__init__(master, **kwargs)

        # Called (no args) whenever the set of checked items changes.
        self._on_change = on_change

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
            ttk.Checkbutton(
                self._inner, text=name, variable=var, command=self._notify,
            ).pack(anchor="w", padx=4, pady=1)
        self.vars = new_vars

    def set_all(self, value):
        """Check (True) or uncheck (False) every item."""
        for var in self.vars.values():
            var.set(value)
        self._notify()

    def _notify(self):
        """Fire the on_change callback after a selection change."""
        if self._on_change is not None:
            self._on_change()

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

    def __init__(self, master, items, root_path, on_change=None, **kwargs):
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

        self.checkbox_list = CheckboxList(container, items, on_change=on_change)
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
    """A single-selection table of feature workspaces (Name / Created / Modified).

    *on_select*, if given, is called with the selected workspace name whenever
    the selection changes.
    """

    # Column ids and their header text / display width (pixels).
    _COLUMNS = (
        ("name", "Name", 220),
        ("created", "Created", 130),
        ("modified", "Modified", 130),
    )

    def __init__(self, master, items, on_select=None, **kwargs):
        super().__init__(master, **kwargs)

        self._on_select = on_select
        # Maps tree row id -> workspace name, so selection returns the name.
        self._names = {}

        column_ids = [col_id for col_id, _, _ in self._COLUMNS]
        self.tree = ttk.Treeview(
            self, columns=column_ids, show="headings", selectmode="browse"
        )
        for col_id, heading, width in self._COLUMNS:
            self.tree.heading(col_id, text=heading)
            # 'name' stretches to fill spare width; the date columns stay fixed.
            self.tree.column(
                col_id, width=width, anchor="w",
                stretch=(col_id == "name"),
            )

        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        if on_select is not None:
            self.tree.bind("<<TreeviewSelect>>", self._handle_select)

        # Treeview cells are not natively selectable; allow Ctrl+C (and a
        # right-click) to copy the selected workspace name to the clipboard.
        self.tree.bind("<Control-c>", self._copy_selected)
        self.tree.bind("<Button-3>", self._copy_selected)

        self.set_items(items)

    def _copy_selected(self, _event=None):
        """Copy the selected workspace name to the clipboard."""
        name = self.get_selected()
        if name:
            self.clipboard_clear()
            self.clipboard_append(name)
        return "break"

    def _handle_select(self, _event=None):
        name = self.get_selected()
        if name is not None and self._on_select is not None:
            self._on_select(name)

    def set_items(self, items):
        """Replace the table contents (keeps no prior selection).

        Each item may be a plain name, or a (name, created, modified) tuple of
        strings for the three columns. get_selected() always returns the name.
        """
        self.tree.delete(*self.tree.get_children())
        self._names = {}
        for item in items:
            if isinstance(item, (tuple, list)):
                name = item[0]
                values = (item[0], *item[1:])
            else:
                name = item
                values = (item, "", "")
            row_id = self.tree.insert("", "end", values=values)
            self._names[row_id] = name

    def get_selected(self):
        """Return the selected workspace name, or None if nothing is selected."""
        selection = self.tree.selection()
        if not selection:
            return None
        return self._names.get(selection[0])
