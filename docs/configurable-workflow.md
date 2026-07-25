# Portable Configurable Workflow

This document is the maintainer map for the configurable workflow used by
`devloop-plan.sh` / `.ps1` and `devloop.sh` / `.ps1`. It does not describe the
separate CodexCLI Textual application or any module under its application,
domain, execution, persistence, UI, or workflow packages.

## Contracts And Ownership

Portable workflow documents use only `devloop.portable-workflow/v3`. The loader
rejects every earlier schema explicitly — both v1 and v2 — through
`SupersededWorkflowSchemaError`; there is no migration, compatibility reader, or
dual-write path. Each Workflow Step is a UUIDv4-keyed instance with a unique
display name, an open Step Component ID, component-owned scope and ports,
explicit Outcome Transitions, typed Port Bindings, Step Execution Settings
when applicable, an independent Execution Budget, capabilities, and optional
bounded Step Guidance.

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

Each superseded-schema rejection is turned into an actionable operator message
at the boundary that owns the document. `workflow_defaults.py` states that the
saved Workflow Default must be recreated in `/options` with `reset-workflow`
then `apply`, and the Workflow Editor opens its fail-closed recovery mode.
`state.py` states that the unfinished Workflow Run cannot be resumed and names
its PRD-local loop-state file. Neither path surfaces a traceback.

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
backend. `codex_cli.py` is the only registered backend today, even though
`ExecutionBackendId` already declares the Claude Code Backend; it owns Codex
command construction, the streaming loop, event translation, structured-message
recovery, and Codex Run-Wide Blocker classification. `codex_events.py` remains
the Codex wire-format parser. This package must not import any CodexCLI
package, which `tests/test_product_boundary.py` enforces.

The deep execution seam is `PortableWorkflowExecutor.run`: callers provide a
resolved Workflow Definition, component catalog, and role-runner adapter. The
executor owns navigation, exact changes-requested record routing, typed input
resolution, pass accounting, checkpoint recovery, and attempt construction.
Tests and the CLI use the same interface.

## Run Stability And Recovery

A new run validates and snapshots the current User Workflow Default before its
first attempt. Once `resolved_workflow` and `resolved_workflow_hash` exist,
reruns preserve the graph, Step Instance IDs, component types, bindings,
budgets, and guidance. Before the next resumed attempt, matching Step Instances
adopt the latest saved model, reasoning effort, Fast preference, Skills, and
Agent References, and the state definition and hash are replaced atomically. The
Execution Backend is deliberately not adopted: a resumed Workflow Run keeps the
backend its Run Snapshot recorded, so a saved default naming another backend
refreshes only that step's capabilities.
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

`model_catalog.py` loads every page of the installed account-aware Codex model
catalog, reached through the Execution Backend's model-discovery operation.
Cached data exists only to render the editor. Before a new run, `cli.py` requires
a fresh catalog, and `preflight_step_execution_settings` asks the registered
Execution Backend to authorize the exact model, reasoning effort, and Fast
preference for every agent-backed instance. Authorization names the affected Step
Display Name and setting and never falls back. Command construction in
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
