Label: ready-for-agent

# Resume Pre-PRD Sessions from the Machine Catalog

## Target Product

Product: devloop-plan + devloop

Portable session supervision, user-wide planner state, planning chat resume,
project-local loop-state discovery, and the Sessions tab. CodexCLI persistence
is out of scope.

## What to build

Make the single v3 session durable from worktree selection onward. Add the
machine-local Portable Session Catalog, store saved-project and session
identity, and persist enough pre-PRD planning state to close and reopen Dev Loop,
select the unfinished session, and resume the exact planning thread and settings.

When a PRD exists, change catalog ownership from planning state to a bounded
pointer and summary over the existing authoritative project-local loop state.
Show saved projects and independent Portable Resume Candidates in the Sessions
tab without copying or rewriting their workflow history.

Covers parent PRD user stories 15–20, 48–55, 95, and 99.

## Acceptance criteria

- [ ] The Portable Session Catalog uses Python's standard SQLite support in the platform-native user state location.
- [ ] Catalog schema version and migration state fail clearly when unsupported or corrupt.
- [ ] Selecting a worktree creates a stable session record before the first planning turn starts.
- [ ] The session record persists its selected checkout, lifecycle status, planning thread identity, settings snapshot, and bounded activity timestamps.
- [ ] After application restart, the Sessions tab lists the pre-PRD session without starting its worker.
- [ ] Explicit Resume continues the exact saved planning thread with its snapshotted settings.
- [ ] Saved projects remain available for starting new work even when they contain no unfinished PRD.
- [ ] Multiple unfinished PRDs within one saved project appear as independent Portable Resume Candidates.
- [ ] After PRD publication, project-local status and issue files remain authoritative and the catalog stores only pointers and bounded summaries.
- [ ] Catalog writes are transactional and a failed write preserves the previous readable state.
- [ ] Catalog records exclude raw logs, credentials, complete command streams, and unredacted provider payloads.
- [ ] Catalog and Sessions-tab behavior is tested through restart, pre-PRD resume, PRD-backed discovery, corruption, and transaction-failure scenarios.

## Blocked by

- Blocked by [Issue 0001: Launch One Isolated Session from the Sessions Tab](./0001-launch-one-isolated-session-from-the-sessions-tab.md)

## User stories addressed

- User stories 15–20
- User stories 48–55
- User story 95
- User story 99

## Implementation Notes

Completed: [x]

- Independent code review passed the complete Issue 0002 implementation range
  `28f8b6b..53b6cf0`, including all catalog, resume-integrity, settings-validation,
  deduplication, and atomic-initialization fixes through `53b6cf0`.
- Independent QA passed 131 focused tests; 48 Plain Mode and redirected-output
  regressions passed with 1 skip; compilation, diff, and product-boundary checks
  passed. The full sandbox unittest run executed 940 tests: 926 passed, 11 skipped,
  and 3 unchanged failures were confirmed unrelated to Issue 0002.
- The repository virtual environment was unavailable, so pytest, Ruff, and mypy
  could not run. Validation used the sandbox Python interpreter and the
  standard-library unittest suite.
- Coordinated shutdown while a planning worker is waiting for input is deferred
  to [Issue 0006](./0006-pause-stop-and-exit-sessions-safely.md), which owns safe
  pause, worker shutdown, and resumable-checkpoint behavior.
