# Changelog

## Unreleased

### Breaking: portable workflow schema `devloop.portable-workflow/v3`

Every agent-backed Workflow Step now persists an Execution Backend alongside its
model, reasoning effort, and Fast preference. The per-step settings type is
Step Execution Settings, persisted under the step's `execution_settings` key, and
the portable workflow schema is `devloop.portable-workflow/v3`. Schemas v1 and v2
are both rejected explicitly; there is no migration, compatibility reader, or
dual-write path. Finish in-flight work before upgrading.

- **A saved Workflow Default created before this change must be recreated.**
  Open `/options`, choose `reset-workflow`, then `apply`, and reapply your
  per-step choices.
- **An unfinished Workflow Run created before this change cannot be resumed.**
  Its PRD-local `*.loop.state.json` holds a v2 resolved workflow. Finish the run
  with the previous version, or delete that loop-state file to start the PRD
  again from its first Workflow Step. Repository changes are untouched.

Both are reported as actionable messages naming the remedy, not stack traces.

Also in this change:

- Enabling Fast is now rejected with a clear message when the selected model
  advertises no Fast support, instead of being accepted and ignored.
- The `/options` Selection Preview shows each Workflow Step's Execution Backend
  ahead of its model, in both the read-only Current Run and the editable
  Workflow Default scopes.
- Component Execution Defaults supply per-role defaults for each Execution
  Backend. The existing Codex per-role model and reasoning-effort defaults are
  unchanged, and produced Codex agent invocations are unchanged.
- No Execution Backend selection is offered yet: the Claude Code Backend is
  declared and displayed but cannot execute an attempt in this change.

## v0.1.0 - 2026-07-17

Initial hackathon release of the installable `codexcli` workflow runner.

- Runs the standard analysis, workspace preparation, development, code review,
  QA, and local finalization workflow through the installed Codex App Server.
- Publishes accepted PRD Packages and schedules dependency-ready Issues.
- Uses fresh role threads, typed artifacts, bounded rework, explicit approvals,
  project-local recovery, and a structured final Handoff Summary.
- Provides the Textual launcher, Composer, Slash Commands, Issue Board, status
  bar, capability profiles, pause/cancel controls, and explicit resume.
- Leaves repository publication and workspace cleanup entirely explicit.

Known limitations and release-gate instructions are documented in
`docs/release-checklist-v0.1.0.md`.
