Label: ready-for-agent

# Edit Outcome Routes and Typed Port Bindings

## Parent

[Configurable Workflow Steps PRD](./configurable-workflow-steps.md)

## Target Product

Product: devloop-plan + devloop

Portable `devloop-plan + devloop`. Extend the portable terminal editor,
workflow model, and issue runner. Do not implement graph editing in CodexCLI.

## What to build

Add structured graph editing beyond the Primary Path in the portable terminal editor. For every outcome declared by a portable Step Contract, allow routing to an existing step, creating a new branch step, inserting a step between a source and destination, or terminating intentionally. Support loops and branch-local ordering. Keep the interaction keyboard-operable through the existing line editor and provide a live text graph preview rather than requiring drag-and-drop.

Add an Advanced Port Bindings editor that displays required and optional typed inputs, compatible producers, current selections, and actionable missing or incompatible errors. Automatically bind only when exactly one compatible producer exists. Validate start semantics, successful terminal reachability, step reachability, declared outcomes, scopes, and port compatibility before Apply.

Covers parent PRD user stories 26-39.

## Acceptance criteria

- [ ] Every supported outcome can target an existing step, a newly created step, an inserted step, or an explicit terminal.
- [ ] The editor supports loops and branch-local ordering without assigning misleading global positions to branch-only steps.
- [ ] A live graph preview updates as structured transition controls change.
- [ ] All graph operations are fully keyboard accessible.
- [ ] Advanced Port Bindings shows typed requirements, compatible producers, current bindings, and validation errors.
- [ ] Exactly one compatible producer may auto-bind; ambiguous producers require an explicit user choice.
- [ ] Apply is blocked when there is no valid start, no successful terminal path, an unreachable required step, an unsupported outcome, an invalid scope relationship, or a missing/incompatible binding.
- [ ] Validation errors identify the affected Step Instance and the transition or port that needs repair.
- [ ] Automated tests execute a successful branch and a changes-requested loop configured through the editor.
- [ ] Tests use the portable issue runner and fake editor; CodexCLI workflow/UI modules are untouched.

## Blocked by

- [Issue 0002: Resume and Rework Arbitrary Step Instances](./0002-resume-and-rework-arbitrary-step-instances.md)
- [Issue 0004: Build and Reorder the Primary Path](./0004-build-and-reorder-the-primary-path.md)
