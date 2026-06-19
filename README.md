# Feature Manager

A tkinter GUI for managing the git repositories under `D:/Repositories`.

Run it with:

```powershell
python featureManager.py
```

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
        "repos": ["ibs", "shared", "wiki"],
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

## Local-only configuration (secrets.json, repo_synonyms.json)

Two files hold machine-specific / sensitive data and are **git-ignored** (never
committed). Templates with a `.example` suffix are committed so a fresh checkout
knows the expected shape — copy each one and drop the `.example` part:

```powershell
Copy-Item secrets.example.json secrets.json
Copy-Item repo_synonyms.example.json repo_synonyms.json
```

- `secrets.json` — Azure DevOps connection details used by **Create workspace
  from PBI**:
  - `ado_organization_url` — e.g. `https://dev.azure.com/your-org`.
  - `ado_project` — your project name.

  The PAT used to authenticate is **not** stored here — it is reused from the
  `ADO_PAT` environment variable (see *Work item linking* below), falling back to
  the stored Git credential for the org host.
- `repo_synonyms.json` — maps each repository folder to the alternative names
  that may appear in a PBI (e.g. `AlgorithmConfiguration` → `algoconfig`, `acg`,
  `ac`). Edit it from **Settings → Repository synonyms…**, or it is extended
  automatically when you map an unrecognised service while creating a workspace.

> `secrets.json` only holds your org URL and project name; the PAT lives solely
> in the `ADO_PAT` environment variable. Keep both out of version control.

## Create workspace from PBI

On the **Workspaces** tab, **Create workspace from PBI** automates building a
feature workspace from a work item:

1. Enter the PBI number; the work item is downloaded from Azure DevOps.
2. The repositories listed in the PBI's **WBS** section are extracted and mapped
   to local folders via `repo_synonyms.json`.
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
