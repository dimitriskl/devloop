from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from devloop.application.config import ApplicationConfig
from devloop.application.control import WorkflowControlService
from devloop.domain.identifiers import (
    FeatureSlug,
    StepInstanceId,
    WorkflowId,
    WorkflowRunId,
)
from devloop.domain.outcomes import StepOutcome
from devloop.domain.run import (
    AnalysisCursor,
    OperationState,
    OperationStatus,
    ResolvedWorkflow,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.persistence.run_store import RUN_SNAPSHOT_SCHEMA, RunStore, new_run_lease


def test_cancel_is_terminal_and_does_not_clean_the_workspace(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    marker = repository / "uncommitted.txt"
    marker.write_text("keep me", encoding="utf-8")
    config = ApplicationConfig.resolve(
        repository,
        platform="win32",
        environment={
            "APPDATA": str(tmp_path / "roaming"),
            "LOCALAPPDATA": str(tmp_path / "local"),
        },
        home=tmp_path,
    )
    now = datetime.now(timezone.utc).isoformat()
    snapshot = WorkflowRunSnapshot(
        schema=RUN_SNAPSHOT_SCHEMA,
        run_id=WorkflowRunId("run-20260712t120001-123456abcdef"),
        repository=str(repository),
        feature_title="Cancel safely",
        feature_slug=FeatureSlug("cancel-safely"),
        workflow=ResolvedWorkflow(WorkflowId("standard"), "1.0.0", "hash"),
        component_locks=(),
        active_step=StepInstanceId("development"),
        run_status=WorkflowRunStatus.RUNNING,
        step_status=StepRunStatus.RUNNING,
        outcome=None,
        analysis=AnalysisCursor(),
        lease=new_run_lease(),
        event_sequence=0,
        updated_at=now,
        operation=OperationState("tool-7", OperationStatus.RUNNING),
    )
    store = RunStore(config.paths.run_root)
    checkpoint = store.create(snapshot)
    store.release_lease(checkpoint)

    cancelled = WorkflowControlService(config).cancel(snapshot.run_id)

    assert cancelled.run_status is WorkflowRunStatus.CANCELLED
    assert cancelled.step_status is StepRunStatus.CANCELLED
    assert cancelled.outcome is StepOutcome.CANCELLED
    assert cancelled.operation.status is OperationStatus.UNKNOWN
    assert marker.read_text(encoding="utf-8") == "keep me"
