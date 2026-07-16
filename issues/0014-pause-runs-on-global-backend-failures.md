Label: ready-for-agent

# Pause Runs on Global Backend Failures

## Type

AFK

## Target Product

Product: devloop-plan + devloop

Portable Codex event classification, scheduler control, durable loop state, and
terminal output only. Do not modify CodexCLI App Server recovery.

## Parent PRD

[`issues/dependency-aware-issue-scheduling.md`](./dependency-aware-issue-scheduling.md)

## What to build

Deliver run-wide pause behavior through the portable Codex execution path.
Classify exhausted usage, invalid authentication, and backend service
unavailability as Run-Wide Blockers rather than issue outcomes. On detection,
atomically pause the whole scheduler before the current issue is charged, keep
all other issues untouched, and stop issuing Codex calls.

Persist a safe pause reason plus the exact issue, Workflow Step, pass,
scheduling phase, round cursor, and remaining budgets. Rerunning the same
command must retry or resume that exact work after backend recovery. Interactive
and append-only output must distinguish a run-wide pause from issue BLOCKED or
FAILED and explain the operator action without exposing credentials or raw
provider data.

## Acceptance criteria

- [ ] Usage-limit, authentication, and service-unavailable Codex events are classified as typed Run-Wide Blockers.
- [ ] Ordinary repository-command failures, test failures, review findings, and issue-specific blockers are not misclassified as run-wide.
- [ ] A Run-Wide Blocker stops all further issue scheduling immediately.
- [ ] The active issue receives no BLOCKED/FAILED role result and spends no normal or additional pass budget.
- [ ] Waiting and independent issues remain untouched and receive zero subsequent Codex calls during the paused run.
- [ ] Durable state preserves a redacted pause reason, issue, Step Instance, pass, scheduling phase, round cursor, and all remaining budgets.
- [ ] Rerunning the same command resumes the exact paused work after the backend becomes available.
- [ ] A repeated run-wide failure updates safe pause evidence without duplicating attempts or spending budget.
- [ ] Interactive, append-only, color, and no-color output clearly show `RUN PAUSED` separately from issue status.
- [ ] Representative JSON event replays cover every run-wide class and nearby non-global failures without a real authenticated Codex call.
- [ ] Bash and PowerShell wrappers reach the same pause/resume behavior.
- [ ] No CodexCLI execution, recovery, persistence, domain, or UI module is changed.

## Blocked by

- Blocked by [Issue 0012: Run Only Dependency-Ready Issues](./0012-run-only-dependency-ready-issues.md)

## User stories addressed

- User stories 27–31
- User story 34
- User story 40

