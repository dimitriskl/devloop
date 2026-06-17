# New PC Setup

This guide sets up the Dev Loop bundle on a new machine.

## 1. Copy The Bundle

Copy the whole `devloop` folder to the new PC, for example:

```text
C:\Tools\devloop
```

Keep the folder structure unchanged.

## 2. Install Python

Install Python 3.10 or later.

Windows check:

```powershell
python --version
```

Ubuntu check:

```bash
python3 --version
```

If Windows opens the Microsoft Store instead of printing a version, install
Python from python.org and disable the Windows App Execution Alias for Python.

## 3. Install Codex

Install Codex CLI and sign in.

Check:

```powershell
codex --version
```

The same command should work on Ubuntu/Linux.

## 4. Install Git

Check:

```powershell
git --version
```

The target project must be a Git checkout because the runner uses Git worktrees.

## 5. Optional: Install .NET 10 SDK

Install .NET 10 SDK if the target project is .NET or if you want the optional
SQL diagnostics MCP.

Check:

```powershell
dotnet --version
```

## 6. Install Skills And Agents

From the `devloop` folder:

Windows:

```powershell
.\install\install-skills.ps1
```

Ubuntu/Linux:

```bash
chmod +x ./install/*.sh
./install/install-skills.sh
```

This copies bundled Codex skills and Claude agents to the user's home folders.

## 7. Optional: Configure SQL MCP

Only do this if you need SQL diagnostics.

Windows:

```powershell
.\install\build-sql-mcp.ps1 -CreateLocalConfig
```

Then edit:

```text
mcp\sql_diagnostics\appsettings.local.json
```

Use read-only SQL credentials. Do not copy real credentials into shared docs.

## 8. Test The Runner

Windows:

```powershell
.\bin\devloop.ps1 --help
```

Ubuntu/Linux:

```bash
chmod +x ./bin/devloop.sh
./bin/devloop.sh --help
```

## 9. Run A Dry Run

Use dry-run first. It creates prompts and state files but does not call Codex.

```powershell
.\bin\devloop.ps1 `
  --prd E:\repo\docs\feature\prd.md `
  --issues E:\repo\docs\feature\issues\README.md `
  --no-worktree `
  --dry-run
```

If the dry run works, remove `--dry-run` and run the real loop.

