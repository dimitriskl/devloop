# New PC Setup

This guide expands the [README Install section](../README.md#install-dev-loop).

You do not copy the repository manually unless you choose the manual path at
the end of this guide. The normal install is one downloaded script plus the
prerequisites below.

## Prerequisites

Install these before running the Dev Loop installer:

| Requirement | Check |
| --- | --- |
| Python 3.10+ | `python --version` or `python3 --version` |
| Git | `git --version` |
| Codex CLI, signed in | `codex --version` then `codex login` |

Optional: .NET 10 SDK only if the target repository or SQL diagnostics MCP
needs .NET builds.

If Windows opens the Microsoft Store instead of printing a Python version,
install Python from python.org and disable the Windows App Execution Alias for
Python.

## Quick Install

Download one installer script:

| Platform | URL |
| --- | --- |
| Linux / macOS | https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.sh |
| Windows | https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.ps1 |

Linux and macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.sh | bash
```

Windows:

```powershell
irm https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.ps1 | iex
```

The installer asks where to install Dev Loop. Press Enter to accept the default,
or type another path. It then clones or updates the bundle, copies bundled Codex
skills and agent references, and prepares its isolated runtime. It does not
create command shortcuts or modify PATH.

Defaults when you press Enter:

- Linux/macOS bundle: `~/devloop`
- Windows bundle: `C:\devloop`

Re-run the same command to update an existing install. The installer prompts for
the install directory again; press Enter if it is already at the default path.

Non-interactive install:

```bash
./devloop-install.sh --dir ~/devloop
```

```powershell
.\devloop-install.ps1 -InstallDir C:\devloop
```

Download first instead of piping:

```bash
curl -fsSL https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.sh -o devloop-install.sh
chmod +x devloop-install.sh
./devloop-install.sh
```

```powershell
irm https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.ps1 -OutFile devloop-install.ps1
.\devloop-install.ps1
```

## Verify

Windows:

```powershell
Set-Location 'C:\devloop\bin'
.\devloop.ps1 --help
.\devloop-plan.ps1 --help
```

Linux/macOS:

```bash
cd "$HOME/devloop/bin"
./devloop.sh --help
./devloop-plan.sh --help
```

## Manual Setup

If you prefer to copy the bundle yourself, continue with the steps below.

For a development checkout that must not create shortcuts, edit PATH, install
global skills, or update Git, run `install/setup-development.ps1` on Windows or
`install/setup-development.sh` on Linux/macOS, then invoke the wrapper directly
from `bin/`.

To undo a normal installation, run the platform `install/uninstall-devloop`
script. It removes installer-managed runtime, command, PATH, and unchanged
capability artifacts while preserving source and project data. Command and PATH
cleanup applies to artifacts created by older installer versions.

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

Ubuntu/macOS check:

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

The same command should work on Ubuntu/Linux and macOS.

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

Ubuntu/macOS:

```bash
chmod +x ./install/*.sh
./install/install-skills.sh
```

This copies bundled Codex skills and Codex agent references to the user's home folders.

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

Ubuntu/macOS:

```bash
chmod +x ./bin/devloop.sh
chmod +x ./bin/devloop-plan.sh
./bin/devloop.sh --help
./bin/devloop-plan.sh --help
```

## 9. Run A Dry Run

Use dry-run first. It creates prompts and state files but does not call Codex.

```powershell
.\bin\devloop.ps1 `
  --prd E:\repo\prd\feature\feature.md `
  --issues E:\repo\prd\feature\issues\README.md `
  --no-worktree `
  --dry-run
```

If the dry run works, remove `--dry-run` and run the real loop.
