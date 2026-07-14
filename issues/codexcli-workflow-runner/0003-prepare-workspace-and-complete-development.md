Label: completed

## Parent

Local-only PRD: `docs/prd/codexcli-workflow-runner.md`

## What to build

Extend an accepted analysis run through explicit workspace preparation and one real development attempt. The workflow must ask before using the current checkout or creating a dedicated branch/worktree, select the first dependency-ready Issue, build a minimal Context Manifest, execute development through a fresh real App Server thread, and persist a validated Implementation Result without marking the Issue complete.

Start this Issue in a fresh Codex context. Do not carry the analysis or prior implementation transcript. Use `gpt-5.6-sol` with ultra reasoning.

## Acceptance criteria

- [x] `prepare-workspace` is a workflow-scoped component with typed repository, PRD, IssueSet, and Workspace Ref ports.
- [x] The user explicitly chooses current checkout or a dedicated branch/worktree, sees the proposed paths and branch, and may cancel before Git changes.
- [x] No workspace operation implicitly merges, pushes, deletes a branch, removes a worktree, or modifies an unrelated checkout.
- [x] Component, workflow, capability, port, transition, data-contract, package-hash, PRD-hash, Issue-hash, and workspace validation completes before development.
- [x] The scheduler selects the first dependency-ready Issue in stable IssueSet order.
- [x] Development starts a fresh real App Server thread containing only the current Issue, relevant PRD sections, repository constraints, focused capability profile, workspace identity, and any immediate Rework Request.
- [x] Development runs with workspace-write permissions and records approvals through the shared typed approval boundary.
- [x] The Implementation Result records attempt identity, base/result state, diff hash, changed files and Change Kinds, criterion Implementation Statuses, commands, redacted evidence, rework resolutions, assumptions, and risks.
- [x] Development yields success only when every criterion is implemented and every rework item is resolved or evidence-backed not applicable.
- [x] Development success advances to code review but does not mark the Issue completed.
- [x] Pause and explicit resume restore the same development Issue, attempt, worktree, Context Manifest, and real App Server thread when available.
- [x] Tests exercise both workspace choices in temporary Git repositories and a real development attempt without importing legacy orchestration.
- [x] Ruff, mypy, focused tests, and the real-backend slice pass.

## Blocked by

- [Issue 0002: Publish a resumable analysis PRD Package](./0002-publish-resumable-analysis-prd-package.md)
