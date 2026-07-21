Label: ready-for-agent

# Resolve Root Blockers With Five Fair Passes

## Type

AFK

## Target Product

Product: devloop-plan + devloop

Portable dependency scheduler, attempt accounting, loop state, and status
projections only. CodexCLI retry policy is out of scope.

## Parent PRD

[`issues/dependency-aware-issue-scheduling.md`](./dependency-aware-issue-scheduling.md)

## What to build

Add Blocker Resolution after Normal Scheduling has no dependency-ready issue
with unused normal budget. Give each unresolved Dependency-Ready Issue exactly
five additional workflow passes in total. Allocate one additional pass per
issue per Blocker Resolution Round in issue-index order rather than exhausting
one blocker first.

Recompute the graph after every additional pass. If a blocker completes, leave
Blocker Resolution immediately and run newly unlocked normal work before
continuing the round. Persist phase, round cursor, per-issue additional budget,
attempt reservation, result, and exact workflow resume cursor atomically. Do
not automatically retry cancellation or required human input. When all
eligible additional budgets are exhausted, stop nonzero and report root
execution blockers plus every directly or transitively waiting descendant.

## Acceptance criteria

- [ ] Blocker Resolution begins only when no Dependency-Ready Issue has unused normal budget.
- [ ] Each unresolved ready issue receives at most five additional workflow passes across the whole run and all resumes.
- [ ] One pass is allocated per blocker per round in issue-index order; the normal budget is not multiplied inside an additional pass.
- [ ] Dependency readiness is recomputed after every additional pass.
- [ ] A successful blocker immediately returns the scheduler to Normal Scheduling for newly unlocked work.
- [ ] A blocked leaf cannot prevent another ready blocker from receiving its next round-robin pass.
- [ ] Explicit cancellation, approval denial, and required human input are not automatically charged to or retried by Blocker Resolution.
- [ ] Phase, round cursor, reserved attempt, consumed additional passes, and workflow cursor survive interruption and rerun without double charging.
- [ ] Existing blocked sequential-run state without dependency counters enters Blocker Resolution conservatively and preserves completed evidence.
- [ ] Exhaustion stops nonzero and reports the unresolved dependency cut with root blockers and all affected descendants.
- [ ] Interactive and append-only output show `round n/5`, the active blocker, remaining per-issue budget, and newly unlocked work.
- [ ] Automated scenarios cover multiple blockers, a successful first retry, a late fifth-pass success, total exhaustion, interruption, and immediate unlock.

## Blocked by

- Blocked by [Issue 0012: Run Only Dependency-Ready Issues](./0012-run-only-dependency-ready-issues.md)

## User stories addressed

- User stories 12–19
- User stories 25–26
- User story 35
- User story 40

## Implementation Notes

Completed: [x]

Implemented bounded Blocker Resolution with five additional passes per ready
blocker, fair `(passes used, index order)` selection, crash-safe reservations,
non-consuming cancellation/human-input release, immediate normal-work unlock,
and unresolved dependency-cut reporting.

Validation covers multiple blockers, first-pass recovery, fifth-pass recovery,
exhaustion, interruption/reload accounting, cancellation, and descendants that
receive no Codex calls while blocked.
