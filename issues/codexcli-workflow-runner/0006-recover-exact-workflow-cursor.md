Label: ready-for-agent

## Parent

Local-only PRD: `docs/prd/codexcli-workflow-runner.md`

## What to build

Make interruption recovery a release-grade invariant across every phase. Durable Checkpoints must preserve the exact Issue, component, attempt, workspace, component locks, Context Manifest, App Server thread/turn, completed protocol items, and operation state. Explicit `/resume` must restore that cursor, refuse unsafe drift, continue a valid thread, or offer a transcript-free Recovery Attempt when continuation is impossible.

Start this Issue in a fresh Codex context. Use `gpt-5.6-sol` with ultra reasoning and the real App Server for every executable recovery scenario.

## Acceptance criteria

- [ ] Every workflow phase checkpoints before an attempt and after each state transition or completed App Server tool item, without checkpointing each streamed text token.
- [x] Run Events append and flush before atomic snapshot replacement; replay is idempotent and reconstructs the latest valid state.
- [x] A stale Run Lease converts a previously running run into a recoverable paused presentation without starting it.
- [ ] An operation active during shutdown becomes `UNKNOWN` and is never automatically replayed.
- [ ] `/resume` displays unfinished runs with feature, workflow, step, Issue, status, workspace, last activity, and validation condition.
- [ ] Selection validates component locks, workflow hash, Artifact hashes, PRD/Issue hashes, App Server compatibility, repository identity, worktree, branch, HEAD, and source drift before continuation.
- [ ] A valid persisted App Server thread resumes the same active attempt; an unavailable thread offers a fresh Recovery Attempt built from locked structured context and never replays a transcript.
- [ ] A corrupt or truncated final event is quarantined with an actionable diagnostic while earlier valid state remains usable.
- [ ] The mandatory real-backend scenario creates ten Issues, completes Issues 1 and 2, interrupts QA on Issue 3, restarts, selects `/resume`, and restores Issue 3 QA with Issues 1-2 completed and Issues 4-10 unchanged.
- [ ] Recovery never auto-merges, pushes, deletes a branch, removes a worktree, accepts source drift, or replays an unknown command.
- [ ] Property tests cover every event/snapshot interruption boundary and terminal-state invariants.
- [ ] Windows and Linux real-backend recovery gates pass.
- [ ] Ruff, mypy, focused tests, and the mandatory release recovery scenario pass.

## Implementation progress

### Developed

- Persisted typed `OperationState` and `OperationStatus` on the Workflow Run snapshot.
- Added App Server `item/started` and `item/completed` callbacks without checkpointing streamed text deltas.
- Analysis, development, review, and QA now checkpoint active and completed App Server item state.
- Stale `RUNNING` leases load as a non-starting `PAUSED` presentation; an interrupted running operation projects as `UNKNOWN`.
- Added a typed `RecoveryService` with recovery candidates, plans, dispositions, and validation conditions.
- Recovery validation covers workflow and component locks, package and Artifact hashes, repository/worktree identity, branch, and HEAD.
- Ruff and configured mypy pass for the new architecture. Focused persistence/App Server/protocol tests pass (`9 passed`).

### Still required

- Complete item/state checkpoint coverage for every phase, including finalization and every resume/recovery path.
- Make every resume path refuse automatic replay of `UNKNOWN` operations.
- Connect `/resume` to `RecoveryService` and display feature, workflow, step, Issue, status, workspace, last activity, and validation condition.
- Validate App Server compatibility and persisted thread availability before continuation.
- Execute the transcript-free fresh Recovery Attempt when the original thread is unavailable.
- Finish source-drift validation without confusing intended in-progress development changes with external drift.
- Surface actionable corrupt-event quarantine diagnostics.
- Add event/snapshot interruption property tests and terminal-state invariants.
- Add and run the mandatory real ten-Issue interruption scenario on Windows; run the corresponding Linux gate in CI or a Linux environment.
- Run the full focused, recovery, and release gates before changing this Issue label to `completed`.

## Blocked by

- [Issue 0005: Process rework and dependent Issues](./0005-process-rework-and-dependent-issues.md)
