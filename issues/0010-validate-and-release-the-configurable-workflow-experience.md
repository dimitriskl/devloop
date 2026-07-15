Label: ready-for-agent

# Validate and Release the Configurable Workflow Experience

## Parent

[Configurable Workflow Steps PRD](./configurable-workflow-steps.md)

## Target Product

Product: devloop-plan + devloop

Portable `devloop-plan + devloop`. Validate Bash and PowerShell wrappers plus
their shared Python planner/runner. CodexCLI release gates are out of scope.

## What to build

Close the feature with integrated evidence through `devloop-plan.sh`/`.ps1` and `devloop.sh`/`.ps1`. Configure Security Review and Final Review as independent instances with different models, efforts, capability profiles, and guidance; apply the workflow; run it; trigger rework; interrupt and rerun to resume; and verify the dynamic dashboard and persisted attempt history. Exercise malformed portable schema-v2 defaults, invalid catalogs, bindings, graph routes, and backend preflight combinations. Reject portable schema v1 explicitly because this release intentionally has no compatibility or migration path.

Run the repository's sandbox-safe automated validation, dry-run, syntax, and whitespace gates. Update user and maintainer documentation for the portable Workflow Editor, run immutability, model-catalog behavior, terminal surfaces, and troubleshooting. Preserve the line editor, commands, approvals, stop handling, capabilities installer, Bash wrapper, and PowerShell wrapper. If a real authenticated Codex check is still required, provide one paste-ready operator command that writes a non-secret result log inside the workspace; do not launch it from the agent session.

Covers parent PRD user stories 26, 34-39, 55-58, 73-76, and 101-110.

## Acceptance criteria

- [ ] An integrated scenario configures and runs Security Review and Final Review with different per-instance execution settings, capabilities, and guidance.
- [ ] The scenario demonstrates changes-requested rework, pause/resume at an arbitrary Step Instance, exact artifact routing, and complete attempt history.
- [ ] Planning and implementation console dashboards show all configured instances, live activity, timers, frozen results, and terminal-safe fallbacks.
- [ ] Malformed schema-v2 defaults, graphs, bindings, catalogs, and unsupported model/effort/Fast combinations fail closed with actionable messages.
- [ ] Schema v1 is rejected explicitly; no compatibility reader, migration, or dual-write path is introduced.
- [ ] The complete automated suite, Python syntax gate, repository dry-run, and whitespace/diff checks pass in the workspace sandbox.
- [ ] Documentation explains editing Future Runs, inspecting Current Run, immutable snapshots, graph and binding repair, model discovery/preflight, guidance safety, and dashboard behavior.
- [ ] Existing composer, slash commands, approvals, interruption/stop behavior, capability installation, and wrapper parity have no regressions.
- [ ] Any credential-dependent real backend validation is handed to the operator as exactly one paste-ready command that writes a non-secret workspace log.
- [ ] Final evidence identifies every automated, inspected, and operator-only gate without claiming unexecuted coverage.
- [ ] `git diff --name-only` confirms the feature did not modify CodexCLI application/domain/execution/persistence/UI/workflow modules.

## Blocked by

- [Issue 0005: Edit Outcome Routes and Typed Port Bindings](./0005-edit-outcome-routes-and-typed-port-bindings.md)
- [Issue 0006: Choose Per-Step Codex Execution Settings](./0006-choose-per-step-codex-execution-settings.md)
- [Issue 0007: Give Each Step Its Own Capabilities and Guidance](./0007-give-each-step-its-own-capabilities-and-guidance.md)
- [Issue 0008: Make Workflow Step Transformations Safe](./0008-make-workflow-step-transformations-safe.md)
- [Issue 0009: Show Dynamic Progress Across Every Terminal Surface](./0009-show-dynamic-progress-across-every-terminal-surface.md)
