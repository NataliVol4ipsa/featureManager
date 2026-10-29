"""ADO board strategy; existing provider behavior."""

from ..contracts import BoardSource
import ado_auth
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request



def _ado_auth_header():
    """Return (auth_header_value, error) for Azure DevOps work-item reads.

    The header itself is derived and cached by :mod:`ado_auth`; this only adds
    the secrets.json organization-URL requirement specific to the PBI feature.
    """
    import pbi
    secrets = pbi.load_secrets()
    org_url = (secrets.get("ado_organization_url") or "").strip()
    if not org_url:
        return None, (
            "Azure DevOps organization URL is not configured. Set "
            "'ado_organization_url' in secrets.json."
        )

    host = (urllib.parse.urlparse(org_url).hostname or "").lower()
    return ado_auth.auth_header(host, cred_url=org_url)


def fetch_work_item(work_item_id):
    """Download a work item from Azure DevOps. Returns (ok, result).

    On success *result* is a dict with "id", "title" and "description" (plain
    text). On failure *result* is an error message string.
    """
    import pbi
    secrets = pbi.load_secrets()
    org_url = (secrets.get("ado_organization_url") or "").strip().rstrip("/")
    if not org_url:
        return False, (
            "Azure DevOps organization URL is not configured. Set "
            "'ado_organization_url' in secrets.json."
        )

    auth, err = pbi._ado_auth_header()
    if err:
        return False, err

    api_url = (
        f"{org_url}/_apis/wit/workitems/"
        f"{urllib.parse.quote(str(work_item_id))}"
        "?fields=System.Title,System.Description&api-version=7.1"
    )
    req = urllib.request.Request(api_url, method="GET")
    req.add_header("Authorization", auth)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        try:
            detail = json.loads(detail).get("message", detail)
        except ValueError:
            pass
        if exc.code == 404:
            detail = detail or f"work item {work_item_id} was not found"
        return False, f"could not download PBI {work_item_id} ({exc.code}): {detail}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"could not download PBI {work_item_id}: {exc}"

    fields = data.get("fields", {})
    return True, {
        "id": data.get("id", work_item_id),
        "title": fields.get("System.Title", "") or "",
        "description": fields.get("System.Description", "") or "",
    }


def _link_work_item_to_pr(org, work_item_id, project_id, repo_id, pr_id,
                          username, password):
    """Attach a PR ArtifactLink to a work item. Returns (ok, error) (best effort).

    Failures are returned but callers treat them as non-fatal since the PR has
    already been created. All links are serialized through a process-wide lock
    so parallel PR creation never issues concurrent PATCHes to the same PBI.
    """
    import gitutils
    # PR artifact id is projectId/repoId/prId with the slashes URL-encoded.
    artifact_id = urllib.parse.quote(
        f"{project_id}/{repo_id}/{pr_id}", safe=""
    )
    artifact_url = f"vstfs:///Git/PullRequestId/{artifact_id}"

    api_url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"_apis/wit/workitems/{urllib.parse.quote(str(work_item_id))}"
        f"?api-version=7.1"
    )
    patch = json.dumps([{
        "op": "add",
        "path": "/relations/-",
        "value": {
            "rel": "ArtifactLink",
            "url": artifact_url,
            "attributes": {"name": "Pull Request"},
        },
    }]).encode("utf-8")

    # Try Basic auth (works for PATs) first, then Bearer (for Azure AD access
    # tokens, which the work-item API may only accept that way). The credential
    # reused from Git can be either form depending on how it was issued.
    basic = base64.b64encode(
        f"{username or ''}:{password}".encode("utf-8")
    ).decode("ascii")
    attempts = [f"Basic {basic}", f"Bearer {password}"]

    # Retry the whole link on transient failures (network blips, throttling,
    # server errors) and on concurrency conflicts (409/412) - the work-item API
    # is occasionally flaky right after a PR is created, and two PRs sharing a
    # PBI can collide when their relations are patched close together.
    max_tries = 5
    retry_delay = 2  # seconds, grows linearly per retry

    def _is_transient(code):
        return code == 429 or code in (409, 412) or code >= 500

    last_code, last_detail = None, ""
    # Serialize the actual HTTP retry loop so concurrent callers do not fight
    # over the same work item; PR creation already happened outside the lock.
    with gitutils._WORK_ITEM_LINK_LOCK:
        for try_num in range(1, max_tries + 1):
            transient = False
            for authorization in attempts:
                req = urllib.request.Request(api_url, data=patch, method="PATCH")
                req.add_header("Content-Type", "application/json-patch+json")
                req.add_header("Authorization", authorization)
                try:
                    with urllib.request.urlopen(req, timeout=30):
                        return True, ""
                except urllib.error.HTTPError as exc:
                    last_code = exc.code
                    last_detail = exc.read().decode("utf-8", "replace").strip()
                    # Throttling, server errors and concurrency conflicts are
                    # transient - retry the whole attempt after a wait.
                    if _is_transient(exc.code):
                        transient = True
                        break
                    # Only an auth failure is worth retrying with the other scheme.
                    if exc.code not in (401, 403):
                        return gitutils._link_failure(work_item_id, last_code, last_detail)
                except (urllib.error.URLError, OSError) as exc:
                    last_code, last_detail = None, str(exc)
                    transient = True
                    break

            if not transient:
                # Exhausted both auth schemes without a transient error - give up.
                break
            if try_num < max_tries:
                time.sleep(retry_delay * try_num)

    return gitutils._link_failure(work_item_id, last_code, last_detail)


def _link_failure(work_item_id, last_code, last_detail):
    """Build the (False, message) tuple for a failed work-item PR link."""
    hint = ""
    if last_code in (401, 403):
        hint = (
            " - the credential lacks Work Items (write) permission; set the "
            "ADO_PAT environment variable to a PAT with that scope, or link the "
            "work item manually"
        )
    if not last_detail:
        last_detail = "(empty response)"
    return False, (
        f"work item {work_item_id} link failed ({last_code}): "
        f"{last_detail}{hint}"
    )


def _work_item_edit_url(org, project, work_item_id):
    """Return the human-facing Azure DevOps edit URL for a work item."""
    return (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_workitems/edit/{work_item_id}"
    )


def get_work_item_report_details(org, project, work_item_id, host):
    """Return (ok, details_or_error) for a work item's title and test reports.

    On success *details* is ``{"title": str, "test_reports": [(name, url), ...]}``
    where each test report is a work item linked to *work_item_id* via the
    "Tested By" relation (name is its title, url is its edit page).
    """
    import pipelines
    auth, err = pipelines._auth_for_host(host, org)
    if err:
        return False, err

    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/wit/workitems/"
        f"{urllib.parse.quote(str(work_item_id))}"
        "?$expand=relations&api-version=7.1"
    )
    try:
        data = pipelines._api_get(url, auth)
    except urllib.error.HTTPError as exc:
        return False, (
            f"work item {work_item_id} lookup failed ({exc.code}): "
            f"{pipelines._http_error_detail(exc)}"
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"work item {work_item_id} lookup failed: {exc}"

    title = ((data.get("fields") or {}).get("System.Title") or "").strip()

    tested_by_ids = []
    for rel in data.get("relations") or []:
        if (rel.get("rel") or "") != pipelines._TESTED_BY_REL:
            continue
        rel_id = (rel.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
        if rel_id.isdigit() and rel_id not in tested_by_ids:
            tested_by_ids.append(rel_id)

    test_reports = []
    for rid in tested_by_ids:
        report_url = (
            f"https://dev.azure.com/{urllib.parse.quote(org)}/"
            f"{urllib.parse.quote(project)}/_apis/wit/workitems/"
            f"{urllib.parse.quote(rid)}?fields=System.Title&api-version=7.1"
        )
        try:
            report_data = pipelines._api_get(report_url, auth)
            name = ((report_data.get("fields") or {}).get("System.Title")
                    or "").strip() or f"Work item {rid}"
        except (urllib.error.HTTPError, urllib.error.URLError,
                OSError, ValueError):
            name = f"Work item {rid}"
        test_reports.append((name, pipelines._work_item_edit_url(org, project, rid)))

    return True, {"title": title, "test_reports": test_reports}


def get_work_item_report_details_for_repo(name, path, work_item_id):
    """Return (ok, details_or_error) using *path*'s Azure DevOps org/project.

    Convenience wrapper around ``get_work_item_report_details`` that resolves the
    org/project/host from the repository remote.
    """
    import pipelines
    if not pipelines.is_git_repo(path):
        return False, f"{name}: not a git repository"
    parsed = pipelines.parse_ado_remote(pipelines.git_remote_url(path))
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, _repo, host = parsed
    return pipelines.get_work_item_report_details(org, project, work_item_id, host)


class AdoBoard(BoardSource):
    @staticmethod
    def pr_completion_options(transition_work_items):
        return True, {"transitionWorkItems": bool(transition_work_items)}

    fetch_work_item = staticmethod(fetch_work_item)
    link_work_item_to_pr = staticmethod(_link_work_item_to_pr)
    get_work_item_report_details = staticmethod(get_work_item_report_details)
    get_work_item_report_details_for_repo = staticmethod(get_work_item_report_details_for_repo)
