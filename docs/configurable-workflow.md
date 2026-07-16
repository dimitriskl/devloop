# Portable Configurable Workflow

This document is the maintainer map for the configurable workflow used by
`devloop-plan.sh` / `.ps1` and `devloop.sh` / `.ps1`. It does not describe the
separate CodexCLI Textual application or any module under its application,
domain, execution, persistence, UI, or workflow packages.

## Contracts And Ownership

Portable workflow documents use only `devloop.portable-workflow/v2`. The loader
rejects schema v1 explicitly; there is no migration, compatibility reader, or
dual-write path. Each Workflow Step is a UUIDv4-keyed instance with a unique
display name, an open Step Component ID, component-owned scope and ports,
explicit Outcome Transitions, typed Port Bindings, Codex Execution Settings
when applicable, an independent Execution Budget, capabilities, and optional
bounded Step Guidance.

`portable_workflow.py` owns the serialization contract, graph and binding
validation, execution, rework routing, and typed attempt records.
`portable_component_catalog.py` and `catalog.py` adapt installed portable roles
and capabilities without importing the CodexCLI registry. `workflow_editor.py`
owns the transactional Future Runs draft. `workflow_defaults.py` atomically
replaces the user default. `state.py` stores the immutable Current Run snapshot,
canonical hash, generic Step Runtime States, interrupted-attempt identity, and
ordered Step Attempt Records.

The deep execution seam is `PortableWorkflowExecutor.run`: callers provide a
resolved Workflow Definition, component catalog, and role-runner adapter. The
executor owns navigation, exact changes-requested record routing, typed input
resolution, pass accounting, checkpoint recovery, and attempt construction.
Tests and the CLI use the same interface.

## Run Immutability And Recovery

A new run validates and snapshots the current User Workflow Default before its
first attempt. Once `resolved_workflow` and `resolved_workflow_hash` exist in
the loop state, reruns load that exact snapshot; `/options` exposes it as
read-only Current Run and edits only Future Runs. A hash mismatch or unknown
field stops recovery instead of normalizing corrupted state.

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
catalog. Cached data exists only to render the editor. Before a new run,
`cli.py` requires a fresh catalog and validates the exact model, reasoning
effort, and Fast preference for every Codex-backed instance. Validation names
the affected Step Display Name and setting and never falls back. Command
construction in `codex_runner.py` passes model, reasoning effort, and explicit
Fast On or Off from the immutable snapshot. Timeouts and checkpoint deadlines
remain separate Execution Budget values.

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
