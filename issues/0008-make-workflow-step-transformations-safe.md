Label: ready-for-agent

# Make Workflow Step Transformations Safe

## Parent

[Configurable Workflow Steps PRD](./configurable-workflow-steps.md)

## Target Product

Product: devloop-plan + devloop

Portable `devloop-plan + devloop`. Implement transactional transformations in
the portable workflow model and terminal editor. Do not modify CodexCLI.

## What to build

Complete transactional Duplicate, Delete, and Type Change operations in the Workflow Editor. Duplication must create a new UUID and unique name, copy instance settings and inputs, avoid copying successful runtime evidence, mark copied guidance `NEEDS_REVIEW`, insert the duplicate safely on the Primary Path when applicable, and warn that unused outputs still need deliberate consumers.

Deletion must preview every affected transition and binding, require confirmation, avoid cascading removal, repair only an unambiguous Primary Path success link, and leave other broken references visible for repair. Type Change must preserve logical identity, name, and position while resetting type-owned settings, ports, and outcomes; preserved guidance must require review. Every transformation must support Undo and the draft must remain unapplied until the resulting graph validates.

Covers parent PRD user stories 6-7, 40-45, and 75-78.

## Acceptance criteria

- [ ] Duplicate creates a new UUIDv4 and unique display name while copying appropriate instance configuration and input bindings.
- [ ] Duplicate does not copy successful runtime state, attempts, or evidence.
- [ ] Duplicated guidance is marked `NEEDS_REVIEW`, and unused outputs are reported as warnings.
- [ ] A Primary Path duplicate is inserted with explicit success-transition rewiring and no silent consumer redirection.
- [ ] Delete previews impacted transitions and bindings, requires confirmation, and never cascades into downstream step deletion.
- [ ] Delete repairs only one unambiguous Primary Path success link; all other broken bindings remain explicit validation errors.
- [ ] Type Change preserves UUID, display name, and position while resetting type-dependent model settings, capabilities, ports, and outcomes to the new component contract.
- [ ] Preserved guidance after Type Change is marked `NEEDS_REVIEW` and must be resolved before Apply.
- [ ] Duplicate, Delete, and Type Change can each be undone without persistence side effects.
- [ ] Apply is blocked for every invalid transformed graph, and a valid transformed workflow executes end to end in automated coverage.
- [ ] Transformations round-trip through portable planner configuration and `*.loop.state.json`; no CodexCLI state is read or written.

## Blocked by

- [Issue 0004: Build and Reorder the Primary Path](./0004-build-and-reorder-the-primary-path.md)
- [Issue 0005: Edit Outcome Routes and Typed Port Bindings](./0005-edit-outcome-routes-and-typed-port-bindings.md)
- [Issue 0006: Choose Per-Step Codex Execution Settings](./0006-choose-per-step-codex-execution-settings.md)
- [Issue 0007: Give Each Step Its Own Capabilities and Guidance](./0007-give-each-step-its-own-capabilities-and-guidance.md)
