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

Persist the chosen values in the User Workflow Default and the portable run
definition. Before starting or resuming an attempt, refresh the catalog and fail
closed if any exact combination is unavailable. At resume, matching Step
Instances adopt the latest model, reasoning effort, Fast, and capability
preferences before the state hash is replaced. Build every fresh or resumed
`codex exec` command with those authorized values so global settings cannot leak
in. Remove automatic FULL/LIGHTWEIGHT intelligence switching; retain timeouts
and checkpoints as a separate Execution Budget.

The built-in defaults are Analysis = Sol/xhigh/Fast Off, Development = Luna/high/Fast Off, Code Review = Sol/xhigh/Fast Off, and QA = Terra/high/Fast Off.

Covers parent PRD user stories 46-65.

## Acceptance criteria

- [x] The editor loads all pages of the live model catalog and presents human-readable model choices.
- [x] Reasoning-effort choices and Fast availability are constrained by the selected model's advertised capabilities.
- [x] Every Codex-backed Step Instance persists an independent model, effort, and Fast value.
- [x] Local deterministic component instances do not show meaningless Codex setting controls.
- [x] Cached catalog data is display-only, is visibly stale, and cannot authorize execution.
- [x] Retry Catalog recovers the editor after a temporary discovery failure.
- [x] Run preflight refreshes availability and names the exact step and invalid setting without silently substituting another value.
- [x] Portable `codex exec` command construction receives the exact authorized model, reasoning effort, and Fast configuration from the current run definition for each step.
- [x] Fast Off is explicit per step and is not changed by global Codex defaults or `/fast` state.
- [x] Automatic FULL/LIGHTWEIGHT model or effort switching is removed, while Execution Budget remains independently configurable.
- [x] Built-in workflow defaults match the four approved model/effort/Fast combinations.
- [x] Backend-adapter, command-construction, loop-state, preflight, and fake-editor tests cover supported and unsupported combinations.
- [x] The catalog adapter does not import CodexCLI App Server execution, RunStore, Workflow Run, or UI modules.

## Blocked by

- [Issue 0003: Edit and Persist Future Workflow Defaults](./0003-edit-and-persist-future-workflow-defaults.md)

## Implementation Notes

Completed: 2026-07-16T17:50:13

### Changed Files
- `src/devloop/codex_runner.py`
- `tests/test_resume.py`

### Verification
- `./bin/devloop.sh --prd issues/configurable-workflow-steps.md --issues issues/configurable-workflow-steps-issues.md --start-issue 0006 --dry-run --no-worktree --no-self-improvement-wiki --non-interactive`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest -b -q tests.test_codex_execution_settings tests.test_model_catalog tests.test_codex_runner tests.test_chat_loop tests.test_interactive_runner tests.test_workflow_editor tests.test_resume`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -b -q`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_resume.LoopStateLoadingTests.test_generated_portable_artifact_with_truncated_display_name_is_ignored -v`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/tmp/devloop-qa-pycache PYTHONPATH=src python3 -m compileall -q src/devloop tests`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/tmp/devloop-review-pycache PYTHONPATH=src python3 -m compileall -q src/devloop tests`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/tmp/devloop-security-review-pycache PYTHONPATH=src python3 -m compileall -q src/devloop tests`
- `git diff --check`

### Workflow Step Results

#### Development

Re-normalized log tokens after truncation and added a regression proving generated portable artifacts are ignored by legacy recovery.

#### Security Review

The truncation-boundary fix is correct: generated filename tokens are normalized after truncation, and the regression verifies portable artifacts cannot corrupt legacy recovery. No blocking issues remain.

#### Final Review

No blocking correctness, security, persistence, architecture, regression, or test-quality issues found.

#### QA

All Issue 0006 acceptance paths have automated coverage and passed QA verification, including catalog/preflight, explicit per-step execution settings, command construction, workflow persistence, and portable resume isolation.
