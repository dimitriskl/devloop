from __future__ import annotations

from dataclasses import replace

from devloop.application.config import ApplicationConfig
from devloop.components.builtin import installed_component_registry
from devloop.domain.execution import ExecutionProfile, ExecutionProfileId
from devloop.domain.identifiers import StepComponentId, WorkflowRunId
from devloop.domain.run import RunEventType, WorkflowRunSnapshot
from devloop.persistence.run_store import RunStore


class ExecutionProfileSelectionError(RuntimeError):
    pass


class ExecutionProfileSelectionService:
    def __init__(self, config: ApplicationConfig) -> None:
        self._store = RunStore(config.paths.run_root)
        self._registry = installed_component_registry()

    def available(self) -> tuple[ExecutionProfile, ...]:
        return tuple(
            profile
            for manifest in self._registry.manifests
            for profile in manifest.execution_profiles
        )

    def selected(self, run_id: WorkflowRunId) -> tuple[ExecutionProfile, ...]:
        return self._store.load(run_id).execution_profiles

    def select(
        self,
        run_id: WorkflowRunId,
        component_id: StepComponentId,
        profile_id: ExecutionProfileId,
    ) -> WorkflowRunSnapshot:
        snapshot = self._store.load(run_id)
        if snapshot.terminal:
            raise ExecutionProfileSelectionError("A terminal run cannot change execution profile.")
        manifest, _ = self._registry.resolve(component_id)
        selected = next(
            (item for item in manifest.execution_profiles if item.profile_id is profile_id),
            None,
        )
        if selected is None:
            raise ExecutionProfileSelectionError(
                "The requested component execution profile is not supported."
            )
        if _component_started(snapshot, component_id):
            raise ExecutionProfileSelectionError(
                "Execution profile is locked after the component binds its App Server thread."
            )
        profiles = tuple(
            item for item in snapshot.execution_profiles if item.component_id != component_id.value
        )
        updated = replace(snapshot, execution_profiles=(*profiles, selected))
        return self._store.record(updated, RunEventType.EXECUTION_PROFILE_SELECTED)


def _component_started(snapshot: WorkflowRunSnapshot, component_id: StepComponentId) -> bool:
    if any(
        event.component_id == component_id.value
        for event in snapshot.execution_telemetry.events
    ):
        return True
    cursors = {
        "analysis": snapshot.analysis,
        "development": snapshot.development,
        "code-review": snapshot.review,
        "qa": snapshot.qa,
    }
    cursor = cursors.get(component_id.value)
    return cursor is not None and cursor.thread_id is not None
