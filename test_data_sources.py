"""Offline contract, routing, persistence and UI tests for provider strategies."""

import inspect
import io
import json
import os
import tempfile
import threading
import tkinter as tk
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import data_sources
import gitutils
import packages
import pbi
import pipeline_estimates
import pipelines
from data_sources import settings as source_settings
import stale_items
from pipeline_monitor import PipelineMonitorWindow
from pr_status import PrStatusWindow
from data_sources.contracts import BoardSource, RepositorySource, PipelineSource, NugetSource
from data_sources.dialog import DataSourcesDialog, install_notifications
from tab_base import ActionTabBase
import theme


ROUTES = [
    (pipelines, "get_pipeline_parameters", "pipelines", "get_pipeline_parameters", ("path",)),
    (pipelines, "build_template_parameters", "pipelines", "build_template_parameters", ("path", "dev")),
    (pipelines, "build_template_parameters_for_roles", "pipelines",
     "build_template_parameters_for_roles", ("path", True, False)),
    (pbi, "fetch_work_item", "board", "fetch_work_item", (42,)),
    (gitutils, "_link_work_item_to_pr", "board", "link_work_item_to_pr",
     ("org", 42, "project", "repo", 1, "user", "token")),
    (pipelines, "get_work_item_report_details", "board",
     "get_work_item_report_details", ("org", "project", 42, "host")),
    (pipelines, "get_work_item_report_details_for_repo", "board",
     "get_work_item_report_details_for_repo", ("repo", "path", 42)),
    (gitutils, "create_ado_pr", "repositories", "create_pr", ("repo", "path", "title")),
    (gitutils, "get_ado_pr_url", "repositories", "get_pr_url", ("repo", "path")),
    (gitutils, "fetch_pr_status", "repositories", "fetch_pr_status", ("repo", "path")),
    (gitutils, "set_pr_draft", "repositories", "set_pr_draft", ({}, True)),
    (gitutils, "fetch_pr_threads", "repositories", "fetch_pr_threads", ({},)),
    (gitutils, "fetch_pr_policy", "repositories", "fetch_pr_policy", ({},)),
    (gitutils, "requeue_pr_policy", "repositories", "requeue_pr_policy", ({}, 1)),
    (gitutils, "complete_ado_pr", "repositories", "complete_pr", ("repo", "path")),
    (stale_items, "scan_repo", "repositories", "scan_repo", (("repo", "path"),)),
    (stale_items, "remove_item", "repositories", "remove_item", ({}, "branches")),
    (pipelines, "get_master_pipeline_profile", "pipelines",
     "get_master_pipeline_profile", ("repo", "path")),
    (pipelines, "find_env_deployment_for_branch", "pipelines",
     "find_env_deployment_for_branch", ("repo", "path", "feature/test", "dev")),
    (pipelines, "get_build_errors", "pipelines", "get_build_errors",
     ("org", "project", "host", 1)),
    (pipelines, "approve_pipeline_stage", "pipelines", "approve_pipeline_stage", ({}, [1])),
    (pipelines, "get_pipeline_stage_statuses", "pipelines", "get_pipeline_stage_statuses", ({},)),
    (pipelines, "rerun_failed_stage", "pipelines", "rerun_failed_stage", ({}, "dev")),
    (pipelines, "cancel_pipeline_run", "pipelines", "cancel_pipeline_run", ({},)),
    (pipelines, "run_pipeline_for_repo_details", "pipelines",
     "run_pipeline_for_repo_details", ("repo", "path", "feature/test", "dev")),
    (pipelines, "rerun_pipeline_from_latest_commit", "pipelines",
     "rerun_pipeline_from_latest_commit", ({},)),
    (pipelines, "redeploy_master_for_repo_details", "pipelines",
     "redeploy_master_for_repo_details", ("repo", "path", True, False)),
    (pipelines, "get_latest_master_pipeline_run_details", "pipelines",
     "get_latest_master_pipeline_run_details", ("repo", "path")),
    (pipelines, "run_pipeline_for_repo", "pipelines", "run_pipeline_for_repo",
     ("repo", "path", "feature/test", "dev")),
    (pipelines, "get_master_pipeline_run_for_merged_branch_details", "pipelines",
     "get_master_pipeline_run_for_merged_branch_details", ("repo", "path", "feature/test")),
    (pipelines, "get_master_pipeline_run_for_merged_branch", "pipelines",
     "get_master_pipeline_run_for_merged_branch", ("repo", "path", "feature/test")),
    (pipelines, "get_latest_deployed_artifacts", "pipelines",
     "get_latest_deployed_artifacts", ("repo", "path")),
    (packages, "get_azure_devops_token", "nuget", "get_token", ()),
    (packages, "prewarm_azure_devops_token", "nuget", "prewarm_token", ()),
    (packages, "check_private_feed", "nuget", "check_private_feed", ("feed", "token")),
    (packages, "healthcheck_private_feeds", "nuget", "healthcheck_private_feeds", (["path"], "token")),
    (packages, "dotnet_restore", "nuget", "dotnet_restore", ("path",)),
]
CONTRACTS = {
    "board": BoardSource, "repositories": RepositorySource,
    "pipelines": PipelineSource, "nuget": NugetSource,
}


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.active = dict.fromkeys(source_settings.SOURCES, "ADO")
        self.selection = patch.object(source_settings, "_selection", self.active)
        self.selection.start()
        self.addCleanup(self.selection.stop)
        data_sources._reported.clear()
        data_sources.pending_notices()

    def test_default_and_independent_selection(self):
        for source, contract in CONTRACTS.items():
            self.assertIsInstance(data_sources.strategy(source), contract)
            self.assertTrue(data_sources.strategy(source).available)
        self.active["board"] = "GitHub"
        self.assertFalse(data_sources.strategy("board").available)
        self.assertTrue(data_sources.strategy("repositories").available)
        self.assertTrue(data_sources.strategy("pipelines").available)
        self.assertTrue(data_sources.strategy("nuget").available)

    def test_settings_path_stays_in_the_application_data_directory(self):
        self.assertEqual(source_settings._PATH, os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "data", "data_sources.json"))

    def test_all_facades_delegate_with_the_original_arguments(self):
        from unittest.mock import Mock
        for module, name, source, method, args in ROUTES:
            with self.subTest(operation=name):
                result = object()
                implementation = Mock(return_value=result)
                selected = SimpleNamespace(**{method: implementation})
                with patch.object(data_sources, "strategy", return_value=selected) as resolver:
                    self.assertIs(getattr(module, name)(*args), result)
                resolver.assert_called_once_with(source)
                expected = inspect.signature(getattr(module, name)).bind(*args)
                expected.apply_defaults()
                implementation.assert_called_once_with(*expected.args, **expected.kwargs)

    def test_github_contracts_are_explicit_failures_without_io(self):
        self.active.update(dict.fromkeys(source_settings.SOURCES, "GitHub"))
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
                patch("socket.create_connection", side_effect=AssertionError("network")), \
                patch("subprocess.run", side_effect=AssertionError("subprocess")):
            for module, name, _source, _method, args in ROUTES:
                with self.subTest(operation=name):
                    if name in (
                            "get_pipeline_parameters", "build_template_parameters",
                            "build_template_parameters_for_roles"):
                        with self.assertRaises(data_sources.SourceUnavailableError):
                            getattr(module, name)(*args)
                        continue
                    result = getattr(module, name)(*args)
                    if name in ("get_azure_devops_token", "prewarm_azure_devops_token"):
                        self.assertIsNone(result)
                    elif name == "scan_repo":
                        self.assertEqual(result["branches"], [])
                        self.assertEqual(result["prs"], [])
                        self.assertIn("not implemented yet", result["errors"][0])
                    else:
                        self.assertFalse(result[0])
                        self.assertIn("not implemented yet", result[1])
                        if name in ("create_ado_pr", "complete_ado_pr"):
                            self.assertEqual(len(result), 3)
            ok, error = packages.compute_repo_bumps(
                "path", include_private=True, token="cached-token")
            self.assertFalse(ok)
            self.assertIn("not implemented yet", error)
            ok, error = data_sources.strategy("nuget").private_versions("path", ["pkg"], "token")
            self.assertFalse(ok)
            self.assertIn("not implemented yet", error)
        self.assertEqual(set(data_sources.pending_notices()), set(source_settings.SOURCES))

    def test_strategy_signatures_match_contracts(self):
        for provider in source_settings.PROVIDERS:
            self.active.update(dict.fromkeys(source_settings.SOURCES, provider))
            for source, contract in CONTRACTS.items():
                instance = data_sources.strategy(source)
                for method in contract.__abstractmethods__:
                    with self.subTest(provider=provider, method=method):
                        self.assertEqual(
                            inspect.signature(getattr(instance, method)),
                            inspect.signature(getattr(contract, method)),
                        )

    def test_notices_are_deduplicated_across_workers(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _index: data_sources.unsupported("board"), range(50)))
        self.assertEqual(data_sources.pending_notices(), ["board"])
        self.assertEqual(data_sources.pending_notices(), [])

    def test_background_operations_stop_before_discovery_or_network(self):
        from unittest.mock import Mock
        self.active.update(dict.fromkeys(source_settings.SOURCES, "GitHub"))
        with patch.object(pipeline_estimates, "_discover_entries") as discover, \
                patch("tab_base.check_ado_connectivity") as probe:
            error_log = Mock()
            pipeline_estimates.refresh_all(error_log=error_log)
            discover.assert_not_called()
            error_log.assert_called_once()
            ActionTabBase.run_pipelines(SimpleNamespace(), [("repo", "path", "branch")], "dev")
            ActionTabBase.redeploy_latest_master(SimpleNamespace(), [("repo", "path", "branch")])
            ActionTabBase.show_master_pipeline_monitor_for_merged_prs(
                SimpleNamespace(), [("repo", "path", "branch")])
            probe.assert_not_called()
        monitor = SimpleNamespace(_closed=False, title=Mock())
        PipelineMonitorWindow._poll_once(monitor)
        monitor.title.assert_called_once()
        PipelineMonitorWindow._rerun_from_latest(SimpleNamespace(), "repo")
        window = SimpleNamespace(_poll_job=None, winfo_exists=lambda: True)
        PrStatusWindow._poll(window)

    def test_board_transition_does_not_mutate_pr_when_unavailable(self):
        self.active["board"] = "GitHub"
        with patch.object(gitutils, "_ado_send_json") as write, \
                patch.object(gitutils, "get_git_credential") as credentials:
            ok, error, _warning = gitutils.complete_ado_pr("repo", "path")
            self.assertFalse(ok)
            self.assertIn("not implemented yet", error)
            write.assert_not_called()
            credentials.assert_not_called()
        self.assertEqual(
            data_sources.strategy("board").pr_completion_options(False), (True, {}))

    def test_public_nuget_is_independent_of_private_source(self):
        self.active["nuget"] = "GitHub"
        with tempfile.TemporaryDirectory() as directory:
            props = os.path.join(directory, "Directory.Packages.props")
            with open(props, "w", encoding="utf-8") as handle:
                handle.write('<Project><PackageVersion Include="Example" Version="1.0.0" /></Project>')
            with patch.object(packages, "fetch_latest_stable", return_value="2.0.0"):
                ok, plan = packages.compute_repo_bumps(directory)
            self.assertTrue(ok)
            self.assertEqual(plan, [(props, "Example", "1.0.0", "2.0.0")])
        self.assertEqual(data_sources.pending_notices(), [])

    def test_ado_private_and_public_versions_are_combined(self):
        with tempfile.TemporaryDirectory() as directory:
            props = os.path.join(directory, "Directory.Packages.props")
            with open(props, "w", encoding="utf-8") as handle:
                handle.write('<Project><PackageVersion Include="Example" Version="1.0.0" /></Project>')
            with patch.object(packages, "find_private_feeds", return_value=["feed"]), \
                    patch.object(packages, "_flat_container_base", return_value="base"), \
                    patch.object(packages, "_fetch_latest_stable_private", return_value="3.0.0"), \
                    patch.object(packages, "fetch_latest_stable", return_value="2.0.0"):
                ok, plan = packages.compute_repo_bumps(
                    directory, include_private=True, token="token")
            self.assertTrue(ok)
            self.assertEqual(plan, [(props, "Example", "1.0.0", "3.0.0")])

    def test_ado_board_reads_keep_their_payload_shape(self):
        body = {"id": 42, "fields": {
            "System.Title": "Example", "System.Description": "<p>WBS</p>"}}
        with patch.object(pbi, "load_secrets", return_value={
                "ado_organization_url": "https://dev.azure.com/org"}), \
                patch.object(pbi, "_ado_auth_header", return_value=("Basic test", "")), \
                patch("urllib.request.urlopen", return_value=io.BytesIO(
                    json.dumps(body).encode("utf-8"))) as get:
            ok, result = pbi.fetch_work_item(42)
        self.assertTrue(ok)
        self.assertEqual(result, {"id": 42, "title": "Example", "description": "<p>WBS</p>"})
        self.assertIn("/_apis/wit/workitems/42?", get.call_args.args[0].full_url)

    def test_ado_pr_creation_keeps_independent_board_warning(self):
        self.active["board"] = "GitHub"
        body = {"pullRequestId": 7, "repository": {
            "id": "repo-id", "name": "Repo",
            "project": {"id": "project-id", "name": "Project"}}}
        with patch.object(gitutils, "is_git_repo", return_value=True), \
                patch.object(gitutils, "git_remote_url",
                             return_value="https://dev.azure.com/org/Project/_git/Repo"), \
                patch.object(gitutils, "get_git_credential", return_value=("user", "token")), \
                patch("urllib.request.urlopen", return_value=io.BytesIO(
                    json.dumps(body).encode("utf-8"))) as send:
            ok, url, warning = gitutils.create_ado_pr(
                "Repo", "path", "Example", draft=True, branch="feature/42_example")
        self.assertTrue(ok)
        self.assertTrue(url.endswith("/pullrequest/7"))
        self.assertIn("not implemented yet", warning)
        payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(payload["sourceRefName"], "refs/heads/feature/42_example")
        self.assertEqual(payload["targetRefName"], "refs/heads/master")
        self.assertTrue(payload["isDraft"])

    def test_ado_completion_preserves_board_options(self):
        body = {"value": [{"pullRequestId": 7, "lastMergeSourceCommit": {
            "commitId": "commit"}, "repository": {"project": {"id": "project-id"}}}]}
        with patch.object(gitutils, "is_git_repo", return_value=True), \
                patch.object(gitutils, "git_remote_url",
                             return_value="https://dev.azure.com/org/Project/_git/Repo"), \
                patch.object(gitutils, "get_git_credential", return_value=("user", "token")), \
                patch.object(gitutils, "_ado_get_json", return_value=(True, body)), \
                patch.object(gitutils, "_pr_policy_state", return_value=(False, [], False, 1)), \
                patch.object(gitutils, "_ado_send_json",
                             return_value=(True, {"status": "completed"})) as send:
            ok, _url, warning = gitutils.complete_ado_pr(
                "Repo", "path", branch="feature/42_example", merge_strategy="squash")
        self.assertTrue(ok)
        self.assertEqual(warning, "")
        payload = send.call_args.args[2]
        self.assertEqual(payload["completionOptions"], {
            "deleteSourceBranch": True, "mergeStrategy": "squash",
            "transitionWorkItems": True})
        self.assertEqual(payload["lastMergeSourceCommit"], {"commitId": "commit"})

    def test_ado_pipeline_inputs_and_cancellation(self):
        with patch.object(pipelines, "_discover_role_params", return_value={
                "dev": "deployDev", "acc": "deployAcc", "prod": "deployProd"}):
            self.assertEqual(pipelines.build_template_parameters("path", "dev"), {
                "deployDev": True, "deployAcc": False, "deployProd": False})
            self.assertEqual(
                pipelines.build_template_parameters_for_roles("path", True, True),
                {"deployDev": True, "deployAcc": True, "deployProd": False})
        info = {"org": "org", "project": "Project", "host": "dev.azure.com", "build_id": 7}
        with patch.object(pipelines, "_auth_for_host", return_value=("Basic test", "")), \
                patch.object(pipelines, "_api_patch") as send:
            self.assertEqual(pipelines.cancel_pipeline_run(info), (True, ""))
        self.assertIn("/_apis/build/builds/7?", send.call_args.args[0])
        self.assertEqual(send.call_args.args[1], {"status": "cancelling"})


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "data_sources.json")
        self.path_patch = patch.object(source_settings, "_PATH", self.path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.selection = patch.object(source_settings, "_selection", None)
        self.selection.start()
        self.addCleanup(self.selection.stop)

    def test_missing_file_defaults_and_atomic_persistence(self):
        defaults = dict.fromkeys(source_settings.SOURCES, "ADO")
        self.assertEqual(source_settings.current(), defaults)
        changed = dict(defaults, board="GitHub", pipelines="GitHub")
        source_settings.save(changed)
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), changed)
        self.assertEqual(source_settings.current(), defaults)
        source_settings._selection = None
        self.assertEqual(source_settings.current(), changed)
        self.assertEqual(os.listdir(self.directory.name), ["data_sources.json"])

    def test_partial_settings_default_remaining_sources(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"board": "GitHub"}, handle)
        self.assertEqual(source_settings.current()["board"], "GitHub")
        self.assertEqual(source_settings.current()["pipelines"], "ADO")

    def test_invalid_settings_are_not_silently_replaced(self):
        for content in ('{"board": "other"}', '{"unknown": "ADO"}', '[]', '{bad'):
            with self.subTest(content=content):
                with open(self.path, "w", encoding="utf-8") as handle:
                    handle.write(content)
                with self.assertRaises(ValueError):
                    source_settings.current()
        with self.assertRaises(ValueError):
            source_settings.save({"board": "other"})

    def test_failed_save_keeps_existing_file_and_cleans_temporary(self):
        source_settings.save({"board": "ADO"})
        with patch("data_sources.settings.os.replace", side_effect=OSError("cannot save")):
            with self.assertRaises(OSError):
                source_settings.save({"board": "GitHub"})
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["board"], "ADO")
        self.assertEqual(os.listdir(self.directory.name), ["data_sources.json"])


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()
        theme.apply_theme(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.selection = patch.object(
            source_settings, "_selection", dict.fromkeys(source_settings.SOURCES, "ADO"))
        self.selection.start()
        self.addCleanup(self.selection.stop)
        self.dialog = DataSourcesDialog(self.root)
        self.addCleanup(lambda: self.dialog.destroy() if self.dialog.winfo_exists() else None)

    def test_modal_rows_save_and_restart_result(self):
        self.assertEqual(set(self.dialog.variables), set(source_settings.SOURCES))
        self.assertEqual(self.dialog.grab_current(), self.dialog)
        self.dialog.variables["board"].set("GitHub")
        with patch.object(source_settings, "save") as save:
            self.dialog._save()
        self.assertTrue(self.dialog.saved)
        expected = dict.fromkeys(source_settings.SOURCES, "ADO")
        expected["board"] = "GitHub"
        save.assert_called_once_with(expected)

    def test_cancel_does_not_persist(self):
        with patch.object(source_settings, "save") as save:
            self.dialog.destroy()
        self.assertFalse(self.dialog.saved)
        save.assert_not_called()

    def test_save_errors_are_visible_and_leave_dialog_open(self):
        with patch.object(source_settings, "save", side_effect=OSError("cannot save")), \
                patch("data_sources.dialog.messagebox.showerror") as error:
            self.dialog._save()
        error.assert_called_once()
        self.assertFalse(self.dialog.saved)
        self.assertTrue(self.dialog.winfo_exists())

    def test_notification_ui_is_only_called_on_main_thread(self):
        from unittest.mock import Mock
        data_sources._reported.clear()
        data_sources.pending_notices()
        worker = threading.Thread(target=data_sources.unsupported, args=("board",))
        worker.start()
        worker.join()
        root = SimpleNamespace(after=Mock())
        install_notifications(root)
        _delay, callback = root.after.call_args.args
        with patch("data_sources.dialog.messagebox.showinfo") as popup:
            callback()
        popup.assert_called_once()
        self.assertIn("not implemented yet", popup.call_args.args[1])
        self.assertEqual(data_sources.pending_notices(), [])


if __name__ == "__main__":
    unittest.main()
