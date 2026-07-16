Label: ready-for-agent

## Parent

Local-only PRD: `docs/prd/codexcli-workflow-runner.md`

## What to build

Make interruption recovery a release-grade invariant across every phase. Durable Checkpoints must preserve the exact Issue, component, attempt, workspace, component locks, Context Manifest, App Server thread/turn, completed protocol items, and operation state. Explicit `/resume` must restore that cursor, refuse unsafe drift, continue a valid thread, or offer a transcript-free Recovery Attempt when continuation is impossible.

Start this Issue in a fresh Codex context. Use `gpt-5.6-sol` with ultra reasoning and the real App Server for every executable recovery scenario.

## Acceptance criteria

- [x] Every workflow phase checkpoints before an attempt and after each state transition or completed App Server tool item, without checkpointing each streamed text token.
- [x] Run Events append and flush before atomic snapshot replacement; replay is idempotent and reconstructs the latest valid state.
- [x] A stale Run Lease converts a previously running run into a recoverable paused presentation without starting it.
- [x] An operation active during shutdown becomes `UNKNOWN` and is never automatically replayed.
- [x] `/resume` displays unfinished runs with feature, workflow, step, Issue, status, workspace, last activity, and validation condition.
- [x] Selection validates component locks, workflow hash, Artifact hashes, PRD/Issue hashes, App Server compatibility, repository identity, worktree, branch, HEAD, and source drift before continuation.
- [x] A valid persisted App Server thread resumes the same active attempt; an unavailable thread offers a fresh Recovery Attempt built from locked structured context and never replays a transcript.
- [x] A corrupt or truncated final event is quarantined with an actionable diagnostic while earlier valid state remains usable.
- [ ] The mandatory real-backend scenario creates ten Issues, completes Issues 1 and 2, interrupts QA on Issue 3, restarts, selects `/resume`, and restores Issue 3 QA with Issues 1-2 completed and Issues 4-10 unchanged.
- [x] Recovery never auto-merges, pushes, deletes a branch, removes a worktree, accepts source drift, or replays an unknown command.
- [x] Property tests cover every event/snapshot interruption boundary and terminal-state invariants.
- [ ] Windows and Linux real-backend recovery gates pass.
- [ ] Ruff, mypy, focused tests, and the mandatory release recovery scenario pass.

## Blocked by

- [Issue 0005: Process rework and dependent Issues](./0005-process-rework-and-dependent-issues.md)

## Implementation Notes

Completed: 2026-07-16T22:24:20+03:00 (development scope)

Exact-cursor recovery is implemented across checkpoints, append-only event replay,
lease recovery, source-drift validation, continued App Server threads, and explicit
transcript-free Recovery Attempts. The mandatory ten-Issue gate now creates and
advances its state through the production analysis, publication, workspace,
scheduler, development, review, QA, recovery, and Textual services; it does not seed
the target snapshot.

Sandbox-safe evidence passed with 483 standard-library tests, Python compilation,
shell syntax checks, and whitespace validation. The three unchecked criteria are
operator release gates requiring authenticated App Server access and both Windows
and Linux. They remain publication blockers in `docs/release-checklist-v0.1.0.md`
and are not represented as executed by this development completion marker.
