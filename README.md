# Feature Manager

A tkinter GUI for managing the git repositories under `D:/Repositories`.

Run it with:

```powershell
python featureManager.py
```

## Workspace completion and deletion

Right-click a workspace in the **Workspaces** tab for **Mark completed** or
**Delete**. Completed workspace files move into `completed/` under the configured
workspaces folder and remain listed by name with muted text. Active workspaces
appear first, followed by completed workspaces; each group is sorted by modified
date, newest first. Completion preserves repository paths, branch settings,
and the workspace's modified date.

**Delete** permanently removes the workspace file after confirmation. It does
not delete repositories or branches.

## Data sources

**Settings > Data sources...** opens a modal table with independent **ADO** /
**GitHub** selectors for **Board**, **Repositories**, **Pipelines**, and
**NuGet feed**. All four default to ADO, preserving the existing behavior.
**Save and restart** persists the selections locally in
`data/data_sources.json` and restarts the app; **Cancel** changes nothing.
Selections remain fixed within a process so a running batch cannot change
providers midway through an operation.

GitHub is a placeholder, not a working integration yet. Using a GitHub source
shows **"not implemented yet"** and stops that source's operation without
network requests. Notifications are coalesced once per source per app session,
including concurrent/background work. Unsupported PR/pipeline polling stops
instead of repeatedly retrying. Startup NuGet cache prewarming is skipped for
an unavailable feed source. Local Git operations, browser URL construction,
saved pipeline history, and public nuget.org lookups remain provider-independent.

Source responsibilities:

| Source | Features |
|---|---|
| Board | PBI download, work-item/PR links, work-item transitions on PR completion, PBI titles and linked test reports |
| Repositories | PR create/open/status/comments/reviews/policies/draft/completion, stale branch and PR scan/cleanup |
| Pipelines | Runtime parameter discovery/inputs, run/redeploy/monitor, approvals, cancellation/retries, build errors, deployment overview and historical timing data |
| NuGet feed | Private package version lookup, feed checks/authentication and package restore |

### Strategy layout

- `data_sources/contracts.py` defines abstract data-access strategies.
- `data_sources/__init__.py` resolves strategies from the active settings and queues
  unsupported-source notifications without calling Tk from worker threads.
- `data_sources/ado/` contains the existing ADO implementations, separated into
  `board.py`, `repositories.py`, `pipelines.py`, and `nuget.py`.
- `data_sources/github.py` implements the same contracts with explicit unsupported
  results. Future GitHub implementations belong here (or in separate GitHub
  strategy modules), not in the rendering code.
- Existing entry points in `pbi.py`, `gitutils.py`, `pipelines.py`,
  `packages.py`, and `stale_items.py` are compatibility facades. Their result
  shapes remain unchanged; local parsing, filesystem/Git helpers and rendering
  remain shared. ADO helper exports preserve existing cache editors and test
  seams; provider-specific helpers are internal to the selected strategies.
- `data_sources/settings.py` owns validated, atomic persistence. Settings remain
  in the application-level `data/` directory, not inside the code package.
  `data_sources/dialog.py` owns the settings modal and main-thread notifications.

Mixed selections are saved independently, but selecting GitHub does not make
that part functional. For example, ADO PR creation can succeed while linking
its work item reports an unsupported GitHub Board warning. PR completion with
work-item transitions requested is blocked before mutation if the Board
strategy cannot provide that operation.

Run the new offline strategy/settings/UI checks with:

```powershell
python -m unittest test_data_sources
```

## Stale branches and PRs

**Tools > Stale branches and PRs** scans all local Services and NuGet
repositories in parallel (up to eight at once). The modal has two tabs:

- **Branches**: your remote `feature/` branches created more than seven days
   ago, with creation date and commits behind the repository's default branch.
   Creation dates come from branch-creation push records, not commit dates.
   Branches with unavailable creation history are reported under **Errors**.
- **PRs**: all your active pull requests, including drafts, regardless of age.
   Completed and abandoned PRs are excluded. Status uses draft, reviewer and
   merge information; approval does not imply all branch policies passed.

Open links per row, select individual items or all items, then delete remote
branches or abandon PRs. Both row and bulk actions require confirmation.
Local branches are untouched; deleting a remote branch fails if its commit
changed since the scan. Cleanup runs in parallel across repositories.
Successful scans are cached for five minutes; **Refresh** bypasses this cache.
Existing authentication and identity caches are reused. Partial scan and action
failures remain visible under **Errors**.

## Deployment status

Hover over a commit in **View deployment status** to see its message, branch
classification, who triggered the run, and (when available) the deployment
stage's completion date/time and the commit's author date/time. Timestamps are
shown in local time without a timezone label, using responses already fetched by the
window; no extra API requests are needed.

## Configuration (config.json)

Folder locations and exclusions are read from `config.json` (next to the code),
so you can change them without editing Python. If the file is missing or a key
is absent, built-in defaults are used.

```json
{
    "repos_root": "D:/Repositories",
    "nugets_root": "D:/Repositories/Shared",
    "workspaces_root": "D:/Workspaces/features",
    "exclusions": {
        "repos": ["shared", "wiki"],
        "nugets": [],
        "workspaces": []
    }
}
```

- `repos_root` — scanned for repositories (the "Services" tab).
- `nugets_root` — sub-folders shown on the "Nugets" tab (defaults to
  `<repos_root>/Shared` if omitted).
- `workspaces_root` — where `.code-workspace` files are read from / written to.
- `exclusions.repos` / `.nugets` / `.workspaces` — folder/workspace names to hide
  from each list (case-insensitive).

## Local-only configuration (data/secrets.json, data/repo_synonyms.json)

Two files hold machine-specific / sensitive data and are **git-ignored** (never
committed). They live in the `data/` folder alongside their committed `.example`
templates, so a fresh checkout knows the expected shape — copy each one and drop
the `.example` part:

```powershell
Copy-Item data/secrets.example.json data/secrets.json
Copy-Item data/repo_synonyms.example.json data/repo_synonyms.json
```

- `data/secrets.json` — Azure DevOps connection details used by **Create
  workspace from PBI**:
  - `ado_organization_url` — e.g. `https://dev.azure.com/your-org`.
  - `ado_project` — your project name.

  The PAT used to authenticate is **not** stored here — it is reused from the
  `ADO_PAT` environment variable (see *Work item linking* below), falling back to
  the stored Git credential for the org host.
- `data/repo_synonyms.json` — maps each repository folder to the alternative
  names that may appear in a PBI (e.g. `AlgorithmConfiguration` → `algoconfig`,
  `acg`, `ac`). Edit it from **Settings → Repository synonyms…**, or it is
  extended automatically when you map an unrecognised service while creating a
  workspace.

> `data/secrets.json` only holds your org URL and project name; the PAT lives
> solely in the `ADO_PAT` environment variable. Keep both out of version control.

## Create workspace from PBI

On the **Workspaces** tab, **Create workspace from PBI** automates building a
feature workspace from a work item:

1. Enter the PBI number; the work item is downloaded from Azure DevOps.
2. The repositories listed in the PBI's **WBS** section are extracted and mapped
   to local folders via `data/repo_synonyms.json`.
3. Any unrecognised service is shown in red — pick its local folder. The new
   mapping is remembered. You cannot continue while a service is unmapped.
4. Name the workspace (pre-filled from the PBI number and title) and the
   `.code-workspace` file is created for the mapped repositories.
5. A matching `feature/<name>` branch is created off updated master in every
   repository (uncommitted changes are handled per repo, as when creating a
   feature branch manually).

## Work item linking (ADO_PAT)

Creating a pull request links the work item referenced in the branch name
(e.g. `feature/514231_my_change` links work item `514231`). The same `ADO_PAT`
is also reused by **Create workspace from PBI** to download work items. The Git
credential reused for PR creation is normally scoped to **Code** only, so the
Work Items API rejects it. To enable linking and PBI download, provide a Personal
Access Token (PAT) with the **Work Items: Read & write** scope via the `ADO_PAT`
environment variable.

1. In Azure DevOps: **User settings → Personal access tokens → New Token**, with
   scope **Work Items: Read & write**.
2. Set the variable (persists for future sessions):

   ```powershell
   setx ADO_PAT "<your-pat>"
   ```

3. **Restart your shell / bash session** (and the app launched from it). `setx`
   only affects processes started *after* it runs, so an already-open terminal
   will not see the new value.

To confirm it is set in a new shell without revealing it:

```powershell
if ($env:ADO_PAT) { "ADO_PAT is set (len=$($env:ADO_PAT.Length))" } else { "not set" }
```

> A PAT is a secret. Keep it only in the environment variable, never in code or
> committed files, and do not paste or screenshot its value. If it is ever
> exposed, revoke it in Azure DevOps and create a new one.
