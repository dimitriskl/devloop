# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Dev Loop is a **portable Codex development-loop runner**. A pure-Python orchestrator
(`src/devloop`) drives the **Codex CLI** through three quality gates — coder →
reviewer → QA — for each issue in a local issue pack, until the issue passes or hits
a pass limit. It is designed to be copied to a machine that has *no* target-project
checkout: it carries the runner, prompts, output schema, copied Codex skills, copied
Claude agent definitions, and an optional SQL-diagnostics MCP server.

The runner does not modify a project's git history (no commit/push/merge). It edits
issue Markdown files in place and writes loop state next to the issue index.

## Commands

The runner is invoked through wrapper scripts that set `PYTHONPATH` to `src/` and run
`python -m devloop`. There is **no install step and no third-party Python dependency**
(stdlib only, Python 3.10+).

```powershell
# Run one pending issue (Windows)
.\bin\devloop.ps1 --prd <prd.md> --issues <issues/README.md>

# Run every pending issue in dependency order
.\bin\devloop.ps1 --prd <prd.md> --issues <issues/README.md> --all

# Start from a specific issue, use current worktree, preview prompts only
.\bin\devloop.ps1 --prd <prd.md> --issues <issues/README.md> --start-issue 0004
.\bin\devloop.ps1 --prd <prd.md> --issues <issues/README.md> --no-worktree
.\bin\devloop.ps1 --prd <prd.md> --issues <issues/README.md> --dry-run --no-worktree

# Help
.\bin\devloop.ps1 --help

# Skip the post-run self-improvement wiki update
.\bin\devloop.ps1 --prd <prd.md> --issues <issues/README.md> --no-self-improvement-wiki

# Disable automatic clean retries for blocked issues
.\bin\devloop.ps1 --prd <prd.md> --issues <issues/README.md> --no-blocked-retry
```

`./bin/devloop.sh` is the Linux/macOS equivalent with the same flags.

Other notable flags (see `cli.build_parser`): `--preset` (default
`presets/generic-minimal.json`, relative paths resolved from bundle root),
`--max-passes` (default 3), `--codex` (executable name/path), `--sandbox` (default
`workspace-write`), `--approval-policy` (default `never`), self-improvement wiki
controls (`--self-improvement-wiki-path`, `--self-improvement-max-lessons`,
`--no-self-improvement-wiki`), blocked retry controls (`--blocked-retry-rounds`,
`--blocked-retry-max-passes`, `--no-blocked-retry`), and worktree controls (`--create-worktree`,
`--worktree-path`, `--branch-name`, `--non-interactive`).

There is **no test suite, linter config, or `pyproject.toml`** in this repo — run the
CLI (use `--dry-run` to render prompts without invoking Codex) to validate changes.

### SQL Diagnostics MCP (optional, .NET 10)

```powershell
.\install\build-sql-mcp.ps1                 # dotnet build -c Release
.\install\build-sql-mcp.ps1 -CreateLocalConfig   # also seed appsettings.local.json
```

### Install copied skills/agents globally (optional)

```powershell
.\install\install-skills.ps1   # -> ~/.codex/skills and ~/.claude/agents
```

The runner reads bundled skill/agent copies directly via the preset, so global
installation is convenient but not required for the loop itself.

## Architecture

### The three-gate state machine (`src/devloop/cli.py` → `run_issue`)

This is the core control flow. For each issue, up to `--max-passes` iterations:

1. **coder** runs. If status ≠ `PASS`, the issue is immediately `BLOCKED` (no retry).
2. **reviewer** runs against the diff. On `FAIL`, its `fix_list` (or `findings`)
   becomes the *next* coder pass's input and the loop continues.
3. **qa** runs. On `FAIL`, same feedback-threading back to the coder.
4. All three `PASS` → the issue file is marked completed and the loop returns.

Reaching the pass limit yields `BLOCKED`. The Python side owns sequencing; Codex only
does the work *inside* each gate.

After the selected issues finish, blocked issues are retried one by one. Each
blocked retry uses a fresh `codex exec` attempt, separate retry log filenames,
and only a compact blocker summary in `FIX_LIST` so the new agent does not carry
the full prior context. Defaults are three retry rounds and one pass per retry;
`--no-blocked-retry` disables the phase.

### Module responsibilities (`src/devloop/`)

- **`cli.py`** — argument parsing, worktree resolution, the per-issue gate loop, and
  mapping PRD/issue paths into the chosen worktree.
- **`issue_pack.py`** — parses the issue index README for `[title](file.md)` links,
  derives a 4-digit issue number from filename/title, detects completion via a
  `Completed: [x]` line or Dev Loop `## Implementation Notes` completion marker,
  and selects only unfinished issues (`--all` / `--start-issue`).
  Uses `git rev-parse --show-toplevel` to find the repo root.
- **`codex_runner.py`** — builds the `codex exec` command, pipes the rendered prompt
  via stdin, enforces output via `--output-schema`, and parses the result into a
  `RoleResult` dataclass. Includes Codex-CLI version handling (legacy `-a` flag vs
  `-c approval_policy="..."`, auto-detected from `codex exec --help`) and indefinite
  retry on websocket connection failures (30s delay).
- **`state.py`** — `LoopStateWriter` writes `<index>.loop.state.json` (machine state +
  event log) and `<index>.loop.md` (human task board) next to the issue index after
  every step. `mark_issue_completed` edits the issue file in place: flips
  `Completed: [x]`, checks acceptance-criteria boxes, and appends an
  `## Implementation Notes` section.
- **`self_improvement_wiki.py`** — initializes the Dev Loop bundle's
  self-improvement wiki (`docs/devloop-self-improvement/SCHEMA.md` and
  `docs/devloop-self-improvement/wiki/*`) and writes sanitized run context for
  the post-run compiler.
- **`templates.py`** — `BundleContext.from_file` self-locates the bundle root via
  `parents[2]`, locating `prompts/` and `schemas/`. `Preset` loads role→skills/agents
  mappings. `render_template` does simple `{{KEY}}` substitution (lists render as
  `- item` bullet lines).
- **`worktree.py`** — optionally creates a dedicated implementation worktree
  (`git worktree add -b`); otherwise uses the source repo directly.

### Prompts, schema, and presets — the gate contract

- **`prompts/{coder,reviewer,qa}.md`** are templates with `{{VAR}}` placeholders
  (`REPO_ROOT`, `ISSUE_PATH`, `REQUIRED_DOCS`, `SKILL_PATHS`, `AGENT_PATHS`,
  `FIX_LIST`, `CODER_RESULT`, etc.) filled by `CodexRunner.build_prompt`.
- Every gate must return JSON matching **`schemas/role-result.schema.json`**:
  `status` (`PASS`|`FAIL`|`BLOCKED`), `summary`, `changed_files`,
  `verification_commands`, `findings`, `fix_list`, `residual_risks`.
- **`prompts/self-improvement.md`** uses the same role-result schema for the
  post-run self-improvement compiler. It reads sanitized run context and only the
  necessary supporting files, then updates the bundle wiki with at most the
  configured number of durable lessons.
- **`presets/*.json`** declare `requiredDocs` and, per role, the `skills` and `agents`
  files to inject into that role's prompt. To change what context a gate sees, edit
  the preset — not the prompt template.

### Runner-owned files (do not touch from gate work)

The runner creates and owns, next to the issue index: `.loop.logs/` (per-pass
`*.prompt.md`, `*.stdout.jsonl`, `*.stderr.txt`, `*.last-message.json`),
`README.loop.md`, and `README.loop.state.json`. The prompts explicitly instruct
agents not to delete or modify these.

After non-dry-run completion, the runner also asks Codex to update the Dev Loop
self-improvement wiki in the bundle root. This is reviewable Markdown and should
not contain raw logs, secrets, credentials, or one-off debug dumps. Use
`--no-self-improvement-wiki` when a run must avoid documentation changes.

### Supporting trees

- **`skills/codex/`** — copied Codex skill definitions (TDD, C#/Angular experts, QA,
  reviewer, etc.) referenced by presets.
- **`agents/claude/`** — copied Claude agent `.md` definitions, also referenced by
  presets as supplemental guidance (repository rules and Codex skills take precedence
  on conflict).
- **`mcp/sql_diagnostics/`** — .NET 10 MCP server (`ModelContextProtocol` 1.3.0)
  exposing read-only SQL Server diagnostics tools: `sql_list_connections`,
  `sql_test_connection`, `sql_describe_database`, `sql_run_readonly`,
  `sql_analyze_statement`, `sql_workload_summary`, `sql_table_health`,
  `sql_find_code_usage`. Connections come from `appsettings.local.json` (gitignored);
  `SqlSafetyValidator` enforces read-only access.
- **`mcp/templates/`** — Codex `config.toml` snippets to register the MCP servers.
- **`initial/`** — sample target-repo docs (a "diagnostic-collector" PRD + issue
  pack). Example input data, not part of the runner.

## Conventions and Gotchas

- **Bundle is self-contained and relocatable.** Paths resolve relative to the bundle
  root (`BundleContext`) or the bundle source file — never assume a CWD.
- **Requires `git` and the Codex CLI on PATH.** `issue_pack` and `worktree` shell out
  to git; the runner shells out to `codex exec`.
- **Issue selection is link-driven.** Only issues reachable as `[title](file.md)`
  links from the `--issues` index file are processed, and only if the linked file
  exists on disk.
- The Python source uses `X | Y` unions and `from __future__ import annotations`
  (Python 3.10+).
