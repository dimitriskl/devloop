Label: ready-for-agent

# Build and Reorder the Primary Path

## Parent

[Configurable Workflow Steps PRD](./configurable-workflow-steps.md)

## Target Product

Product: devloop-plan + devloop

Portable `devloop-plan + devloop`. Extend the portable terminal Workflow Editor
and workflow model only. Do not use the CodexCLI Component Registry or UI.

## What to build

Make the portable terminal Workflow Editor capable of constructing the normal successful path. Populate the Type picker from the portable installed role/skill and step-adapter catalog, show the selected component's read-only scope, and create new instances from portable defaults. Support Add, Insert, Move Up, Move Down, and direct one-based Position edits. Represent Primary Path order through `SUCCEEDED` transitions and renumber its display positions without gaps.

Keep every existing Step Instance ID stable through moves and insertions. When a simple linear insertion creates exactly one compatible source for a required input, bind it automatically; otherwise leave the binding unresolved for deliberate selection. Demonstrate that an edited Primary Path can be applied and executed.

Covers parent PRD user stories 4-5, 8-10, and 19-27.

## Acceptance criteria

- [ ] The Type picker lists installed built-in and custom portable Workflow Step Components without a core type enum edit or CodexCLI registry dependency.
- [ ] The selected component's Step Scope is visible and cannot be overridden by the workflow instance.
- [ ] Add and Insert create a UUIDv4 instance with a unique name and component defaults.
- [ ] Move Up, Move Down, and one-based Position edits update `SUCCEEDED` transitions consistently.
- [ ] Primary Path positions are always displayed contiguously from one.
- [ ] Moving or inserting existing steps does not change their Step Instance IDs.
- [ ] A required input is auto-bound only when exactly one compatible source exists.
- [ ] Ambiguous or missing bindings remain explicit validation work rather than being guessed.
- [ ] Any built-in step can be removed or replaced; no mandatory phase list is enforced.
- [ ] An edited valid Primary Path persists and completes an end-to-end run in automated coverage.

## Blocked by

- [Issue 0003: Edit and Persist Future Workflow Defaults](./0003-edit-and-persist-future-workflow-defaults.md)
