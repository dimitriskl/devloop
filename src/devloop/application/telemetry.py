from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone

from devloop.domain.execution import ExecutionPhase
from devloop.domain.run import RunEventType, WorkflowRunSnapshot
from devloop.persistence.run_store import RunStore


class ExecutionTelemetryRecorder:
    def __init__(
        self,
        store: RunStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def record(
        self,
        snapshot: WorkflowRunSnapshot,
        component_id: str,
        attempt_key: str,
        phase: ExecutionPhase,
        *,
        applicable: bool = True,
    ) -> WorkflowRunSnapshot:
        telemetry = snapshot.execution_telemetry
        if telemetry.has_phase(component_id, attempt_key, phase):
            return snapshot
        updated = telemetry.record(
            phase,
            self._clock(),
            component_id=component_id,
            attempt_key=attempt_key,
            applicable=applicable,
        )
        return self._store.record(
            replace(snapshot, execution_telemetry=updated),
            RunEventType.EXECUTION_PHASE_RECORDED,
        )
