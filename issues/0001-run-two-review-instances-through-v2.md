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

- [ ] Portable schema v2 accepts only valid UUIDv4 Step Instance IDs, unique display names, installed portable component types, and component-owned scopes.
- [ ] A static Development -> Security Review -> Final Review -> QA workflow completes one Issue successfully.
- [ ] Security Review and Final Review use the same portable reviewer adapter but have distinct IDs, prompt sessions, attempt histories, results, and dashboard rows.
- [ ] Runtime dispatch resolves a configured instance through the portable component catalog without assuming one instance per type.
- [ ] Issue lifecycle stores generic `IN_PROGRESS` separately from the exact current Step Instance ID.
- [ ] Generic Step Runtime State and Step Attempt Records are keyed by Step Instance ID and optional Issue identity.
- [ ] QA receives the successful Final Review role result through an explicit typed binding.
- [ ] The resolved workflow and canonical hash survive a `LoopStateWriter` round trip without semantic changes.
- [ ] Schema, runner, state, result-resolution, dashboard, and happy-path behavior have standard-library automated tests.
- [ ] No files under the CodexCLI application/domain/persistence/UI/workflow module trees are changed.

## Blocked by

None.
