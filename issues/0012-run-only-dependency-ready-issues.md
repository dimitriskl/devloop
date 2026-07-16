Label: ready-for-agent

# Run Only Dependency-Ready Issues

## Type

AFK

## Target Product

Product: devloop-plan + devloop

Portable issue scheduling, loop state, and terminal projections only. Do not
reuse or modify the separate CodexCLI scheduler.

## Parent PRD

[`issues/dependency-aware-issue-scheduling.md`](./dependency-aware-issue-scheduling.md)

## What to build

Execute a complete normal-scheduling path through the validated graph from
Issue 0011. Introduce a deterministic scheduling interface that selects the
first Dependency-Ready Issue in issue-index order. A dependency is satisfied
only by durable full-workflow `COMPLETED` status. An unfinished issue with an
incomplete prerequisite becomes `WAITING_ON_DEPENDENCY`, receives no Codex
call, and spends no normal or retry budget.

When a ready issue blocks or fails, retain its own outcome and continue any
independent ready branch. Recompute readiness after every completed issue so
newly unlocked work runs immediately in authored order. Persist scheduling
state atomically and show ready, running, execution-blocked, and
dependency-waiting issues—including the blocking dependency identities—through
the interactive dashboard, append-only output, task board, and resume path.

## Acceptance criteria

- [ ] A pure deterministic scheduler selects only issues whose direct dependencies are durably `COMPLETED`.
- [ ] Multiple ready issues are selected in issue-index order without inferring new dependencies from that order.
- [ ] `WAITING_ON_DEPENDENCY` is distinct from `BLOCKED` and `FAILED` in typed state, persistence, and presentation.
- [ ] Dependency-waiting issues receive zero fake Codex calls and no attempt-budget changes.
- [ ] A dependency is not satisfied by Development PASS, Review PASS, partial attempts, or unpersisted file changes.
- [ ] If one ready issue blocks, an independent ready issue can complete while descendants of the blocker remain waiting.
- [ ] Completing a prerequisite immediately recomputes readiness and schedules newly unlocked normal work before unrelated escalation.
- [ ] State records the scheduling decision before execution and recovers without duplicating a completed attempt after interruption.
- [ ] Interactive and non-interactive output identify ready counts, waiting counts, execution blockers, and each waiting issue's incomplete dependencies.
- [ ] Existing completed issue evidence and unfinished sequential-run state are interpreted conservatively without re-executing completed issues.
- [ ] Bash and PowerShell wrappers use the same shared scheduling behavior and existing commands remain available.
- [ ] Standard-library tests cover a chain, diamond, multiple roots, independent branches, and mixed completed/blocked/waiting states.

## Blocked by

- Blocked by [Issue 0011: Reject Invalid Issue Dependency Graphs](./0011-reject-invalid-issue-dependency-graphs.md)

## User stories addressed

- User stories 4–11
- User story 24
- User stories 32–38
- User story 40

