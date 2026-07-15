# Dev Loop Product Boundaries

This repository contains two independent terminal applications. A feature or
bug must name its target product before planning and implementation begin.
Portable PRDs and issues declare `Product: devloop-plan + devloop` as the first
content line under `## Target Product`; the runner rejects an explicit
`Product: codexcli` declaration before it performs work.

## Portable Dev Loop

**Product name:** `devloop-plan + devloop`

**Entrypoints:**

- `bin/devloop-plan.sh` and `bin/devloop-plan.ps1`
- `bin/devloop.sh` and `bin/devloop.ps1`

**Purpose:** Plan a change into a Markdown PRD and issue pack, then run each
issue through coder, reviewer, and QA roles in Bash or PowerShell.

**Owning modules:**

- `src/devloop/interactive_runner.py`
- `src/devloop/chat_loop.py`
- `src/devloop/cli.py`
- `src/devloop/codex_runner.py`
- `src/devloop/issue_pack.py`
- `src/devloop/state.py`
- `src/devloop/statusui.py`
- `src/devloop/worktree.py`

**State:** PRD-local `devloop.status.json`, `devloop.status.md`,
`*.loop.state.json`, `*.loop.md`, and `.loop.logs/`.

**Resume:** Startup **Resume an unfinished PRD**, planning `/resume`, explicit
`--prd`, or rerunning the same `devloop` command. Resume continues PRD/issue
execution state; it does not use CodexCLI Workflow Runs.

## CodexCLI

**Product name:** `codexcli`

**Entrypoints:** Installed `codexcli doctor`, `codexcli run`, and
`codexcli-gate` commands from `pyproject.toml`.

**Purpose:** A separate Textual workflow application built around Codex App
Server, typed workflow snapshots, component locks, and its own launcher.

**Owning modules:**

- `src/devloop/entrypoint.py`
- `src/devloop/application/`
- `src/devloop/components/`
- `src/devloop/domain/`
- `src/devloop/execution/`
- `src/devloop/persistence/`
- `src/devloop/ui/`
- `src/devloop/workflow/`

**State:** Project/user CodexCLI data including `.devloop/runs/`, as documented
in `codexcli-user-guide.md`.

**Resume:** The CodexCLI launcher `/resume` command validates and resumes its
own Workflow Runs. It cannot resume a portable Dev Loop PRD issue pack.

## Planning Rule

Planning started by `devloop-plan` targets **Portable Dev Loop** by default.
Every generated PRD and issue must include a `Target Product` section naming
`devloop-plan + devloop` and its relevant owning modules. It must not prescribe
CodexCLI `RunStore`, Textual UI, App Server Workflow Runs, or CodexCLI domain
modules unless the user explicitly requests work on `codexcli`.

As a final guard, the portable `devloop` runner refuses a PRD or linked issue
whose `Target Product` section explicitly targets `codexcli` without naming
`devloop-plan + devloop`. The portable `/resume` catalog also omits explicit
CodexCLI PRDs. Older portable artifacts without this section remain accepted.

Conversely, CodexCLI work must not claim that changing its launcher or Workflow
Run model changes the behavior of `devloop-plan.sh`, `devloop-plan.ps1`,
`devloop.sh`, or `devloop.ps1`.
