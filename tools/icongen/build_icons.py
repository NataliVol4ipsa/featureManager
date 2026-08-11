"""Embed _icons.json into ../../icons.py as ACTION_ICON_DARK / ACTION_ICON_LIGHT.

Run after gen_icons.js. Keeps the VS Code + Git Bash product logos (read from the
current icons.py) and rewrites the two theme-specific icon dicts. Location-aware:
resolves the repo root as two levels up, so it works from tools/icongen.
"""
import json, textwrap, base64, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
import icons as cur

data = json.load(open(os.path.join(HERE, "_icons.json"), encoding="utf-8"))

# The two product logos are theme-independent; reuse from whatever the current
# icons.py exposes (dual ACTION_ICON_DARK or the legacy single ACTION_ICON_B64).
_src = getattr(cur, "ACTION_ICON_DARK", None) or cur.ACTION_ICON_B64
logos = [
    ("Open workspace in VS Code", _src["Open workspace in VS Code"]),
    ("Open in Git Bash tabs", _src["Open in Git Bash tabs"]),
]


def emit(b64):
    lines = textwrap.wrap(b64, 76)
    return "(\n" + "".join(f'        "{c}"\n' for c in lines) + "    )"


def block(name, mapping, extra=None):
    entries = list(mapping.items()) + (logos if extra is None else extra)
    for label, b64 in entries:
        assert base64.b64decode(b64)[:8] == b"\x89PNG\r\n\x1a\n", label
    body = "".join(f"    {label!r}: {emit(b64)},\n" for label, b64 in entries)
    return f"{name} = {{\n{body}}}\n"


header = '''\
"""Embedded toolbar icon assets (pure data - no Tk / GUI imports).

Each action button label maps to a base64-encoded PNG shown in the top toolbar
instead of a Unicode glyph. Kept out of toolbar.py so the (large) base64 blobs
don't clutter the UI logic. The two logos are downscaled by toolbar._image_for();
the Lucide icons are already rendered at final strip size.

There are two theme-specific sets: ACTION_ICON_DARK and ACTION_ICON_LIGHT.
toolbar._image_for() picks one via theme.load_dark_preference(). The icons are
Lucide (https://lucide.dev, ISC licence - see licenses/lucide-LICENSE.txt),
rasterised to 18px PNG, recoloured per action group (blue=workspace/branch,
red=git, brown=packages, green=pipelines, yellow=open); the light set uses
darker/more saturated tones so they read on the light toolbar. The private-feed
bump is a package+padlock composite. The VS Code and Git Bash entries are the
products' own logos (shared by both sets). MISC_ICON_DARK / MISC_ICON_LIGHT hold
non-toolbar icons (e.g. the pipeline monitor's "previous run" marker).
Regenerate via tools/icongen.
"""


'''

out = header + block("ACTION_ICON_DARK", data["dark"]) + "\n" + block("ACTION_ICON_LIGHT", data["light"])
misc = data.get("misc")
if misc:
    out += "\n" + block("MISC_ICON_DARK", misc["dark"], extra=[])
    out += "\n" + block("MISC_ICON_LIGHT", misc["light"], extra=[])
open(os.path.join(ROOT, "icons.py"), "w", encoding="utf-8").write(out)
print("wrote icons.py: dark", len(data["dark"]) + len(logos),
      "light", len(data["light"]) + len(logos))
