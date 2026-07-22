# Ubuntu Install

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
https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.sh
```

Run it:

```bash
curl -fsSL https://raw.githubusercontent.com/dimitriskl/devloop/main/install/devloop.sh | bash
```

The installer asks where to install Dev Loop. Press Enter for `~/devloop`, or
type another path.

Re-run the same command to update an existing install.

The installer does not create command links and does not modify PATH.

## Development checkout

```bash
cd ./bin
./devloop-plan.sh
```

The first launch automatically prepares the checkout-local `.venv`.

## Uninstall

```bash
./install/uninstall-devloop.sh --dir "$HOME/devloop"
```

The source checkout and project data are preserved.

## Verify

Verify prerequisites, then run the installed scripts directly:

```bash
python3 --version
codex --version
git --version
cd "$HOME/devloop/bin"
./devloop.sh --help
./devloop-plan.sh --help
```

## Manual setup

If you prefer to copy the bundle yourself, use [new-pc-setup.md](new-pc-setup.md).
