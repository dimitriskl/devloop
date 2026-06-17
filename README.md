# Dev Loop

Portable Codex development-loop runner for local PRD and issue packs.

The bundle is designed to be copied to a machine that does not already have a
target project checkout. It contains the runner, prompts, output schemas, copied
Codex skills, copied Claude agent definitions, MCP setup templates, and setup
documentation.

## First Setup On A New PC

Read this first:

- `docs/new-pc-setup.md`

Main prerequisites:

- Python 3.10 or later. The runner will not start without it.
- Codex CLI installed and authenticated.
- Git.
- .NET 10 SDK only when the target repo or SQL MCP needs .NET builds.

## Quick Start

Windows:

```powershell
.\bin\devloop.ps1 `
  --prd E:\path\to\feature-prd.md `
  --issues E:\path\to\issues\README.md `
  --preset .\presets\generic-minimal.json
```

Ubuntu/Linux:

```bash
./bin/devloop.sh \
  --prd /path/to/feature-prd.md \
  --issues /path/to/issues/README.md \
  --preset ./presets/generic-minimal.json
```

The default run processes one pending issue. Add `--all` to continue through
all pending issues in dependency order.

## Documentation

All detailed documentation is under `docs/`:

- `docs/new-pc-setup.md`
- `docs/install-windows.md`
- `docs/install-ubuntu.md`
- `docs/usage.md`
- `docs/worktrees.md`
- `docs/skills-and-agents.md`
- `docs/mcp-setup.md`
- `docs/troubleshooting.md`



