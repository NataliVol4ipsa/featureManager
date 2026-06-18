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

## Work item linking (ADO_PAT)

Creating a pull request links the work item referenced in the branch name
(e.g. `feature/514231_my_change` links work item `514231`). The Git credential
reused for PR creation is normally scoped to **Code** only, so the Work Items
API rejects it. To enable linking, provide a Personal Access Token (PAT) with
the **Work Items: Read & write** scope via the `ADO_PAT` environment variable.

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
