# Dev Loop

```text
+------------------------------------------------------------+
|  ____  _______     __  _     ___   ___  ____              |
| |  _ \| ____\ \   / / | |   / _ \ / _ \|  _ \             |
| | | | |  _|  \ \ / /  | |  | | | | | | | |_) |            |
| | |_| | |___  \ V /   | |__| |_| | |_| |  __/             |
| |____/|_____|  \_/    |_____\___/ \___/|_|                |
|                                                            |
|    [ ANALYSIS ] => [ BUILD ] => [ REVIEW ] => [ QA ]      |
+------------------------------------------------------------+
```

Portable Codex development-loop runner for local PRD and issue packs.

## Two Separate Applications

This repository contains two applications. They share some source packages but
they do not share an interface, command registry, or run-state format.

| Application | Start command | Interface and state | Resume |
| --- | --- | --- | --- |
| Portable Dev Loop | `devloop-plan.sh` / `.ps1`, then `devloop.sh` / `.ps1` | Line editor and bounded console dashboard; PRD-local `*.loop.state.json` | Startup **Resume an unfinished PRD**, planning `/resume`, or rerun the same `devloop` command |
| CodexCLI | Installed `codexcli run` command | Separate Textual application; `.devloop/runs/` and Codex App Server threads | Its own `/resume` command |

`devloop-plan` is not a launcher or compatibility wrapper for `codexcli`.
Changes requested while running `devloop-plan` target the portable Dev Loop
unless the user explicitly names CodexCLI. See
`docs/product-boundaries.md` before changing workflow architecture.

## Separate Optional Application: CodexCLI v0.1.0

The installable hackathon workflow is available as `codexcli`:

```text
uv tool install .
codexcli doctor --repo /path/to/repository
codexcli run --repo /path/to/repository
```

`pipx install .` is also supported. Codex CLI installation and authentication
are prerequisites; the real Codex App Server is the only executable backend.
See `docs/codexcli-quickstart.md` for the five-minute path and
`docs/release-checklist-v0.1.0.md` for Windows/Linux release gates and known
limitations.

The bundle is designed to be copied to a machine that does not already have a
target project checkout. It contains the runner, prompts, output schemas, copied
Codex skills, Codex agent references, MCP setup templates, and setup
documentation.

Try the standard workflow without rebuilding from scratch:

```bash
uv tool install .
./examples/release-demo/run-demo.sh   # Linux
# .\examples\release-demo\run-demo.ps1  # Windows
```

Submit the request in `examples/release-demo/feature-request.md`. The script
creates a disposable Git repository and opens `codexcli` against it.

## Built with Codex and GPT-5.6

Dev Loop was built and runs through the **Codex CLI / Codex App Server** for
session management, tool use, approvals, and workflow execution. Each workflow
step calls **GPT-5.6** models through Codex; we did not call the OpenAI API
directly.

| Step | Role | CodexCLI (`codexcli`) | Portable Dev Loop (`devloop`) |
| --- | --- | --- | --- |
| Analysis | Planning and PRD | `gpt-5.6-sol` / xhigh | `gpt-5.6-sol` / xhigh |
| Development | Implementation | `gpt-5.6-sol` / xhigh | `gpt-5.6-luna` / high |
| Code review | Independent review | `gpt-5.6-sol` / xhigh | `gpt-5.6-sol` / xhigh |
| QA | Verification | `gpt-5.6-sol` / xhigh | `gpt-5.6-terra` / high |

**Codex** provides orchestration: the App Server protocol, fresh role threads,
permission prompts, `/resume`, and structured workflow state. **GPT-5.6** provides
the reasoning for each step. Models are selected through Codex CLI (`-m
gpt-5.6-sol`, etc.) and locked per component in CodexCLI execution profiles.

During development of this repository, Codex was used for implementation,
refactoring, and release verification. GPT-5.6 models executed analysis,
development, review, and QA turns through the installed Codex App Server.

## What You Can Run

The portable Dev Loop has two entrypoints:

- `devloop` runs implementation from an existing PRD and local issue pack.
- `devloop-plan` starts from an idea and runs one continuous session:
  a Codex-backed planning chat (analysis), then development, review, and QA,
  with the current stage always visible. Paste screenshots with Alt+V.
  No exit or Ctrl+C is ever needed between stages.

Use `devloop-plan` when you still need to decide what to build. Use `devloop`
when `prd/<change>/<change>.md` and `prd/<change>/issues/README.md` already exist.

## First Setup On A New PC

Read this first:

- `docs/new-pc-setup.md`
- `docs/how-to-use.md`

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
all dependency-ready issues. Declare prerequisites as local Markdown links
under an issue's `## Blocked by` heading. Index order is priority among ready
issues; it never creates an implicit dependency.

If a ready issue blocks, its descendants become `WAITING_ON_DEPENDENCY` and
receive no Codex calls while independent ready work continues. When normal
work is exhausted, Blocker Resolution gives each unresolved ready blocker one
additional workflow pass per round, up to five total. Use
`--blocked-retry-rounds` to lower that cap or `--no-blocked-retry` to disable
Blocker Resolution. `--blocked-retry-max-passes` remains accepted for command
compatibility, but an additional attempt always consumes exactly one workflow
pass.

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

The session shows a stage banner (`analysis -> development -> review -> qa`).
At startup choose **Start a new change** or **Resume an unfinished PRD**. Resume
lists only PRD/issue packs with unfinished issues and shows completion counts,
the active issue when known, and last activity. The same catalog is available
through `/resume` during planning. Chat with Codex to sharpen a new change; when
the PRD and issue pack are written, press Enter on the summary screen to start
development. Type `/options` at any prompt to open the Workflow Editor for
future-run defaults, including independent `model`, `reasoning`, and `fast`
choices for each Codex-backed step and a separate `budget` timeout/checkpoint
for every step. Its `capabilities` command still lets you
search and toggle agents and skills for the selected Step Instance, reset that
profile to its component defaults, or install new ones from GitHub. Required
capabilities stay enabled and show the component-contract reason. Use `guidance`
for bounded multiline instructions specific to the selected step; the editor
shows that contracts, execution policy, permissions, safety, and output rules
take precedence. Type `/help` for all commands. The
self-improvement wiki is always used: planning reads it, and every run updates
it.

`devloop-plan` asks for the target checkout. On the first run there is no target
default; after a valid selection it saves that checkout and shows it as the
default on later runs. If the selected folder does not exist, the runner asks
whether to create it, then asks whether to initialize Git in the new folder so it
can be used as a target checkout. It then asks whether to use the current branch,
create a new branch, or create a new worktree for planning. If you do not pass
`--goal`, the change request is typed at the chat prompt; devloop's own line
editor provides arrow-key editing, command history, and Alt+V screenshot paste
regardless of what the installed Codex CLI supports natively. The planning
session follows this sequence:

1. `$grill-with-docs` sharpens the change through questions and records domain
   terms or ADRs when justified.
2. `$to-prd` writes `prd/<change-name>/<change-name>.md`.
3. `$to-issues` writes `prd/<change-name>/issues/README.md` and numbered issue
   files with real Markdown links.
4. The wrapper detects those paths on disk and flips straight to the
   DEVELOPMENT summary screen; there is nothing to exit or quit.

Press Enter on that summary screen to start development immediately with
sensible defaults (all pending issues, a dedicated worktree, the
self-improvement wiki always on). Type `/run-options` to change the start issue,
worktree parent path, worktree folder name, or branch first. Type `/options` to
edit the User Workflow Default for Future Runs and, when resuming an existing
implementation worktree, inspect its immutable Current Run snapshot. When a
run finishes successfully, the runner asks whether to merge the implementation
branch or worktree into another branch.

The final handoff command is equivalent to:

```powershell
.\bin\devloop.ps1 --prd C:\path\to\project\prd\example\example.md --issues C:\path\to\project\prd\example\issues\README.md --all --create-worktree --worktree-path C:\path\to\project-example-dev --branch-name devloop/example --self-improvement-wiki
```

To continue an existing PRD without reopening planning:

```powershell
.\bin\devloop-plan.ps1 --prd C:\path\to\project\prd\example\example.md
```

The runner finds `issues\README.md`, prints the PRD status, and shows the
DEVELOPMENT summary screen (Enter to start, `/run-options` to adjust this
launch, or `/options` to open the Workflow Editor). Dev Loop writes
`devloop.status.json` and `devloop.status.md` in the PRD folder, while keeping
the older `issues\README.loop.state.json` and `issues\README.loop.md` files for
compatibility. Reruns with `all` skip completed issue files. If a run was
interrupted, Dev Loop resumes the unfinished issue at its next coder, reviewer,
or QA gate and preserves the current pass and review/QA fix list. When an
existing implementation worktree is reused, its issue mapping can narrow the
source selection but cannot reintroduce issues already completed in the source
PRD package.

## How Skills Are Used

Skills live under `skills/codex/`. The runner does not require a global install:
it passes the bundled skill paths into Codex prompts so each role can read the
same local instructions on every machine.

The main implementation loop uses `presets/generic-minimal.json`:

- coder: TDD plus C# and Angular/TypeScript development guidance
- reviewer: senior code review guidance
- QA: focused verification guidance

The interactive planning loop uses these skills by default. Type `/options` in
the planning chat, then `capabilities` in the Workflow Editor, to pick a
different set, override per-role agents/skills, or install more from GitHub:

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

- `docs/hackathon-submission.md`
- `docs/how-to-use.md`
- `docs/new-pc-setup.md`
- `docs/install-windows.md`
- `docs/install-ubuntu.md`
- `docs/install-macos.md`
- `docs/usage.md`
- `docs/interactive-runner.md`
- `docs/configurable-workflow.md`
- `docs/worktrees.md`
- `docs/skills-and-agents.md`
- `docs/mcp-setup.md`
- `docs/troubleshooting.md`
