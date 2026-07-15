Label: ready-for-agent

# Configurable Workflow Steps Issue Pack

## Parent

Parent PRD: `configurable-workflow-steps.md`

## Target Product

All issues target the portable `devloop-plan + devloop` wrappers and their
Python runner modules. They do not target the separately installed `codexcli`
Textual application. `docs/product-boundaries.md` is authoritative.

## Execution order

1. [Run Two Review Instances Through a v2 Workflow](./0001-run-two-review-instances-through-v2.md)
2. [Resume and Rework Arbitrary Step Instances](./0002-resume-and-rework-arbitrary-step-instances.md)
3. [Edit and Persist Future Workflow Defaults](./0003-edit-and-persist-future-workflow-defaults.md)
4. [Build and Reorder the Primary Path](./0004-build-and-reorder-the-primary-path.md)
5. [Edit Outcome Routes and Typed Port Bindings](./0005-edit-outcome-routes-and-typed-port-bindings.md)
6. [Choose Per-Step Codex Execution Settings](./0006-choose-per-step-codex-execution-settings.md)
7. [Give Each Step Its Own Capabilities and Guidance](./0007-give-each-step-its-own-capabilities-and-guidance.md)
8. [Make Workflow Step Transformations Safe](./0008-make-workflow-step-transformations-safe.md)
9. [Show Dynamic Progress Across Every Terminal Surface](./0009-show-dynamic-progress-across-every-terminal-surface.md)
10. [Validate and Release the Configurable Workflow Experience](./0010-validate-and-release-the-configurable-workflow-experience.md)

Dependencies are declared in each issue's `Blocked by` section. Issues with satisfied dependencies may be implemented in parallel, while the numbered order preserves a safe default sequence.
