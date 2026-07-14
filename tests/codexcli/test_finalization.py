from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from devloop.application.config import ApplicationConfig
from devloop.application.finalization import FinalizationService, build_handoff_summary
from devloop.domain.development import (
    ArtifactRef,
    IssueRuntimeState,
    IssueStatus,
    WorkspaceKind,
    WorkspaceRef,
)
from devloop.domain.finalization import HANDOFF_SUMMARY_SCHEMA, WorkspaceDisposition
from devloop.domain.identifiers import (
    ExecutionThreadId,
    FeatureSlug,
    IssueId,
    StepInstanceId,
    WorkflowId,
    WorkflowRunId,
)
from devloop.domain.outcomes import StepOutcome
from devloop.domain.run import (
    AnalysisCursor,
    ResolvedWorkflow,
    RunEventType,
    RunLease,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.domain.scheduler import AttemptStatus, IssueAttemptRecord
from devloop.persistence.run_store import RUN_SNAPSHOT_SCHEMA, RunStore
from devloop.workflow.definition import load_standard_workflow

RUN_ID = WorkflowRunId("run-20260712t120000-123456abcdef")


def _drained_snapshot(repository: Path) -> WorkflowRunSnapshot:
    workflow = load_standard_workflow()
    implementation = ArtifactRef("implementation-results/ISSUE-001-attempt-001.json", "i" * 64)
    review = ArtifactRef("review-results/ISSUE-001-attempt-001.json", "r" * 64)
    qa_result = ArtifactRef("qa-results/ISSUE-001-attempt-001.json", "q" * 64)
    return WorkflowRunSnapshot(
        schema=RUN_SNAPSHOT_SCHEMA,
        run_id=RUN_ID,
        repository=str(repository),
        feature_title="Release finalization",
        feature_slug=FeatureSlug("release-finalization"),
        workflow=ResolvedWorkflow(
            WorkflowId("standard-development"),
            workflow.version,
            workflow.definition_hash,
        ),
        component_locks=(),
        active_step=StepInstanceId("workspace-finalization"),
        run_status=WorkflowRunStatus.AWAITING_USER,
        step_status=StepRunStatus.NOT_STARTED,
        outcome=StepOutcome.SUCCEEDED,
        analysis=AnalysisCursor(),
        lease=RunLease("lease", os.getpid(), "2026-07-12T12:00:00+00:00"),
        event_sequence=0,
        updated_at="2026-07-12T12:00:00+00:00",
        workspace=WorkspaceRef(
            WorkspaceKind.CURRENT_CHECKOUT,
            str(repository),
            str(repository),
            "feature/release",
            "a" * 40,
        ),
        issues=(IssueRuntimeState(IssueId("ISSUE-001"), IssueStatus.COMPLETED),),
        attempts=(
            IssueAttemptRecord(
                IssueId("ISSUE-001"),
                1,
                AttemptStatus.COMPLETED,
                StepOutcome.SUCCEEDED,
                implementation,
                review,
                qa_result,
                None,
                development_thread=ExecutionThreadId("thread-development"),
                review_thread=ExecutionThreadId("thread-review"),
                qa_thread=ExecutionThreadId("thread-qa"),
            ),
        ),
    )


def test_handoff_summary_aggregates_typed_completed_results() -> None:
    snapshot = _drained_snapshot(Path("/repo"))

    summary = build_handoff_summary(
        snapshot,
        implementations=(
            {
                "changed_files": [
                    {"path": "src/example.py", "kind": "MODIFIED"},
                    {"path": "src/example.py", "kind": "MODIFIED"},
                ],
                "risks": ["Linux gate remains to be run."],
            },
        ),
        qa_results=(
            {
                "checks": [
                    {
                        "id": "QA-001",
                        "status": "PASSED",
                        "command": "pytest -q",
                        "evidence": "214 tests passed.",
                    }
                ],
                "residual_risks": ["Linux gate remains to be run."],
            },
        ),
    )

    assert summary.schema == HANDOFF_SUMMARY_SCHEMA
    assert summary.completed_issues == (IssueId("ISSUE-001"),)
    assert summary.changed_files == ("src/example.py",)
    assert summary.verification_evidence == (
        "QA-001 | PASSED | pytest -q | 214 tests passed.",
    )
    assert summary.residual_risks == ("Linux gate remains to be run.",)
    assert summary.workspace_disposition is WorkspaceDisposition.LEAVE_INTACT


def test_finalization_persists_handoff_and_completes_without_repository_actions(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "project"
    repository.mkdir()
    marker = repository / "keep.txt"
    marker.write_text("unchanged\n", encoding="utf-8")
    config = ApplicationConfig.resolve(repository, platform="linux", home=tmp_path / "home")
    store = RunStore(config.paths.run_root)
    snapshot = _drained_snapshot(repository)
    created = store.create(snapshot)
    implementation = store.save_json_artifact(
        RUN_ID,
        Path(snapshot.attempts[0].implementation.path),
        {"changed_files": [{"path": "src/example.py", "kind": "MODIFIED"}], "risks": []},
    )
    review = store.save_json_artifact(
        RUN_ID,
        Path(snapshot.attempts[0].review.path),
        {"schema": "devloop.review-result/v1", "findings": [], "summary": "Passed."},
    )
    qa_result = store.save_json_artifact(
        RUN_ID,
        Path(snapshot.attempts[0].qa_result.path),
        {
            "checks": [
                {
                    "id": "QA-001",
                    "status": "PASSED",
                    "command": "pytest -q",
                    "evidence": "All tests passed.",
                }
            ],
            "residual_risks": [],
        },
    )
    archived = replace(
        snapshot.attempts[0],
        implementation=implementation,
        review=review,
        qa_result=qa_result,
    )
    store.record(
        replace(created, attempts=(archived,)),
        RunEventType.ISSUE_ATTEMPT_ARCHIVED,
    )

    completed = FinalizationService(config).finalize(RUN_ID)

    assert completed.snapshot.run_status is WorkflowRunStatus.COMPLETED
    assert completed.snapshot.step_status is StepRunStatus.SUCCEEDED
    assert completed.snapshot.finalization is not None
    assert completed.summary.workspace_disposition is WorkspaceDisposition.LEAVE_INTACT
    assert marker.read_text(encoding="utf-8") == "unchanged\n"
    assert completed.snapshot.workspace is not None
    assert Path(completed.snapshot.workspace.path) == repository
