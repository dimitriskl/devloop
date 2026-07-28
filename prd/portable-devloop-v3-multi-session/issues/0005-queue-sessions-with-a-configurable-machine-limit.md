Label: ready-for-agent

# Queue Sessions with a Configurable Machine Limit

## Target Product

Product: devloop-plan + devloop

Portable session supervisor, Sessions-tab options, user-wide catalog settings,
cross-process scheduling, and worker lifecycle. CodexCLI execution budgets are
out of scope.

## What to build

Apply one machine-wide Portable Session Concurrency Limit across every
interactive application and Plain Mode process. Default the limit to two active
executions, expose it through Options, and represent excess start/resume
requests as visibly queued sessions that advance fairly when capacity becomes
available.

Capacity measures active execution, not tabs or durable sessions. Paused and
waiting-for-input sessions release their slot, while returning to execution must
reacquire capacity atomically. Keep queue status consistent in the catalog,
Sessions tab, session tab, and other application instances.

Covers parent PRD user stories 56–65.

## Acceptance criteria

- [ ] A new installation initializes the user-wide concurrency limit to two active executions.
- [ ] Options displays and atomically updates the concurrency limit with clear validation.
- [ ] Two sessions may execute while a third requested session remains visibly `QUEUED`.
- [ ] A queued session starts automatically when capacity becomes available without another user command.
- [ ] `PAUSED` and `WAITING_FOR_INPUT` sessions release capacity.
- [ ] A waiting session that receives input reacquires capacity or returns to `QUEUED` before execution.
- [ ] Open, hidden, completed, failed, and unavailable tabs do not consume active capacity.
- [ ] Separate Dev Loop application instances observe and obey the same catalog-backed limit.
- [ ] Capacity acquisition and release are transactional under simultaneous process attempts.
- [ ] Queue advancement is deterministic and prevents starvation among older eligible requests.
- [ ] Reducing the limit below current usage does not kill work; it prevents new execution until usage falls.
- [ ] Cross-process tests prove default, option changes, queuing, release on pause/input wait, reacquisition, fairness, and crash-safe capacity recovery.

## Blocked by

- Blocked by [Issue 0002: Resume Pre-PRD Sessions from the Machine Catalog](./0002-resume-pre-prd-sessions-from-the-machine-catalog.md)
- Blocked by [Issue 0003: Create and Lease Distinct Worktrees](./0003-create-and-lease-distinct-worktrees.md)
- Blocked by [Issue 0004: Run and Monitor Concurrent Session Tabs](./0004-run-and-monitor-concurrent-session-tabs.md)

## User stories addressed

- User stories 56–65

## Implementation Notes

Completed: [x]

- Implemented in commits `11c8b80` through `a7972b7`.
- The catalog-backed scheduler enforces the user-wide default/configured capacity
  transactionally across application processes, advances eligible queued sessions fairly,
  preserves running work when the limit is reduced, and recovers abandoned capacity after
  a process crash.
- Paused and input-waiting sessions release capacity; input submission reacquires a slot or
  leaves the session visibly queued. Sessions, session details, Options, and Plain Mode use
  the same queue/capacity state and labels.
- Final review passed for `9a522a7..a7972b7`. Final QA passed 107 focused `unittest`
  tests plus compile and diff checks, covering scheduling, fairness, capacity, input,
  Options, Plain Mode, migration, cross-process behavior, crash recovery, and labels.
- Environment limitation: pytest, Ruff, and mypy were unavailable, so the focused
  standard-library `unittest` suite was used as the executable fallback.
- Stale-application recovery remains excluded here and belongs to Issue 0007.
