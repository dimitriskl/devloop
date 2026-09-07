Label: ready-for-agent

# Issue 0002: Default Delivery to a Complete In-Place Run

## Target Product

Product: devloop-plan + devloop

## What to build

Complete the Portable Direct Run shorthand by making full issue breadth and the
source checkout the default when their controls are omitted. A PRD-only command
must select every unfinished dependency-ready Issue through the existing
scheduler, run directly in the source checkout without a worktree prompt, and
retain bounded, isolated, and compatibility paths through explicit options.

Add the single-issue opt-out and preserve the established start-Issue selection
rules. Keep explicit worktree creation behavior intact and continue accepting
the existing all-issues and no-worktree flags silently. This slice changes
default choices only; it does not redefine dependency eligibility, retries,
resume, review, QA, or workspace finalization.

## User stories covered

11–25, 29–30, and 37.

## Acceptance criteria

- [x] Omitting issue-breadth options selects all unfinished Issues for the existing dependency scheduler.
- [x] Completed Issue files remain skipped and dependency validation still occurs before execution.
- [x] Dependency-Ready Issues, Ready Issue Order, independent progress, waiting descendants, and bounded Blocker Resolution retain their current behavior.
- [x] A new single-issue option selects at most the first unfinished eligible Issue when no start Issue is supplied.
- [x] A start Issue without the single-issue option selects that Issue and later unfinished Issues using the existing matching and ordering rules.
- [x] A start Issue combined with the single-issue option selects only the matched Issue.
- [x] The all-issues and single-issue options are mutually exclusive and invalid combinations fail clearly during argument processing.
- [x] The existing all-issues flag remains silently accepted as an explicit expression of the default.
- [x] Omitting both worktree options always uses the source checkout in interactive and non-interactive modes without showing a worktree prompt.
- [x] Explicit worktree creation remains the only request for isolation and retains current interactive prompts, non-interactive validation, branch sanitation, mapping, transfer, and reuse behavior.
- [x] The existing no-worktree flag remains silently accepted as an explicit expression of the default.
- [x] Existing planning or session launchers may continue passing explicit values that reflect choices already made in their UI.
- [x] Public-entrypoint tests prove the PRD-only full in-place behavior without relying only on internal parser fields or private helper call order.

## Verification

- [x] Run focused public-entrypoint, issue-selection, dependency-scheduler, and worktree tests.
- [x] Exercise default full breadth, explicit all breadth, default single-Issue selection, start-Issue full breadth, and start-Issue single breadth against representative temporary Issue graphs.
- [x] Verify omitted worktree options make no input request in both application and Plain Mode test harnesses.
- [x] Verify explicit worktree creation regression cases continue to pass.
- [x] Run Ruff and strict mypy over the affected packages.

## Blocked by

- [Issue 0001: Infer the Canonical Issue Index for a Portable Direct Run](./0001-infer-canonical-issue-index.md)

## Implementation Notes

Completed: [x]

Verified on 2026-09-07 against the current code and disposable PRD Packages.
See [verification evidence](../verification.md) for the implementation, focused
regressions, static checks, runtime details, and unrelated baseline failure.
