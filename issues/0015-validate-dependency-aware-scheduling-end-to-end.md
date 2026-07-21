Label: ready-for-agent

# Validate Dependency-Aware Scheduling End to End

## Type

AFK

## Target Product

Product: devloop-plan + devloop

Portable Bash/PowerShell wrappers and their shared Python planner, scheduler,
runner, state, and terminal presentation. CodexCLI release gates are out of
scope.

## Parent PRD

[`issues/dependency-aware-issue-scheduling.md`](./dependency-aware-issue-scheduling.md)

## What to build

Close the feature with an integrated dependency-aware run and durable evidence.
Use a representative graph containing a chain, diamond, independent branch,
execution blocker, waiting descendants, and later unlock. Demonstrate normal
scheduling, five-pass round-robin blocker resolution, interruption and exact
resume, and a run-wide backend pause without spending issue budget.

Exercise malformed dependency packs and explicitly selected subsets, verify
interactive and redirected terminal behavior, and update user and maintainer
documentation for dependency declarations, statuses, budgets, pause/resume,
and troubleshooting. Run sandbox-safe tests, compilation, shell syntax, local
dry-run, dependency validation, and whitespace gates. Any credential-dependent
backend smoke must be handed to the operator as one paste-ready command that
writes a non-secret result log; do not launch it from an agent session.

## Acceptance criteria

- [ ] One integrated scenario proves deterministic index priority among ready issues without creating implicit dependencies.
- [ ] The scenario proves a failed branch does not block independent work and its descendants receive zero premature Codex calls.
- [ ] The scenario proves five-pass round-robin blocker resolution, immediate normal-work unlock, and bounded exhaustion reporting.
- [ ] Interruption and rerun preserve the exact issue, Workflow Step, pass, phase, round cursor, attempt history, and remaining budgets.
- [ ] A replayed run-wide backend failure pauses globally without issue outcome or budget mutation, then resumes the same work.
- [ ] Unknown, out-of-pack, duplicate, self, cyclic, and omitted-subset dependencies all fail before a fake Codex call.
- [ ] Task-board, interactive dashboard, append-only output, color, no-color, narrow, wide, Unicode, and ASCII modes distinguish ready, waiting, blocked, and paused states.
- [ ] Documentation explains `Blocked by`, full-completion readiness, index tie-breaking, Normal Scheduling, five additional passes, unresolved dependency cuts, and Run-Wide Blockers.
- [ ] Bash and PowerShell wrappers are verified to enter the same shared scheduler without duplicated scheduling implementations.
- [ ] The complete standard-library suite, Python compilation, Bash syntax, local dry-run, dependency-pack validation, and `git diff --check` pass.
- [ ] Any unavailable PowerShell or authenticated backend gate is reported as operator-only without claiming execution.
- [ ] `git diff --name-only` confirms no CodexCLI application/domain/execution/persistence/UI/workflow implementation file changed.

## Blocked by

- Blocked by [Issue 0013: Resolve Root Blockers With Five Fair Passes](./0013-resolve-root-blockers-with-five-fair-passes.md)
- Blocked by [Issue 0014: Pause Runs on Global Backend Failures](./0014-pause-runs-on-global-backend-failures.md)

## User stories addressed

- User stories 24–26
- User stories 32–34
- User stories 37–40

## Implementation Notes

Completed: [x]

Integrated the dependency scheduler with the portable Bash/PowerShell shared
Python runner, dashboard, append-only output, loop JSON/Markdown state, resume,
bounded exhaustion, and documentation. No CodexCLI implementation module was
changed.

Validation passed with the complete standard-library suite, redirected-bytecode
Python compilation, Bash syntax, repeatable wrapper dry runs, issue-pack graph
validation, and `git diff --check`. PowerShell (`pwsh`) is unavailable on this
Ubuntu host, so no native PowerShell execution is claimed; both wrappers enter
the same verified Python scheduler.
