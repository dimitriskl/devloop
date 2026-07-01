# Dev Loop

Portable Codex development-loop runner for local PRD and issue packs.

The bundle is designed to be copied to a machine that does not already have a
target project checkout. It contains the runner, prompts, output schemas, copied
Codex skills, Codex agent references, MCP setup templates, and setup
documentation.

## What You Can Run

Dev Loop has two entrypoints:

- `devloop` runs implementation from an existing PRD and local issue pack.
- `devloop-plan` starts from an idea, opens an interactive Codex planning
  session, creates the PRD and issue pack, then offers to start `devloop`.

Use `devloop-plan` when you still need to decide what to build. Use `devloop`
when `prd/<change>/<change>.md` and `prd/<change>/issues/README.md` already exist.

## First Setup On A New PC

Read this first:

- `docs/new-pc-setup.md`

Main prerequisites:

- Python 3.10 or later. The runner will not start without it.
- Codex CLI installed and authenticated.
- Git.
- .NET 10 SDK only when the target repo or SQL MCP needs .NET builds.

## Quick Start: Existing PRD And Issues

Windows:

```powershell
.\bin\devloop.ps1 `
  --prd E:\path\to\prd\feature\feature.md `
  --issues E:\path\to\prd\feature\issues\README.md `
  --preset .\presets\generic-minimal.json
```

Ubuntu/Linux:

```bash
./bin/devloop.sh \
  --prd /path/to/prd/feature/feature.md \
  --issues /path/to/prd/feature/issues/README.md \
  --preset ./presets/generic-minimal.json
```

The default run processes one pending issue. Add `--all` to continue through
all pending issues in dependency order.

If any issue blocks, the runner retries blocked issues at the end with clean
Codex attempts and compact blocker context. Tune this with
`--blocked-retry-rounds`, `--blocked-retry-max-passes`, or disable it with
`--no-blocked-retry`.

After a real run, Dev Loop compiles the most important durable lessons into its
own self-improvement wiki at `docs/devloop-self-improvement/wiki/`. The role
agents read that wiki by default. Use `--no-self-improvement-wiki` to skip both
wiki reading and post-run updates, or `--self-improvement-wiki-path` to choose a
different bundle-relative path.

## Quick Start: Plan Then Build

Windows:

```powershell
.\bin\devloop-plan.ps1 --repo C:\path\to\project
```

Ubuntu/macOS:

```bash
./bin/devloop-plan.sh --repo /path/to/project
```

`devloop-plan` asks for the target checkout. On the first run there is no target
default; after a valid selection it saves that checkout and shows it as the
default on later runs. It then asks whether to use the current branch, create a
new branch, or create a new worktree for planning. If you do not pass `--goal`,
the change request is typed inside Codex, so normal Codex input features such as
arrow-key editing and Alt+V image paste are available when your installed CLI
supports them. The planning session follows this sequence:

1. `$grill-with-docs` sharpens the change through questions and records domain
   terms or ADRs when justified.
2. `$to-prd` writes `prd/<change-name>/<change-name>.md`.
3. `$to-issues` writes `prd/<change-name>/issues/README.md` and numbered issue
   files with real Markdown links.
4. The wrapper detects those paths, asks whether to continue to development,
   and collects start issue, all-issues mode, worktree, branch, and wiki choices.

Development defaults to a dedicated implementation worktree and using the Dev
Loop self-improvement wiki. When a run finishes successfully, the runner asks
whether to merge the implementation branch or worktree into another branch.

The final handoff command is equivalent to:

```powershell
.\bin\devloop.ps1 --prd C:\path\to\project\prd\example\example.md --issues C:\path\to\project\prd\example\issues\README.md --start-issue 0001 --all --create-worktree --worktree-path C:\path\to\project-example-dev --branch-name devloop/example
```

## How Skills Are Used

Skills live under `skills/codex/`. The runner does not require a global install:
it passes the bundled skill paths into Codex prompts so each role can read the
same local instructions on every machine.

The main implementation loop uses `presets/generic-minimal.json`:

- coder: TDD plus C# and Angular/TypeScript development guidance
- reviewer: senior code review guidance
- QA: focused verification guidance

The interactive planning loop uses:

- `grill-with-docs` and `domain-modeling` for design clarification, glossary
  terms, and ADR decisions
- `to-prd` for the canonical local PRD
- `to-issues` for the local Markdown issue pack consumed by `devloop`
- those skill are written by https://github.com/mattpocock
  
Codex agent-reference files live under `agents/codex/`. They are extra role
guides read by the prompts; the canonical automation still comes from the
runner, presets, prompts, and `skills/codex/`.

## Skill Provenance

The engineering workflow skills in `skills/codex/` are bundled local copies so
the runner is portable. Where a skill name or content identifies Matt Pocock's
engineering skill set, including `setup-matt-pocock-skills`, that origin is
preserved in the filename or skill text. Dev Loop-specific runner code, prompts,
presets, SQL diagnostics, wrapper scripts, and local documentation are maintained
in this repository.

## Documentation

All detailed documentation is under `docs/`:

- `docs/new-pc-setup.md`
- `docs/install-windows.md`
- `docs/install-ubuntu.md`
- `docs/install-macos.md`
- `docs/usage.md`
- `docs/interactive-runner.md`
- `docs/worktrees.md`
- `docs/skills-and-agents.md`
- `docs/mcp-setup.md`
- `docs/troubleshooting.md`
