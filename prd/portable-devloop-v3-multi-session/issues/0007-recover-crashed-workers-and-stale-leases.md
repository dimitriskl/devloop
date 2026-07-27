Label: ready-for-agent

# Recover Crashed Workers and Stale Leases

## Target Product

Product: devloop-plan + devloop

Portable worker supervision, protocol heartbeat, catalog leases, crash
diagnostics, session recovery, and multi-instance safety. CodexCLI recovery
attempts are out of scope.

## What to build

Make worker and application failure recoverable without risking duplicate
execution. Detect unexpected worker exit, retain bounded standard-error
diagnostics, mark only its session `INTERRUPTED`, and require an explicit Resume
or Retry. Never replay an active turn or tool operation automatically.

Renew machine-wide worktree ownership while the worker is healthy. Associate
leases with a stable owner identity, process identity, process-start
fingerprint, and heartbeat. Reclaim an expired lease only after confirming its
owner is dead; treat ambiguous ownership as a visible blocker rather than a
guess.

Covers parent PRD user stories 35–37 and 66–72.

## Acceptance criteria

- [ ] Workers emit a validated heartbeat associated with their exact session and owner identity.
- [ ] Unexpected worker exit marks only the owning session `INTERRUPTED` and leaves sibling sessions running.
- [ ] Bounded standard-error diagnostics are retained and viewable from the interrupted session.
- [ ] An interrupted session is never restarted or replayed automatically.
- [ ] Explicit Resume or Retry creates a fresh worker and uses the last durable checkpoint and preserved partial-work context.
- [ ] Worktree leases record owner identity, process identity, process-start fingerprint, and renewable heartbeat.
- [ ] A healthy worker renews its lease without rewriting unrelated session state.
- [ ] A lease is released only after normal worker stop or confirmed owner death.
- [ ] An expired lease with a confirmed-dead owner is reclaimed transactionally.
- [ ] An ambiguous or unverifiable lease blocks execution, remains inspectable, and offers no unsafe force-reclaim shortcut.
- [ ] Process-ID reuse cannot make a new unrelated process appear to own an old lease.
- [ ] Real multi-process tests cover clean stop, abrupt worker death, application death, stale heartbeat, process-ID reuse, ambiguous ownership, explicit recovery, and sibling isolation.

## Blocked by

- Blocked by [Issue 0003: Create and Lease Distinct Worktrees](./0003-create-and-lease-distinct-worktrees.md)
- Blocked by [Issue 0004: Run and Monitor Concurrent Session Tabs](./0004-run-and-monitor-concurrent-session-tabs.md)
- Blocked by [Issue 0006: Pause, Stop, and Exit Sessions Safely](./0006-pause-stop-and-exit-sessions-safely.md)

## User stories addressed

- User stories 35–37
- User stories 66–72

## Implementation Notes

Completed: [ ]
