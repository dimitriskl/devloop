Label: ready-for-agent

# Run Two Review Instances Through a v2 Workflow

## Parent

[Configurable Workflow Steps PRD](./configurable-workflow-steps.md)

## Target Product

Product: devloop-plan + devloop

Portable `devloop-plan + devloop`. Implement in `cli.py`, `codex_runner.py`,
`state.py`, `statusui.py`, and new portable modules beside them. Do not modify
the separate CodexCLI directories listed in `docs/product-boundaries.md`.

## What to build

Deliver the first end-to-end vertical slice of the configurable workflow model inside the portable issue runner. Define `devloop.portable-workflow/v2` with UUIDv4 instance IDs, unique display names, portable component types, component-owned scopes, outcome transitions, and typed result bindings. Supply a static default containing Development, Security Review, Final Review, and QA, with both review steps backed by the same portable reviewer adapter but retained as independent instances.

Run one Issue through that path using the existing `RoleRunner`/`codex_runner.py` behavior behind portable step adapters. Dispatch by Step Instance ID, persist generic runtime state and attempt records in `*.loop.state.json`, and bind QA to the Final Review result. Replace phase-specific lifecycle status with generic `IN_PROGRESS` plus a separate current Step Instance ID. Store and round-trip the resolved portable workflow and canonical hash. Expose both Review instances independently through the existing console status projection.

Covers parent PRD user stories 16-18, 21-25, 87-93, and 107-110.

## Acceptance criteria

- [x] Portable schema v2 accepts only valid UUIDv4 Step Instance IDs, unique display names, installed portable component types, and component-owned scopes.
- [x] A static Development -> Security Review -> Final Review -> QA workflow completes one Issue successfully.
- [x] Security Review and Final Review use the same portable reviewer adapter but have distinct IDs, prompt sessions, attempt histories, results, and dashboard rows.
- [x] Runtime dispatch resolves a configured instance through the portable component catalog without assuming one instance per type.
- [x] Issue lifecycle stores generic `IN_PROGRESS` separately from the exact current Step Instance ID.
- [x] Generic Step Runtime State and Step Attempt Records are keyed by Step Instance ID and optional Issue identity.
- [x] QA receives the successful Final Review role result through an explicit typed binding.
- [x] The resolved workflow and canonical hash survive a `LoopStateWriter` round trip without semantic changes.
- [x] Schema, runner, state, result-resolution, dashboard, and happy-path behavior have standard-library automated tests.
- [x] No files under the CodexCLI application/domain/persistence/UI/workflow module trees are changed.

## Blocked by

None.

## Implementation Notes

Completed: 2026-07-16T06:10:37

### Changed Files
- `src/devloop/state.py`
- `src/devloop/portable_workflow.py`
- `tests/test_resume.py`
- `tests/test_portable_workflow.py`

### Verification
- `bin/devloop.sh --prd /tmp/devloop-v2-issue-0001-dry-run-final/configurable-workflow-steps.md --issues /tmp/devloop-v2-issue-0001-dry-run-final/configurable-workflow-steps-issues.md --start-issue 1 --dry-run --no-worktree --no-blocked-retry --no-self-improvement-wiki --non-interactive`
- `bin/devloop.sh --prd /tmp/devloop-v2-qa-pass2/configurable-workflow-steps.md --issues /tmp/devloop-v2-qa-pass2/configurable-workflow-steps-issues.md --start-issue 1 --dry-run --no-worktree --no-blocked-retry --no-self-improvement-wiki --non-interactive`
- `git diff --check`
- `git diff --name-only -- src/devloop/application src/devloop/components src/devloop/domain src/devloop/execution src/devloop/persistence src/devloop/ui src/devloop/workflow`
- `python3 -m compileall -q src tests`
- `python3 -m unittest discover -s tests`
- `python3 -m unittest discover -s tests -b`
- `python3 -m unittest tests.test_portable_workflow tests.test_codex_runner tests.test_cli_banners tests.test_resume tests.test_statusui`

### Workflow Step Results

#### Development

State loading now fails closed for unreadable, malformed, or non-object existing state without modifying the file. Portable workflow v2 validation now rejects unknown top-level fields before normalization and hashing. Regression coverage was added for both review findings.

#### Security Review

No blocking correctness, security, data-safety, architecture, or test issues remain. Both prior review findings are resolved and verified.

#### Final Review

No blocking findings remain. The pass-2 fixes fail closed on invalid existing state and reject unknown workflow root fields before normalization and hashing. The configurable four-step workflow, persistence, resume, bindings, prompts, and per-instance dashboard behavior passed focused and full regression verification.

#### QA

All 10 acceptance criteria have sufficient automated or manual coverage. Focused 78-test and full 273-test suites passed; compilation, diff validation, portable dry-run, workflow persistence, and product-boundary checks also passed.
