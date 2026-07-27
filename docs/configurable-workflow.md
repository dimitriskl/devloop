# Portable Configurable Workflow

This document is the maintainer map for the configurable workflow used by
`devloop-plan.sh` / `.ps1` and `devloop.sh` / `.ps1`. It does not describe the
separate CodexCLI Textual application or any module under its application,
domain, execution, persistence, UI, or workflow packages.

## Contracts And Ownership

Portable workflow documents use only `devloop.portable-workflow/v3`. The strict
loader rejects earlier schemas through `SupersededWorkflowSchemaError`. At the
run-state boundary, `state.py` provides one narrow compatibility path for a
hash-valid v2 resolved workflow: it converts the formerly Codex-only settings
to Step Execution Settings with `CODEX_CLI` as their explicit backend, validates
the v3 result, re-hashes it, records a migration event, and preserves the run's
issue and attempt state. User Workflow Defaults and v1 runs remain fail-closed.
Each Workflow Step is a UUIDv4-keyed instance with a unique display name, an
open Step Component ID, component-owned scope and ports, explicit Outcome
Transitions, typed Port Bindings, Step Execution Settings when applicable, an
independent Execution Budget, capabilities, and optional bounded Step Guidance.
Every agent-backed built-in Step starts with role-specific guidance. Analysis,
Development, Security Review, Final Review, and QA have distinct built-in
defaults; the two reviewer instances intentionally do not share the same text.
New component instances use their component's reusable guidance default, while
resetting a built-in instance restores its instance-specific default.

Step Execution Settings are persisted under the step's `execution_settings` key
and carry a required `backend` naming one member of the closed
`ExecutionBackendId` set, plus the model, reasoning effort, and Fast preference.
`backend` is parsed at the persistence boundary by
`parse_execution_backend_id`, and again at the command-line boundary in
`codex_execution_settings_args`, which refuses to build a Codex invocation for
settings naming another backend. Fast may be enabled only when the selected
model advertises it, so it is rejected for a backend that advertises none.
Component Execution Defaults are backend-parameterised: every built-in role
supplies defaults for every backend, and a new Workflow Step starts on
`DEFAULT_EXECUTION_BACKEND`.

Each unsupported-schema rejection is turned into an actionable operator message
at the boundary that owns the document. `workflow_defaults.py` states that a
saved Workflow Default must be recreated in `/options` with `reset-workflow`
then `apply`, and the Workflow Editor opens its fail-closed recovery mode.
`state.py` migrates a valid v2 run automatically; it rejects v1 or malformed
state and names the PRD-local loop-state file. Neither path surfaces a traceback.

`portable_workflow.py` owns the serialization contract, graph and binding
validation, execution, rework routing, and typed attempt records.
`portable_component_catalog.py` and `catalog.py` adapt installed portable roles
and capabilities without importing the CodexCLI registry. `workflow_editor.py`
owns the transactional Workflow Default draft. `workflow_defaults.py`
atomically replaces the user default. `state.py` stores the Current Run
definition, canonical hash, generic Step Runtime States, interrupted-attempt
identity, and ordered Step Attempt Records.

`portable_execution_backend/` owns the Execution Backend boundary: the
interface with its frozen Step Attempt request and result types, the neutral
step-activity event that the Portable Activity Feed and Execution Budget
checkpointing both consume, the Run-Wide Blocker domain type, and one module per
backend. A backend's result also carries the provenance it can state about the
attempt — the model the finished turn's usage accounting reported, its cost, and
its turn count. `step_configuration.py` owns that record beside the Step Attempt
Context, including the derived requested-versus-serving model mismatch that the
role runner completes with the backend it dispatched to and the model the Step
Execution Settings requested. `registry.py` registers both members of `ExecutionBackendId`:
`ExecutionBackendId.CODEX_CLI` to `codex_cli.py` and
`ExecutionBackendId.CLAUDE_CODE` to `claude_code.py`, each behind a factory
called only when a Workflow Step actually needs that backend, so a Workflow that
uses one provider stays independent of the other provider's installation.
`codex_cli.py` owns Codex command construction, the streaming loop, event
translation, structured-message recovery, and Codex Run-Wide Blocker
classification; `codex_events.py` remains the Codex wire-format parser.
`claude_code.py` owns `claude -p` command construction and its reproducibility
isolation, the `stream-json` streaming loop under the Execution Budget, event
translation, Permission Denial recognition, structured-result recovery, Claude
Run-Wide Blocker classification, and run authorization, and reaches its Model
Catalog and its one verification call through `claude_catalog.py`; both are
injectable on the backend so run authorization is testable from recorded provider
output. `transient_retry.py` owns the bounded retry policy both backends share:
one attempt-wide Execution Budget across every process run, one delay, and one
accumulated transcript. What is worth retrying is asked of the backend through
`is_retryable_transient_failure` on the interface, which is also where each
backend keeps the promise that a Run-Wide Blocker is never retried. This package
must not import any CodexCLI package, which `tests/test_product_boundary.py`
enforces.

The deep execution seam is `PortableWorkflowExecutor.run`: callers provide a
resolved Workflow Definition, component catalog, and role-runner adapter. The
executor owns navigation, exact changes-requested record routing, typed input
resolution, pass accounting, checkpoint recovery, and attempt construction.
Tests and the CLI use the same interface.

## Run Stability And Recovery

A new run validates and snapshots the current User Workflow Default before its
first attempt. Once `resolved_workflow` and `resolved_workflow_hash` exist,
reruns preserve the graph, Step Instance IDs, component types, bindings,
and transitions. Applying the Workflow Default while inspecting a Current Run,
or resolving that unfinished run again, replaces every non-structural preference
on matching Step Instances: display name, Execution Backend, model, reasoning
effort, Fast preference, execution budget, Skills, Agent References, and
guidance. The state definition and hash are replaced atomically, issue cursors
and Step Attempt Records are preserved, and the change is recorded as a
`workflow-preferences-applied` event. An attempt that has already launched keeps
the settings it started with; subsequent attempts on unfinished Issues use the
new preferences.
The editor exposes Current Run as read-only and the Workflow Default as
editable. A hash mismatch or unknown field stops recovery instead of
normalizing corrupted state.

Every attempt retains Step Instance ID, optional Issue ID, pass, prompt session
and attempt identity, outcome, typed outputs, timing, safe context, and rework
linkage. Ordinary bindings select the latest compatible successful output.
Rework binds the exact `CHANGES_REQUESTED` Step Attempt Record that selected the
transition. Failed, blocked, or cancelled output is excluded unless a binding
explicitly permits that outcome. Interruption checkpoints the active instance
and attempt identity so rerunning the same wrapper resumes there without
replaying completed steps.

## Catalog And Backend Preflight

`model_catalog.py` owns the Model Catalog type, which belongs to one Execution
Backend and is reached through that backend's model-discovery operation. The
Codex CLI Backend loads every page of the installed account-aware catalog. The
Claude Code Backend has no catalog endpoint, so `catalogs/claude-code-models.json`
carries its entries as bundle reference data, parsed by
`portable_execution_backend/claude_catalog.py`: browsing costs nothing, and one
model selection costs one verification call that resolves a short alias to the
concrete pinned identifier the session-initialisation event reports, which is the
only identifier ever persisted. Cached data exists only to render the editor; its
path is backend-qualified, with the Codex cache keeping its historical name, and a
cache recorded for one backend is refused for another. Before a new run,
`preflight_step_execution_settings` groups the agent-backed Workflow Steps by the
Execution Backend each one names and asks that backend to authorize its own
steps' exact model, reasoning effort, and Fast preference against its own fresh
catalog. Both the backend and its catalog are resolved lazily, per referenced
backend, which is what keeps a Workflow that uses one provider independent of the
other provider's installation and sign-in; `tests/test_claude_run_preflight.py`
asserts both directions by failing if an unreferenced backend is resolved, if its
catalog is requested, if its command is looked up on the executable search path,
or if any provider process starts.
The Codex CLI Backend authorizes from its account-aware catalog alone. The Claude
Code Backend verifies each *distinct* model the Run Snapshot selects exactly once,
however many Workflow Steps select it, and a model the account cannot use is
refused before any attempt budget is spent. Authorization names the affected Step
Display Name and setting, is re-raised naming the backend that refused, and never
falls back. Command construction in
`portable_execution_backend/codex_cli.py` passes model, reasoning effort, and
explicit Fast On or Off from the currently authorized run definition. Timeouts
and checkpoint deadlines remain separate Execution Budget values.

## Terminal Projection

`statusui.py` builds one presentation-independent, Step Instance ID-keyed
Workflow Progress Dashboard projection. Planning intake, the implementation
console, Bash, PowerShell, TTY, and redirected output consume it through the
same Python modules. Interactive output reuses a bounded current-Issue region;
non-TTY output appends snapshots and never emits cursor movement. Text labels
carry all state meaning when color or Unicode is unavailable.

## Release Validation

The sandbox-safe release gates are:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=src python3 -m compileall -q src tests
bash -n bin/devloop.sh bin/devloop-plan.sh
./bin/devloop.sh --help
./bin/devloop-plan.sh --help
git diff --check
```

Run the dry-run wrapper against a disposable issue pack rather than this
repository's active issue state. `tests/test_configurable_workflow_release.py`
is the integrated deterministic scenario: it configures Security Review and
Final Review independently, requests changes, routes the exact triggering
record to rework, interrupts Final Review, resumes it, proves QA receives the
Final Review artifact, then inspects complete attempt history and the shared
dashboard projection.

An authenticated live Codex catalog/preflight remains operator-only in managed
agent environments. Record only the PASS/FAIL result and model count under the
ignored `.release-evidence/` directory; never persist catalog payloads,
credentials, environment dumps, or raw agent transcripts.
