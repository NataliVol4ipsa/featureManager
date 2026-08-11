"""Top-of-app action toolbar strip.

Mirrors the active tab's action buttons as a compact row of icons, grouped by
the same sections and separated by thin vertical dividers. Icons are Unicode
glyphs (placeholder choices - easy to revisit) except the VS Code action, which
uses the embedded VS Code logo. Hovering an icon shows the action name; dwelling
longer adds its full description (see widgets.ActionTooltip). Colours come from
theme.* so the strip survives the dark/light restart.
"""

import tkinter as tk

import icons
import theme
from widgets import ActionTooltip


# PhotoImages kept module-level so Tk doesn't garbage-collect them; the per-label
# cache lets tab switches reuse the same image.
_images = []
_image_cache = {}

# The two product logos are raster PNGs supplied larger than strip size, so they
# get downscaled; Lucide action icons are already rendered at final size.
_SUBSAMPLE = {
    "Open in Git Bash tabs": 4,
}


def _icon_set():
    """The theme-matched icon set (dark or light), resolved once per run."""
    if theme.load_dark_preference():
        return icons.ACTION_ICON_DARK
    return icons.ACTION_ICON_LIGHT


def _image_for(label):
    """Return the toolbar-sized PNG icon for *label*, or None if it has none."""
    data = _icon_set().get(label)
    if data is None:
        return None
    if label in _image_cache:
        return _image_cache[label]
    try:
        img = tk.PhotoImage(data=data)
    except tk.TclError:
        return None
    factor = _SUBSAMPLE.get(label, 1)
    if factor > 1:
        img = img.subsample(factor, factor)
    _images.append(img)
    _image_cache[label] = img
    return img


# Action label -> placeholder Unicode glyph. Labels shared across the two tabs
# map to the same glyph. The VS Code action (see _VSCODE_LABELS) uses the logo
# image instead. Unknown labels fall back to a bullet.
GLYPHS = {
    # Local
    "Create workspace from PBI": "\U0001f195",           # NEW
    "Switch to selected workspace": "\u21c4",            # left-right arrows
    "Manage workspace branches": "\U0001f527",           # wrench (configure)
    "Restore state before switch": "\u21ba",            # anticlockwise arrow
    "Rebase current branch on master": "\u2934",        # up-right curved arrow
    "Commit all changes": "\u2714",                     # check mark
    "Checkout & Pull master": "\u2913",                 # down arrow to bar
    "Create feature branch": "\u2387",                  # alternative (branch)
    "Create feature workspace and branches": "\U0001f5c2",  # card index dividers
    # Remote
    "Git push": "\u2b06",                               # up arrow
    "Create pull request": "\U0001f4e4",                # outbox (submit request)
    "Copy PR links": "\U0001f517",                      # link
    "Complete pull request": "\U0001f500",              # twisted arrows (merge)
    # Packages
    "Bump NuGet packages (public)": "\U0001f4e6",       # package
    "Bump NuGet packages (private)": "\U0001f510",      # locked with key
    "Bump all NuGet packages": "\U0001f504",            # counterclockwise arrows
    "Restore NuGet packages": "\u267b",                 # recycle
    # Pipelines
    "Run dev pipelines": "\u25b6",                      # play
    "Run acc pipelines": "\u23e9",                      # fast-forward
    "View merged master pipelines": "\U0001f440",       # eyes
    # Open
    "Open in Git Bash tabs": "\U0001f5a5",              # desktop computer
    "Open repositories (master)": "\U0001f310",         # globe
    "Open remote branches": "\U0001f500",               # twisted arrows
    "Open pull requests": "\U0001f50d",                 # magnifier
}

# Actions rendered with a product logo image use _IMAGE_ICONS above.


def _make_icon(parent, glyph, name, description, command, image=None):
    """A single flat, hoverable icon 'button' with a two-stage tooltip.

    Uses *image* when given, otherwise the *glyph* text. The tooltip shows
    *name* on hover and adds *description* after a longer dwell.
    """
    if image is not None:
        icon = tk.Label(parent, image=image, background=theme.BG_PANEL,
                        padx=6, pady=2, cursor="hand2")
    else:
        icon = tk.Label(
            parent,
            text=glyph,
            background=theme.BG_PANEL,
            foreground=theme.FG,
            font=("Segoe UI Emoji", 11),
            padx=6,
            pady=2,
            cursor="hand2",
        )
    icon.bind("<Enter>", lambda _e: icon.config(background=theme.BG_RAISED))
    icon.bind("<Leave>", lambda _e: icon.config(background=theme.BG_PANEL))
    icon.bind("<Button-1>", lambda _e: command())
    ActionTooltip(icon, name, description)
    return icon


def _divider(parent):
    """A thin vertical separator between groups (matches the app section border)."""
    return tk.Frame(parent, width=1, background=theme.BORDER)


def build_action_toolbar(host, sections):
    """(Re)build the toolbar inside *host* from a tab's action *sections*.

    *sections* is the list of (title, actions) groups a tab passes to
    build_middle_sections; *actions* are (label, command, hint) tuples. Each
    group's icons are packed left-to-right in the section order, with a divider
    between groups. Existing children of *host* are cleared first so the strip
    can follow the active tab.
    """
    for child in host.winfo_children():
        child.destroy()
    # Small left inset so the first icon isn't flush against the window edge.
    tk.Frame(host, width=16, background=theme.BG_PANEL).pack(side="left")
    groups = [actions for _title, actions in sections if actions]
    last = len(groups) - 1
    for gi, actions in enumerate(groups):
        for label, command, hint in actions:
            image = _image_for(label)
            glyph = GLYPHS.get(label, "\u2022")
            _make_icon(host, glyph, label, hint, command,
                       image=image).pack(side="left")
        if gi != last:
            _divider(host).pack(side="left", fill="y", padx=6, pady=5)
    return host
