Label: ready-for-agent

# Run Two Review Instances Through a v2 Workflow

## Parent

[Configurable Workflow Steps PRD](./configurable-workflow-steps.md)

## What to build

Deliver the first end-to-end vertical slice of the configurable workflow model. Define a schema-v2 workflow whose steps have UUIDv4 instance IDs, unique display names, installed component types, component-owned scopes, outcome transitions, and typed bindings. Use a static workflow containing Development, Security Review, Final Review, and QA, with both review steps backed by the same Review component but retained as independent instances.

Run one Issue through the successful path. Dispatch by Step Instance ID, persist generic runtime state and attempt records, and bind QA to the Final Review output. Replace phase-specific lifecycle status with generic `IN_PROGRESS` plus a separate current Step Instance ID. Store and round-trip the resolved run snapshot and canonical hash. Expose enough read-only workflow progress for both Review instances to be selected and inspected independently.

Covers parent PRD user stories 16-18, 21-25, 87-93, and 107-110.

## Acceptance criteria

- [ ] Schema v2 accepts only valid UUIDv4 Step Instance IDs, unique display names, installed component types, and component-owned scopes.
- [ ] A static Development -> Security Review -> Final Review -> QA workflow completes one Issue successfully.
- [ ] Security Review and Final Review use the same component type but have distinct IDs, attempt histories, artifacts, and selectable views.
- [ ] Runtime dispatch resolves a configured instance through the component registry without assuming one instance per component type.
- [ ] Issue lifecycle stores generic `IN_PROGRESS` separately from the exact current Step Instance ID.
- [ ] Generic Step Runtime State and Step Attempt Records are keyed by Step Instance ID and optional Issue identity.
- [ ] QA receives the successful Final Review artifact through an explicit typed binding.
- [ ] The resolved workflow snapshot and canonical hash survive a RunStore round trip without semantic changes.
- [ ] Schema, runtime, persistence, artifact-resolution, and happy-path behavior have automated tests.

## Blocked by

None.
