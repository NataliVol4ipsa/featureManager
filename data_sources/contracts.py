"""Data-access contracts shared by the provider strategies."""

from abc import ABC, abstractmethod


class BoardSource(ABC):
    available = True

    @staticmethod
    @abstractmethod
    def pr_completion_options(transition_work_items):
        """Return (ok, options_or_error) for board transitions on PR completion."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def fetch_work_item(work_item_id):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def link_work_item_to_pr(org, work_item_id, project_id, repo_id, pr_id, username, password):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_work_item_report_details(org, project, work_item_id, host):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_work_item_report_details_for_repo(name, path, work_item_id):
        """Implement this operation using the selected provider."""
        raise NotImplementedError


class RepositorySource(ABC):
    available = True

    @staticmethod
    @abstractmethod
    def create_pr(name, path, title, description='', target='master', draft=False, branch=None):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_pr_url(name, path, target='master', branch=None):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def fetch_pr_status(name, path, target='master', branch=None):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def set_pr_draft(ctx, draft):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def fetch_pr_threads(ctx):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def fetch_pr_policy(ctx):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def requeue_pr_policy(ctx, evaluation_id):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def complete_pr(name, path, target='master', merge_strategy='noFastForward', delete_source_branch=True, transition_work_items=True, publish_draft=True, auto_complete_when_not_ready=True, queue_build=True, branch=None):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def scan_repo(entry, force=False, now=None):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def remove_item(item, kind, context=None):
        """Implement this operation using the selected provider."""
        raise NotImplementedError


class PipelineSource(ABC):
    available = True

    @staticmethod
    @abstractmethod
    def get_pipeline_parameters(repo_path):
        """Return normalized parameters, or raise SourceUnavailableError."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def build_template_parameters(repo_path, environment):
        """Return provider inputs, or raise SourceUnavailableError."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def build_template_parameters_for_roles(repo_path, deploy_dev, deploy_acc):
        """Return deployment inputs, or raise SourceUnavailableError."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_master_pipeline_profile(name, path, count=15, scan=40):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def find_env_deployment_for_branch(name, path, branch, environment):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_build_errors(org, project, host, build_id):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def approve_pipeline_stage(run_info, approval_ids):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_pipeline_stage_statuses(run_info):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def rerun_failed_stage(run_info, stage_ref_name):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def cancel_pipeline_run(run_info):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def run_pipeline_for_repo_details(name, path, branch, environment, extra_parameters=None):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def rerun_pipeline_from_latest_commit(run_info, override_parameters=None):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def redeploy_master_for_repo_details(name, path, deploy_dev, deploy_acc):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_latest_master_pipeline_run_details(name, path):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def run_pipeline_for_repo(name, path, branch, environment):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_master_pipeline_run_for_merged_branch_details(name, path, branch):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_master_pipeline_run_for_merged_branch(name, path, branch):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_latest_deployed_artifacts(name, path, force=False):
        """Implement this operation using the selected provider."""
        raise NotImplementedError


class NugetSource(ABC):
    available = True

    @staticmethod
    @abstractmethod
    def private_versions(repo_path, package_ids, token):
        """Return (ok, {configured, versions}) or (False, error)."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_token():
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def prewarm_token():
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def check_private_feed(feed_url, token):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def healthcheck_private_feeds(repo_paths, token):
        """Implement this operation using the selected provider."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def dotnet_restore(repo_path, token=None):
        """Implement this operation using the selected provider."""
        raise NotImplementedError
