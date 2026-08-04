"""Offline smoke test for the tab wiring (no git / network / dialogs).

Run with:  python smoke_test.py   (exit code 0 = pass, 1 = fail)

Builds both real tabs and checks:
  * both tabs expose the same five action groups (Local / Remote / Packages /
    Pipelines / Open), each a well-formed (label, callable, hint) list;
  * the Repositories tab gained the ported actions and did NOT inherit the
    workspace-only ones; and
  * every ported Repositories action delegates to the shared ActionTabBase
    method with the selected repositories (heavy base methods are stubbed, so
    no git, network, browser, terminal or dialog side effects occur).

This exercises the wiring end-to-end without touching the disk repos or ADO.
"""

import sys
import os
import shutil
import stat
import subprocess
import tempfile
import tkinter as tk

import gitutils
import manual_tab
import packages
import pbi
import theme
from tab_base import ActionTabBase
from manual_tab import ManualTab
from workspaces_tab import WorkspacesTab


EXPECTED_GROUPS = ["Local", "Remote", "Packages", "Pipelines", "Open"]

# Actions that must NOT appear on the Repositories tab (workspace-only).
FORBIDDEN_REPO_LABELS = {
    "Create workspace from PBI",
    "Switch to selected workspace",
    "Manage workspace branches",
    "Restore state before switch",
    "Open workspace in VS Code",
}

# Actions that must have been ported onto the Repositories tab.
REQUIRED_REPO_LABELS = {
    "Copy PR links",
    "Bump NuGet packages (public)",
    "Bump NuGet packages (private)",
    "Bump all NuGet packages",
    "Run dev pipelines",
    "Run acc pipelines",
    "View merged master pipelines",
    "Open repositories (master)",
    "Open remote branches",
    "Open pull requests",
}

# Shared helpers that must live on the base class after the refactor.
BASE_SHARED_METHODS = [
    "bump_packages", "run_pipelines", "open_repos_master", "open_branches",
    "open_prs", "show_master_pipeline_monitor_for_merged_prs", "copy_pr_links",
    "open_terminals", "push_all", "create_prs", "commit_all_changes",
]

FAKE_REPOS = [("RepoA", "C:/fake/RepoA"), ("RepoB", "C:/fake/RepoB")]

_failures = []


def check(condition, message):
    """Record a failure (with *message*) when *condition* is falsy."""
    if not condition:
        _failures.append(message)


def _validate_sections(tab, tab_name):
    """Assert a tab's _sections() is well-formed and grouped as expected."""
    sections = tab._sections()
    titles = [title for title, _ in sections]
    check(titles == EXPECTED_GROUPS,
          f"{tab_name}: groups {titles} != {EXPECTED_GROUPS}")

    labels = set()
    for title, actions in sections:
        check(len(actions) > 0, f"{tab_name}/{title}: empty group")
        for entry in actions:
            check(isinstance(entry, tuple) and len(entry) == 3,
                  f"{tab_name}/{title}: malformed action entry {entry!r}")
            label, command, hint = entry
            check(isinstance(label, str) and label,
                  f"{tab_name}/{title}: bad label {label!r}")
            check(callable(command),
                  f"{tab_name}/{title}/{label}: command not callable")
            check(isinstance(hint, str) and hint.strip(),
                  f"{tab_name}/{title}/{label}: missing hint")
            labels.add(label)
    return labels


def _check_base_methods():
    for name in BASE_SHARED_METHODS:
        check(callable(getattr(ActionTabBase, name, None)),
              f"ActionTabBase is missing shared method {name!r}")


def _check_repo_delegation(repo_tab):
    """Stub the base methods and confirm each ported action delegates to them."""
    calls = []

    def recorder(name):
        return lambda *args, **kwargs: calls.append((name, args, kwargs))

    # Stub every heavy base method on the instance so nothing external runs.
    for name in ("bump_packages", "run_pipelines", "copy_pr_links",
                 "open_repos_master", "open_branches", "open_prs",
                 "show_master_pipeline_monitor_for_merged_prs",
                 "open_terminals", "push_all", "create_prs",
                 "commit_all_changes"):
        setattr(repo_tab, name, recorder(name))

    # Avoid a real `git` subprocess in the pipeline wrapper.
    original_branch = manual_tab.git_current_branch
    manual_tab.git_current_branch = lambda _path: "feature/x"

    # Pretend two repos are selected.
    repo_tab._all_selected_repos = lambda: list(FAKE_REPOS)

    try:
        def last():
            return calls[-1] if calls else (None, None, None)

        repo_tab._action_copy_pr_links()
        name, args, _ = last()
        check(name == "copy_pr_links" and args == (FAKE_REPOS,),
              f"Copy PR links did not delegate correctly: {last()!r}")

        repo_tab._action_bump_public()
        name, args, _ = last()
        check(name == "bump_packages"
              and args == (FAKE_REPOS, True, False, "public feed"),
              f"Bump public did not delegate correctly: {last()!r}")

        repo_tab._action_bump_private()
        name, args, _ = last()
        check(name == "bump_packages"
              and args == (FAKE_REPOS, False, True, "private feed"),
              f"Bump private did not delegate correctly: {last()!r}")

        repo_tab._action_bump_all()
        name, args, _ = last()
        check(name == "bump_packages"
              and args == (FAKE_REPOS, True, True, "all feeds"),
              f"Bump all did not delegate correctly: {last()!r}")

        repo_tab._action_run_dev_pipeline()
        name, args, _ = last()
        expected_active = [(n, p, "feature/x") for n, p in FAKE_REPOS]
        check(name == "run_pipelines" and args == (expected_active, "dev"),
              f"Run dev pipelines did not delegate correctly: {last()!r}")

        repo_tab._action_run_acc_pipeline()
        name, args, _ = last()
        check(name == "run_pipelines" and args == (expected_active, "acc"),
              f"Run acc pipelines did not delegate correctly: {last()!r}")

        repo_tab._action_open_repos_master()
        name, args, _ = last()
        check(name == "open_repos_master" and args == (FAKE_REPOS,),
              f"Open repositories (master) did not delegate correctly: {last()!r}")

        repo_tab._action_open_branches()
        name, args, _ = last()
        check(name == "open_branches" and args == (FAKE_REPOS,),
              f"Open remote branches did not delegate correctly: {last()!r}")

        repo_tab._action_open_prs()
        name, args, _ = last()
        check(name == "open_prs" and args == (FAKE_REPOS,),
              f"Open pull requests did not delegate correctly: {last()!r}")

        repo_tab._action_show_master_pipelines_merged_pr()
        name, args, _ = last()
        check(name == "show_master_pipeline_monitor_for_merged_prs"
              and args == (expected_active,),
              "View merged master pipelines did not delegate "
              f"correctly: {last()!r}")

        repo_tab._action_open_terminals()
        name, args, _ = last()
        check(name == "open_terminals" and args == (FAKE_REPOS,),
              f"Open in Git Bash tabs did not delegate correctly: {last()!r}")

        # Empty selection must short-circuit before any base call.
        calls.clear()
        repo_tab._all_selected_repos = lambda: []
        repo_tab._action_bump_all()
        repo_tab._action_run_dev_pipeline()
        check(not calls,
              f"Empty selection should not delegate, but got: {calls!r}")
    finally:
        manual_tab.git_current_branch = original_branch


def _check_pure_logic():
    """Assert the core pure functions (parsing / validation) behave correctly."""
    # -- gitutils.is_valid_branch_name --
    for good in ("feature/123_x", "feature/my-branch", "a_b.c"):
        check(gitutils.is_valid_branch_name(good), f"should be valid: {good!r}")
    for bad in ("", "has space", "/leading", "trailing/", "a..b", "ends."):
        check(not gitutils.is_valid_branch_name(bad), f"should be invalid: {bad!r}")

    # -- gitutils.default_workspace_branch --
    check(gitutils.default_workspace_branch("foo") == "feature/foo",
          "default_workspace_branch('foo') != 'feature/foo'")

    # -- gitutils.ado_pr_title_from_branch --
    check(gitutils.ado_pr_title_from_branch("feature/123_my_description")
          == "feature(123) My description",
          "PR title from 'feature/123_my_description' is wrong")
    check(gitutils.ado_pr_title_from_branch("refs/heads/feature/514231_add_new_thing")
          == "feature(514231) Add new thing",
          "PR title from a refs/heads branch is wrong")

    # -- gitutils.ado_work_item_id_from_branch --
    check(gitutils.ado_work_item_id_from_branch("feature/514231_x") == "514231",
          "work item id extraction failed")
    check(gitutils.ado_work_item_id_from_branch("feature/no_number") == "",
          "work item id should be empty when no leading number")

    # -- gitutils.parse_ado_remote (HTTPS, SSH, .git-stripped, URL-decoded) --
    check(gitutils.parse_ado_remote(
        "https://dev.azure.com/org/My%20Project/_git/repo.git")
        == ("org", "My Project", "repo", "dev.azure.com"),
        "parse_ado_remote HTTPS dev.azure.com form failed")
    check(gitutils.parse_ado_remote("git@ssh.dev.azure.com:v3/org/proj/repo")
          == ("org", "proj", "repo", "dev.azure.com"),
          "parse_ado_remote SSH form failed")

    # -- packages._is_newer (zero-padded numeric compare) --
    check(packages._is_newer("1.3", "1.2.9"), "1.3 should be newer than 1.2.9")
    check(packages._is_newer("2.0.0", "1.9.9"), "2.0.0 should be newer than 1.9.9")
    check(not packages._is_newer("1.2.0", "1.2"), "1.2.0 should equal 1.2")
    check(not packages._is_newer("1.2", "1.10"), "1.2 should be older than 1.10")

    # -- pbi.slugify_title --
    check(pbi.slugify_title("My Title") == "my_title", "slugify 'My Title' failed")
    check(pbi.slugify_title("Bug | Fix This!") == "fix_this",
          "slugify should keep only the part after '|'")

    # -- pbi._clean_service (markdown / bullets / trailing parens stripped) --
    check(pbi._clean_service("~~XAPI~~") == "XAPI", "_clean_service strike failed")
    check(pbi._clean_service("**Algo config**") == "Algo config",
          "_clean_service bold failed")
    check(pbi._clean_service("Algo config (service)") == "Algo config",
          "_clean_service trailing parenthetical failed")

    # -- pbi.parse_wbs_services (markdown WBS list, mixed markup, section break) --
    description = (
        "Some intro text\n"
        "WBS:\n"
        "* ServiceA\n"
        "* ~~ServiceB~~\n"
        "Testing:\n"
        "* not part of the WBS\n"
    )
    check(pbi.parse_wbs_services(description) == ["ServiceA", "ServiceB"],
          f"parse_wbs_services returned {pbi.parse_wbs_services(description)!r}")

    # -- pbi.map_service (direct match, synonym match, unknown) --
    synonyms = {"ExperienceApi": ["xapi", "experience api"]}
    folders = ["ExperienceApi", "Other"]
    check(pbi.map_service("ExperienceApi", synonyms, folders) == "ExperienceApi",
          "map_service direct match failed")
    check(pbi.map_service("xapi", synonyms, folders) == "ExperienceApi",
          "map_service synonym match failed")
    check(pbi.map_service("totally unknown", synonyms, folders) is None,
          "map_service should return None for an unknown service")

    # -- theme dark-preference round-trip (against a temp file, not ui_prefs.json) --
    original_path = theme._PREFS_PATH
    tmp_path = os.path.join(tempfile.gettempdir(), "fm_ui_prefs_smoke.json")
    theme._PREFS_PATH = tmp_path
    try:
        theme.save_dark_preference(False)
        check(theme.load_dark_preference() is False,
              "theme preference did not round-trip False")
        theme.save_dark_preference(True)
        check(theme.load_dark_preference() is True,
              "theme preference did not round-trip True")
    finally:
        theme._PREFS_PATH = original_path
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# --------------------------------------------------------------------------- #
# Real git-backed integration checks (local bare origin, no network)
# --------------------------------------------------------------------------- #

def _run(cmd, cwd=None):
    """Run *cmd*, returning (returncode, combined output). Never raises."""
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _git_available():
    try:
        return _run(["git", "--version"])[0] == 0
    except OSError:
        return False


def _rmtree(path):
    """Remove a tree, clearing the read-only bit git sets on pack files."""
    def _onerror(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    shutil.rmtree(path, onerror=_onerror)


def _init_repo(base, name):
    """Create a working repo (with a local bare origin) holding one commit."""
    work = os.path.join(base, name)
    origin = os.path.join(base, name + ".git")
    check(_run(["git", "-c", "init.defaultBranch=master", "init", "--bare",
                origin])[0] == 0, f"{name}: bare init failed")
    check(_run(["git", "clone", origin, work])[0] == 0, f"{name}: clone failed")
    _run(["git", "config", "user.email", "smoke@test"], cwd=work)
    _run(["git", "config", "user.name", "Smoke Test"], cwd=work)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=work)
    with open(os.path.join(work, "README"), "w", encoding="utf-8") as handle:
        handle.write("init\n")
    _run(["git", "add", "-A"], cwd=work)
    _run(["git", "commit", "-m", "init"], cwd=work)
    check(_run(["git", "push", "-u", "origin", "master"], cwd=work)[0] == 0,
          f"{name}: initial push failed")
    return work


def _check_repo_basics(work, base):
    check(gitutils.is_git_repo(work), "is_git_repo should be True for a repo")
    check(not gitutils.is_git_repo(base), "is_git_repo should be False for a plain dir")
    check(gitutils.git_current_branch(work) == "master",
          "fresh repo should be on master")
    check(not gitutils.git_has_changes(work), "fresh repo should be clean")


def _check_savepos_restore(work):
    """Exercise the staged/unstaged-preserving save + restore round-trip."""
    with open(os.path.join(work, "staged.txt"), "w", encoding="utf-8") as handle:
        handle.write("new\n")
    _run(["git", "add", "staged.txt"], cwd=work)               # staged (new file)
    with open(os.path.join(work, "README"), "a", encoding="utf-8") as handle:
        handle.write("more\n")                                  # unstaged (tracked)

    check(gitutils.git_has_staged(work), "expected a staged change before save")
    check(gitutils.git_has_unstaged(work), "expected an unstaged change before save")

    ok, err = gitutils.save_uncommitted(work, "savepos")
    check(ok, f"save_uncommitted failed: {err}")
    check(gitutils.has_savepos(work, "savepos"), "HEAD should be a savepos commit")
    check(not gitutils.git_has_changes(work), "working tree should be clean after save")
    check(gitutils.git_commit_message(work, "HEAD") == "savepos - unstaged",
          "top savepos commit subject is wrong")

    ok, err = gitutils.restore_uncommitted(work, "savepos")
    check(ok, f"restore_uncommitted failed: {err}")
    check(gitutils.git_commit_message(work, "HEAD") == "init",
          "restore should return HEAD to the original commit")
    check(gitutils.git_has_staged(work),
          "restore should put the staged change back staged")
    check(gitutils.git_has_unstaged(work),
          "restore should put the unstaged change back unstaged")


def _check_create_feature_branch(work):
    ok, err = gitutils.create_feature_branch("repo", work, "myfeat", None)
    check(ok, f"create_feature_branch failed: {err}")
    check(gitutils.git_current_branch(work) == "feature/myfeat",
          "should be on the new feature branch")
    check(gitutils.git_branch_exists(work, "feature/myfeat"),
          "feature branch should exist")


def _check_rebase_on_master(work):
    _run(["git", "checkout", "-b", "feature/rb"], cwd=work)
    with open(os.path.join(work, "f1.txt"), "w", encoding="utf-8") as handle:
        handle.write("f1\n")
    _run(["git", "add", "-A"], cwd=work)
    _run(["git", "commit", "-m", "F1"], cwd=work)

    _run(["git", "checkout", "master"], cwd=work)
    with open(os.path.join(work, "m1.txt"), "w", encoding="utf-8") as handle:
        handle.write("m1\n")
    _run(["git", "add", "-A"], cwd=work)
    _run(["git", "commit", "-m", "M1"], cwd=work)
    _run(["git", "push", "origin", "master"], cwd=work)
    _run(["git", "checkout", "feature/rb"], cwd=work)

    ok, err = gitutils.rebase_on_master("repo", work, "save changes before rebase", None)
    check(ok, f"rebase_on_master failed: {err}")
    check(gitutils.git_current_branch(work) == "feature/rb",
          "should end back on the feature branch after rebase")
    rc, _ = _run(["git", "merge-base", "--is-ancestor", "master", "feature/rb"],
                 cwd=work)
    check(rc == 0, "master should be an ancestor of the rebased feature branch")


def _check_workspace_and_overrides(base, repos):
    """Exercise write/read workspace + branch-override round-trip (ignore-git)."""
    ws_root = os.path.join(base, "ws")
    original_ws_root = gitutils.WORKSPACES_ROOT
    gitutils.WORKSPACES_ROOT = ws_root
    try:
        ok, msg = gitutils.write_workspace("myws", repos)
        check(ok, f"write_workspace failed: {msg}")

        ok, got = gitutils.read_workspace_repos("myws")
        check(ok, f"read_workspace_repos failed: {got}")
        names = [n for n, _ in got] if ok else []
        expected = [n for n, _ in repos]
        check(names == expected,
              f"read_workspace_repos names {names} != {expected}")

        ok, entries = gitutils.workspace_branch_entries("myws")
        check(ok, f"workspace_branch_entries failed: {entries}")
        if ok:
            check(all(e["branch"] == "feature/myws" and not e["ignoreGit"]
                      for e in entries),
                  "default entries should be feature/myws with ignoreGit off")

        overrides = {
            expected[0]: {gitutils.IGNORE_GIT_KEY: True, "branch": "master"},
            expected[1]: {"branch": "feature/custom"},
        }
        ok, msg = gitutils.save_branch_overrides("myws", overrides)
        check(ok, f"save_branch_overrides failed: {msg}")
        check(gitutils.read_branch_overrides("myws") == overrides,
              "branch overrides did not round-trip")

        ok, entries = gitutils.workspace_branch_entries("myws")
        by_name = {e["name"]: e for e in entries} if ok else {}
        check(by_name.get(expected[0], {}).get("ignoreGit") is True,
              "first repo should now be ignoreGit=True")
        check(by_name.get(expected[0], {}).get("branch") == "master",
              "first repo's stored branch should be 'master'")
        check(by_name.get(expected[1], {}).get("branch") == "feature/custom",
              "second repo's branch override should apply")
    finally:
        gitutils.WORKSPACES_ROOT = original_ws_root


def _check_git_integration():
    """Run the git-backed checks against throwaway temp repos."""
    if not _git_available():
        print("  (git not found - skipping git integration checks)")
        return

    base = tempfile.mkdtemp(prefix="fm_smoke_git_")
    try:
        repo1 = _init_repo(base, "repo1")
        repo2 = _init_repo(base, "repo2")
        repo3 = _init_repo(base, "repo3")

        _check_repo_basics(repo1, base)
        _check_savepos_restore(repo1)
        _check_create_feature_branch(repo2)
        _check_rebase_on_master(repo3)
        _check_workspace_and_overrides(
            base, [("repo2", repo2), ("repo3", repo3)]
        )
    finally:
        _rmtree(base)


def main():
    root = tk.Tk()
    root.withdraw()  # no visible window
    theme.apply_theme(root)

    try:
        workspaces_tab = WorkspacesTab(root)
        repo_tab = ManualTab(root)

        _validate_sections(workspaces_tab, "Workspaces")
        repo_labels = _validate_sections(repo_tab, "Repositories")

        leaked = FORBIDDEN_REPO_LABELS & repo_labels
        check(not leaked,
              f"Repositories tab leaked workspace-only actions: {sorted(leaked)}")

        missing = REQUIRED_REPO_LABELS - repo_labels
        check(not missing,
              f"Repositories tab is missing ported actions: {sorted(missing)}")

        _check_base_methods()
        _check_repo_delegation(repo_tab)
        _check_pure_logic()
    finally:
        root.destroy()

    _check_git_integration()

    if _failures:
        print("SMOKE TEST FAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("SMOKE TEST OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
