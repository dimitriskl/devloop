from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from devloop.application.config import ApplicationConfig
from devloop.application.recovery import (
    RecoveryBackendStatus,
    RecoveryDisposition,
    RecoveryService,
    RecoveryValidation,
)
from devloop.components.builtin import installed_component_registry
from devloop.domain.development import (
    ArtifactRef,
    ContextManifestRef,
    DevelopmentCursor,
    IssueRuntimeState,
    IssueStatus,
    WorkspaceKind,
    WorkspaceRef,
)
from devloop.domain.identifiers import (
    AttemptId,
    ExecutionThreadId,
    ExecutionTurnId,
    FeatureSlug,
    IssueId,
    StepInstanceId,
    WorkflowRunId,
)
from devloop.domain.outcomes import StepOutcome
from devloop.domain.review_qa import QaCursor, ReviewCursor
from devloop.domain.run import (
    AnalysisCursor,
    ComponentLock,
    OperationState,
    OperationStatus,
    ResolvedWorkflow,
    RunEventType,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.domain.scheduler import AttemptStatus, IssueAttemptRecord
from devloop.execution.app_server import AppServerTurnStatus
from devloop.persistence.run_store import RUN_SNAPSHOT_SCHEMA, RunStore, new_run_lease
from devloop.workflow.definition import load_standard_workflow

RUN_ID = WorkflowRunId("run-20260712t120000-123456abcdef")


def _compatible_backend(snapshot: WorkflowRunSnapshot) -> RecoveryBackendStatus:
    return RecoveryBackendStatus(
        compatible=True,
        thread_available=True,
        turn_status=AppServerTurnStatus.COMPLETED,
    )


def _analysis_snapshot(repository: Path) -> WorkflowRunSnapshot:
    workflow = load_standard_workflow()
    locks = tuple(
        ComponentLock(
            manifest.component_id,
            manifest.version,
            manifest.distribution,
            manifest.package_hash,
        )
        for manifest in installed_component_registry().manifests
    )
    return WorkflowRunSnapshot(
        schema=RUN_SNAPSHOT_SCHEMA,
        run_id=RUN_ID,
        repository=str(repository.resolve()),
        feature_title="Recover exact cursor",
        feature_slug=FeatureSlug("recover-exact-cursor"),
        workflow=ResolvedWorkflow(
            workflow.workflow_id,
            workflow.version,
            workflow.definition_hash,
        ),
        component_locks=locks,
        active_step=StepInstanceId("analysis"),
        run_status=WorkflowRunStatus.PAUSED,
        step_status=StepRunStatus.RUNNING,
        outcome=None,
        analysis=AnalysisCursor(
            ExecutionThreadId("analysis-thread"),
            ExecutionTurnId("analysis-turn"),
            1,
            None,
            ("completed-item",),
        ),
        lease=new_run_lease(),
        event_sequence=0,
        updated_at=datetime.now(timezone.utc).isoformat(),
        operation=OperationState("unknown-command", OperationStatus.UNKNOWN),
    )


def test_fresh_recovery_attempt_records_unknown_operation_and_discards_thread_state(
    tmp_path: Path,
) -> None:
    config = ApplicationConfig.resolve(tmp_path)
    store = RunStore(config.paths.run_root)
    store.create(_analysis_snapshot(tmp_path))
    service = RecoveryService(config, backend_probe=_compatible_backend)

    plan = service.inspect(RUN_ID)
    recovered = service.start_fresh_attempt(RUN_ID)

    assert plan.disposition is RecoveryDisposition.FRESH_ATTEMPT
    assert plan.validation is RecoveryValidation.UNKNOWN_OPERATION
    assert recovered.run_status is WorkflowRunStatus.RUNNING
    assert recovered.operation == OperationState()
    assert recovered.analysis.thread_id is None
    assert recovered.analysis.turn_id is None
    assert recovered.analysis.completed_item_ids == ()
    events = [
        json.loads(line)
        for line in (store.run_directory(RUN_ID) / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["type"] for event in events[-2:]] == [
        "OPERATION_UNKNOWN",
        "RECOVERY_ATTEMPT_STARTED",
    ]


def test_workspace_checkpoint_continues_without_an_app_server_thread(tmp_path: Path) -> None:
    config = ApplicationConfig.resolve(tmp_path)
    store = RunStore(config.paths.run_root)
    checkpoint = replace(
        _analysis_snapshot(tmp_path),
        active_step=StepInstanceId("workspace-preparation"),
        step_status=StepRunStatus.AWAITING_USER,
        analysis=AnalysisCursor(),
        operation=OperationState(),
    )
    store.create(checkpoint)

    plan = RecoveryService(config, backend_probe=_compatible_backend).inspect(RUN_ID)

    assert plan.disposition is RecoveryDisposition.CONTINUE_WORKFLOW
    assert plan.validation is RecoveryValidation.VALID


def test_shutdown_before_workspace_finalization_restores_a_safe_presentation(
    tmp_path: Path,
) -> None:
    config = ApplicationConfig.resolve(tmp_path)
    store = RunStore(config.paths.run_root)
    checkpoint = replace(
        _analysis_snapshot(tmp_path),
        active_step=StepInstanceId("workspace-finalization"),
        run_status=WorkflowRunStatus.AWAITING_USER,
        step_status=StepRunStatus.NOT_STARTED,
        analysis=AnalysisCursor(),
        operation=OperationState(),
    )
    store.create(checkpoint)

    plan = RecoveryService(config, backend_probe=_compatible_backend).inspect(RUN_ID)

    assert plan.disposition is RecoveryDisposition.CONTINUE_WORKFLOW
    assert plan.validation is RecoveryValidation.VALID


def test_shutdown_during_workspace_finalization_refuses_the_unknown_operation(
    tmp_path: Path,
) -> None:
    config = ApplicationConfig.resolve(tmp_path)
    store = RunStore(config.paths.run_root)
    running = store.create(
        replace(
            _analysis_snapshot(tmp_path),
            active_step=StepInstanceId("workspace-finalization"),
            run_status=WorkflowRunStatus.RUNNING,
            step_status=StepRunStatus.RUNNING,
            analysis=AnalysisCursor(),
            operation=OperationState("finalization-operation", OperationStatus.RUNNING),
        )
    )
    lease_path = store.run_directory(RUN_ID) / "lease.json"
    lease = json.loads(lease_path.read_text(encoding="utf-8"))
    lease["process_id"] = 2_147_483_647
    lease_path.write_text(json.dumps(lease), encoding="utf-8")

    plan = RecoveryService(config, backend_probe=_compatible_backend).inspect(
        running.run_id
    )

    assert plan.snapshot.run_status is WorkflowRunStatus.PAUSED
    assert plan.snapshot.operation.status is OperationStatus.UNKNOWN
    assert plan.disposition is RecoveryDisposition.REFUSE
    assert plan.validation is RecoveryValidation.UNKNOWN_OPERATION


def test_resume_refuses_a_checkpoint_with_a_missing_component_lock(tmp_path: Path) -> None:
    config = ApplicationConfig.resolve(tmp_path)
    store = RunStore(config.paths.run_root)
    checkpoint = _analysis_snapshot(tmp_path)
    store.create(replace(checkpoint, component_locks=checkpoint.component_locks[:-1]))

    plan = RecoveryService(config, backend_probe=_compatible_backend).inspect(RUN_ID)

    assert plan.disposition is RecoveryDisposition.REFUSE
    assert plan.validation is RecoveryValidation.DRIFT
    assert "Locked Workflow components changed or are unavailable." in plan.diagnostics


def test_resume_refuses_an_incompatible_app_server_before_continuation(
    tmp_path: Path,
) -> None:
    config = ApplicationConfig.resolve(tmp_path)
    store = RunStore(config.paths.run_root)
    checkpoint = replace(
        _analysis_snapshot(tmp_path),
        operation=OperationState(),
    )
    store.create(checkpoint)

    service = RecoveryService(
        config,
        backend_probe=lambda snapshot: RecoveryBackendStatus(
            compatible=False,
            thread_available=False,
            diagnostic="Codex App Server compatibility could not be validated.",
        ),
    )
    plan = service.inspect(RUN_ID)

    assert plan.disposition is RecoveryDisposition.REFUSE
    assert plan.validation is RecoveryValidation.APP_SERVER_INCOMPATIBLE
    assert plan.diagnostics == (
        "Codex App Server compatibility could not be validated.",
    )


@pytest.mark.parametrize(
    ("turn_status", "expected_disposition"),
    [
        (None, RecoveryDisposition.FRESH_ATTEMPT),
        (AppServerTurnStatus.INTERRUPTED, RecoveryDisposition.FRESH_ATTEMPT),
        (AppServerTurnStatus.IN_PROGRESS, RecoveryDisposition.CONTINUE_THREAD),
        (AppServerTurnStatus.COMPLETED, RecoveryDisposition.CONTINUE_THREAD),
    ],
)
def test_resume_probes_the_exact_persisted_turn_before_continuing(
    tmp_path: Path,
    turn_status: AppServerTurnStatus | None,
    expected_disposition: RecoveryDisposition,
) -> None:
    config = ApplicationConfig.resolve(tmp_path)
    store = RunStore(config.paths.run_root)
    store.create(replace(_analysis_snapshot(tmp_path), operation=OperationState()))
    service = RecoveryService(
        config,
        backend_probe=lambda snapshot: RecoveryBackendStatus(
            compatible=True,
            thread_available=True,
            turn_status=turn_status,
        ),
    )

    plan = service.inspect(RUN_ID)

    assert plan.disposition is expected_disposition


def test_resume_refuses_source_drift_from_the_implementation_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "devloop@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Dev Loop Tests"],
        check=True,
    )
    (repository / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "baseline"],
        check=True,
    )
    branch = subprocess.run(
        ["git", "-C", str(repository), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config = ApplicationConfig.resolve(repository)
    store = RunStore(config.paths.run_root)
    workspace = WorkspaceRef(
        WorkspaceKind.CURRENT_CHECKOUT,
        str(repository.resolve()),
        str(repository.resolve()),
        branch,
        head,
    )
    created = store.create(
        replace(
            _analysis_snapshot(repository),
            active_step=StepInstanceId("code-review"),
            analysis=AnalysisCursor(),
            workspace=workspace,
            operation=OperationState(),
            workspace_state_hash="checkpointed-source-state",
        )
    )
    context = store.save_json_artifact(
        RUN_ID,
        Path("context-manifests/ISSUE-003.json"),
        {"schema": "devloop.context-manifest/v1"},
    )
    implementation = store.save_json_artifact(
        RUN_ID,
        Path("implementation-results/ISSUE-003.json"),
        {"repository_state_hash": "checkpointed-source-state"},
    )
    checkpoint = store.record(
        replace(
            created,
            development=DevelopmentCursor(
                IssueId("ISSUE-003"),
                3,
                10,
                AttemptId("attempt-001"),
                ContextManifestRef(context.path, context.content_hash),
                implementation_result=implementation,
            ),
        ),
        RunEventType.DEVELOPMENT_SUCCEEDED,
    )

    plan = RecoveryService(
        config,
        backend_probe=_compatible_backend,
        source_state_probe=lambda path: "drifted-source-state",
    ).inspect(checkpoint.run_id)

    assert plan.disposition is RecoveryDisposition.REFUSE
    assert plan.validation is RecoveryValidation.DRIFT
    assert "Workspace source state differs from the checkpoint." in plan.diagnostics


def test_resume_refuses_source_drift_during_an_active_development_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    xdg_config = tmp_path / "xdg"
    xdg_config.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "devloop@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Dev Loop Tests"],
        check=True,
    )
    (repository / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "baseline"],
        check=True,
    )
    branch = subprocess.run(
        ["git", "-C", str(repository), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config = ApplicationConfig.resolve(repository)
    store = RunStore(config.paths.run_root)
    workspace = WorkspaceRef(
        WorkspaceKind.CURRENT_CHECKOUT,
        str(repository.resolve()),
        str(repository.resolve()),
        branch,
        head,
    )
    created = store.create(
        replace(
            _analysis_snapshot(repository),
            active_step=StepInstanceId("development"),
            analysis=AnalysisCursor(),
            workspace=workspace,
            operation=OperationState(),
            workspace_state_hash="checkpointed-development-state",
        )
    )
    context = store.save_json_artifact(
        RUN_ID,
        Path("context-manifests/ISSUE-003.json"),
        {"schema": "devloop.context-manifest/v1"},
    )
    checkpoint = store.record(
        replace(
            created,
            development=DevelopmentCursor(
                IssueId("ISSUE-003"),
                3,
                10,
                AttemptId("attempt-001"),
                ContextManifestRef(context.path, context.content_hash),
                ExecutionThreadId("development-thread"),
                ExecutionTurnId("development-turn"),
            ),
        ),
        RunEventType.DEVELOPMENT_TURN_STARTED,
    )

    plan = RecoveryService(
        config,
        backend_probe=_compatible_backend,
        source_state_probe=lambda path: "drifted-development-state",
    ).inspect(checkpoint.run_id)

    assert plan.disposition is RecoveryDisposition.REFUSE
    assert plan.validation is RecoveryValidation.DRIFT
    assert "Workspace source state differs from the checkpoint." in plan.diagnostics


def test_fresh_qa_recovery_preserves_structured_input_and_discards_execution_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "devloop@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Dev Loop Tests"],
        check=True,
    )
    (repository / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "baseline"],
        check=True,
    )
    branch = subprocess.run(
        ["git", "-C", str(repository), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config = ApplicationConfig.resolve(repository)
    store = RunStore(config.paths.run_root)
    workspace = WorkspaceRef(
        WorkspaceKind.CURRENT_CHECKOUT,
        str(repository.resolve()),
        str(repository.resolve()),
        branch,
        head,
    )
    created = store.create(
        replace(
            _analysis_snapshot(repository),
            active_step=StepInstanceId("qa"),
            analysis=AnalysisCursor(),
            workspace=workspace,
            workspace_state_hash="checkpointed-source-state",
        )
    )
    context = store.save_json_artifact(
        RUN_ID,
        Path("context-manifests/ISSUE-003.json"),
        {"schema": "devloop.context-manifest/v1"},
    )
    implementation = store.save_json_artifact(
        RUN_ID,
        Path("implementation-results/ISSUE-003.json"),
        {"repository_state_hash": "checkpointed-source-state"},
    )
    review_input = store.save_json_artifact(
        RUN_ID,
        Path("review-inputs/ISSUE-003.json"),
        {"schema": "devloop.review-input/v1"},
    )
    review_result = store.save_json_artifact(
        RUN_ID,
        Path("review-results/ISSUE-003.json"),
        {"schema": "devloop.review-result/v1"},
    )
    qa_input = store.save_json_artifact(
        RUN_ID,
        Path("qa-inputs/ISSUE-003.json"),
        {"schema": "devloop.qa-input/v1", "capability_profile": ["qa"]},
    )
    issue_id = IssueId("ISSUE-003")
    attempt_id = AttemptId("attempt-001")
    checkpoint = store.record(
        replace(
            created,
            development=DevelopmentCursor(
                issue_id,
                3,
                10,
                attempt_id,
                ContextManifestRef(context.path, context.content_hash),
                implementation_result=implementation,
            ),
            review=ReviewCursor(
                issue_id,
                attempt_id,
                review_input,
                ExecutionThreadId("review-thread"),
                ExecutionTurnId("review-turn"),
                ("review-item",),
                review_result,
            ),
            qa=QaCursor(
                issue_id,
                attempt_id,
                qa_input,
                ExecutionThreadId("qa-thread"),
                ExecutionTurnId("qa-turn"),
                ("qa-item",),
            ),
        ),
        RunEventType.QA_TURN_STARTED,
    )
    service = RecoveryService(
        config,
        backend_probe=_compatible_backend,
        source_state_probe=lambda path: "checkpointed-source-state",
    )

    recovered = service.start_fresh_attempt(checkpoint.run_id)

    assert recovered.qa is not None
    assert recovered.qa.input_manifest == qa_input
    assert recovered.qa.thread_id is None
    assert recovered.qa.turn_id is None
    assert recovered.qa.completed_item_ids == ()
    assert store.load_json_artifact(RUN_ID, recovered.qa.input_manifest) == {
        "schema": "devloop.qa-input/v1",
        "capability_profile": ["qa"],
    }
    events = [
        json.loads(line)["type"]
        for line in (store.run_directory(RUN_ID) / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events[-2:] == ["OPERATION_UNKNOWN", "RECOVERY_ATTEMPT_STARTED"]


def test_resume_refuses_a_tampered_approval_artifact_reachable_from_the_cursor(
    tmp_path: Path,
) -> None:
    config = ApplicationConfig.resolve(tmp_path)
    store = RunStore(config.paths.run_root)
    created = store.create(
        replace(
            _analysis_snapshot(tmp_path),
            active_step=StepInstanceId("development"),
            analysis=AnalysisCursor(),
            operation=OperationState(),
        )
    )
    context = store.save_json_artifact(
        RUN_ID,
        Path("context-manifests/ISSUE-003.json"),
        {"schema": "devloop.context-manifest/v1"},
    )
    approval = store.save_json_artifact(
        RUN_ID,
        Path("approvals/ISSUE-003.json"),
        {"schema": "devloop.approval-request/v1"},
    )
    checkpoint = store.record(
        replace(
            created,
            development=DevelopmentCursor(
                IssueId("ISSUE-003"),
                3,
                10,
                AttemptId("attempt-001"),
                ContextManifestRef(context.path, context.content_hash),
                approval_request=approval,
            ),
        ),
        RunEventType.DEVELOPMENT_APPROVAL_REQUIRED,
    )
    (store.run_directory(RUN_ID) / approval.path).write_text(
        '{"schema":"tampered"}\n',
        encoding="utf-8",
    )

    plan = RecoveryService(config, backend_probe=_compatible_backend).inspect(
        checkpoint.run_id
    )

    assert plan.disposition is RecoveryDisposition.REFUSE
    assert "Run Artifact hash does not match its checkpoint." in plan.diagnostics


def test_resume_refuses_a_missing_artifact_reachable_only_from_attempt_history(
    tmp_path: Path,
) -> None:
    config = ApplicationConfig.resolve(tmp_path)
    store = RunStore(config.paths.run_root)
    created = store.create(replace(_analysis_snapshot(tmp_path), operation=OperationState()))
    implementation = store.save_json_artifact(
        RUN_ID, Path("history/implementation.json"), {"kind": "implementation"}
    )
    review = store.save_json_artifact(
        RUN_ID, Path("history/review.json"), {"kind": "review"}
    )
    qa_result = store.save_json_artifact(
        RUN_ID, Path("history/qa.json"), {"kind": "qa"}
    )
    attempt = IssueAttemptRecord(
        IssueId("ISSUE-001"),
        1,
        AttemptStatus.COMPLETED,
        StepOutcome.SUCCEEDED,
        ArtifactRef(implementation.path, implementation.content_hash),
        ArtifactRef(review.path, review.content_hash),
        ArtifactRef(qa_result.path, qa_result.content_hash),
        None,
        ExecutionThreadId("development-thread-1"),
        ExecutionThreadId("review-thread-1"),
        ExecutionThreadId("qa-thread-1"),
    )
    checkpoint = store.record(
        replace(created, attempts=(attempt,)),
        RunEventType.ISSUE_ATTEMPT_ARCHIVED,
    )
    (store.run_directory(RUN_ID) / qa_result.path).unlink()

    plan = RecoveryService(config, backend_probe=_compatible_backend).inspect(
        checkpoint.run_id
    )

    assert plan.disposition is RecoveryDisposition.REFUSE
    assert "Run Artifact is missing or invalid." in plan.diagnostics


def test_ten_issue_checkpoint_recovers_issue_three_qa_without_advancing_other_issues(
    tmp_path: Path,
) -> None:
    config = ApplicationConfig.resolve(tmp_path)
    store = RunStore(config.paths.run_root)
    issue_states = tuple(
        IssueRuntimeState(
            IssueId(f"ISSUE-{number:03}"),
            IssueStatus.COMPLETED
            if number <= 2
            else IssueStatus.IN_QA
            if number == 3
            else IssueStatus.PENDING,
            StepInstanceId("qa") if number == 3 else None,
        )
        for number in range(1, 11)
    )
    created = store.create(
        replace(
            _analysis_snapshot(tmp_path),
            active_step=StepInstanceId("qa"),
            analysis=AnalysisCursor(),
            issues=issue_states,
            operation=OperationState(),
        )
    )
    qa_input = store.save_json_artifact(
        RUN_ID,
        Path("qa-inputs/ISSUE-003.json"),
        {"schema": "devloop.qa-input/v1"},
    )
    checkpoint = store.record(
        replace(
            created,
            qa=QaCursor(
                IssueId("ISSUE-003"),
                AttemptId("attempt-001"),
                qa_input,
                ExecutionThreadId("qa-thread-issue-003"),
                ExecutionTurnId("qa-turn-issue-003"),
                ("qa-item-1",),
            ),
        ),
        RunEventType.QA_TURN_STARTED,
    )
    service = RecoveryService(config, backend_probe=_compatible_backend)

    candidate = service.list_candidates()[0]
    plan = service.inspect(checkpoint.run_id)

    assert candidate.issue_id == IssueId("ISSUE-003")
    assert candidate.step == StepInstanceId("qa")
    assert plan.disposition is RecoveryDisposition.CONTINUE_THREAD
    assert plan.snapshot.qa is not None
    assert plan.snapshot.qa.thread_id == ExecutionThreadId("qa-thread-issue-003")
    assert [state.status for state in plan.snapshot.issues[:3]] == [
        IssueStatus.COMPLETED,
        IssueStatus.COMPLETED,
        IssueStatus.IN_QA,
    ]
    assert all(state.status is IssueStatus.PENDING for state in plan.snapshot.issues[3:])
