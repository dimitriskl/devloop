# Windows Install

Start with the [README Install section](../README.md#install-dev-loop).

## Prerequisites

Install these before running the Dev Loop installer:

- Python 3.10 or later
- Git
- Codex CLI, authenticated with `codex login`

Optional: .NET 10 SDK if the target repository or SQL MCP needs .NET builds.

## Download and run

Download one installer script:

```text
https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.ps1
```

Run it:

```powershell
irm https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.ps1 | iex
```

The installer asks where to install Dev Loop. Press Enter for `C:\devloop`, or
type another path.

Re-run the same command to update an existing install.

The installer does not create command shortcuts and does not modify PATH.

## Development checkout

From a checkout where you edit Dev Loop itself, use the local-only setup:

```powershell
& '.\install\setup-development.ps1'
& '.\bin\devloop-plan.ps1'
```

It creates only `.venv` inside the checkout.

## Uninstall

```powershell
& '.\install\uninstall-devloop.ps1' -InstallDir 'C:\devloop'
```

This removes the runtime and unchanged installed capabilities. It also cleans
up shortcuts and PATH changes made by older installer versions. It preserves
source and project data.

## Verify

Verify prerequisites, then run the installed scripts directly:

```powershell
python --version
codex --version
git --version
Set-Location 'C:\devloop\bin'
.\devloop.ps1 --help
.\devloop-plan.ps1 --help
```

## Manual setup

If you prefer to copy the bundle yourself, use [new-pc-setup.md](new-pc-setup.md).
