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
from tkinter import ttk
from unittest.mock import patch

import gitutils
import deployment_status
import manual_tab
import packages
import pbi
import pipelines
import pr_status
import stale_items
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
    "Bump NuGet packages (public)",
    "Bump NuGet packages (private)",
    "Bump all NuGet packages",
    "Restore NuGet packages",
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
            "open_prs", "show_master_pipeline_monitor_for_merged_prs",
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
    check("Copy PR links" not in labels,
          f"{tab_name}: Copy PR links still appears as a standalone action")
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
    for name in ("bump_packages", "restore_packages", "run_pipelines",
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

        repo_tab._action_restore()
        name, args, _ = last()
        check(name == "restore_packages" and args == (FAKE_REPOS,),
              f"Restore did not delegate correctly: {last()!r}")

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

    _run(["git", "checkout", "master"], cwd=work)
    with open(os.path.join(work, "m2.txt"), "w", encoding="utf-8") as handle:
        handle.write("m2\n")
    _run(["git", "add", "-A"], cwd=work)
    _run(["git", "commit", "-m", "M2"], cwd=work)
    _run(["git", "push", "origin", "master"], cwd=work)
    _run(["git", "reset", "--hard", "HEAD~1"], cwd=work)
    with open(os.path.join(work, "dirty.txt"), "w", encoding="utf-8") as handle:
        handle.write("uncommitted\n")
    nested_repo = os.path.join(work, "nested-repo")
    os.makedirs(nested_repo)
    _run(["git", "init", "--quiet", nested_repo])

    ok, err = gitutils.rebase_on_master(
        "repo", work, "save changes before rebase", "delete", pull_only=True
    )
    check(ok, f"pull-only on master failed: {err}")
    check(gitutils.git_current_branch(work) == "master",
          "pull-only should leave the repo on master")
    check(os.path.isfile(os.path.join(work, "m2.txt")),
          "pull-only should fetch the latest master commit")
    check(not os.path.exists(os.path.join(work, "dirty.txt")),
          "pull-only should apply the selected uncommitted-change decision")
    check(not os.path.exists(nested_repo),
          "delete should remove an untracked nested git repository")
    check(not gitutils.git_has_changes(work),
          "delete should leave the worktree clean before pulling")
    rc, _ = _run(["git", "merge-base", "--is-ancestor", "master", "feature/rb"],
                 cwd=work)
    check(rc != 0, "pull-only should not rebase the feature branch")


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


def _check_pr_status(root):
      with patch.object(pr_status.PrStatusWindow, "_load"):
            window = pr_status.PrStatusWindow(
                  root, [("RepoA", "fake/A", "feature/a"),
                           ("RepoB", "fake/B", "feature/b")],
            )
      try:
            window._build_header()
            policy = dict(build_state="succeeded", rejected=False, pending=False)
            for row, name in enumerate(("RepoA", "RepoB"), start=2):
                  window._row_index[name] = row
                  service = tk.Frame(window._inner)
                  service.grid(row=row, column=0)
                  tk.Label(service, text=name).pack()
                  window._service_cells[name] = service
                  ctx = dict(org="org", project="My Project", repo=name,
                                 pr_id=row, auth="test")
                  info = dict(status="active", is_draft=True, has_conflicts=False,
                                    vote_summary="approved", ctx=ctx, pr_id=row,
                                    reviewers=[
                                          dict(vote=10, is_container=False),
                                          dict(vote=5, is_container=False),
                                          dict(vote=0, is_container=False),
                                          dict(vote=-10, is_container=False),
                                          dict(vote=10, is_container=True),
                                    ],
                                    web_url="https://example.invalid")
                  window._render_row(name, True, info, [], None, True, policy, None)
            root.update()
            vote_col = next(index for index, column in enumerate(window._columns())
                                    if column[0] == "vote")
            for row in (2, 3):
                  vote_cell = window._inner.grid_slaves(row=row, column=vote_col)[0]
                  check(vote_cell.cget("text") == "approved(2)",
                          "Vote cell did not show the status and individual approver count")
            check(window._copy_links_button.winfo_x() < window._refresh_button.winfo_x(),
                    "Copy PR links button was not left of Refresh")
            with patch.object(window, "clipboard_clear") as clear, \
                        patch.object(window, "clipboard_append") as append, \
                        patch.object(theme, "load_copy_links_formatted",
                                     return_value=False), \
                        patch.object(theme, "load_list_numbering",
                                     return_value=(False, 0)):
                  window._copy_links_button.invoke()
                  clear.assert_called_once()
                  append.assert_called_once_with(
                        "RepoA - https://example.invalid\nRepoB - https://example.invalid"
                  )
                  window._row_data["RepoA"]["ok"] = False
                  window._row_data["RepoB"]["info"] = None
                  window._copy_links_button.invoke()
                  check(clear.call_count == 1,
                          "Empty PR links cleared the clipboard")
                  window._row_data["RepoA"]["ok"] = True
                  window._row_data["RepoB"]["info"] = dict(info)
            draft_col = next(index for index, column in enumerate(window._columns())
                                     if column[:2] == ("draft", "Draft"))
            cell = window._inner.grid_slaves(row=2, column=draft_col)[0]
            label = next(child for child in cell.winfo_children()
                               if isinstance(child, tk.Label))
            button = next(child for child in cell.winfo_children()
                                if isinstance(child, ttk.Button))
            size = (cell.winfo_width(), cell.winfo_height())
            label.event_generate("<Enter>")
            root.update()
            check(button.winfo_manager() == "place", "Draft hover did not show toggle")
            check((cell.winfo_width(), cell.winfo_height()) == size,
                    "Draft hover changed the cell size")
            button.event_generate("<Leave>")
            root.update()
            check(not button.winfo_manager(), "Draft toggle did not hide on leave")

            class ImmediateThread:
                  def __init__(self, target, daemon):
                        self.target = target

                  def start(self):
                        self.target()

            with patch.object(pr_status.threading, "Thread", ImmediateThread), \
                        patch.object(pr_status, "fetch_pr_policy", return_value=(True, policy)), \
                        patch.object(gitutils, "_ado_send_json", return_value=(True, {})) as send:
                  for draft in (False, True):
                        window._toggle_draft("RepoA")
                        check("RepoA" in window._draft_updates,
                                "Draft update was not marked pending")
                        window._toggle_draft("RepoA")
                        root.update()
                        send.assert_called_with(
                              "https://dev.azure.com/org/My%20Project/_apis/git/"
                              "repositories/RepoA/pullrequests/2?api-version=7.1",
                              "test", {"isDraft": draft}, "PATCH",
                        )
                        check(window._row_data["RepoA"]["info"]["is_draft"] == draft,
                                "Draft state did not update")
                        check(window._row_data["RepoB"]["info"]["is_draft"],
                                "Draft toggle changed another row")
                        complete = next(widget for widget in window._inner.grid_slaves(row=2)
                                                if isinstance(widget, ttk.Button))
                        check(complete.instate(["disabled"]) == draft,
                                "Complete button did not follow draft readiness")
                  check(send.call_count == 2, "Pending toggle allowed duplicate requests")
            with patch.object(pr_status.threading, "Thread", ImmediateThread), \
                        patch.object(pr_status, "set_pr_draft", return_value=(False, "denied")), \
                        patch.object(pr_status.messagebox, "showerror") as error:
                  window._toggle_draft("RepoA")
                  root.update()
                  check(error.called, "Draft failure was not reported")
                  check(window._row_data["RepoA"]["info"]["is_draft"],
                          "Failed draft toggle changed local state")
                  check(not window._draft_updates, "Failed draft toggle stayed pending")
            data = window._row_data["RepoA"]
            data["info"]["is_draft"] = False
            for state in ("running", "failed"):
                  data["policy"] = dict(policy, build_state=state)
                  check(not window._is_ready(data), f"{state} build was ready to complete")
            data["policy_ok"] = False
            check(not window._is_ready(data), "Unknown policy was ready to complete")
            with patch.object(window, "_tab") as tab:
                  window._complete_one("RepoA")
                  check(not tab.complete_prs.called, "Unready PR completion was dispatched")
            for change in ("build failed", "new comment"):
                  window._show_changes([("RepoA", change)])
                  root.update()
                  check(root.grab_current() is None,
                          "Automatic PR changes warning blocked the application")
            check(len(window._changes) == 2,
                    "Repeated PR changes warning lost earlier changes")
            window._changes_window.event_generate("<Escape>")
            root.update()
            check(window._changes_window is None and not window._changes,
                    "Dismissing PR changes warning did not clear its state")
      finally:
            window.destroy()


def _check_deployment_timestamps():
      from datetime import datetime
      import urllib.error

      authored = "2026-10-26T10:20:30Z"
      deployed = "2026-10-27T12:34:56Z"
      builds = [
            {"id": 12, "sourceVersion": "abc123", "status": "inProgress",
             "requestedFor": {"displayName": "Test User"}},
      ]
      timeline = {"records": [
            {"type": "Stage", "name": "Development", "state": "completed",
             "result": "succeeded", "finishTime": deployed},
            {"type": "Stage", "name": "Acceptance", "state": "completed",
             "result": "succeeded", "finishTime": "2026-10-28T08:00:00Z"},
            {"type": "Stage", "name": "Production", "state": "inProgress"},
      ]}
      commit = {"comment": "Commit subject\nMore detail",
                "author": {"date": authored}}

      def api_get(url, auth):
            if "/build/builds?" in url:
                  return {"value": builds}
            if "/commits/abc123?" in url:
                  return commit
            raise AssertionError(f"Unexpected deployment request: {url}")

      with patch.object(pipelines, "is_git_repo", return_value=True), \
                  patch.object(pipelines, "git_remote_url", return_value="remote"), \
                  patch.object(pipelines, "parse_ado_remote",
                               return_value=("org", "project", "repo", "host")), \
                  patch.object(pipelines, "_auth_for_host", return_value=("auth", None)), \
                  patch.object(pipelines, "_cached_repo_id", return_value="repo-id"), \
                  patch.object(pipelines, "_cached_pipeline_id", return_value=7), \
                  patch.object(pipelines, "_deploy_status_cache", {}), \
                  patch.object(pipelines, "_api_build_timeline_safe",
                               return_value=timeline), \
                  patch.object(pipelines, "_api_get", side_effect=api_get) as request:
            ok, data = pipelines.get_latest_deployed_artifacts("Repo", "path")
            check(ok, "Deployment timestamp lookup failed")
            dev = data["development"]
            check(dev["deployed_at"] == deployed,
                  "Deployment date did not come from the completed stage")
            check(data["acceptance"]["deployed_at"] == "2026-10-28T08:00:00Z",
                  "Stages with the same commit lost their distinct deployment dates")
            check(data["production"] is None, "Unfinished deployment gained a commit")
            check(dev["commit_created_at"] == authored,
                  "Commit creation date did not come from the author")
            check(dev["message"] == "Commit subject", "Commit subject changed")
            check(request.call_count == 2,
                  "Deployment timestamps added requests or duplicated a commit lookup")
            pipelines.get_latest_deployed_artifacts("Repo", "path")
            check(request.call_count == 2, "Deployment cache was not reused")

      tooltip = deployment_status._cell_tooltip("master", dev)
      for value, label in ((deployed, "Deployed"), (authored, "Commit authored")):
            local = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
            check(f"- {label}: {local.strftime('%Y-%m-%d %H:%M:%S')}" in tooltip.splitlines(),
                  f"{label} timestamp was not shown in local time without a timezone label")
      check("Commit subject" in tooltip and "Triggered by: Test User" in tooltip
            and "latest commit on master" in tooltip,
            "Deployment tooltip lost existing details")
      check(deployment_status._cell_tooltip("master", {}) == "- latest commit on master",
            "Missing deployment dates added empty tooltip lines")
      check("- Deployed: invalid" in deployment_status._cell_tooltip(
            "master", {"deployed_at": "invalid"}),
            "Invalid timestamp was hidden or crashed the tooltip")

      with patch.object(pipelines, "_api_get", return_value={}):
            check(pipelines._commit_details_from_ado(
                  "org", "project", "repo", "abc", "auth") ==
                  {"message": "", "commit_created_at": ""},
                  "Missing commit metadata was not handled")
      with patch.object(pipelines, "_api_get",
                        side_effect=urllib.error.URLError("unavailable")):
            check(pipelines._commit_details_from_ado(
                  "org", "project", "repo", "abc", "auth") == {},
                  "Unavailable commit metadata changed best-effort lookup behavior")


def _check_stale_items(root):
      from datetime import datetime, timezone

      now = datetime(2026, 10, 28, tzinfo=timezone.utc)
      owner = "my-id"
      with patch.object(gitutils, "_IDENTITY_CACHE") as identity_cache, \
           patch.object(gitutils, "_ado_get_json", return_value=(True, {
                 "authenticatedUser": {"id": owner},
           })) as identity_get:
            identity_cache.get.side_effect = [None, owner]
            check(gitutils._ado_current_user_id("test-org", "auth") == owner,
                  "Identity lookup must return the authenticated Azure DevOps user")
            check(identity_get.call_args.args[0] == "https://dev.azure.com/test-org/_apis/connectionData",
                  "connectionData must not request the unsupported stable 7.1 API version")
            identity_cache.set.assert_called_once_with("test-org", owner)
            check(gitutils._ado_current_user_id("test-org", "auth") == owner,
                  "Identity lookup must reuse its existing cache")
            identity_get.assert_called_once()
      base_ref = {"creator": {"id": owner}, "objectId": "a" * 40}
      refs = [dict(base_ref, name="refs/heads/feature/old"),
              dict(base_ref, name="refs/heads/feature/new"),
              dict(base_ref, name="refs/heads/feature/unknown"),
              dict(base_ref, name="refs/heads/feature/other", creator={"id": "other"})]
      active = {"pullRequestId": 12, "createdBy": {"id": owner},
                "status": "active", "creationDate": "2026-10-27T00:00:00Z",
                "title": "My draft", "isDraft": True}
      calls = []

      def get(url, auth, params=None, response_headers=None):
            calls.append((url, params))
            if url.endswith("/pullrequests"):
                  check(params["searchCriteria.creatorId"] == owner,
                        "Stale PR query must filter by current creator")
                  check(params["searchCriteria.status"] == "active",
                        "Stale PR query must request only active PRs")
                  return {"value": [active, dict(active, pullRequestId=13, status="abandoned"),
                                    dict(active, pullRequestId=14, createdBy={"id": "other"})]}
            if url.endswith("/refs"):
                  if "continuationToken" not in params:
                        response_headers["X-MS-ContinuationToken"] = "opaque-token"
                        return {"value": refs[:2]}
                  check(params["continuationToken"] == "opaque-token",
                        "Stale branches must use the server continuation token")
                  return {"value": refs[2:]}
            if url.endswith("/stats/branches"):
                  check(params["baseVersionDescriptor.version"] == "main",
                        "Stale branches must compare with the repository default branch")
                  return {"value": [{"name": "feature/old", "behindCount": 7}]}
            if url.endswith("/pushes"):
                  ref = params["searchCriteria.refName"]
                  if ref.endswith("unknown"):
                        return {"value": []}
                  date = "2026-10-01T00:00:00Z" if ref.endswith("old") else "2026-10-27T00:00:00Z"
                  return {"value": [{"date": date, "refUpdates": [
                        {"name": ref, "oldObjectId": stale_items._ZERO}]}]}
            return {"defaultBranch": "refs/heads/main"}

      stale_items._CACHE.clear()
      with patch.object(gitutils, "git_remote_url", return_value="https://dev.azure.com/org/project/_git/repo"), \
           patch.object(stale_items.ado_auth, "auth_header", return_value=("Basic auth", "")), \
           patch.object(gitutils, "_ado_current_user_id", return_value=owner), \
           patch.object(stale_items, "_get", side_effect=get):
            result = stale_items.scan_repo(("Repo", "fake"), force=True, now=now)
            check([item["title"] for item in result["branches"]] == ["feature/old"],
                  "Stale branches must exclude recent, unknown-age and other-owned refs")
            check(result["branches"][0]["state"] == "7", "Stale branches must show behind count")
            check(len(result["prs"]) == 1 and result["prs"][0]["state"] == "Draft",
                  "Stale PRs must include recent own drafts but exclude abandoned and other-owned PRs")
            check(len(result["errors"]) == 1, "Missing branch creation dates must be reported")
            with patch.object(stale_items, "_creation_date", return_value="2026-10-01T00:00:00Z"):
                  stale_items.scan_repo(("Repo", "fake"), force=True, now=now)
                  count = len(calls)
                  stale_items.scan_repo(("Repo", "fake"), now=now)
                  check(len(calls) == count, "Reopening stale items must reuse fresh scan results")

      branch = result["branches"][0]
      with patch.object(gitutils, "_pr_rest_context", return_value=(True, {"auth": "auth"})), \
           patch.object(gitutils, "_ado_send_json", return_value=(True, {"value": [{"success": True}]})) as send:
            ok, error = stale_items.remove_item(branch, "branches")
            check(ok and not error, "Stale branch deletion must accept successful ref updates")
            body = send.call_args.args[2][0]
            check(body["oldObjectId"] == branch["object_id"] and body["newObjectId"] == stale_items._ZERO,
                  "Branch deletion must guard against concurrent pushes with the displayed commit ID")
            send.return_value = (True, {"value": [{"success": False, "updateStatus": "staleOldObjectId"}]})
            ok, error = stale_items.remove_item(branch, "branches")
            check(not ok and "staleOldObjectId" in error, "Failed ref updates must not remove the row")
            send.return_value = (True, {"status": "abandoned"})
            ok, error = stale_items.remove_item(result["prs"][0], "prs")
            check(ok and send.call_args.args[2] == {"status": "abandoned"},
                  "PR cleanup must abandon rather than complete the PR")

      with patch.object(stale_items, "repository_entries", return_value=[]):
            window = stale_items.StaleItemsWindow(root)
            window._busy = False
            window._rows["branches"] = [{"item": branch, "selected": tk.BooleanVar(value=True)}]
            window._render("branches")
            check(len(window._notebook.tabs()) == 2, "Stale cleanup must have two tabs")
            with patch.object(stale_items.messagebox, "askyesno", return_value=False) as confirm:
                  window._act("branches")
                  check(confirm.called, "Bulk branch deletion must require confirmation")
            window._rows["branches"] = [
                  {"item": dict(branch, repo=repo, state=state, date=date),
                   "selected": tk.BooleanVar(value=True)}
                  for repo, state, date in (
                        ("Zulu", "10", "2026-10-01T00:00:00Z"),
                        ("alpha", "2", "2026-10-02T00:00:00Z"),
                        ("Beta", "Unknown", "2026-10-03T00:00:00Z"),
                  )
            ]
            window._sort_table("branches", "state")
            check([row["item"]["state"] for row in window._rows["branches"]] == ["2", "10", "Unknown"],
                  "Branch behind counts must sort numerically with unknown values last")
            header = window._pages["branches"]["table"].grid_slaves(row=0, column=5)[0]
            header.event_generate("<Button-1>")
            check([row["item"]["state"] for row in window._rows["branches"]] == ["10", "2", "Unknown"],
                  "Clicking the same header must reverse sorting and keep unknown values last")
            window._sort_table("branches", "repo")
            check([row["item"]["repo"] for row in window._rows["branches"]] == ["alpha", "Beta", "Zulu"],
                  "Repository sorting must be case insensitive")
            window._sort_table("branches", "date")
            check(window._rows["branches"][0]["item"]["repo"] == "Zulu",
                  "Creation dates must sort chronologically")
            window._rows["branches"].append({
                  "item": dict(branch, repo="New arrival", date="2026-09-01T00:00:00Z"),
                  "selected": tk.BooleanVar(value=True),
            })
            window._render("branches")
            check(window._rows["branches"][0]["item"]["repo"] == "New arrival",
                  "New scan results must retain the selected sort order")
            check(all(row["selected"].get() for row in window._rows["branches"]),
                  "Sorting must preserve existing row selections")
            window._rows["prs"] = [
                  {"item": dict(result["prs"][0], pr_id=pr_id, title=f"#{pr_id} PR", state=state),
                   "selected": tk.BooleanVar(value=False)}
                  for pr_id, state in ((10, "Waiting for approvals"), (2, "Draft"))
            ]
            window._sort_table("prs", "title")
            check([row["item"]["pr_id"] for row in window._rows["prs"]] == [2, 10],
                  "The PR column must sort PR numbers numerically")
            window._sort_table("prs", "state")
            check(window._rows["prs"][0]["item"]["state"] == "Draft",
                  "PR status must sort alphabetically")
            check(window._sort["branches"] == ("date", False),
                  "Each tab must retain an independent sort order")
            window._close()
      stale_items._CACHE.clear()


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
            _check_deployment_timestamps()
            _check_pr_status(root)
            _check_stale_items(root)
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
