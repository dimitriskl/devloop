# Repository Guidelines

## Project Structure & Module Organization

This repository contains a portable Dev Loop runner for local PRD and issue-pack workflows. The Python runner lives in `src/devloop/`: `cli.py` orchestrates runs, `issue_pack.py` parses issues, `codex_runner.py` invokes Codex, `state.py` writes loop state, and `worktree.py` handles worktrees.

Wrapper scripts are in `bin/`. Documentation is under `docs/`. Prompt templates, schemas, and presets are in `prompts/`, `schemas/`, and `presets/`. Bundled Codex skills and Codex agent references live in `skills/codex/` and `agents/codex/`. The optional SQL diagnostics MCP server is under `mcp/sql_diagnostics/`.

## Build, Test, and Development Commands

```powershell
.\bin\devloop.ps1 --help
```
Shows runner flags and verifies Python startup.

```powershell
.\bin\devloop.ps1 --prd <prd.md> --issues <issues\README.md> --dry-run --no-worktree
```
Renders prompts without invoking Codex or creating a worktree.

Issue selection skips completed files. `--all` runs only blocked or unfinished
issues; `--start-issue` advances to the next unfinished issue when the requested
one is already done.

```powershell
.\bin\devloop.ps1 --prd <prd.md> --issues <issues\README.md> --no-self-improvement-wiki
```
Runs without the post-run self-improvement wiki update.

Blocked issues are retried automatically at the end with clean Codex attempts.
Use `--blocked-retry-rounds 5` to allow more rounds or `--no-blocked-retry` to
disable this phase.

```powershell
.\install\build-sql-mcp.ps1
```
Builds the optional .NET SQL MCP server.

```powershell
.\install\install-skills.ps1
```
Copies bundled skills and agents globally.

```bash
./install/devloop.sh
```
Installs or updates the portable Dev Loop bundle on Linux/macOS. Re-run to update.

```powershell
.\install\devloop.ps1
```
Installs or updates the portable Dev Loop bundle on Windows. Re-run to update.

## Coding Style & Naming Conventions

Python targets 3.10+. Core workflow, domain, state-machine, and execution-backend modules use the standard library and must not depend on presentation-framework types. The terminal presentation layer uses a pinned Textual version and its required transitive dependencies. Follow the existing style: 4-space indentation, `from __future__ import annotations`, typed signatures, `Path` for filesystem paths, and small dataclasses for structured results. Keep filenames lowercase with underscores. C# MCP code uses nullable reference types and implicit usings.

Apply the [Clean Code summary](https://gist.github.com/wojteklu/73c6914cc446146b8b533c0988cf8d29) throughout the repository: keep functions and components small and single-purpose, use descriptive names, keep configurable data at high levels, prefer dependency injection and polymorphism, avoid flag arguments and hidden side effects, and keep tests readable, independent, and repeatable.

Use enums for closed domain sets such as outcomes, lifecycle states, and built-in roles. Do not compare or persist domain states through scattered hard-coded strings. Give repeated protocol, schema, command, and UI literals named constants; keep a constant beside its owning contract, and place it in a shared constants module only when it is genuinely used across components. Parse external strings into typed values at system boundaries and fail clearly on unsupported values.

## Testing Guidelines

There is no formal Python test suite or linter config. Validate runner changes with `--dry-run --no-worktree` against a small local issue pack, and inspect `.loop.logs`, `README.loop.md`, and `README.loop.state.json` when behavior changes. For SQL MCP changes, run `.\install\build-sql-mcp.ps1`.

## Self-Improvement Wiki

Real runs update `docs/devloop-self-improvement/wiki/` in this bundle with durable lessons. Keep entries short, evidence-backed, and safe to commit. Do not store raw logs, credentials, tokens, connection strings, or one-off debug dumps. Use `--self-improvement-wiki-path` for another bundle-relative location.

## Commit & Pull Request Guidelines

Recent commits use short, direct subjects such as `updates scripts`. Keep subjects concise, action-oriented, and specific; correct typos before committing. Pull requests should describe the affected workflow, list validation commands, note docs updates, and include screenshots or log snippets only when useful.

## Security & Configuration Tips

Do not commit secrets or machine-specific config. `.gitignore` excludes `.env*`, `appsettings.local.json`, virtual environments, Python caches, and .NET build output. Keep SQL diagnostics read-only and base examples on `appsettings.local.example.json`.

## Managed Shell Safety

Do not launch commands through an approval-backed or escalated managed shell in
this repository. On Windows, that execution boundary can take over the user's
visible PowerShell session and leave it at a raw prompt when the managed turn is
interrupted. Keep agent-run commands inside the workspace sandbox.

Do not agent-launch long-running authenticated, installation, release, or real
Codex integration gates. When a required gate needs user-profile credentials or
access outside the sandbox, provide exactly one physical, paste-ready command
for the operator to run in a separate terminal. The command must write a
non-secret result log inside the workspace so the agent can inspect it using
sandboxed read-only commands. Do not use `Start-Process`, detached children, or
hidden background processes as a workaround.
