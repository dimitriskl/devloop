Label: ready-for-agent

## Parent

Local-only PRD: `docs/prd/codexcli-workflow-runner.md`

## What to build

Turn the single-Issue path into the complete sequential outcome-driven scheduler. Normalize must-fix review findings and failed QA checks into minimal Rework Requests, create fresh development-review-QA attempts for corrections, enforce bounded retry policies, continue independent ready Issues when one blocks, and process dependency-ordered IssueSets without leaking cross-Issue context.

Start this Issue in a fresh Codex context. Use only the approved contracts and outcomes from the preceding slices with `gpt-5.6-sol` and ultra reasoning.

## Acceptance criteria

- [x] Review and QA emit the shared versioned Rework Request containing only required finding/check IDs, evidence, expected behavior, and acceptance conditions.
- [x] `CHANGES_REQUESTED` creates a fresh development attempt followed by fresh review and QA attempts; no attempt reuses another attempt's thread.
- [x] Retry limits come from the versioned Workflow Definition and cannot become an unbounded hard-coded loop.
- [x] A blocked Issue allows other dependency-ready Issues to continue, then leaves the Workflow Run explicitly blocked/pausable for user-directed retry.
- [x] Failed attempts follow the approved terminal/reset policy and never masquerade as blocked or completed.
- [x] An Issue becomes ready only when every dependency is completed; missing, blocked, or failed dependencies remain visible reasons for pending state.
- [x] The scheduler processes one attempt at a time in stable IssueSet order and never shares mutable state between components.
- [x] Context Manifests exclude unrelated Issues, transcripts, raw logs, event history, and reasoning for every development, review, QA, and retry attempt.
- [x] Aggregated implementation, review, and QA result sets contain only completed issue-scoped output references and hashes.
- [x] The Issue Board projection accurately reflects pending, ready, active, changes-requested, blocked, failed, and completed Issues throughout rework.
- [x] Real-backend coverage demonstrates both review-requested and QA-requested rework and a dependency chain with multiple Issues.
- [x] Property tests cover transition invariants, retry bounds, dependency graphs, and illegal lifecycle/outcome combinations.
- [x] Ruff, mypy, focused tests, and the real-backend slice pass.

## Verification evidence

- Real review-requested rework: passed with a typed four-field Rework Request and fresh attempt.
- Real QA-requested rework: passed in 3:17 with source state preserved and fresh attempt.
- Real two-Issue dependency scheduler: passed in 30:51 with six distinct phase threads and completed-only aggregations.
- Full local suite: 209 passed, 7 opt-in real tests skipped; Ruff and mypy passed for the new architecture; source and wheel artifacts built successfully.

## Blocked by

- [Issue 0004: Review and QA an Issue to completion](./0004-review-and-qa-an-issue.md)
