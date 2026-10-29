"""GitHub placeholders: explicit failures, no network or Tk access."""

import data_sources
from .contracts import (
    BoardSource, RepositorySource, PipelineSource, NugetSource,
)


class GitHubBoard(BoardSource):
    available = False

    @staticmethod
    def pr_completion_options(transition_work_items):
        if transition_work_items:
            return False, data_sources.unsupported("board")
        return True, {}

    @staticmethod
    def fetch_work_item(work_item_id):
        return False, data_sources.unsupported("board")

    @staticmethod
    def link_work_item_to_pr(org, work_item_id, project_id, repo_id, pr_id, username, password):
        return False, data_sources.unsupported("board")

    @staticmethod
    def get_work_item_report_details(org, project, work_item_id, host):
        return False, data_sources.unsupported("board")

    @staticmethod
    def get_work_item_report_details_for_repo(name, path, work_item_id):
        return False, data_sources.unsupported("board")


class GitHubRepositories(RepositorySource):
    available = False

    @staticmethod
    def create_pr(name, path, title, description='', target='master', draft=False, branch=None):
        return False, data_sources.unsupported("repositories"), ""

    @staticmethod
    def get_pr_url(name, path, target='master', branch=None):
        return False, data_sources.unsupported("repositories")

    @staticmethod
    def fetch_pr_status(name, path, target='master', branch=None):
        return False, data_sources.unsupported("repositories")

    @staticmethod
    def set_pr_draft(ctx, draft):
        return False, data_sources.unsupported("repositories")

    @staticmethod
    def fetch_pr_threads(ctx):
        return False, data_sources.unsupported("repositories")

    @staticmethod
    def fetch_pr_policy(ctx):
        return False, data_sources.unsupported("repositories")

    @staticmethod
    def requeue_pr_policy(ctx, evaluation_id):
        return False, data_sources.unsupported("repositories")

    @staticmethod
    def complete_pr(name, path, target='master', merge_strategy='noFastForward', delete_source_branch=True, transition_work_items=True, publish_draft=True, auto_complete_when_not_ready=True, queue_build=True, branch=None):
        return False, data_sources.unsupported("repositories"), ""

    @staticmethod
    def scan_repo(entry, force=False, now=None):
        return {"branches": [], "prs": [], "errors": [data_sources.unsupported("repositories")]}

    @staticmethod
    def remove_item(item, kind, context=None):
        return False, data_sources.unsupported("repositories")


class GitHubPipelines(PipelineSource):
    available = False

    @staticmethod
    def build_template_parameters(repo_path, environment):
        raise data_sources.SourceUnavailableError(
            data_sources.unsupported("pipelines"))

    @staticmethod
    def build_template_parameters_for_roles(repo_path, deploy_dev, deploy_acc):
        raise data_sources.SourceUnavailableError(
            data_sources.unsupported("pipelines"))

    @staticmethod
    def get_pipeline_parameters(repo_path):
        raise data_sources.SourceUnavailableError(
            data_sources.unsupported("pipelines"))

    @staticmethod
    def get_master_pipeline_profile(name, path, count=15, scan=40):
        return False, data_sources.unsupported("pipelines")

    @staticmethod
    def find_env_deployment_for_branch(name, path, branch, environment):
        return False, data_sources.unsupported("pipelines")

    @staticmethod
    def get_build_errors(org, project, host, build_id):
        return False, data_sources.unsupported("pipelines")

    @staticmethod
    def approve_pipeline_stage(run_info, approval_ids):
        return False, data_sources.unsupported("pipelines")

    @staticmethod
    def get_pipeline_stage_statuses(run_info):
        return False, data_sources.unsupported("pipelines")

    @staticmethod
    def rerun_failed_stage(run_info, stage_ref_name):
        return False, data_sources.unsupported("pipelines")

    @staticmethod
    def cancel_pipeline_run(run_info):
        return False, data_sources.unsupported("pipelines")

    @staticmethod
    def run_pipeline_for_repo_details(name, path, branch, environment, extra_parameters=None):
        return False, data_sources.unsupported("pipelines")

    @staticmethod
    def rerun_pipeline_from_latest_commit(run_info, override_parameters=None):
        return False, data_sources.unsupported("pipelines")

    @staticmethod
    def redeploy_master_for_repo_details(name, path, deploy_dev, deploy_acc):
        return False, data_sources.unsupported("pipelines")

    @staticmethod
    def get_latest_master_pipeline_run_details(name, path):
        return False, data_sources.unsupported("pipelines")

    @staticmethod
    def run_pipeline_for_repo(name, path, branch, environment):
        return False, data_sources.unsupported("pipelines")

    @staticmethod
    def get_master_pipeline_run_for_merged_branch_details(name, path, branch):
        return False, data_sources.unsupported("pipelines")

    @staticmethod
    def get_master_pipeline_run_for_merged_branch(name, path, branch):
        return False, data_sources.unsupported("pipelines")

    @staticmethod
    def get_latest_deployed_artifacts(name, path, force=False):
        return False, data_sources.unsupported("pipelines")


class GitHubNuget(NugetSource):
    available = False

    @staticmethod
    def private_versions(repo_path, package_ids, token):
        return False, data_sources.unsupported("nuget")

    @staticmethod
    def get_token():
        data_sources.unsupported("nuget")
        return None

    @staticmethod
    def prewarm_token():
        return None

    @staticmethod
    def check_private_feed(feed_url, token):
        return False, data_sources.unsupported("nuget")

    @staticmethod
    def healthcheck_private_feeds(repo_paths, token):
        return False, data_sources.unsupported("nuget")

    @staticmethod
    def dotnet_restore(repo_path, token=None):
        return False, data_sources.unsupported("nuget")
