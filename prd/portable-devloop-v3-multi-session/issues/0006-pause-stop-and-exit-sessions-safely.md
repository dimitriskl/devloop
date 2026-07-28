Label: ready-for-agent

# Pause, Stop, and Exit Sessions Safely

## Target Product

Product: devloop-plan + devloop

Portable session lifecycle, supervisor/worker control protocol, durable
checkpoints, Application Shell actions, and coordinated shutdown. CodexCLI Stop
Actions and Workflow Runs are out of scope.

## What to build

Deliver explicit lifecycle controls for live v3 sessions. Pause must stop new
scheduling, let the active operation reach a durable checkpoint, transition
through `PAUSING` to `PAUSED`, and stop the worker without losing resumable
context. Force Stop must remain a separate deliberate action that may interrupt
the active operation while preserving partial filesystem work, diagnostics, and
the last confirmed checkpoint.

Keep Cancel, tab close, and application exit distinct. Closing a tab hides only
its view. Application exit asks once, coordinates safe pause across every live
session, persists outcomes, stops all workers, and leaves no detached daemon.

Covers parent PRD user stories 29–40.

## Acceptance criteria

- [ ] Pause stops scheduling new work and transitions the owning session through `PAUSING` to `PAUSED`.
- [ ] A cooperative pause records the latest durable planning or issue-workflow checkpoint before its worker stops.
- [ ] Paused sessions release execution capacity and retain their worktree association for explicit Resume.
- [ ] Resume continues from the persisted checkpoint without repeating completed roles or steps.
- [ ] Force Stop is a separate confirmed action that may interrupt the active operation immediately.
- [ ] Force Stop preserves partial filesystem changes, captured diagnostics, and the last durable checkpoint without claiming interrupted work completed.
- [ ] Cancel records `CANCELLED` through an explicit action and is not triggered by tab close or ordinary application navigation.
- [ ] Closing a workflow tab changes no lifecycle state and sends no pause, stop, or cancel command.
- [ ] Application exit presents one aggregate confirmation for all running sessions.
- [ ] Confirmed exit safely pauses sessions where possible, persists their latest state, stops every worker, and leaves no detached process.
- [ ] Cancelling the exit returns to the unchanged running application.
- [ ] Supervisor-level tests cover pause during idle and active work, force stop, cancel, tab close, aggregate exit, resume, and partial-work preservation.

## Blocked by

- Blocked by [Issue 0004: Run and Monitor Concurrent Session Tabs](./0004-run-and-monitor-concurrent-session-tabs.md)

## User stories addressed

- User stories 29–40

## Implementation Notes

Completed: [x]

- Implemented in commits `05d1866` through `fa90d97`.
- Cooperative pause records an authoritative durable checkpoint, releases worker
  execution capacity, and retains the session checkout for exact planning-thread
  or PRD role/pass-cursor resume without replaying completed work.
- Force Stop, explicit Cancel, tab close, and aggregate application exit remain
  distinct. Cleanup retains process-tree ownership until termination is confirmed,
  quarantines late worker outcomes, and preserves partial work and diagnostics.
- Final review passed for `05d1866..fa90d97`. Final QA passed the focused
  supervisor, concurrency, Application Shell, worker, backend, and process-tree
  lifecycle coverage, plus compilation and diff checks.
- Environment limitation: the repository Python environment could not be launched
  from the managed sandbox, so pytest, Ruff, and mypy were not rerun in this
  completion-record step; the final QA used the standard-library unittest fallback.
