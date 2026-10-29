"""ADO repositories strategy; existing provider behavior."""

from ..contracts import RepositorySource
import ado_auth
import base64
import json
import os
import data_sources
import time
import urllib.error
import urllib.parse
import urllib.request



def create_ado_pr(name, path, title, description="", target="master", draft=False,
                  branch=None):
    """Create an Azure DevOps pull request for one repo.

    Returns (ok, url_or_err, warning). The PR goes from *branch* (the repo's
    current branch when not given) to *target* (master). When *draft* is true
    the PR is created as a draft. The remote branch must already be pushed.
    Authentication reuses the Git credential already stored for the host, so no
    extra credentials are requested. When the branch name embeds a work-item id
    (e.g. ``feature/514231_...``) that work item is linked to the new PR; a link
    failure does not fail the PR but is returned as *warning*.
    """
    import gitutils
    if not gitutils.is_git_repo(path):
        return False, f"{name}: not a git repository", ""
    branch = branch or gitutils.git_current_branch(path)
    if not branch:
        return False, f"{name}: not on a branch (detached HEAD)", ""
    if branch == target:
        return False, f"{name}: on {target}; nothing to create a pull request for", ""

    remote_url = gitutils.git_remote_url(path)
    parsed = gitutils.parse_ado_remote(remote_url)
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository", ""
    org, project, repo, host = parsed

    username, password = gitutils.get_git_credential(host, remote_url)
    if not password:
        return False, f"{name}: no stored Git credential for {host}", ""

    # Project-scoped endpoint; project and repo are re-encoded here (they were
    # decoded by parse_ado_remote, so names with spaces work correctly).
    api_url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/git/repositories/"
        f"{urllib.parse.quote(repo)}/pullrequests?api-version=7.1"
    )
    body = json.dumps({
        "sourceRefName": f"refs/heads/{branch}",
        "targetRefName": f"refs/heads/{target}",
        "title": title,
        "description": description,
        "isDraft": bool(draft),
    }).encode("utf-8")
    auth = base64.b64encode(
        f"{username or ''}:{password}".encode("utf-8")
    ).decode("ascii")

    req = urllib.request.Request(api_url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        try:
            detail = json.loads(detail).get("message", detail)
        except ValueError:
            pass
        if not detail:
            # ADO returns an empty 404 body when the resource path is wrong;
            # include the URL so the org/repo can be verified.
            detail = f"resource not found at {api_url}"
        return False, f"{name}: pull request failed ({exc.code}): {detail}", ""
    except (urllib.error.URLError, OSError) as exc:
        return False, f"{name}: pull request failed: {exc}", ""

    pr_id = data.get("pullRequestId")
    # Prefer the project/repo names returned by the API for an accurate link.
    repo_info = data.get("repository") or {}
    project_name = (repo_info.get("project") or {}).get("name") or project
    repo_name = repo_info.get("name") or repo
    web_url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project_name)}/_git/"
        f"{urllib.parse.quote(repo_name)}/pullrequest/{pr_id}"
    )

    # Best-effort: link the work item referenced by the branch name to the PR.
    work_item_id = gitutils.ado_work_item_id_from_branch(branch)
    if work_item_id and pr_id is not None:
        project_id = (repo_info.get("project") or {}).get("id")
        repo_id = repo_info.get("id")
        if not (project_id and repo_id):
            warning = (
                f"{name}: PR created but could not link work item "
                f"{work_item_id} (missing project/repo id in API response)"
            )
            return True, web_url, warning
        # The Git credential is usually scoped to Code only, so the Work Items
        # API rejects it. Prefer a dedicated PAT from ADO_PAT (Work Items: write)
        # for the link step, falling back to the Git credential.
        link_token = os.environ.get("ADO_PAT") or password
        ok_link, link_err = gitutils._link_work_item_to_pr(
            org, work_item_id, project_id, repo_id, pr_id, username, link_token
        )
        if not ok_link:
            return True, web_url, f"{name}: {link_err}"

    return True, web_url, ""


def get_ado_pr_url(name, path, target="master", branch=None):
    """Return (ok, url_or_err) for an existing open PR of the repo's branch.

    Looks up the active Azure DevOps pull request that goes from *branch* (the
    repo's current branch when not given) to *target* (master) and returns a
    browser link to it. Authentication reuses the Git credential already stored
    for the host (no prompts). ok is False - with an explanatory message - when
    the repo is not an ADO repo, has no stored credential, or has no open pull
    request for its branch.
    """
    import gitutils
    if not gitutils.is_git_repo(path):
        return False, f"{name}: not a git repository"
    branch = branch or gitutils.git_current_branch(path)
    if not branch:
        return False, f"{name}: not on a branch (detached HEAD)"

    remote_url = gitutils.git_remote_url(path)
    parsed = gitutils.parse_ado_remote(remote_url)
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, repo, host = parsed

    username, password = gitutils.get_git_credential(host, remote_url)
    if not password:
        return False, f"{name}: no stored Git credential for {host}"

    query = urllib.parse.urlencode({
        "searchCriteria.sourceRefName": f"refs/heads/{branch}",
        "searchCriteria.targetRefName": f"refs/heads/{target}",
        "searchCriteria.status": "active",
        "api-version": "7.1",
    })
    api_url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/git/repositories/"
        f"{urllib.parse.quote(repo)}/pullrequests?{query}"
    )
    auth = base64.b64encode(
        f"{username or ''}:{password}".encode("utf-8")
    ).decode("ascii")

    req = urllib.request.Request(api_url, method="GET")
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        try:
            detail = json.loads(detail).get("message", detail)
        except ValueError:
            pass
        return False, f"{name}: pull request lookup failed ({exc.code}): {detail}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"{name}: pull request lookup failed: {exc}"

    prs = data.get("value") or []
    if not prs:
        return False, f"{name}: no open pull request for branch '{branch}'"

    pr = prs[0]
    pr_id = pr.get("pullRequestId")
    repo_info = pr.get("repository") or {}
    project_name = (repo_info.get("project") or {}).get("name") or project
    repo_name = repo_info.get("name") or repo
    web_url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project_name)}/_git/"
        f"{urllib.parse.quote(repo_name)}/pullrequest/{pr_id}"
    )
    return True, web_url


def _pr_rest_context(name, path, target="master", branch=None):
    """Resolve the Azure DevOps REST context for one repo's PR calls.

    Returns (ok, ctx_or_err). *ctx* carries everything the follow-up PR calls
    (threads, policy) need so they never re-derive the credential: org, project,
    repo, host, the Basic *auth* token (base64, no "Basic " prefix), branch and
    target.
    """
    import gitutils
    if not gitutils.is_git_repo(path):
        return False, f"{name}: not a git repository"
    branch = branch or gitutils.git_current_branch(path)
    if not branch:
        return False, f"{name}: not on a branch (detached HEAD)"
    remote_url = gitutils.git_remote_url(path)
    parsed = gitutils.parse_ado_remote(remote_url)
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository"
    org, project, repo, host = parsed
    username, password = gitutils.get_git_credential(host, remote_url)
    if not password:
        return False, f"{name}: no stored Git credential for {host}"
    auth = base64.b64encode(
        f"{username or ''}:{password}".encode("utf-8")
    ).decode("ascii")
    return True, {
        "name": name, "org": org, "project": project, "repo": repo,
        "host": host, "auth": auth, "branch": branch, "target": target,
    }


def fetch_pr_status(name, path, target="master", branch=None):
    """Fetch the latest pull request (any status) for *branch* -> *target*.

    One REST call. Returns (ok, info_or_err). *info* is a dict with the summary
    fields the PR-status table needs and a reusable ``ctx`` (credential + ids)
    so the threads/policy follow-ups make no extra credential lookups:

      pr_id, web_url, title, is_draft, status ('active'|'abandoned'|'completed'),
      merge_status, has_conflicts, reviewers (list of {name, vote, label,
      required, voted}), vote_summary, project_id, repo_id, ctx.

    ``info`` is None (ok True) when the repo simply has no PR for the branch.
    """
    import gitutils
    ok, ctx = gitutils._pr_rest_context(name, path, target, branch)
    if not ok:
        return False, ctx
    org, project, repo = ctx["org"], ctx["project"], ctx["repo"]
    query = urllib.parse.urlencode({
        "searchCriteria.sourceRefName": f"refs/heads/{ctx['branch']}",
        "searchCriteria.targetRefName": f"refs/heads/{target}",
        "searchCriteria.status": "all",
        "$top": "20",
        "api-version": "7.1",
    })
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/git/repositories/"
        f"{urllib.parse.quote(repo)}/pullrequests?{query}"
    )
    ok, data = gitutils._ado_get_json(url, ctx["auth"])
    if not ok:
        return False, f"{name}: pull request lookup failed {data}"

    prs = data.get("value") or []
    if not prs:
        return True, None
    # Newest first - ADO returns active before abandoned/completed, but sort by
    # creation date so the genuinely latest PR wins regardless of status.
    prs.sort(key=lambda p: p.get("creationDate") or "", reverse=True)
    pr = prs[0]

    pr_id = pr.get("pullRequestId")
    repo_info = pr.get("repository") or {}
    project_info = repo_info.get("project") or {}
    project_name = project_info.get("name") or project
    repo_name = repo_info.get("name") or repo
    web_url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project_name)}/_git/"
        f"{urllib.parse.quote(repo_name)}/pullrequest/{pr_id}"
    )

    reviewers = []
    for reviewer in pr.get("reviewers") or []:
        vote = int(reviewer.get("vote") or 0)
        reviewers.append({
            "name": reviewer.get("displayName") or "",
            "vote": vote,
            "label": gitutils.PR_VOTE_LABELS.get(vote, str(vote)),
            "required": bool(reviewer.get("isRequired")),
            # Container (team) reviewers vote on behalf of a group - skip them
            # when deciding the overall vote so a team's 0 does not mask people.
            "is_container": bool(reviewer.get("isContainer")),
            "voted": vote != 0,
        })

    merge_status = (pr.get("mergeStatus") or "").lower()
    ctx["pr_id"] = pr_id
    ctx["project_id"] = project_info.get("id")
    ctx["repo_id"] = repo_info.get("id")
    info = {
        "pr_id": pr_id,
        "web_url": web_url,
        "title": pr.get("title") or "",
        "is_draft": bool(pr.get("isDraft")),
        "status": (pr.get("status") or "").lower(),
        "merge_status": merge_status,
        "has_conflicts": merge_status == "conflicts",
        "reviewers": reviewers,
        "vote_summary": gitutils._summarise_votes(reviewers),
        "project_id": project_info.get("id"),
        "repo_id": repo_info.get("id"),
        "ctx": ctx,
    }
    return True, info


def set_pr_draft(ctx, draft):
    """Set the draft flag on the exact PR identified by a fetched context."""
    import gitutils
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(ctx['org'])}/"
        f"{urllib.parse.quote(ctx['project'])}/_apis/git/repositories/"
        f"{urllib.parse.quote(ctx['repo'])}/pullrequests/{ctx['pr_id']}?api-version=7.1"
    )
    return gitutils._ado_send_json(url, ctx["auth"], {"isDraft": bool(draft)}, "PATCH")


def _summarise_votes(reviewers):
    """Reduce a PR's reviewer votes to one overall label for the table cell."""
    votes = [r["vote"] for r in reviewers if not r["is_container"]]
    if not votes:
        return "no reviewers"
    if any(v == -10 for v in votes):
        return "rejected"
    if any(v == -5 for v in votes):
        return "waiting for author"
    approvals = [v for v in votes if v >= 5]
    if approvals and all(v >= 5 for v in votes):
        return "approved"
    if approvals:
        return "approved (partial)"
    return "no votes"


def fetch_pr_threads(ctx):
    """Fetch the PR's comment threads using the reusable *ctx* from fetch_pr_status.

    One REST call. Returns (ok, threads_or_err) where each simplified thread is
    ``{id, status, resolved, is_system, file_path, line_range, comments:[...]}``
    and each comment is ``{id, parent_id, author, date, content, is_system}``.
    The same list backs both the summary counts and the row details, so the
    details view makes no further call.
    """
    import gitutils
    org, project, repo = ctx["org"], ctx["project"], ctx["repo"]
    pr_id = ctx.get("pr_id")
    if not pr_id:
        return False, "no pull request id in context"
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/git/repositories/"
        f"{urllib.parse.quote(repo)}/pullRequests/{pr_id}/threads?api-version=7.1"
    )
    ok, data = gitutils._ado_get_json(url, ctx["auth"])
    if not ok:
        return False, f"thread lookup failed {data}"

    threads = []
    for thread in data.get("value") or []:
        if thread.get("isDeleted"):
            continue
        comments = []
        for comment in thread.get("comments") or []:
            if comment.get("isDeleted"):
                continue
            ctype = (comment.get("commentType") or "text").lower()
            comments.append({
                "id": comment.get("id"),
                "parent_id": comment.get("parentCommentId") or 0,
                "author": (comment.get("author") or {}).get("displayName") or "",
                "date": comment.get("publishedDate") or "",
                "content": comment.get("content") or "",
                "is_system": ctype == "system",
            })
        if not comments:
            continue
        status = (thread.get("status") or "").lower()
        context = thread.get("threadContext") or {}
        file_path = context.get("filePath") or ""
        line_range = gitutils._thread_line_range(context)
        threads.append({
            "id": thread.get("id"),
            "status": status,
            # Resolved statuses per the ADO comment-thread model.
            "resolved": status in ("fixed", "wontfix", "closed", "bydesign"),
            # A thread a person started has a non-system first comment; purely
            # system threads (votes, ref updates, policy notes) are excluded.
            "is_system": comments[0]["is_system"],
            "file_path": file_path,
            "line_range": line_range,
            "comments": comments,
        })
    return True, threads


def _thread_line_range(context):
    """Human line range like 'L12' or 'L12-18' from a thread's file context."""
    start = (context.get("rightFileStart") or context.get("leftFileStart") or {})
    end = (context.get("rightFileEnd") or context.get("leftFileEnd") or {})
    start_line = start.get("line")
    end_line = end.get("line")
    if not start_line:
        return ""
    if end_line and end_line != start_line:
        return f"L{start_line}-{end_line}"
    return f"L{start_line}"


def fetch_pr_policy(ctx):
    """Fetch a PR's branch-policy evaluations using the reusable *ctx*.

    One REST call per PR during the initial status scan. Returns
    (ok, dict_or_err) with: ``build_state`` ('succeeded'|'failed'|'expired'|
    'running'|'none'), ``build_id``, ``build_url``, ``rejected``, ``pending``,
    ``expired_ids`` (evaluation ids of expired builds, for requeueing) and
    ``policies`` (a [{name, status}] list for display). Build-error *text* is a
    separate lazy call (pipelines.get_build_errors) only when build_state failed.
    """
    import gitutils
    org, project = ctx["org"], ctx["project"]
    project_id, pr_id = ctx.get("project_id"), ctx.get("pr_id")
    if not (project_id and pr_id):
        return False, "missing project/pull-request id"
    artifact = f"vstfs:///CodeReview/CodeReviewId/{project_id}/{pr_id}"
    query = urllib.parse.urlencode({
        "artifactId": artifact,
        "api-version": "7.1-preview.1",
    })
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/policy/evaluations?{query}"
    )
    ok, data = gitutils._ado_get_json(url, ctx["auth"])
    if not ok:
        return False, f"policy lookup failed {data}"

    build_state = "none"
    build_id = None
    rejected = False
    pending = False
    expired_ids = []
    policies = []
    for evaluation in data.get("value") or []:
        status = (evaluation.get("status") or "").lower()
        config = evaluation.get("configuration") or {}
        type_name = ((config.get("type") or {}).get("displayName") or "").strip()
        # "Require a merge strategy" only constrains HOW a PR merges, never
        # whether it can be completed - keep it out of the readiness verdict.
        if "merge strategy" in type_name.lower():
            continue
        policies.append({"name": type_name or "policy", "status": status})
        if status in ("rejected", "broken"):
            rejected = True
        if status in ("queued", "running"):
            pending = True
        if "build" in type_name.lower():
            context = evaluation.get("context") or {}
            bid = context.get("buildId") or context.get("lastBuildId")
            if bid:
                build_id = bid
            if status in ("rejected", "broken"):
                build_state = "failed"
            elif context.get("isExpired"):
                if evaluation.get("evaluationId"):
                    expired_ids.append(evaluation["evaluationId"])
                if build_state != "failed":
                    build_state = "expired"
            elif (status in ("queued", "running")
                  and build_state not in ("failed", "expired")):
                build_state = "running"
            elif status == "approved" and build_state == "none":
                build_state = "succeeded"
    build_url = ""
    if build_id:
        build_url = (
            f"https://dev.azure.com/{urllib.parse.quote(org)}/"
            f"{urllib.parse.quote(project)}/_build/results?buildId={build_id}"
        )
    return True, {
        "build_state": build_state,
        "build_id": build_id,
        "build_url": build_url,
        "rejected": rejected,
        "pending": pending,
        "expired_ids": expired_ids,
        "policies": policies,
    }


def requeue_pr_policy(ctx, evaluation_id):
    """Requeue one policy evaluation (e.g. re-run an expired build) of a PR."""
    import gitutils
    return gitutils._requeue_policy_evaluation(ctx["org"], ctx["project"], evaluation_id, ctx["auth"])


def _ado_get_json(url, auth, timeout=30, response_headers=None):
    """GET *url* with Basic *auth* and return (ok, json_or_error_message)."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if response_headers is not None:
                response_headers.update(resp.headers)
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        try:
            detail = json.loads(detail).get("message", detail)
        except ValueError:
            pass
        return False, f"({exc.code}) {detail}"
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def _ado_send_json(url, auth, body, method, timeout=30):
    """Send *body* (dict, or None for an empty payload) to *url*.

    Returns (ok, json_or_error_message). Used for the PR PATCH calls and the
    empty-body policy requeue.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Basic {auth}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return True, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        try:
            detail = json.loads(detail).get("message", detail)
        except ValueError:
            pass
        return False, f"({exc.code}) {detail}"
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def _ado_current_user_id(org, auth):
    """Return the authenticated user's identity id (for auto-complete), or None.

    Cached cross-session per org (the identity never changes for the signed-in
    account), so PR auto-complete does not re-fetch connectionData each time.
    """
    import gitutils
    key = (org or "").lower()
    cached = gitutils._IDENTITY_CACHE.get(key)
    if cached:
        return cached
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"_apis/connectionData"
    )
    ok, data = gitutils._ado_get_json(url, auth)
    if not ok:
        return None
    user_id = (data.get("authenticatedUser") or {}).get("id")
    if user_id:
        gitutils._IDENTITY_CACHE.set(key, user_id)
    return user_id


def _pr_policy_state(org, project, project_id, pr_id, auth):
    """Inspect the branch-policy evaluations of a PR.

    Returns (pending, unrun_build_ids, rejected, count) where *pending* is True
    when any required policy is queued or running, *unrun_build_ids* holds the
    evaluation ids of build policies that are queued without a build yet (they
    can be requeued to start the build), *rejected* is True when a policy failed,
    and *count* is the number of evaluations returned.
    """
    import gitutils
    artifact = f"vstfs:///CodeReview/CodeReviewId/{project_id}/{pr_id}"
    query = urllib.parse.urlencode({
        "artifactId": artifact,
        "api-version": "7.1-preview.1",
    })
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/policy/evaluations?{query}"
    )
    ok, data = gitutils._ado_get_json(url, auth)
    if not ok:
        return False, [], False, 0

    pending = False
    rejected = False
    unrun_build_ids = []
    evals = data.get("value") or []
    for evaluation in evals:
        status = (evaluation.get("status") or "").lower()
        config = evaluation.get("configuration") or {}
        type_name = ((config.get("type") or {}).get("displayName") or "").lower()
        # "Require a merge strategy" is satisfied by the completion itself, so
        # its pre-merge "rejected" state must not block completing the PR.
        if "merge strategy" in type_name:
            continue
        is_build = "build" in type_name
        if status in ("queued", "running"):
            pending = True
        if status in ("rejected", "broken"):
            rejected = True
        if is_build and status == "queued":
            context = evaluation.get("context") or {}
            if not context.get("buildId"):
                eid = evaluation.get("evaluationId")
                if eid:
                    unrun_build_ids.append(eid)
    return pending, unrun_build_ids, rejected, len(evals)


def _requeue_policy_evaluation(org, project, evaluation_id, auth):
    """Requeue a branch-policy evaluation (e.g. start a build). Returns (ok, err)."""
    import gitutils
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/policy/evaluations/"
        f"{urllib.parse.quote(str(evaluation_id))}?api-version=7.1"
    )
    return gitutils._ado_send_json(url, auth, None, "PATCH")


def complete_ado_pr(name, path, target="master", merge_strategy="noFastForward",
                    delete_source_branch=True, transition_work_items=True,
                    publish_draft=True, auto_complete_when_not_ready=True,
                    queue_build=True, branch=None):
    """Complete (merge) the open Azure DevOps PR for the repo's *branch*.

    Returns (ok, url_or_err, warning). Looks up the active pull request that
    goes from *branch* (the repo's current branch when not given) to *target*
    (master) and tries to merge it with the given options. Authentication
    reuses the Git credential stored for the host (no prompts).

    The PR state is inspected and remediations are applied when enabled:

    * ``publish_draft`` - when the PR is a draft it is published (unmarked as
      draft) so its branch policies start; without this a draft cannot complete.
    * ``queue_build`` - when a required build policy is queued but has no build
      yet (typical for a freshly published draft) it is requeued so the build
      runs.
    * ``auto_complete_when_not_ready`` - when the PR is not immediately mergeable
      (build/policies still running) it is set to auto-complete so Azure DevOps
      merges it once every policy passes, instead of completing right now.

    ok is False - with an explanatory message - when the repo is not an ADO
    repo, has no stored credential, has no open pull request, a required
    remediation is disabled, a policy was rejected, or the merge is refused.
    """
    import gitutils
    board_ok, board_options = data_sources.strategy("board").pr_completion_options(
        transition_work_items
    )
    if not board_ok:
        return False, board_options, ""
    if not gitutils.is_git_repo(path):
        return False, f"{name}: not a git repository", ""
    branch = branch or gitutils.git_current_branch(path)
    if not branch:
        return False, f"{name}: not on a branch (detached HEAD)", ""
    if branch == target:
        return False, f"{name}: on {target}; no pull request to complete", ""

    remote_url = gitutils.git_remote_url(path)
    parsed = gitutils.parse_ado_remote(remote_url)
    if not parsed:
        return False, f"{name}: remote is not an Azure DevOps repository", ""
    org, project, repo, host = parsed

    username, password = gitutils.get_git_credential(host, remote_url)
    if not password:
        return False, f"{name}: no stored Git credential for {host}", ""

    auth = base64.b64encode(
        f"{username or ''}:{password}".encode("utf-8")
    ).decode("ascii")

    # 1) Find the active PR for this branch (need its id and merge source commit).
    query = urllib.parse.urlencode({
        "searchCriteria.sourceRefName": f"refs/heads/{branch}",
        "searchCriteria.targetRefName": f"refs/heads/{target}",
        "searchCriteria.status": "active",
        "api-version": "7.1",
    })
    base_url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project)}/_apis/git/repositories/"
        f"{urllib.parse.quote(repo)}/pullrequests"
    )
    ok, data = gitutils._ado_get_json(f"{base_url}?{query}", auth)
    if not ok:
        return False, f"{name}: pull request lookup failed {data}", ""

    prs = data.get("value") or []
    if not prs:
        return False, f"{name}: no open pull request for branch '{branch}'", ""

    pr = prs[0]
    pr_id = pr.get("pullRequestId")
    is_draft = bool(pr.get("isDraft"))
    merge_commit = (pr.get("lastMergeSourceCommit") or {}).get("commitId")
    repo_info = pr.get("repository") or {}
    project_id = (repo_info.get("project") or {}).get("id")
    project_name = (repo_info.get("project") or {}).get("name") or project
    repo_name = repo_info.get("name") or repo
    web_url = (
        f"https://dev.azure.com/{urllib.parse.quote(org)}/"
        f"{urllib.parse.quote(project_name)}/_git/"
        f"{urllib.parse.quote(repo_name)}/pullrequest/{pr_id}"
    )
    pr_url = f"{base_url}/{pr_id}?api-version=7.1"

    if merge_strategy not in gitutils.PR_MERGE_STRATEGIES:
        merge_strategy = "noFastForward"

    notes = []  # non-fatal actions taken, surfaced together as one warning

    # 2) Publish the PR if it is a draft (draft PRs cannot complete and their
    #    build policies do not run until published).
    if is_draft:
        if not publish_draft:
            return (
                False,
                f"{name}: pull request is a draft; enable 'publish draft PRs' "
                "to complete it",
                "",
            )
        ok, result = gitutils._ado_send_json(pr_url, auth, {"isDraft": False}, "PATCH")
        if not ok:
            return False, f"{name}: could not publish draft PR: {result}", ""
        notes.append("published draft")

    # 3) Inspect branch-policy state to decide between an immediate merge and
    #    setting auto-complete.
    pending, unrun_build_ids, rejected, evaluation_count = (False, [], False, 0)
    if project_id:
        pending, unrun_build_ids, rejected, evaluation_count = gitutils._pr_policy_state(
            org, project, project_id, pr_id, auth
        )

    if rejected:
        return (
            False,
            f"{name}: a required branch policy was rejected; resolve it in the "
            "PR before completing",
            "",
        )

    # Queue any required build that has not started (common right after a draft
    # is published). Doing so means the PR cannot merge immediately, so it will
    # go down the auto-complete path below.
    if queue_build and unrun_build_ids:
        queued = 0
        for eid in unrun_build_ids:
            ok, _ = gitutils._requeue_policy_evaluation(org, project, eid, auth)
            if ok:
                queued += 1
        if queued:
            pending = True
            notes.append(f"queued {queued} build{'s' if queued != 1 else ''}")

    # A freshly published draft may not have its evaluations listed yet; assume
    # policies still need to run so it is not completed prematurely.
    if "published draft" in notes and evaluation_count == 0:
        pending = True

    completion_options = {
        "deleteSourceBranch": bool(delete_source_branch),
        "mergeStrategy": merge_strategy,
        **board_options,
    }

    # 4a) Not ready to merge now -> set auto-complete so ADO merges it later.
    if pending:
        if not auto_complete_when_not_ready:
            extra = f" ({', '.join(notes)})" if notes else ""
            return (
                False,
                f"{name}: pull request is not ready to complete - build or "
                f"policies still running{extra}; enable auto-complete to merge "
                "it automatically",
                "",
            )
        user_id = gitutils._ado_current_user_id(org, auth)
        if not user_id:
            return (
                False,
                f"{name}: could not resolve identity to set auto-complete",
                "",
            )
        body = {
            "autoCompleteSetBy": {"id": user_id},
            "completionOptions": completion_options,
        }
        ok, result = gitutils._ado_send_json(pr_url, auth, body, "PATCH")
        if not ok:
            return False, f"{name}: could not set auto-complete: {result}", ""
        notes.append("set to auto-complete")
        return True, web_url, f"{name}: {', '.join(notes)} (merges when policies pass)"

    # 4b) Ready now -> complete immediately.
    patch_body = {
        "status": "completed",
        "completionOptions": completion_options,
    }
    if merge_commit:
        patch_body["lastMergeSourceCommit"] = {"commitId": merge_commit}
    ok, result = gitutils._ado_send_json(pr_url, auth, patch_body, "PATCH")
    if not ok:
        return False, f"{name}: pull request completion failed {result}", ""

    # ADO processes the merge asynchronously: the completion PATCH usually
    # returns while status is still "active" and mergeStatus "queued". That is
    # not a second merge - it is the same one being applied. Poll the PR until
    # the merge finishes so a queued merge that succeeds is reported as done,
    # and only a real conflict/failure/rejection surfaces as an error.
    status = result.get("status")
    if status and status != "completed":
        status, merge_status = gitutils._await_pr_merge(pr_url, auth)
        if status == "completed":
            pass  # fall through to the success return below
        elif merge_status in ("conflicts", "failure", "rejectedByPolicy"):
            return (
                False,
                f"{name}: merge {merge_status}; resolve it in the PR",
                "",
            )
        else:
            # Still queued after polling - not an error, just not confirmed yet.
            notes.append("merge queued")
            return True, web_url, f"{name}: {', '.join(notes)}; check the PR"

    if notes:
        return True, web_url, f"{name}: {', '.join(notes)}, then completed"
    return True, web_url, ""


def _await_pr_merge(pr_url, auth, attempts=8, delay=1.5):
    """Poll a completing PR until its merge settles. Returns (status, mergeStatus).

    ADO applies the merge asynchronously after the completion PATCH, so the PR
    briefly stays ``active`` with ``mergeStatus`` ``queued``. This re-reads the
    PR a few times until it reports ``completed`` (or an abandoned/failed merge)
    rather than treating the transient queued state as an error.
    """
    import gitutils
    status, merge_status = "active", "queued"
    for _ in range(attempts):
        ok, data = gitutils._ado_get_json(pr_url, auth)
        if not ok:
            break
        status = data.get("status") or status
        merge_status = data.get("mergeStatus") or merge_status
        if status == "completed" or status == "abandoned":
            break
        if merge_status in ("conflicts", "failure", "rejectedByPolicy"):
            break
        time.sleep(delay)
    return status, merge_status


def _get(url, auth, params=None, response_headers=None):
    import gitutils
    query = dict(params or {}, **{"api-version": "7.1"})
    options = {"response_headers": response_headers} if response_headers is not None else {}
    ok, data = gitutils._ado_get_json(
        url + "?" + urllib.parse.urlencode(query), auth, **options
    )
    if not ok:
        raise RuntimeError(data)
    return data


def _pr_state(pr):
    if pr.get("isDraft"):
        return "Draft"
    if pr.get("mergeStatus") == "conflicts":
        return "Merge conflicts"
    reviewers = pr.get("reviewers") or []
    votes = [int(reviewer.get("vote") or 0) for reviewer in reviewers]
    if -10 in votes:
        return "Rejected"
    if -5 in votes:
        return "Waiting for author"
    if pr.get("autoCompleteSetBy"):
        return "Auto-complete pending"
    required = [reviewer for reviewer in reviewers if reviewer.get("isRequired")]
    if any(int(reviewer.get("vote") or 0) < 5 for reviewer in required):
        return "Waiting for approvals"
    if any(vote >= 5 for vote in votes):
        return "Approved (policies not checked)"
    return "Waiting for approvals"


def _creation_date(base, auth, ref):
    import stale_items
    offset = 0
    while True:
        pushes = stale_items._get(base + "/pushes", auth, {
            "searchCriteria.refName": ref,
            "searchCriteria.includeRefUpdates": "true",
            "$top": 100, "$skip": offset,
        }).get("value", [])
        for push in pushes:
            for update in push.get("refUpdates") or []:
                if update.get("name") == ref and update.get("oldObjectId") == stale_items._ZERO:
                    return push["date"]
        if len(pushes) < 100:
            return None
        offset += len(pushes)


def scan_repo(entry, force=False, now=None):
    """Return branches, PRs and errors; never infer ownership from commit author."""
    import gitutils
    import stale_items
    name, path = entry
    result = {"branches": [], "prs": [], "errors": []}
    try:
        remote = gitutils.git_remote_url(path)
        parsed = gitutils.parse_ado_remote(remote)
        if not parsed:
            raise RuntimeError("origin is not an Azure DevOps repository")
        org, project, repo, host = parsed
        header, error = ado_auth.auth_header(host, org=org, cred_url=remote)
        if not header:
            raise RuntimeError(error)
        auth = header.removeprefix("Basic ")
        with stale_items._IDENTITY_LOCK:
            user_id = gitutils._ado_current_user_id(org, auth)
        if not user_id:
            raise RuntimeError("Cannot resolve your Azure DevOps identity")
        key = (org.lower(), project.lower(), repo.lower(), user_id.lower())
        with stale_items._LOCK:
            cached = stale_items._CACHE.get(key)
            if not force and cached and time.monotonic() - cached[0] < stale_items._CACHE_SECONDS:
                return cached[1]
        base = (
            "https://dev.azure.com/" + urllib.parse.quote(org, safe="") + "/"
            + urllib.parse.quote(project, safe="") + "/_apis/git/repositories/"
            + urllib.parse.quote(repo, safe="")
        )
        web = (
            "https://dev.azure.com/" + urllib.parse.quote(org, safe="") + "/"
            + urllib.parse.quote(project, safe="") + "/_git/"
            + urllib.parse.quote(repo, safe="")
        )
        context = {"base": base, "path": path, "repo": name, "key": key}
        try:
            offset = 0
            while True:
                prs = stale_items._get(base + "/pullrequests", auth, {
                    "searchCriteria.status": "active",
                    "searchCriteria.creatorId": user_id,
                    "$top": 100, "$skip": offset,
                }).get("value", [])
                for pr in prs:
                    if (pr.get("status") != "active" or
                            (pr.get("createdBy") or {}).get("id", "").lower() != user_id.lower()):
                        continue
                    result["prs"].append(dict(
                        context, title=f"#{pr['pullRequestId']} {pr.get('title', '')}",
                        pr_id=pr["pullRequestId"], date=pr["creationDate"],
                        link=web + f"/pullrequest/{pr['pullRequestId']}",
                        state=stale_items._pr_state(pr),
                    ))
                if len(prs) < 100:
                    break
                offset += len(prs)
        except Exception as exc:
            result["errors"].append(f"{name} PRs: {exc}")
        try:
            refs = []
            continuation = None
            while True:
                params = {"filter": "heads/feature/", "$top": 1000}
                if continuation:
                    params["continuationToken"] = continuation
                headers = {}
                page = stale_items._get(base + "/refs", auth, params, headers).get("value", [])
                refs.extend(page)
                continuation = next((value for key, value in headers.items()
                                     if key.lower() == "x-ms-continuationtoken"), None)
                if not continuation:
                    break
            owned = [ref for ref in refs if
                     (ref.get("creator") or {}).get("id", "").lower() == user_id.lower()]
            stats = {}
            if owned:
                metadata = stale_items._get(base, auth)
                target = metadata.get("defaultBranch") or "refs/heads/master"
                stats = {stat["name"]: stat for stat in stale_items._get(
                    base + "/stats/branches", auth,
                    {"baseVersionDescriptor.version": target.removeprefix("refs/heads/"),
                     "baseVersionDescriptor.versionType": "branch"},
                ).get("value", [])}
            cutoff = (now or stale_items.datetime.now(stale_items.timezone.utc)) - stale_items.timedelta(days=7)
            for ref in owned:
                try:
                    created = stale_items._creation_date(base, auth, ref["name"])
                    if not created:
                        raise RuntimeError("branch creation date unavailable")
                    if stale_items._date(created) >= cutoff:
                        continue
                    branch = ref["name"].removeprefix("refs/heads/")
                    behind = stats.get(branch, {}).get("behindCount")
                    result["branches"].append(dict(
                        context, title=branch, ref=ref["name"],
                        object_id=ref["objectId"], date=created,
                        link=web + "?" + urllib.parse.urlencode({"version": "GB" + branch}),
                        state=str(behind) if behind is not None else "Unknown",
                        target=target.removeprefix("refs/heads/"),
                    ))
                except Exception as exc:
                    result["errors"].append(f"{name} {ref['name']}: {exc}")
        except Exception as exc:
            result["errors"].append(f"{name} branches: {exc}")
        if not result["errors"]:
            with stale_items._LOCK:
                stale_items._CACHE[key] = (time.monotonic(), result)
    except Exception as exc:
        result["errors"].append(f"{name}: {exc}")
    return result


def remove_item(item, kind, context=None):
    """Use repository Git credentials for writes, as existing PR actions do."""
    import gitutils
    import stale_items
    try:
        ctx = context
        if ctx is None:
            ok, ctx = gitutils._pr_rest_context(item["repo"], item["path"], branch="master")
            if not ok:
                return False, ctx
        if kind == "branches":
            url = item["base"] + "/refs?api-version=7.1"
            body = [{"name": item["ref"], "oldObjectId": item["object_id"],
                     "newObjectId": stale_items._ZERO}]
            ok, data = gitutils._ado_send_json(url, ctx["auth"], body, "POST")
            if ok:
                updates = data.get("value") or []
                ok = bool(updates) and all(update.get("success") for update in updates)
                if not ok:
                    data = "; ".join(str(update.get("customMessage") or
                                         update.get("updateStatus")) for update in updates)
        else:
            url = item["base"] + f"/pullrequests/{item['pr_id']}?api-version=7.1"
            ok, data = gitutils._ado_send_json(url, ctx["auth"], {"status": "abandoned"}, "PATCH")
        with stale_items._LOCK:
            stale_items._CACHE.pop(item["key"], None)
        return ok, "" if ok else str(data)
    except Exception as exc:
        return False, str(exc)


class AdoRepositories(RepositorySource):
    create_pr = staticmethod(create_ado_pr)
    get_pr_url = staticmethod(get_ado_pr_url)
    fetch_pr_status = staticmethod(fetch_pr_status)
    set_pr_draft = staticmethod(set_pr_draft)
    fetch_pr_threads = staticmethod(fetch_pr_threads)
    fetch_pr_policy = staticmethod(fetch_pr_policy)
    requeue_pr_policy = staticmethod(requeue_pr_policy)
    complete_pr = staticmethod(complete_ado_pr)
    scan_repo = staticmethod(scan_repo)
    remove_item = staticmethod(remove_item)
