Label: ready-for-agent

# Choose Per-Step Codex Execution Settings

## Parent

[Configurable Workflow Steps PRD](./configurable-workflow-steps.md)

## Target Product

Product: devloop-plan + devloop

Portable `devloop-plan + devloop`. Implement through `interactive_runner.py`,
`codex_runner.py`, and a small portable model-catalog adapter. Do not use
CodexCLI RunStore, Workflow Runs, or Textual settings screens.

## What to build

Give every Codex-backed Step Instance authoritative model, reasoning-effort, and Fast settings in the portable runner. Populate choices through a small portable model-catalog adapter that queries the installed Codex backend, filter effort and Fast choices by advertised support, and use cached data only to keep the editor informative when discovery is unavailable. Add Retry Catalog and actionable discovery/preflight errors. Local deterministic steps must clearly show that Codex settings do not apply.

Persist the chosen values in the User Workflow Default and portable immutable run state. Before starting a run, refresh the catalog and fail closed if any exact combination is unavailable. Build every fresh or resumed `codex exec` command with the snapshotted model, reasoning effort, and explicit Fast-on/Fast-off configuration so global settings cannot leak in. Remove automatic FULL/LIGHTWEIGHT intelligence switching; retain timeouts and checkpoints as a separate Execution Budget.

The built-in defaults are Analysis = Sol/xhigh/Fast Off, Development = Luna/high/Fast Off, Code Review = Sol/xhigh/Fast Off, and QA = Terra/high/Fast Off.

Covers parent PRD user stories 46-65.

## Acceptance criteria

- [ ] The editor loads all pages of the live model catalog and presents human-readable model choices.
- [ ] Reasoning-effort choices and Fast availability are constrained by the selected model's advertised capabilities.
- [ ] Every Codex-backed Step Instance persists an independent model, effort, and Fast value.
- [ ] Local deterministic component instances do not show meaningless Codex setting controls.
- [ ] Cached catalog data is display-only, is visibly stale, and cannot authorize execution.
- [ ] Retry Catalog recovers the editor after a temporary discovery failure.
- [ ] Run preflight refreshes availability and names the exact step and invalid setting without silently substituting another value.
- [ ] Portable `codex exec` command construction receives the exact snapshotted model, reasoning effort, and Fast configuration for each step.
- [ ] Fast Off is explicit per step and is not changed by global Codex defaults or `/fast` state.
- [ ] Automatic FULL/LIGHTWEIGHT model or effort switching is removed, while Execution Budget remains independently configurable.
- [ ] Built-in workflow defaults match the four approved model/effort/Fast combinations.
- [ ] Backend-adapter, command-construction, loop-state, preflight, and fake-editor tests cover supported and unsupported combinations.
- [ ] The catalog adapter does not import CodexCLI App Server execution, RunStore, Workflow Run, or UI modules.

## Blocked by

- [Issue 0003: Edit and Persist Future Workflow Defaults](./0003-edit-and-persist-future-workflow-defaults.md)
