"""Theme for Feature Manager - a dark anthracite/dark-blue look and a light one.

Call :func:`apply_theme(root)` exactly once, right after creating the Tk root
and BEFORE any widgets are built. It configures two things:

  * the **ttk styles** used by every ``ttk.*`` widget, and
  * the **Tk option database** defaults used by the classic ``tk.*`` widgets
    (``tk.Label``, ``tk.Frame``, ``tk.Button``, ``tk.Entry``, ``tk.Text`` ...),
    which do NOT inherit ttk styles and must be coloured separately.

Which palette is used is a persisted user preference (see
:func:`load_dark_preference` / :func:`save_dark_preference`). The active palette
is exposed as module-level colour names (``theme.BG``, ``theme.FG`` ...) so the
rest of the app can reference them; :func:`apply_theme` swaps them in.
"""

from tkinter import ttk

import ctypes
import json
import os
import sys


# -- Palettes ------------------------------------------------------------- #
# Anthracite (dark charcoal) with dark-blue accents.
DARK = {
    "BG":           "#22272e",  # anthracite - window / frame background
    "BG_PANEL":     "#1b1f25",  # darker - menus, panels, notebook strip
    "BG_RAISED":    "#2c323c",  # raised - buttons, hovered / selected rows
    "BG_INPUT":     "#191d23",  # entry / text / list background
    "ACCENT":       "#3d5a80",  # dark blue - active tab, pressed button
    "ACCENT_HOVER": "#4a6fa5",  # dark blue (lighter) - hover
    "SELECT":       "#2f4562",  # selection highlight (dark blue)
    "FG":           "#dfe3e8",  # primary text
    "FG_MUTED":     "#9aa1ab",  # secondary / hint text
    "BORDER":       "#3a414c",  # separators, entry borders
    "LINK":         "#5a9bd8",  # hyperlinks
    "ERROR":        "#e06c6c",  # errors (lightened for a dark background)
    "SUCCESS":      "#5cba5c",  # done / mapped (green)
    "WARNING":      "#e0a23c",  # in-progress / warnings (amber)
    "TOOLTIP_BG":   "#2c323c",  # tooltip background
    "TOOLTIP_FG":   "#dfe3e8",  # tooltip text
}

# Standard light look (used when the dark preference is off).
LIGHT = {
    "BG":           "#f3f4f6",
    "BG_PANEL":     "#e6e8ec",
    "BG_RAISED":    "#d7dadf",
    "BG_INPUT":     "#ffffff",
    "ACCENT":       "#cfe0f5",  # light blue - hovered menu rows / selection
    "ACCENT_HOVER": "#b9d3f0",
    "SELECT":       "#cfe0f5",
    "FG":           "#1e2226",
    "FG_MUTED":     "#5c636e",
    "BORDER":       "#c2c6cc",
    "LINK":         "#0a6cff",
    "ERROR":        "#c0392b",
    "SUCCESS":      "#1a9e1a",
    "WARNING":      "#d98c00",
    "TOOLTIP_BG":   "#ffffe0",
    "TOOLTIP_FG":   "#1e2226",
}

# Active palette colours, exposed as module attributes. Default to DARK so any
# import-time reference resolves; apply_theme() swaps in the chosen palette.
globals().update(DARK)


_PREFS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "ui_prefs.json")

# Pipeline monitor polling interval (seconds).
PIPELINE_POLL_MIN_SECONDS = 10
PIPELINE_POLL_MAX_SECONDS = 10000
PIPELINE_POLL_DEFAULT_SECONDS = 30


def _load_prefs():
    """Return persisted UI preferences, or {} when the file is unavailable."""
    try:
        with open(_PREFS_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_prefs(data):
    """Persist UI preferences; write failures are ignored."""
    try:
        with open(_PREFS_PATH, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=4)
    except OSError:
        pass


def load_dark_preference():
    """Return the persisted "use dark theme" preference (defaults to True)."""
    return bool(_load_prefs().get("dark_theme", True))


def save_dark_preference(dark):
    """Persist the "use dark theme" preference to the local prefs file."""
    data = _load_prefs()
    data["dark_theme"] = bool(dark)
    _save_prefs(data)


def load_pipeline_poll_seconds():
    """Return pipeline monitor polling interval in seconds.

    Invalid or missing values fall back to the default.
    """
    value = _load_prefs().get("pipeline_poll_seconds", PIPELINE_POLL_DEFAULT_SECONDS)
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return PIPELINE_POLL_DEFAULT_SECONDS
    if seconds < PIPELINE_POLL_MIN_SECONDS or seconds > PIPELINE_POLL_MAX_SECONDS:
        return PIPELINE_POLL_DEFAULT_SECONDS
    return seconds


def save_pipeline_poll_seconds(seconds):
    """Persist pipeline monitor polling interval (clamped to valid bounds)."""
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        value = PIPELINE_POLL_DEFAULT_SECONDS
    value = max(PIPELINE_POLL_MIN_SECONDS, min(PIPELINE_POLL_MAX_SECONDS, value))
    data = _load_prefs()
    data["pipeline_poll_seconds"] = value
    _save_prefs(data)


def apply_theme(root, dark=None):
    """Apply the chosen palette to *root* and every widget created under it.

    *dark* selects the palette; when None the persisted preference is used.
    Returns the resolved boolean so callers know which theme is active.
    """
    if dark is None:
        dark = load_dark_preference()
    globals().update(DARK if dark else LIGHT)

    style = ttk.Style(root)
    root.configure(background=BG)

    if not dark:
        # Native light look: fall back to the platform theme; the standard
        # widget colours are already light, so no heavy overrides are needed.
        for name in ("vista", "winnative", "clam"):
            if name in style.theme_names():
                style.theme_use(name)
                break
        _set_titlebar(root, dark=False)
        return dark

    # "clam" is the most customisable built-in ttk theme on every platform.
    style.theme_use("clam")

    # -- ttk widgets ------------------------------------------------------ #
    style.configure(
        ".",
        background=BG, foreground=FG,
        fieldbackground=BG_INPUT, bordercolor=BORDER,
        lightcolor=BG, darkcolor=BG, troughcolor=BG_PANEL,
        focuscolor=ACCENT, insertcolor=FG, arrowcolor=FG,
    )
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("TLabelframe", background=BG, bordercolor=BORDER)
    style.configure("TLabelframe.Label", background=BG, foreground=FG_MUTED)

    style.configure("TButton", background=BG_RAISED, foreground=FG,
                    bordercolor=BORDER, focuscolor=BG_RAISED, padding=4)
    style.map(
        "TButton",
        background=[("pressed", ACCENT), ("active", ACCENT_HOVER),
                    ("disabled", BG_PANEL)],
        foreground=[("disabled", FG_MUTED)],
        bordercolor=[("focus", ACCENT_HOVER)],
    )

    style.configure("TCheckbutton", background=BG, foreground=FG)
    style.map(
        "TCheckbutton",
        background=[("active", BG)],
        indicatorcolor=[("selected", ACCENT_HOVER), ("!selected", BG_INPUT)],
        foreground=[("disabled", FG_MUTED)],
    )
    style.configure("TRadiobutton", background=BG, foreground=FG)
    style.map(
        "TRadiobutton",
        background=[("active", BG)],
        indicatorcolor=[("selected", ACCENT_HOVER), ("!selected", BG_INPUT)],
    )

    style.configure("TEntry", fieldbackground=BG_INPUT, foreground=FG,
                    bordercolor=BORDER, insertcolor=FG)
    style.map("TEntry", fieldbackground=[("readonly", BG)],
              bordercolor=[("focus", ACCENT_HOVER)])

    style.configure("TCombobox", fieldbackground=BG_INPUT, foreground=FG,
                    background=BG_RAISED, bordercolor=BORDER, arrowcolor=FG)
    style.map("TCombobox",
              fieldbackground=[("readonly", BG_INPUT)],
              foreground=[("disabled", FG_MUTED)])

    style.configure("TNotebook", background=BG, bordercolor=BG,
                    borderwidth=0, lightcolor=BG, darkcolor=BG)
    style.configure("TNotebook.Tab", background=BG_PANEL, foreground=FG_MUTED,
                    padding=(12, 6), bordercolor=BORDER,
                    lightcolor=BG_PANEL, darkcolor=BG_PANEL)
    style.map(
        "TNotebook.Tab",
        background=[("selected", ACCENT), ("active", BG_RAISED)],
        foreground=[("selected", FG)],
        lightcolor=[("selected", ACCENT)],
        bordercolor=[("selected", ACCENT)],
    )

    style.configure("TScrollbar", background=BG_RAISED, troughcolor=BG_PANEL,
                    bordercolor=BORDER, arrowcolor=FG_MUTED)
    style.map("TScrollbar", background=[("active", ACCENT_HOVER)])

    style.configure("TSeparator", background=BORDER)
    style.configure("TProgressbar", background=ACCENT_HOVER,
                    troughcolor=BG_PANEL, bordercolor=BORDER)

    style.configure("Treeview", background=BG_INPUT, fieldbackground=BG_INPUT,
                    foreground=FG, bordercolor=BORDER, borderwidth=1,
                    relief="flat", focuscolor=BG_INPUT)
    style.map(
        "Treeview",
        background=[("selected", SELECT)],
        foreground=[("selected", FG)],
        # Keep the border colour constant so the blue focus ring doesn't appear.
        bordercolor=[("focus", BORDER), ("!focus", BORDER)],
        lightcolor=[("focus", BG_INPUT)],
        darkcolor=[("focus", BG_INPUT)],
    )
    style.configure("Treeview.Heading", background=BG_PANEL, foreground=FG_MUTED)

    # -- classic tk widgets (via the option database) --------------------- #
    # These defaults apply to any tk.* widget that does not pass an explicit
    # colour of its own. Explicit widget options always win over these.
    opts = {
        "*Frame.background": BG,
        "*Toplevel.background": BG,
        "*Label.background": BG,
        "*Label.foreground": FG,
        "*Button.background": BG_RAISED,
        "*Button.foreground": FG,
        "*Button.activeBackground": ACCENT_HOVER,
        "*Button.activeForeground": FG,
        "*Button.highlightBackground": BG,
        "*Button.borderWidth": 1,
        "*Checkbutton.background": BG,
        "*Checkbutton.foreground": FG,
        "*Checkbutton.activeBackground": BG,
        "*Checkbutton.activeForeground": FG,
        "*Checkbutton.selectColor": BG_INPUT,
        "*Radiobutton.background": BG,
        "*Radiobutton.foreground": FG,
        "*Radiobutton.activeBackground": BG,
        "*Radiobutton.activeForeground": FG,
        "*Radiobutton.selectColor": BG_INPUT,
        "*Entry.background": BG_INPUT,
        "*Entry.foreground": FG,
        "*Entry.insertBackground": FG,
        "*Entry.readonlyBackground": BG,
        "*Text.background": BG_INPUT,
        "*Text.foreground": FG,
        "*Text.insertBackground": FG,
        "*Text.selectBackground": SELECT,
        "*Listbox.background": BG_INPUT,
        "*Listbox.foreground": FG,
        "*Listbox.selectBackground": SELECT,
        "*Listbox.selectForeground": FG,
        "*Canvas.background": BG,
        "*Canvas.highlightBackground": BG,
        "*Menu.background": BG_PANEL,
        "*Menu.foreground": FG,
        "*Menu.activeBackground": ACCENT,
        "*Menu.activeForeground": FG,
        "*Menu.selectColor": FG,
        "*Menu.relief": "flat",
        "*Menu.borderWidth": 0,
        "*Menu.activeBorderWidth": 0,
    }
    for pattern, value in opts.items():
        root.option_add(pattern, value)

    _set_titlebar(root, dark=True)
    return dark


def _set_titlebar(window, dark):
    """Set *window*'s native Windows title bar to dark or light (no-op else).

    Uses the DWM ``USE_IMMERSIVE_DARK_MODE`` window attribute so the OS-drawn
    title bar matches the app (as File Explorer does). Attribute id 20 is the
    modern value; 19 is the pre-20H1 fallback. Silently ignored on failure.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            ) == 0:
                break
        # Nudge the frame so the title bar repaints immediately.
        ctypes.windll.user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020
        )
    except Exception:
        pass


def enable_dark_titlebar(window, dark=None):
    """Apply the current (or given) title-bar theme to a secondary *window*."""
    if dark is None:
        dark = load_dark_preference()
    _set_titlebar(window, dark)
