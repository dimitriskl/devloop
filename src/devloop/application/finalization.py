from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from devloop.application.config import ApplicationConfig
from devloop.domain.development import IssueStatus
from devloop.domain.finalization import (
    HANDOFF_SUMMARY_SCHEMA,
    FinalizationCursor,
    HandoffSummary,
    WorkspaceDisposition,
)
from devloop.domain.identifiers import IssueId, WorkflowRunId
from devloop.domain.outcomes import StepOutcome
from devloop.domain.run import (
    RunEventType,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.domain.scheduler import AttemptStatus
from devloop.persistence.run_store import RunStore, RunStoreError

HANDOFF_DIRECTORY = "handoff"
HANDOFF_FILENAME = "handoff-summary.json"


class FinalizationError(RuntimeError):
    pass


@dataclass(frozen=True)
class FinalizationCompleted:
    snapshot: WorkflowRunSnapshot
    summary: HandoffSummary


class FinalizationService:
    def __init__(self, config: ApplicationConfig) -> None:
        self._store = RunStore(config.paths.run_root)

    def finalize(self, run_id: WorkflowRunId) -> FinalizationCompleted:
        snapshot = self._store.load(run_id)
        if snapshot.finalization is not None:
            payload = self._store.load_json_artifact(
                snapshot.run_id,
                snapshot.finalization.handoff_summary,
            )
            return FinalizationCompleted(snapshot, handoff_summary_from_dict(payload))
        if snapshot.workspace is None:
            raise FinalizationError("Workspace finalization requires a selected workspace.")
        if not snapshot.issues or any(
            issue.status is not IssueStatus.COMPLETED for issue in snapshot.issues
        ):
            raise FinalizationError("Workspace finalization requires every Issue to be completed.")
        if snapshot.terminal:
            raise FinalizationError("A terminal Workflow Run cannot be finalized again.")
        try:
            self._store.validate_lease(snapshot)
        except RunStoreError:
            snapshot = self._store.take_lease(snapshot)
        started = self._store.record(
            replace(
                snapshot,
                run_status=WorkflowRunStatus.RUNNING,
                step_status=StepRunStatus.RUNNING,
                outcome=None,
            ),
            RunEventType.FINALIZATION_STARTED,
        )
        implementations: list[dict[str, object]] = []
        qa_results: list[dict[str, object]] = []
        for attempt in started.attempts:
            if attempt.status is not AttemptStatus.COMPLETED:
                continue
            if attempt.implementation is not None:
                implementations.append(
                    self._store.load_json_artifact(started.run_id, attempt.implementation)
                )
            if attempt.qa_result is not None:
                qa_results.append(self._store.load_json_artifact(started.run_id, attempt.qa_result))
        summary = build_handoff_summary(
            started,
            implementations=implementations,
            qa_results=qa_results,
        )
        artifact = self._store.save_json_artifact(
            started.run_id,
            Path(HANDOFF_DIRECTORY) / HANDOFF_FILENAME,
            handoff_summary_to_dict(summary),
        )
        completed = self._store.record(
            replace(
                started,
                finalization=FinalizationCursor(
                    artifact,
                    WorkspaceDisposition.LEAVE_INTACT,
                ),
                run_status=WorkflowRunStatus.COMPLETED,
                step_status=StepRunStatus.SUCCEEDED,
                outcome=StepOutcome.SUCCEEDED,
            ),
            RunEventType.RUN_COMPLETED,
        )
        self._store.release_lease(completed)
        return FinalizationCompleted(completed, summary)


def build_handoff_summary(
    snapshot: WorkflowRunSnapshot,
    *,
    implementations: Sequence[Mapping[str, object]],
    qa_results: Sequence[Mapping[str, object]],
) -> HandoffSummary:
    if snapshot.workspace is None:
        raise FinalizationError("A Handoff Summary requires a selected workspace.")
    completed_issues = tuple(
        item.issue_id for item in snapshot.issues if item.status is IssueStatus.COMPLETED
    )
    if len(completed_issues) != len(snapshot.issues):
        raise FinalizationError("A Handoff Summary requires every Issue to be completed.")
    changed_files: list[str] = []
    residual_risks: list[str] = []
    for implementation in implementations:
        changed_files.extend(_changed_paths(implementation.get("changed_files")))
        residual_risks.extend(_strings(implementation.get("risks")))
    verification: list[str] = []
    for result in qa_results:
        verification.extend(_verification_lines(result.get("checks")))
        residual_risks.extend(_strings(result.get("residual_risks")))
    return HandoffSummary(
        HANDOFF_SUMMARY_SCHEMA,
        snapshot.run_id,
        completed_issues,
        tuple(_unique(verification)),
        tuple(_unique(changed_files)),
        tuple(_unique(residual_risks)),
        WorkspaceDisposition.LEAVE_INTACT,
        snapshot.workspace.path,
    )


def handoff_summary_to_dict(summary: HandoffSummary) -> dict[str, object]:
    return {
        "schema": summary.schema,
        "run_id": summary.run_id.value,
        "completed_issues": [item.value for item in summary.completed_issues],
        "verification_evidence": list(summary.verification_evidence),
        "changed_files": list(summary.changed_files),
        "residual_risks": list(summary.residual_risks),
        "workspace_disposition": summary.workspace_disposition.value,
        "workspace_path": summary.workspace_path,
    }


def handoff_summary_from_dict(value: Mapping[str, object]) -> HandoffSummary:
    if value.get("schema") != HANDOFF_SUMMARY_SCHEMA:
        raise FinalizationError("Handoff Summary schema is unsupported.")
    run_id = value.get("run_id")
    workspace_path = value.get("workspace_path")
    disposition = value.get("workspace_disposition")
    if not isinstance(run_id, str) or not isinstance(workspace_path, str) or not workspace_path:
        raise FinalizationError("Handoff Summary identity is invalid.")
    if not isinstance(disposition, str):
        raise FinalizationError("Handoff Summary workspace disposition is invalid.")
    return HandoffSummary(
        HANDOFF_SUMMARY_SCHEMA,
        WorkflowRunId(run_id),
        tuple(IssueId(item) for item in _strings(value.get("completed_issues"))),
        _strings(value.get("verification_evidence")),
        _strings(value.get("changed_files")),
        _strings(value.get("residual_risks")),
        WorkspaceDisposition(disposition),
        workspace_path,
    )


def _changed_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    paths: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        path = cast(dict[str, object], item).get("path")
        if isinstance(path, str) and path:
            paths.append(path)
    return tuple(paths)


def _verification_lines(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    lines: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        row = cast(dict[str, object], item)
        identifier = row.get("id") or row.get("check_id")
        status = row.get("status")
        if not isinstance(identifier, str) or not identifier:
            continue
        if not isinstance(status, str) or not status:
            continue
        parts = [identifier, status]
        command = row.get("command")
        evidence = row.get("evidence")
        if isinstance(command, str) and command:
            parts.append(command)
        if isinstance(evidence, str) and evidence:
            parts.append(evidence)
        lines.append(" | ".join(parts))
    return tuple(lines)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return ()
    return tuple(cast(list[str], value))


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))
