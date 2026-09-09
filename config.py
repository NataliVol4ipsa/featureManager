"""Configuration for Feature Manager.

Settings are read from ``config.json`` (next to this file) so the folders and
exclusions can be changed without editing code. If the file is missing or a key
is absent, the built-in defaults below are used. Edit ``config.json`` to point
the app at your local folders.
"""

import os
import json

# Built-in defaults, used when config.json is missing or a key is not set.
_DEFAULTS = {
    "repos_root": r"D:/Repositories",
    "nugets_root": r"D:/Repositories/Shared",
    "workspaces_root": r"D:/Workspaces/features",
    # Repo-relative folder holding each service's azure-pipelines.yml. Empty
    # means the repository root.
    "pipeline_yaml_location": "deployment",
    # Whole stage phrases that classify a pipeline run parameter - each entry
    # must equal the parameter's name (camelCase) or its full display phrase
    # exactly, never a substring (which would produce false positives).
    # "environment" = deployment target toggles chosen by the environment
    # selection (hidden from the run dialog); "standard" = recognised template
    # flags shown but not treated as custom. Anything that matches neither is a
    # custom flag the user is prompted to configure. List both the camelCase
    # name form and the human display phrase.
    "pipeline_parameters": {
        "environment_stages": [
            "deploy development environment", "deploydevelopment", "deploydev",
            "deploy acceptance environment", "deployacceptance", "deployacc",
            "deploy production environment", "deployproduction", "deployprod",
        ],
        "standard_stages": [
            "skip build solutions", "skipbuildsolutions",
            "force build solution", "forcebuildsolution", "forcebuild",
            "enable docker pipeline caching", "enabledockerpipelinecaching",
            "enforce veracode scan", "enforceveracodescan",
            "delete and purge obsolete secrets from keyvault", "cleankeyvault",
            "deploy infrastructure", "deployinfrastructure",
        ],
    },
    "exclusions": {
        "repos": ["shared", "wiki"],
        "nugets": [],
        "workspaces": [],
    },
}

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "config.json")


def _load_config():
    """Return the settings from config.json merged over the defaults.

    A missing file or unreadable/invalid JSON falls back to the defaults so the
    app always starts. Individual missing keys also fall back per key.
    """
    data = {}
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    exclusions = data.get("exclusions")
    if not isinstance(exclusions, dict):
        exclusions = {}

    def _excluded(kind):
        values = exclusions.get(kind, _DEFAULTS["exclusions"][kind])
        if not isinstance(values, list):
            values = _DEFAULTS["exclusions"][kind]
        # Case-insensitive matching against folder names.
        return {str(name).lower() for name in values}

    repos_root = data.get("repos_root") or _DEFAULTS["repos_root"]
    nugets_root = (
        data.get("nugets_root")
        or os.path.join(repos_root, "Shared")
    )
    workspaces_root = data.get("workspaces_root") or _DEFAULTS["workspaces_root"]
    # An explicit empty string is honoured (YAML at the repo root); only a
    # missing key falls back to the default subfolder.
    pipeline_yaml_location = data.get("pipeline_yaml_location")
    if pipeline_yaml_location is None:
        pipeline_yaml_location = _DEFAULTS["pipeline_yaml_location"]

    pipeline_params = data.get("pipeline_parameters")
    if not isinstance(pipeline_params, dict):
        pipeline_params = {}

    def _stages(kind):
        values = pipeline_params.get(kind, _DEFAULTS["pipeline_parameters"][kind])
        if not isinstance(values, list):
            values = _DEFAULTS["pipeline_parameters"][kind]
        return [str(word).lower() for word in values]

    return {
        "repos_root": repos_root,
        "nugets_root": nugets_root,
        "workspaces_root": workspaces_root,
        "pipeline_yaml_location": pipeline_yaml_location,
        "pipeline_environment_stages": _stages("environment_stages"),
        "pipeline_standard_stages": _stages("standard_stages"),
        "excluded_repos": _excluded("repos"),
        "excluded_nugets": _excluded("nugets"),
        "excluded_workspaces": _excluded("workspaces"),
    }


_settings = _load_config()

# Root folder that is scanned for repositories.
REPOS_ROOT = _settings["repos_root"]

# Sub-folder used to populate the "Nugets" tab.
NUGETS_ROOT = _settings["nugets_root"]

# Folder where generated VS Code workspace files are written / read from.
WORKSPACES_ROOT = _settings["workspaces_root"]

# Repo-relative folder that holds each service's azure-pipelines.yml (empty = root).
PIPELINE_YAML_LOCATION = _settings["pipeline_yaml_location"]

# Whole stage phrases used to recognise environment / standard pipeline run parameters.
PIPELINE_ENVIRONMENT_STAGES = _settings["pipeline_environment_stages"]
PIPELINE_STANDARD_STAGES = _settings["pipeline_standard_stages"]

# Folder names to hide from each list (case-insensitive).
EXCLUDED_FOLDERS = _settings["excluded_repos"]   # repos / "Services" tab
EXCLUDED_NUGETS = _settings["excluded_nugets"]
EXCLUDED_WORKSPACES = _settings["excluded_workspaces"]
