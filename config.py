"""Configuration constants for Feature Manager.

Adjust these paths to point at your local folders.
"""

import os

# Root folder that is scanned for repositories.
REPOS_ROOT = r"D:/Repositories"

# Sub-folder used to populate the "Nugets" tab.
NUGETS_ROOT = os.path.join(REPOS_ROOT, "Shared")

# Folder where generated VS Code workspace files are written / read from.
WORKSPACES_ROOT = r"D:/Workspaces/features"

# Folder names that must never appear in the "Services" tab (case-insensitive).
EXCLUDED_FOLDERS = {"ibs", "shared", "wiki"}
