// Rasterize the toolbar's Lucide icons to PNG, recoloured per action group, for
// BOTH the dark and light themes. Emits { "dark": {...}, "light": {...} } as
// _icons.json (consumed by build_icons.py, which embeds them into ../../icons.py).
//
// Usage (from tools/icongen):  npm install  &&  node gen_icons.js  &&  python build_icons.py
//
// To add/rename an icon for a NEW action:
//   1. Make sure the action's button label matches EXACTLY the (label, command,
//      hint) tuple in the tab's _sections() (workspaces_tab.py / manual_tab.py).
//   2. Pick a Lucide slug from https://lucide.dev/icons and add "slug": "Label"
//      to MAP below, then add the Label to the correct colour group in GROUP.
//   3. Run the three commands above; the toolbar picks it up by label. A product
//      logo (VS Code / Git Bash) instead of a glyph lives in ../../icons.py and
//      toolbar._SUBSAMPLE - not here.
const fs = require("fs");
const path = require("path");
const { Resvg } = require("@resvg/resvg-js");

const SIZE = 18; // px; final toolbar size (render crisp here - do NOT Tk-subsample)
const iconsDir = path.join(__dirname, "node_modules", "lucide-static", "icons");

// Group colours per theme. Light-theme tones are darker/more saturated so they
// stay legible on the light toolbar (esp. yellow -> dark gold).
const PALETTE = {
  dark: { blue: "#3d9be8", red: "#e8432e", brown: "#9c5711", green: "#2f9e44", yellow: "#f5df84", amber: "#e0a23c", gray: "#9aa1ab" },
  light: { blue: "#1f77c4", red: "#cf3a26", brown: "#8a4a0e", green: "#2c8b3f", yellow: "#b8860b", amber: "#b8791e", gray: "#5c636e" },
};

// Lucide slug -> exact action button label.
const MAP = {
  "folder-plus": "Create workspace from PBI",
  "folder-sync": "Switch to selected workspace",
  "settings-2": "Manage workspace branches",
  "history": "Restore state before switch",
  "git-graph": "Rebase current branch on master",
  "check": "Commit all changes",
  "download": "Checkout & Pull master",
  "git-branch-plus": "Create feature branch",
  "folder-git-2": "Create feature workspace and branches",
  "upload": "Git push",
  "git-pull-request": "Create pull request",
  "link": "Copy PR links",
  "git-merge": "Complete pull request",
  "package": "Bump NuGet packages (public)",
  "boxes": "Bump all NuGet packages",
  "package-check": "Restore NuGet packages",
  "play": "Run dev pipelines",
  "fast-forward": "Run acc pipelines",
  "rocket": "View merged master pipelines",
  "brush-cleaning": "Redeploy latest master commit",
  "globe": "Open repositories (master)",
  "git-branch": "Open remote branches",
  "git-pull-request-arrow": "Open pull requests",
};

// Action label -> group name (drives the colour).
const GROUP = {};
const grp = (g, labels) => labels.forEach((l) => (GROUP[l] = g));
grp("blue", [
  "Create workspace from PBI", "Switch to selected workspace",
  "Manage workspace branches", "Restore state before switch",
  "Checkout & Pull master", "Create feature branch",
  "Create feature workspace and branches",
]);
grp("red", [
  "Rebase current branch on master", "Commit all changes",
  "Git push", "Create pull request", "Copy PR links",
  "Complete pull request",
]);
grp("brown", [
  "Bump NuGet packages (public)", "Bump NuGet packages (private)",
  "Bump all NuGet packages", "Restore NuGet packages",
]);
grp("green", ["Run dev pipelines", "Run acc pipelines", "View merged master pipelines", "Redeploy latest master commit"]);
grp("yellow", ["Open repositories (master)", "Open remote branches", "Open pull requests"]);

// Non-action ("misc") icons keyed by a stable name, each with its own colour.
// Used outside the toolbar - e.g. the pipeline monitor's "previous run" marker.
const MISC = { "undo-2": "previous-run" };
const MISC_COLOR = { "previous-run": "gray" };

function wrap(inner, color) {
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" ` +
    `stroke="${color}" stroke-width="2" stroke-linecap="round" ` +
    `stroke-linejoin="round">${inner}</svg>`
  );
}

// Composite "package with a padlock badge" for the private-feed bump, in the
// lucide package-plus style (full box, cut lower-right corner, native-size lock).
function packageLockSvg(color) {
  const body =
    `<path d="M12 22V12"/>` +
    `<path d="M21 10.9V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.729l7 4a2 2 0 0 0 2 .001l1.2-.686"/>` +
    `<path d="M3.29 7 12 12l8.71-5"/>` +
    `<path d="m7.5 4.27 8.997 5.148"/>`;
  const lock =
    `<rect x="16" y="16.6" width="6" height="4" rx="1.2"/>` +
    `<path d="M17.5 16.6 V15.2 A1.5 1.5 0 0 1 20.5 15.2 V16.6"/>`;
  return wrap(body + lock, color);
}

function render(svg) {
  const png = new Resvg(svg, { fitTo: { mode: "width", value: SIZE } })
    .render()
    .asPng();
  return Buffer.from(png).toString("base64");
}

const missing = [];
for (const slug of [...Object.keys(MAP), ...Object.keys(MISC)]) {
  if (!fs.existsSync(path.join(iconsDir, slug + ".svg"))) missing.push(slug);
}
if (missing.length) {
  console.error("MISSING SLUGS: " + missing.join(", "));
  process.exit(2);
}

const out = {};
for (const theme of Object.keys(PALETTE)) {
  const colors = PALETTE[theme];
  const set = {};
  for (const [slug, label] of Object.entries(MAP)) {
    const color = colors[GROUP[label]] || "#dfe3e8";
    const raw = fs.readFileSync(path.join(iconsDir, slug + ".svg"), "utf8");
    set[label] = render(raw.replace(/currentColor/g, color));
  }
  set["Bump NuGet packages (private)"] = render(packageLockSvg(colors.brown));
  out[theme] = set;
}

// Misc icons (own colours), keyed by stable name, for both themes.
out.misc = {};
for (const theme of Object.keys(PALETTE)) {
  const colors = PALETTE[theme];
  const set = {};
  for (const [slug, key] of Object.entries(MISC)) {
    const raw = fs.readFileSync(path.join(iconsDir, slug + ".svg"), "utf8");
    set[key] = render(raw.replace(/currentColor/g, colors[MISC_COLOR[key]]));
  }
  out.misc[theme] = set;
}

fs.writeFileSync(path.join(__dirname, "_icons.json"), JSON.stringify(out));
console.log("OK generated dark+light (" + Object.keys(out.dark).length + " each)");
