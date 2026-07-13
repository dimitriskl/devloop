Label: completed
Completed: [x]

## Parent

Local-only PRD: `docs/prd/codexcli-workflow-runner.md`

## What to build

Complete the first Issue through independent code review and QA. Code review must use a fresh read-only real App Server thread and produce evidence-backed structured findings. QA must use a separate fresh verification-only thread, map every acceptance criterion to required checks, preserve source-controlled files, and alone control the transition to Issue completion.

Start this Issue in a fresh Codex context. Do not include the development transcript. Use only structured inputs and `gpt-5.6-sol` with ultra reasoning.

## Acceptance criteria

- [x] `code-review` and `qa` are independently registered issue-scoped components with their approved named typed ports and Step Execution Policies.
- [x] Code review receives only the Issue, Workspace Ref, Implementation Result, relevant diff, repository constraints, and its focused capability profile.
- [x] Review executes read-only and cannot modify the implementation.
- [x] Every Review Finding has a stable ID, severity, disposition, title, rationale, repository evidence, file path/optional line, and acceptance condition.
- [x] Unsupported findings fail result validation; must-fix findings produce `CHANGES_REQUESTED`; advisory-only or empty findings produce `SUCCEEDED`.
- [x] QA receives only the Issue, Workspace Ref, Implementation Result, accepted Review Result, relevant repository state, and its focused capability profile.
- [x] QA may run builds and checks and write ignored output or Run Artifacts but blocks on any unexpected source-controlled change without reverting it.
- [x] Every acceptance criterion maps to at least one required QA Check with typed kind, requirement, status, execution data, evidence, and reason.
- [x] Required failures produce `CHANGES_REQUESTED`; required blocked/skipped/unknown checks produce `BLOCKED`; all required checks passing produces `SUCCEEDED`.
- [x] Only successful QA changes the Issue Status to `COMPLETED` and checkpoints the result.
- [x] Review and QA use different real App Server threads and never receive the development transcript or one another's transcripts.
- [x] Real-backend tests demonstrate one complete development-review-QA path and verify that review and QA do not mutate tracked sources.
- [x] Ruff, mypy, focused tests, and the real-backend slice pass.

## Blocked by

- [Issue 0003: Prepare a workspace and complete development](./0003-prepare-workspace-and-complete-development.md)
