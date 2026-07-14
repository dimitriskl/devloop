from __future__ import annotations

from dataclasses import replace

from devloop.application.config import ApplicationConfig
from devloop.domain.identifiers import WorkflowRunId
from devloop.domain.outcomes import StepOutcome
from devloop.domain.run import (
    OperationStatus,
    RunEventType,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.persistence.run_store import RunStore, RunStoreError


class WorkflowControlError(RuntimeError):
    pass


class WorkflowControlService:
    """Persists explicit run controls without performing repository cleanup."""

    def __init__(self, config: ApplicationConfig) -> None:
        self._store = RunStore(config.paths.run_root)

    def cancel(self, run_id: WorkflowRunId) -> WorkflowRunSnapshot:
        snapshot = self._store.load(run_id)
        if snapshot.terminal:
            raise WorkflowControlError("The Workflow Run is already terminal.")
        current = self._ensure_lease(snapshot)
        if current.operation.status is OperationStatus.RUNNING:
            current = replace(
                current,
                operation=replace(current.operation, status=OperationStatus.UNKNOWN),
            )
            current = self._store.record(current, RunEventType.OPERATION_UNKNOWN)
        cancelled = replace(
            current,
            run_status=WorkflowRunStatus.CANCELLED,
            step_status=StepRunStatus.CANCELLED,
            outcome=StepOutcome.CANCELLED,
        )
        cancelled = self._store.record(cancelled, RunEventType.RUN_CANCELLED)
        self._store.release_lease(cancelled)
        return cancelled

    def _ensure_lease(self, snapshot: WorkflowRunSnapshot) -> WorkflowRunSnapshot:
        try:
            self._store.validate_lease(snapshot)
            return snapshot
        except (RunStoreError, OSError, ValueError):
            return self._store.take_lease(snapshot)
