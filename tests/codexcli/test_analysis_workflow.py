from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from devloop.analysis.package import parse_analysis_draft
from devloop.application.analysis import AnalysisWorkflowError, AnalysisWorkflowService
from devloop.application.config import ApplicationConfig
from devloop.components.analysis import (
    AnalysisComponentRunner,
    AnalysisTurnOutput,
)
from devloop.components.builtin import installed_component_registry
from devloop.domain.identifiers import (
    ExecutionThreadId,
    ExecutionTurnId,
    FeatureSlug,
    StepInstanceId,
    WorkflowRunId,
)
from devloop.domain.run import (
    AnalysisCursor,
    AnalysisResponseKind,
    ComponentLock,
    OperationState,
    OperationStatus,
    ResolvedWorkflow,
    RunEventType,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.persistence.run_store import RUN_SNAPSHOT_SCHEMA, RunStore, new_run_lease
from devloop.workflow.definition import load_standard_workflow

RUN_ID = WorkflowRunId("run-20260711t120000-123456abcdef")


def _draft_payload() -> dict[str, object]:
    return {
        "schema": "devloop.analysis-draft/v1",
        "feature_title": "Price comparison",
        "feature_slug": "price-comparison",
        "prd_markdown": """<!-- devloop:prd:v1 -->
<!-- devloop:section:problem -->
REQ-001: Compare totals.
<!-- devloop:section:solution -->
Collect prices safely.
<!-- devloop:section:requirements -->
REQ-001: Compare totals.
""",
        "requirements": ["REQ-001"],
        "issues": [
            {
                "id": "ISSUE-001",
                "slug": "compare-totals",
                "title": "Compare totals",
                "requirements": ["REQ-001"],
                "dependencies": [],
                "acceptance_criteria": [
                    {"id": "AC-ISSUE-001-001", "text": "The lowest total is selected."}
                ],
                "markdown": """<!-- devloop:issue:v1 -->
<!-- devloop:section:description -->
Implement REQ-001.
<!-- devloop:section:acceptance -->
AC-ISSUE-001-001: The lowest total is selected.
""",
            }
        ],
        "revision": 1,
    }


def test_submitted_feature_locks_every_discovered_component_before_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    config = ApplicationConfig.resolve(
        repository,
        platform="linux",
        environment={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        home=tmp_path / "home",
    )

    def request_clarification(
        self: AnalysisComponentRunner,
        **kwargs: object,
    ) -> AnalysisTurnOutput:
        return AnalysisTurnOutput(
            AnalysisResponseKind.CLARIFICATION,
            ExecutionThreadId("thread-analysis-001"),
            ExecutionTurnId("turn-analysis-001"),
            "Which users need this feature?",
            None,
            (),
        )

    monkeypatch.setattr(AnalysisComponentRunner, "run_turn", request_clarification)

    result = AnalysisWorkflowService(config).start("Build a feature.")

    assert {lock.component_id for lock in result.snapshot.component_locks} == {
        manifest.component_id for manifest in installed_component_registry().manifests
    }


def test_analysis_checkpoints_the_running_attempt_before_starting_app_server_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    config = ApplicationConfig.resolve(
        repository,
        platform="linux",
        environment={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        home=tmp_path / "home",
    )

    def observe_checkpoint(
        self: AnalysisComponentRunner,
        **kwargs: object,
    ) -> AnalysisTurnOutput:
        persisted = RunStore(config.paths.run_root).list_unfinished()
        assert len(persisted) == 1
        assert persisted[0].run_status is WorkflowRunStatus.RUNNING
        assert persisted[0].step_status is StepRunStatus.RUNNING
        return AnalysisTurnOutput(
            AnalysisResponseKind.CLARIFICATION,
            ExecutionThreadId("thread-analysis-001"),
            ExecutionTurnId("turn-analysis-001"),
            "Which users need this feature?",
            None,
            (),
        )

    monkeypatch.setattr(AnalysisComponentRunner, "run_turn", observe_checkpoint)

    AnalysisWorkflowService(config).start("Build a feature.")


def _post_analysis_service(
    tmp_path: Path,
) -> tuple[AnalysisWorkflowService, RunStore, WorkflowRunSnapshot]:
    repository = tmp_path / "project"
    repository.mkdir()
    config = ApplicationConfig.resolve(
        repository,
        platform="linux",
        environment={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        home=tmp_path / "home",
    )
    workflow = load_standard_workflow()
    snapshot = WorkflowRunSnapshot(
        RUN_SNAPSHOT_SCHEMA,
        RUN_ID,
        str(repository),
        "Price comparison",
        FeatureSlug("price-comparison"),
        ResolvedWorkflow(workflow.workflow_id, workflow.version, workflow.definition_hash),
        _installed_component_locks(),
        StepInstanceId("workspace-preparation"),
        WorkflowRunStatus.AWAITING_USER,
        StepRunStatus.NOT_STARTED,
        None,
        AnalysisCursor(
            thread_id=ExecutionThreadId("thread-analysis-001"),
            draft_revision=1,
        ),
        new_run_lease(),
        0,
        datetime.now(timezone.utc).isoformat(),
    )
    store = RunStore(config.paths.run_root)
    created = store.create(snapshot)
    return AnalysisWorkflowService(config), store, created


def _awaiting_analysis_service(
    tmp_path: Path,
) -> tuple[AnalysisWorkflowService, RunStore, WorkflowRunSnapshot]:
    repository = tmp_path / "project"
    repository.mkdir()
    config = ApplicationConfig.resolve(
        repository,
        platform="linux",
        environment={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        home=tmp_path / "home",
    )
    workflow = load_standard_workflow()
    snapshot = WorkflowRunSnapshot(
        RUN_SNAPSHOT_SCHEMA,
        RUN_ID,
        str(repository),
        "Price comparison",
        FeatureSlug("price-comparison"),
        ResolvedWorkflow(workflow.workflow_id, workflow.version, workflow.definition_hash),
        _installed_component_locks(),
        StepInstanceId("analysis"),
        WorkflowRunStatus.AWAITING_USER,
        StepRunStatus.AWAITING_USER,
        None,
        AnalysisCursor(
            thread_id=ExecutionThreadId("thread-analysis-001"),
            draft_revision=1,
        ),
        new_run_lease(),
        0,
        datetime.now(timezone.utc).isoformat(),
    )
    store = RunStore(config.paths.run_root)
    created = store.create(snapshot)
    store.save_draft(parse_analysis_draft(_draft_payload(), snapshot.run_id))
    return AnalysisWorkflowService(config), store, created


def _installed_component_locks() -> tuple[ComponentLock, ...]:
    return tuple(
        ComponentLock(
            manifest.component_id,
            manifest.version,
            manifest.distribution,
            manifest.package_hash,
        )
        for manifest in installed_component_registry().manifests
    )


def test_previous_draft_cannot_be_accepted_while_clarification_is_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, snapshot = _awaiting_analysis_service(tmp_path)

    def request_clarification(
        self: AnalysisComponentRunner,
        **kwargs: object,
    ) -> AnalysisTurnOutput:
        return AnalysisTurnOutput(
            AnalysisResponseKind.CLARIFICATION,
            ExecutionThreadId("thread-analysis-001"),
            ExecutionTurnId("turn-analysis-002"),
            "Which repositories should the revision cover?",
            None,
            (),
        )

    monkeypatch.setattr(AnalysisComponentRunner, "run_turn", request_clarification)

    result = service.continue_analysis(snapshot.run_id, "Revise the package scope.")

    assert result.clarification == "Which repositories should the revision cover?"
    with pytest.raises(AnalysisWorkflowError, match="clarification"):
        service.accept(snapshot.run_id)
    assert not (Path(snapshot.repository) / "prd" / "price-comparison").exists()
    assert store.load(snapshot.run_id).analysis.clarification == result.clarification


def test_resume_restores_an_interrupted_analysis_to_an_awaiting_user_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, snapshot = _awaiting_analysis_service(tmp_path)
    interrupted = store.record(
        replace(
            snapshot,
            run_status=WorkflowRunStatus.PAUSED,
            step_status=StepRunStatus.RUNNING,
        ),
        RunEventType.RUN_PAUSED,
    )
    store.release_lease(interrupted)
    monkeypatch.setattr(
        AnalysisComponentRunner,
        "validate_resume",
        lambda self, repository, thread_id: None,
    )

    result = service.resume(snapshot.run_id)

    assert result.snapshot.run_status is WorkflowRunStatus.AWAITING_USER
    assert result.snapshot.step_status is StepRunStatus.AWAITING_USER


def test_resume_recovers_the_checkpointed_analysis_turn_before_accepting_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, snapshot = _awaiting_analysis_service(tmp_path)
    turn_id = ExecutionTurnId("turn-analysis-002")
    interrupted = store.record(
        replace(
            snapshot,
            run_status=WorkflowRunStatus.PAUSED,
            step_status=StepRunStatus.RUNNING,
            analysis=replace(snapshot.analysis, turn_id=turn_id),
        ),
        RunEventType.RUN_PAUSED,
    )
    store.release_lease(interrupted)
    payload = _draft_payload()
    payload["revision"] = 2
    recovered_draft = parse_analysis_draft(payload, snapshot.run_id)
    recovered_calls: list[tuple[ExecutionThreadId, ExecutionTurnId]] = []

    def recover_turn(
        self: AnalysisComponentRunner,
        **kwargs: object,
    ) -> AnalysisTurnOutput:
        thread_id = kwargs["thread_id"]
        recovered_turn_id = kwargs["turn_id"]
        assert isinstance(thread_id, ExecutionThreadId)
        assert isinstance(recovered_turn_id, ExecutionTurnId)
        item_started = kwargs["on_item_started"]
        item_completed = kwargs["on_item_completed"]
        assert callable(item_started)
        assert callable(item_completed)
        item_started("analysis-item-002")
        assert store.load(snapshot.run_id).operation.item_id == "analysis-item-002"
        item_completed("analysis-item-002")
        assert store.load(snapshot.run_id).analysis.completed_item_ids == (
            "analysis-item-002",
        )
        recovered_calls.append((thread_id, recovered_turn_id))
        return AnalysisTurnOutput(
            AnalysisResponseKind.DRAFT,
            thread_id,
            recovered_turn_id,
            None,
            recovered_draft,
            ("analysis-item-002",),
        )

    monkeypatch.setattr(AnalysisComponentRunner, "recover_turn", recover_turn, raising=False)
    monkeypatch.setattr(
        AnalysisComponentRunner,
        "validate_resume",
        lambda self, repository, thread_id: None,
    )

    result = service.resume(snapshot.run_id)

    assert recovered_calls == [(snapshot.analysis.thread_id, turn_id)]
    assert result.draft == recovered_draft
    assert result.snapshot.analysis.draft_revision == 2
    assert result.snapshot.step_status is StepRunStatus.AWAITING_USER
    assert store.load_draft(snapshot.run_id) == recovered_draft


def test_analysis_resume_never_replays_an_unknown_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, snapshot = _awaiting_analysis_service(tmp_path)
    interrupted = store.record(
        replace(
            snapshot,
            run_status=WorkflowRunStatus.PAUSED,
            step_status=StepRunStatus.RUNNING,
            analysis=replace(
                snapshot.analysis,
                turn_id=ExecutionTurnId("turn-analysis-unknown"),
            ),
            operation=OperationState("command-unknown", OperationStatus.UNKNOWN),
        ),
        RunEventType.RUN_PAUSED,
    )
    store.release_lease(interrupted)

    def unexpected_recovery(self: AnalysisComponentRunner, **kwargs: object) -> None:
        raise AssertionError("Unknown work reached the App Server recovery boundary.")

    monkeypatch.setattr(AnalysisComponentRunner, "recover_turn", unexpected_recovery)

    with pytest.raises(AnalysisWorkflowError, match="Recovery Attempt"):
        service.resume(snapshot.run_id)


def test_fresh_analysis_recovery_uses_locked_state_without_a_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, snapshot = _awaiting_analysis_service(tmp_path)
    fresh = store.record(
        replace(
            snapshot,
            run_status=WorkflowRunStatus.RUNNING,
            step_status=StepRunStatus.RUNNING,
            analysis=replace(
                snapshot.analysis,
                thread_id=None,
                turn_id=None,
                completed_item_ids=(),
            ),
            operation=OperationState(),
        ),
        RunEventType.RECOVERY_ATTEMPT_STARTED,
    )
    payload = _draft_payload()
    payload["revision"] = 2
    recovered_draft = parse_analysis_draft(payload, snapshot.run_id)

    def recover_from_context(
        self: AnalysisComponentRunner,
        **kwargs: object,
    ) -> AnalysisTurnOutput:
        assert kwargs["thread_id"] is None
        message = kwargs["message"]
        assert isinstance(message, str)
        assert "devloop.analysis-recovery-context/v1" in message
        assert "thread-analysis-001" not in message
        assert "full transcript" not in message.casefold()
        return AnalysisTurnOutput(
            AnalysisResponseKind.DRAFT,
            ExecutionThreadId("thread-analysis-recovery"),
            ExecutionTurnId("turn-analysis-recovery"),
            None,
            recovered_draft,
            (),
        )

    monkeypatch.setattr(AnalysisComponentRunner, "run_turn", recover_from_context)

    result = service.recover_fresh(fresh.run_id)

    assert result.snapshot.analysis.thread_id == ExecutionThreadId(
        "thread-analysis-recovery"
    )
    assert result.draft == recovered_draft


def test_continuation_is_rejected_after_analysis_has_advanced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, snapshot = _post_analysis_service(tmp_path)

    def unexpected_turn(self: AnalysisComponentRunner, **kwargs: object) -> None:
        raise AssertionError("Post-analysis continuation reached the execution boundary.")

    monkeypatch.setattr(AnalysisComponentRunner, "run_turn", unexpected_turn)

    with pytest.raises(AnalysisWorkflowError, match="awaiting user input in analysis"):
        service.continue_analysis(snapshot.run_id, "Change the accepted plan.")

    persisted = store.load(snapshot.run_id)
    assert persisted.active_step == StepInstanceId("workspace-preparation")
    assert persisted.event_sequence == snapshot.event_sequence


def test_acceptance_is_rejected_after_analysis_has_advanced(tmp_path: Path) -> None:
    service, store, snapshot = _post_analysis_service(tmp_path)
    store.save_draft(parse_analysis_draft(_draft_payload(), snapshot.run_id))

    with pytest.raises(AnalysisWorkflowError, match="awaiting user input in analysis"):
        service.accept(snapshot.run_id)

    persisted = store.load(snapshot.run_id)
    assert persisted.active_step == StepInstanceId("workspace-preparation")
    assert persisted.event_sequence == snapshot.event_sequence
    assert not (Path(snapshot.repository) / "prd" / "price-comparison").exists()
