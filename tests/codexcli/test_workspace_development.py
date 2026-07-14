from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from devloop.analysis.package import parse_analysis_draft, publish_analysis_package
from devloop.application.config import ApplicationConfig
from devloop.application.development import (
    DevelopmentInterrupted,
    DevelopmentPaused,
    ReworkLimitReachedError,
    WorkspaceDevelopmentError,
    WorkspaceDevelopmentService,
    implementation_result_to_dict,
)
from devloop.application.finalization import FinalizationService
from devloop.application.review_qa import (
    ReviewQaError,
    ReviewQaPaused,
    ReviewQaService,
    review_result_to_dict,
)
from devloop.application.scheduler import SchedulerAction, WorkflowSchedulerService
from devloop.components.builtin import builtin_component_registry
from devloop.components.development import (
    DevelopmentAgentOutput,
    DevelopmentComponentError,
    DevelopmentTurnInterrupted,
    DevelopmentTurnPaused,
)
from devloop.components.qa import QaAgentOutput, QaComponentError, QaTurnPaused
from devloop.components.review import ReviewAgentOutput
from devloop.components.workspace import WorkspacePreparationCancelled, WorkspaceProposal
from devloop.domain.development import (
    ArtifactRef,
    ChangeKind,
    CriterionImplementation,
    CriterionImplementationStatus,
    ImplementationResult,
    IssueStatus,
    PlanningPackageRef,
    WorkspaceChoice,
    WorkspaceKind,
)
from devloop.domain.doctor import redact_diagnostic
from devloop.domain.identifiers import (
    AcceptanceCriterionId,
    AttemptId,
    ExecutionThreadId,
    ExecutionTurnId,
    FeatureSlug,
    IssueId,
    QaCheckId,
    ReviewFindingId,
    StepInstanceId,
    WorkflowRunId,
)
from devloop.domain.outcomes import StepOutcome
from devloop.domain.review_qa import (
    REVIEW_RESULT_SCHEMA,
    CheckRequirement,
    FindingDisposition,
    FindingSeverity,
    QaCheck,
    QaCheckKind,
    QaCheckStatus,
    QaCursor,
    ReviewCursor,
    ReviewFinding,
    ReviewResult,
)
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
from devloop.execution.app_server import (
    AppServerApprovalKind,
    AppServerApprovalRequest,
    AppServerApprovalRequired,
    AppServerRequestMethod,
    AppServerTransientError,
)
from devloop.infrastructure.development_approval import is_safe_development_command
from devloop.infrastructure.git import (
    GitOperationError,
    capture_repository_state_hash,
    capture_workspace_baseline,
    capture_worktree_changes,
    render_relevant_diff,
    run_git,
)
from devloop.infrastructure.windows_acl import is_safe_windows_acl_grant
from devloop.persistence.run_store import RUN_SNAPSHOT_SCHEMA, RunStore, new_run_lease
from devloop.workflow.definition import load_standard_workflow


def _payload() -> dict[str, object]:
    return {
        "schema": "devloop.analysis-draft/v1",
        "feature_title": "Hello feature",
        "feature_slug": "hello-feature",
        "prd_markdown": """<!-- devloop:prd:v1 -->
<!-- devloop:section:problem -->
REQ-001: A greeting is needed.
<!-- devloop:section:solution -->
Add a greeting module.
<!-- devloop:section:requirements -->
REQ-001: Add a tested greeting function.
""",
        "requirements": ["REQ-001"],
        "issues": [
            {
                "id": "ISSUE-001",
                "slug": "add-greeting",
                "title": "Add greeting",
                "requirements": ["REQ-001"],
                "dependencies": [],
                "acceptance_criteria": [
                    {
                        "id": "AC-ISSUE-001-001",
                        "text": "A tested greeting function returns Hello.",
                    }
                ],
                "markdown": """<!-- devloop:issue:v1 -->
<!-- devloop:section:description -->
Add a small `greeting.py` module and focused automated test.
<!-- devloop:section:acceptance -->
AC-ISSUE-001-001: A tested greeting function returns Hello.
""",
            }
        ],
        "revision": 1,
    }


def _git_repository(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "core.excludesFile", ""], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "devloop-tests@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Dev Loop Tests"], cwd=path, check=True)
    (path / "README.md").write_text("# Test project\n", encoding="utf-8")
    (path / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n",
        encoding="ascii",
    )
    subprocess.run(["git", "add", "README.md", ".gitignore"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


def _accepted_run(
    repository: Path,
    run_id: WorkflowRunId,
    *,
    payload: dict[str, object] | None = None,
) -> ApplicationConfig:
    environment = {
        "APPDATA": str(repository.parent / "user-config"),
        "LOCALAPPDATA": str(repository.parent / "user-data"),
    }
    config = ApplicationConfig.resolve(repository, environment=environment)
    draft = parse_analysis_draft(_payload() if payload is None else payload, run_id)
    published = publish_analysis_package(repository, draft)
    workflow = load_standard_workflow()
    locks = tuple(
        ComponentLock(
            manifest.component_id,
            manifest.version,
            manifest.distribution,
            manifest.package_hash,
        )
        for manifest in builtin_component_registry().manifests
    )
    snapshot = WorkflowRunSnapshot(
        RUN_SNAPSHOT_SCHEMA,
        run_id,
        str(repository),
        draft.feature_title,
        FeatureSlug("hello-feature"),
        ResolvedWorkflow(workflow.workflow_id, workflow.version, workflow.definition_hash),
        locks,
        StepInstanceId("workspace-preparation"),
        WorkflowRunStatus.AWAITING_USER,
        StepRunStatus.NOT_STARTED,
        None,
        AnalysisCursor(draft_revision=1),
        new_run_lease(),
        0,
        datetime.now(timezone.utc).isoformat(),
        PlanningPackageRef(published.root, published.prd_hash, published.issue_set_hash),
    )
    RunStore(config.paths.run_root).create(snapshot)
    return config


def _dependency_payload() -> dict[str, object]:
    payload = _payload()
    issues = payload["issues"]
    assert isinstance(issues, list)
    issues.append(
        {
            "id": "ISSUE-002",
            "slug": "add-farewell",
            "title": "Add farewell",
            "requirements": ["REQ-001"],
            "dependencies": ["ISSUE-001"],
            "acceptance_criteria": [
                {
                    "id": "AC-ISSUE-002-001",
                    "text": "A tested farewell function returns Goodbye.",
                }
            ],
            "markdown": """<!-- devloop:issue:v1 -->
<!-- devloop:section:description -->
Add a small `farewell.py` module and focused automated test.
<!-- devloop:section:acceptance -->
AC-ISSUE-002-001: A tested farewell function returns Goodbye.
""",
        }
    )
    return payload


def _blocked_dependency_payload() -> dict[str, object]:
    payload = _dependency_payload()
    issues = payload["issues"]
    assert isinstance(issues, list)
    second = issues[1]
    assert isinstance(second, dict)
    second["dependencies"] = []
    issues.append(
        {
            "id": "ISSUE-003",
            "slug": "use-greeting",
            "title": "Use greeting",
            "requirements": ["REQ-001"],
            "dependencies": ["ISSUE-001"],
            "acceptance_criteria": [
                {
                    "id": "AC-ISSUE-003-001",
                    "text": "The completed greeting is consumed.",
                }
            ],
            "markdown": """<!-- devloop:issue:v1 -->
<!-- devloop:section:description -->
Use the completed greeting.
<!-- devloop:section:acceptance -->
AC-ISSUE-003-001: The completed greeting is consumed.
""",
        }
    )
    return payload


def _seed_implementation(
    config: ApplicationConfig,
    run_id: WorkflowRunId,
    *,
    source: str,
    test_source: str,
) -> WorkflowRunSnapshot:
    store = RunStore(config.paths.run_root)
    snapshot = store.load(run_id)
    workspace = snapshot.workspace
    development = snapshot.development
    assert workspace is not None
    assert development is not None
    repository = Path(workspace.path)
    (repository / "greeting.py").write_text(source, encoding="utf-8")
    tests = repository / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_greeting.py").write_text(test_source, encoding="utf-8")
    changes = capture_worktree_changes(
        repository,
        workspace.base_commit,
        workspace.baseline,
    )
    result = ImplementationResult(
        "devloop.implementation-result/v1",
        development.attempt_id,
        changes.base_state,
        changes.result_state,
        changes.diff_hash,
        changes.repository_state_hash,
        changes.changed_files,
        (
            CriterionImplementation(
                AcceptanceCriterionId(
                    development.issue_id.value.replace("ISSUE", "AC-ISSUE") + "-001"
                ),
                CriterionImplementationStatus.IMPLEMENTED,
                "Seeded repository state for a real downstream phase test.",
            ),
        ),
        ("pytest -q",),
        (),
        (),
        (),
        "Seeded typed implementation result.",
    )
    artifact = store.save_json_artifact(
        run_id,
        Path("implementation-results")
        / f"{development.issue_id.value}-{development.attempt_id.value}.json",
        implementation_result_to_dict(result),
    )
    updated = replace(
        snapshot,
        active_step=StepInstanceId("code-review"),
        step_status=StepRunStatus.NOT_STARTED,
        issues=tuple(
            replace(
                item,
                status=IssueStatus.IN_REVIEW,
                current_step=StepInstanceId("code-review"),
            )
            for item in snapshot.issues
        ),
        development=replace(
            development,
            thread_id=ExecutionThreadId("seed-development-thread"),
            implementation_result=artifact,
        ),
        workspace_state_hash=changes.repository_state_hash,
    )
    return store.record(updated, RunEventType.DEVELOPMENT_SUCCEEDED)


def _seed_accepted_review(
    config: ApplicationConfig,
    run_id: WorkflowRunId,
) -> WorkflowRunSnapshot:
    store = RunStore(config.paths.run_root)
    snapshot = store.load(run_id)
    development = snapshot.development
    assert development is not None
    assert development.implementation_result is not None
    implementation = store.load_json_artifact(run_id, development.implementation_result)
    diff_hash = implementation["diff_hash"]
    assert isinstance(diff_hash, str)
    input_ref = store.save_json_artifact(
        run_id,
        Path("review-inputs") / f"{development.issue_id.value}-seed.json",
        {"schema": "devloop.review-input/v1"},
    )
    review_result = ReviewResult(
        REVIEW_RESULT_SCHEMA,
        development.attempt_id,
        diff_hash,
        (),
        "Seeded accepted review for a real QA phase test.",
    )
    result_ref = store.save_json_artifact(
        run_id,
        Path("review-results") / f"{development.issue_id.value}-seed.json",
        review_result_to_dict(review_result),
    )
    updated = replace(
        snapshot,
        active_step=StepInstanceId("qa"),
        issues=tuple(
            replace(item, status=IssueStatus.IN_QA, current_step=StepInstanceId("qa"))
            for item in snapshot.issues
        ),
        review=ReviewCursor(
            development.issue_id,
            development.attempt_id,
            input_ref,
            ExecutionThreadId("seed-review-thread"),
            review_result=result_ref,
        ),
    )
    return store.record(updated, RunEventType.REVIEW_SUCCEEDED)


def test_workflow_scheduler_projects_board_and_drains_completed_issue(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260711t040000-123456abcdef")
    config = _accepted_run(repository, run_id)
    development = WorkspaceDevelopmentService(config)
    prepared = development.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    scheduler = WorkflowSchedulerService(config)

    board = scheduler.issue_board(run_id)

    assert len(board) == 1
    assert board[0].status is IssueStatus.IN_DEVELOPMENT
    completed = replace(
        prepared.snapshot,
        issues=(replace(prepared.snapshot.issues[0], status=IssueStatus.COMPLETED),),
    )
    RunStore(config.paths.run_root).record(completed, RunEventType.ISSUE_ATTEMPT_ARCHIVED)

    advanced = scheduler.advance(run_id)

    assert advanced.action is SchedulerAction.WORKFLOW_DRAINED
    assert advanced.snapshot.active_step == StepInstanceId("workspace-finalization")


@pytest.mark.parametrize(
    ("choice", "expected_kind"),
    [
        (WorkspaceChoice.CURRENT_CHECKOUT, WorkspaceKind.CURRENT_CHECKOUT),
        (WorkspaceChoice.DEDICATED_WORKTREE, WorkspaceKind.DEDICATED_WORKTREE),
    ],
)
def test_workspace_choice_is_explicit_and_context_contains_only_selected_issue(
    tmp_path: Path,
    choice: WorkspaceChoice,
    expected_kind: WorkspaceKind,
) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId(
        "run-20260710t120010-123456abcdef"
        if choice is WorkspaceChoice.CURRENT_CHECKOUT
        else "run-20260710t120011-123456abcdef"
    )
    config = _accepted_run(repository, run_id, payload=_dependency_payload())
    service = WorkspaceDevelopmentService(config)
    worktree_parent = tmp_path / "worktrees"
    proposal = service.proposal(run_id, worktree_parent=worktree_parent)

    assert not proposal.dedicated_path.exists()
    prepared = service.prepare(run_id, choice, worktree_parent=worktree_parent)

    assert prepared.workspace.kind is expected_kind
    assert prepared.snapshot.development is not None
    context_ref = prepared.snapshot.development.context_manifest
    context_path = config.paths.run_root / run_id.value / context_ref.path
    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert context["issue"]["id"] == "ISSUE-001"
    assert "ISSUE-002" not in json.dumps(context)
    assert prepared.snapshot.issues[0].status.value == "IN_DEVELOPMENT"
    assert prepared.snapshot.active_step.value == "development"


def test_workspace_choice_is_checkpointed_before_git_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260710t120019-123456abcdef")
    config = _accepted_run(repository, run_id)
    service = WorkspaceDevelopmentService(config)
    def observe_checkpoint(
        proposal: WorkspaceProposal,
        choice: WorkspaceChoice,
    ) -> object:
        persisted = RunStore(config.paths.run_root).load(run_id)
        assert persisted.active_step == StepInstanceId("workspace-preparation")
        assert persisted.run_status is WorkflowRunStatus.RUNNING
        assert persisted.step_status is StepRunStatus.RUNNING
        raise WorkspacePreparationCancelled("Stop after observing the checkpoint.")

    monkeypatch.setattr(service._workspace_runner, "prepare", observe_checkpoint)

    with pytest.raises(WorkspacePreparationCancelled):
        service.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)


def test_workspace_cancel_makes_no_git_or_path_change(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260710t120012-123456abcdef")
    config = _accepted_run(repository, run_id)
    service = WorkspaceDevelopmentService(config)
    worktree_parent = tmp_path / "worktrees"
    proposal = service.proposal(run_id, worktree_parent=worktree_parent)
    refs_before = subprocess.run(
        ["git", "show-ref"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout

    with pytest.raises(WorkspacePreparationCancelled):
        service.prepare(run_id, WorkspaceChoice.CANCEL, worktree_parent=worktree_parent)

    refs_after = subprocess.run(
        ["git", "show-ref"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout
    assert refs_after == refs_before
    assert not proposal.dedicated_path.exists()


def test_development_revalidates_the_selected_branch_before_backend_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260710t120014-123456abcdef")
    config = _accepted_run(repository, run_id)
    service = WorkspaceDevelopmentService(config)
    service.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    subprocess.run(
        ["git", "switch", "-c", "unexpected-branch"],
        cwd=repository,
        check=True,
        capture_output=True,
    )

    def backend_must_not_start(**_: object) -> object:
        raise AssertionError("The real App Server was reached before workspace validation.")

    monkeypatch.setattr(service._development_runner, "run_turn", backend_must_not_start)

    with pytest.raises(WorkspaceDevelopmentError, match="branch"):
        service.develop(run_id)


def test_second_issue_implementation_excludes_completed_issue_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260712t120000-123456abcdef")
    config = _accepted_run(repository, run_id, payload=_dependency_payload())
    service = WorkspaceDevelopmentService(config)
    first = service.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    (repository / "greeting.py").write_text("def greeting(): return 'Hello'\n", encoding="utf-8")
    store = RunStore(config.paths.run_root)
    completed_first = replace(
        first.snapshot,
        issues=tuple(
            replace(item, status=IssueStatus.COMPLETED)
            if item.issue_id.value == "ISSUE-001"
            else item
            for item in first.snapshot.issues
        ),
    )
    store.record(completed_first, RunEventType.ISSUE_ATTEMPT_ARCHIVED)
    second = service.prepare_next_ready(run_id)

    def complete_second(**_: object) -> DevelopmentAgentOutput:
        (repository / "farewell.py").write_text(
            "def farewell(): return 'Goodbye'\n",
            encoding="utf-8",
        )
        return DevelopmentAgentOutput(
            ExecutionThreadId("development-thread-issue-002"),
            ExecutionTurnId("development-turn-issue-002"),
            (),
            (
                CriterionImplementation(
                    AcceptanceCriterionId("AC-ISSUE-002-001"),
                    CriterionImplementationStatus.IMPLEMENTED,
                    "Focused behavior implemented.",
                ),
            ),
            ("python -m unittest",),
            (),
            (),
            (),
            "Implemented only the second Issue.",
        )

    monkeypatch.setattr(service._development_runner, "run_turn", complete_second)

    completed = service.develop(run_id)

    assert second.issue.issue_id.value == "ISSUE-002"
    assert [item.path for item in completed.result.changed_files] == ["farewell.py"]

    review_qa = ReviewQaService(config)
    captured_review_input: dict[str, object] = {}

    def approve_second_issue(**arguments: object) -> ReviewAgentOutput:
        review_input = arguments["review_input"]
        assert isinstance(review_input, dict)
        captured_review_input.update(review_input)
        thread_bound = arguments["on_thread_bound"]
        turn_started = arguments["on_turn_started"]
        assert callable(thread_bound)
        assert callable(turn_started)
        thread_bound(ExecutionThreadId("review-thread-issue-002"))
        turn_started(ExecutionTurnId("review-turn-issue-002"))
        return ReviewAgentOutput(
            ExecutionThreadId("review-thread-issue-002"),
            ExecutionTurnId("review-turn-issue-002"),
            (),
            (),
            "The second Issue is ready for QA.",
            None,
        )

    monkeypatch.setattr(review_qa._review_runner, "run_turn", approve_second_issue)

    review_qa.review(run_id)

    assert "farewell.py" in str(captured_review_input["relevant_diff"])
    assert "greeting.py" not in str(captured_review_input["relevant_diff"])

    captured_qa_input: dict[str, object] = {}

    def verify_second_issue(**arguments: object) -> QaAgentOutput:
        qa_input = arguments["qa_input"]
        assert isinstance(qa_input, dict)
        captured_qa_input.update(qa_input)
        thread_bound = arguments["on_thread_bound"]
        turn_started = arguments["on_turn_started"]
        assert callable(thread_bound)
        assert callable(turn_started)
        thread_bound(ExecutionThreadId("qa-thread-issue-002"))
        turn_started(ExecutionTurnId("qa-turn-issue-002"))
        return QaAgentOutput(
            ExecutionThreadId("qa-thread-issue-002"),
            ExecutionTurnId("qa-turn-issue-002"),
            (),
            (
                QaCheck(
                    QaCheckId("QC-002"),
                    AcceptanceCriterionId("AC-ISSUE-002-001"),
                    QaCheckKind.TEST,
                    CheckRequirement.REQUIRED,
                    QaCheckStatus.PASSED,
                    "python -m unittest",
                    0,
                    1,
                    "The focused behavior passed.",
                    "",
                    "The farewell returns Goodbye.",
                    "The focused check exits successfully.",
                ),
            ),
            (),
            "Every required QA check passed.",
        )

    monkeypatch.setattr(review_qa._qa_runner, "run_turn", verify_second_issue)

    review_qa.qa(run_id)

    repository_state = captured_qa_input["repository_state"]
    assert isinstance(repository_state, dict)
    assert repository_state["changed_files"] == [
        {"path": "farewell.py", "kind": "UNTRACKED"}
    ]
    assert "greeting.py" not in str(repository_state["relevant_diff"])


def test_development_pause_and_resume_preserve_the_same_attempt_context_and_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260710t120015-123456abcdef")
    config = _accepted_run(repository, run_id)
    service = WorkspaceDevelopmentService(config)
    prepared = service.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    assert prepared.snapshot.workspace_state_hash == capture_repository_state_hash(repository)
    original_cursor = prepared.snapshot.development
    original_workspace = prepared.snapshot.workspace
    assert original_cursor is not None
    assert original_workspace is not None
    service.request_development_pause(run_id)

    def interrupt_turn(**arguments: object) -> object:
        thread_bound = arguments["on_thread_bound"]
        turn_started = arguments["on_turn_started"]
        pause_requested = arguments["pause_requested"]
        assert callable(thread_bound)
        assert callable(turn_started)
        assert callable(pause_requested)
        thread_bound(ExecutionThreadId("development-thread-same-attempt"))
        turn_started(ExecutionTurnId("development-turn-interrupted"))
        assert pause_requested()
        raise DevelopmentTurnPaused("Paused by the user.")

    monkeypatch.setattr(service._development_runner, "run_turn", interrupt_turn)

    with pytest.raises(DevelopmentPaused) as paused_error:
        service.develop(run_id)

    paused = paused_error.value.snapshot
    paused_cursor = paused.development
    assert paused.run_status is WorkflowRunStatus.PAUSED
    assert paused_cursor is not None
    assert paused_cursor.issue_id == original_cursor.issue_id
    assert paused_cursor.attempt_id == original_cursor.attempt_id
    assert paused_cursor.context_manifest == original_cursor.context_manifest
    assert paused_cursor.thread_id == ExecutionThreadId("development-thread-same-attempt")
    assert paused_cursor.turn_id is None
    assert paused.workspace == original_workspace

    monkeypatch.setattr(
        service._development_runner,
        "validate_resume",
        lambda workspace, thread_id: None,
    )

    def complete_resumed_turn(**arguments: object) -> DevelopmentAgentOutput:
        assert arguments["thread_id"] == ExecutionThreadId(
            "development-thread-same-attempt"
        )
        context = arguments["context_manifest"]
        assert isinstance(context, dict)
        assert context["issue"]["id"] == "ISSUE-001"
        turn_started = arguments["on_turn_started"]
        assert callable(turn_started)
        turn_started(ExecutionTurnId("development-turn-resumed"))
        (repository / "greeting.py").write_text(
            'def greeting() -> str:\n    return "Hello"\n',
            encoding="utf-8",
        )
        return DevelopmentAgentOutput(
            ExecutionThreadId("development-thread-same-attempt"),
            ExecutionTurnId("development-turn-resumed"),
            (),
            (
                CriterionImplementation(
                    AcceptanceCriterionId("AC-ISSUE-001-001"),
                    CriterionImplementationStatus.IMPLEMENTED,
                    "The resumed turn added the greeting behavior.",
                ),
            ),
            ("python -m pytest -q",),
            (),
            (),
            (),
            "Resumed the same attempt and completed development.",
        )

    monkeypatch.setattr(service._development_runner, "run_turn", complete_resumed_turn)

    completed = service.resume_development(run_id)

    assert completed.snapshot.active_step == StepInstanceId("code-review")
    assert completed.snapshot.development is not None
    assert completed.snapshot.development.attempt_id == original_cursor.attempt_id
    assert completed.snapshot.development.context_manifest == original_cursor.context_manifest
    assert completed.snapshot.workspace == original_workspace


def test_development_interrupt_stops_only_the_active_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260710t120017-123456abcdef")
    config = _accepted_run(repository, run_id)
    service = WorkspaceDevelopmentService(config)
    prepared = service.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    original = prepared.snapshot.development
    assert original is not None

    def interrupt_turn(**arguments: object) -> object:
        thread_bound = arguments["on_thread_bound"]
        turn_started = arguments["on_turn_started"]
        interrupt_requested = arguments["interrupt_requested"]
        assert callable(thread_bound)
        assert callable(turn_started)
        assert callable(interrupt_requested)
        thread_bound(ExecutionThreadId("development-thread-interrupt-only"))
        turn_started(ExecutionTurnId("development-turn-interrupt-only"))
        service.request_development_interrupt(run_id)
        assert interrupt_requested()
        raise DevelopmentTurnInterrupted("Interrupted by the user.")

    monkeypatch.setattr(service._development_runner, "run_turn", interrupt_turn)

    with pytest.raises(DevelopmentInterrupted) as interrupted_error:
        service.develop(run_id)

    interrupted = interrupted_error.value.snapshot
    cursor = interrupted.development
    assert interrupted.run_status is WorkflowRunStatus.AWAITING_USER
    assert interrupted.step_status is StepRunStatus.AWAITING_USER
    assert cursor is not None
    assert cursor.attempt_id == original.attempt_id
    assert cursor.thread_id == ExecutionThreadId("development-thread-interrupt-only")
    assert cursor.turn_id is None


def test_development_resume_checkpoints_recovered_app_server_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260710t120020-123456abcdef")
    config = _accepted_run(repository, run_id)
    service = WorkspaceDevelopmentService(config)
    prepared = service.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    cursor = prepared.snapshot.development
    assert cursor is not None
    store = RunStore(config.paths.run_root)
    interrupted = store.record(
        replace(
            prepared.snapshot,
            run_status=WorkflowRunStatus.PAUSED,
            development=replace(
                cursor,
                thread_id=ExecutionThreadId("development-thread-recovery"),
                turn_id=ExecutionTurnId("development-turn-recovery"),
            ),
        ),
        RunEventType.RUN_PAUSED,
    )
    store.release_lease(interrupted)

    def observe_recovered_items(**arguments: object) -> DevelopmentAgentOutput:
        item_started = arguments["on_item_started"]
        item_completed = arguments["on_item_completed"]
        assert callable(item_started)
        assert callable(item_completed)
        assert store.load(run_id).run_status is WorkflowRunStatus.RUNNING
        item_started("development-command-recovered")
        assert store.load(run_id).operation == OperationState(
            "development-command-recovered",
            OperationStatus.RUNNING,
        )
        (repository / "recovered.txt").write_text("recovered bytes\n", encoding="utf-8")
        item_completed("development-command-recovered")
        persisted = store.load(run_id)
        assert persisted.operation == OperationState()
        assert persisted.workspace_state_hash == capture_repository_state_hash(repository)
        assert persisted.development is not None
        assert persisted.development.completed_item_ids == (
            "development-command-recovered",
        )
        raise DevelopmentComponentError("Stop after observing recovered items.")

    monkeypatch.setattr(
        service._development_runner,
        "recover_completed_turn",
        observe_recovered_items,
    )

    with pytest.raises(DevelopmentComponentError, match="observing recovered items"):
        service.resume_development(run_id)


def test_development_persists_a_typed_redacted_approval_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260710t120016-123456abcdef")
    config = _accepted_run(repository, run_id)
    service = WorkspaceDevelopmentService(config)
    service.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)

    def request_approval(**arguments: object) -> object:
        thread_bound = arguments["on_thread_bound"]
        turn_started = arguments["on_turn_started"]
        assert callable(thread_bound)
        assert callable(turn_started)
        thread_bound(ExecutionThreadId("development-thread-approval"))
        turn_started(ExecutionTurnId("development-turn-approval"))
        raise AppServerApprovalRequired(
            AppServerApprovalRequest(
                "approval-request-7",
                AppServerApprovalKind.COMMAND,
                AppServerRequestMethod.COMMAND_APPROVAL.value,
                "git status --short token=secret-value",
                str(repository),
                "Inspect token=secret-value before continuing.",
                ("accept", "acceptForSession", "decline", "cancel"),
                "development-thread-approval",
                "development-turn-approval",
                "item-approval-7",
            )
        )

    monkeypatch.setattr(service._development_runner, "run_turn", request_approval)

    with pytest.raises(WorkspaceDevelopmentError, match="approval decision"):
        service.develop(run_id)

    paused = RunStore(config.paths.run_root).load(run_id)
    assert paused.run_status is WorkflowRunStatus.PAUSED
    assert paused.development is not None
    approval_ref = paused.development.approval_request
    assert approval_ref is not None
    approval = RunStore(config.paths.run_root).load_json_artifact(run_id, approval_ref)
    assert approval == {
        "schema": "devloop.approval-request/v1",
        "step_id": "development",
        "issue_id": "ISSUE-001",
        "attempt_id": "attempt-001",
        "request_id": "approval-request-7",
        "kind": "COMMAND",
        "method": "item/commandExecution/requestApproval",
        "action": "git status --short token=[redacted]",
        "target": redact_diagnostic(str(repository), limit=2000),
        "reason": "Inspect token=[redacted] before continuing.",
        "supported_decisions": ["accept", "acceptForSession", "decline", "cancel"],
        "decision": None,
        "thread_id": "development-thread-approval",
        "turn_id": "development-turn-approval",
        "item_id": "item-approval-7",
    }


@pytest.mark.parametrize(
    "recovery_error",
    [AppServerTransientError, DevelopmentComponentError],
)
def test_transient_failed_turn_restarts_on_the_same_development_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recovery_error: type[Exception],
) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260710t120017-123456abcdef")
    config = _accepted_run(repository, run_id)
    service = WorkspaceDevelopmentService(config)
    service.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    run_calls: list[ExecutionThreadId | None] = []
    recover_calls: list[tuple[ExecutionThreadId, ExecutionTurnId]] = []
    monkeypatch.setattr("devloop.application.retry.time.sleep", lambda _: None)

    def run_turn(**arguments: object) -> DevelopmentAgentOutput:
        thread_id = arguments["thread_id"]
        assert thread_id is None or isinstance(thread_id, ExecutionThreadId)
        run_calls.append(thread_id)
        thread_bound = arguments["on_thread_bound"]
        turn_started = arguments["on_turn_started"]
        assert callable(thread_bound)
        assert callable(turn_started)
        thread_bound(ExecutionThreadId("development-thread-transient"))
        if len(run_calls) == 1:
            turn_started(ExecutionTurnId("development-turn-disconnected"))
            raise AppServerTransientError("response stream disconnected")
        turn_started(ExecutionTurnId("development-turn-replacement"))
        (repository / "greeting.py").write_text(
            'def greeting() -> str:\n    return "Hello"\n',
            encoding="utf-8",
        )
        return DevelopmentAgentOutput(
            ExecutionThreadId("development-thread-transient"),
            ExecutionTurnId("development-turn-replacement"),
            (),
            (
                CriterionImplementation(
                    AcceptanceCriterionId("AC-ISSUE-001-001"),
                    CriterionImplementationStatus.IMPLEMENTED,
                    "The replacement turn completed the greeting behavior.",
                ),
            ),
            ("python -m pytest -q",),
            (),
            (),
            (),
            "Recovered the transient turn failure.",
        )

    def recover_completed_turn(**arguments: object) -> DevelopmentAgentOutput:
        thread_id = arguments["thread_id"]
        turn_id = arguments["turn_id"]
        assert isinstance(thread_id, ExecutionThreadId)
        assert isinstance(turn_id, ExecutionTurnId)
        recover_calls.append((thread_id, turn_id))
        raise recovery_error("checkpointed turn has no usable structured output")

    monkeypatch.setattr(service._development_runner, "run_turn", run_turn)
    monkeypatch.setattr(
        service._development_runner,
        "recover_completed_turn",
        recover_completed_turn,
    )

    completed = service.develop(run_id)

    assert run_calls == [None, ExecutionThreadId("development-thread-transient")]
    assert recover_calls == [
        (
            ExecutionThreadId("development-thread-transient"),
            ExecutionTurnId("development-turn-disconnected"),
        )
    ]
    assert completed.snapshot.active_step == StepInstanceId("code-review")
    assert completed.snapshot.development is not None
    assert completed.snapshot.development.transient_retries == 1


def test_development_diff_excludes_unchanged_preexisting_untracked_files(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "planning.md").write_text("accepted planning\n", encoding="utf-8")
    baseline = capture_workspace_baseline(repository)
    (repository / "implementation.py").write_text("VALUE = 1\n", encoding="utf-8")

    changes = capture_worktree_changes(repository, base_commit, baseline)

    assert [item.path for item in changes.changed_files] == ["implementation.py"]


def test_worktree_changes_distinguish_index_only_state_changes(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "README.md").write_text("# Changed\n", encoding="utf-8")
    unstaged = capture_worktree_changes(repository, base_commit)

    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    staged = capture_worktree_changes(repository, base_commit)

    assert staged.changed_files == unstaged.changed_files
    assert staged.diff_hash == unstaged.diff_hash
    assert staged.result_state == unstaged.result_state
    assert staged != unstaged


def test_worktree_changes_distinguish_index_content_with_the_same_status(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    readme = repository / "README.md"
    readme.write_text("# First staged value\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    readme.write_text("# Stable worktree value\n", encoding="utf-8")
    before = capture_worktree_changes(repository, base_commit)

    readme.write_text("# Second staged value\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    readme.write_text("# Stable worktree value\n", encoding="utf-8")
    after = capture_worktree_changes(repository, base_commit)

    assert after.changed_files == before.changed_files
    assert after.diff_hash == before.diff_hash
    assert after.result_state == before.result_state
    assert after != before


def test_repository_state_hash_distinguishes_unstaged_and_untracked_bytes_with_same_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    xdg_config = tmp_path / "xdg"
    xdg_config.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    repository = tmp_path / "project"
    _git_repository(repository)
    readme = repository / "README.md"
    untracked = repository / "notes.txt"
    readme.write_text("# First unstaged value\n", encoding="utf-8")
    untracked.write_text("first untracked value\n", encoding="utf-8")
    before = capture_repository_state_hash(repository)

    readme.write_text("# Second unstaged value\n", encoding="utf-8")
    untracked.write_text("second untracked value\n", encoding="utf-8")
    after = capture_repository_state_hash(repository)

    assert after != before


def test_worktree_changes_distinguish_head_only_state_changes(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    before = capture_worktree_changes(repository, base_commit)

    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "unexpected QA commit"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    after = capture_worktree_changes(repository, base_commit)

    assert after.changed_files == before.changed_files
    assert after.diff_hash == before.diff_hash
    assert after.result_state == before.result_state
    assert after != before


def test_worktree_changes_distinguish_branch_only_state_changes(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    before = capture_worktree_changes(repository, base_commit)

    subprocess.run(
        ["git", "switch", "-c", "unexpected-qa-branch"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    after = capture_worktree_changes(repository, base_commit)

    assert after.changed_files == before.changed_files
    assert after.diff_hash == before.diff_hash
    assert after.result_state == before.result_state
    assert after != before


def test_relevant_diff_contains_only_the_implementation_result_files(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "README.md").write_text("# Changed\n", encoding="utf-8")
    (repository / "new.py").write_text("VALUE = 1\n", encoding="utf-8")
    changes = capture_worktree_changes(repository, base_commit)

    rendered = render_relevant_diff(repository, base_commit, changes.changed_files)

    assert "README.md" in rendered
    assert "new.py" in rendered
    assert "# Test project" in rendered
    assert "VALUE = 1" in rendered


def test_development_diff_records_rename_destination(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    (repository / "old.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "old.py"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add old path"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "mv", "old.py", "new.py"], cwd=repository, check=True)

    changes = capture_worktree_changes(repository, base_commit)

    assert [(item.path, item.kind) for item in changes.changed_files] == [
        ("new.py", ChangeKind.RENAMED)
    ]
    assert "VALUE = 1" in render_relevant_diff(
        repository,
        base_commit,
        changes.changed_files,
    )


def test_git_state_capture_rejects_success_with_an_incomplete_status_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def incomplete_status(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=0,
            stdout="?? greeting.py\x00",
            stderr="warning: could not open directory 'tests/': Permission denied",
        )

    monkeypatch.setattr("devloop.infrastructure.git.subprocess.run", incomplete_status)

    with pytest.raises(GitOperationError, match="incomplete"):
        run_git(tmp_path, ("status", "--porcelain=v1"), fail_on_stderr=True)


def test_rework_prepares_a_fresh_attempt_with_only_the_typed_request(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260711t120000-123456abcdef")
    config = _accepted_run(repository, run_id, payload=_dependency_payload())
    service = WorkspaceDevelopmentService(config)
    prepared = service.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    cursor = prepared.snapshot.development
    assert cursor is not None
    store = RunStore(config.paths.run_root)
    rework = store.save_json_artifact(
        run_id,
        Path("rework-requests") / "ISSUE-001-attempt-001-code_review.json",
        {
            "schema": "devloop.rework-request/v1",
            "source": "CODE_REVIEW",
            "attempt_id": "attempt-001",
            "items": [
                {
                    "id": "RF-001",
                    "evidence": "greeting.py returns the wrong text.",
                    "expected_behavior": "Return Hello.",
                    "acceptance_condition": "The focused test passes.",
                }
            ],
        },
    )
    review_input = store.save_json_artifact(
        run_id,
        Path("review-inputs") / "ISSUE-001-attempt-001.json",
        {"schema": "devloop.review-input/v1"},
    )
    changes_requested = replace(
        prepared.snapshot,
        active_step=StepInstanceId("development"),
        outcome=StepOutcome.CHANGES_REQUESTED,
        issues=(
            replace(prepared.snapshot.issues[0], status=IssueStatus.CHANGES_REQUESTED),
        ),
        review=ReviewCursor(
            cursor.issue_id,
            cursor.attempt_id,
            review_input,
            rework_request=rework,
        ),
    )
    store.record(changes_requested, RunEventType.REVIEW_CHANGES_REQUESTED)

    retried = service.prepare_rework(run_id)

    retry_cursor = retried.snapshot.development
    assert retry_cursor is not None
    assert retry_cursor.attempt_id == AttemptId("attempt-002")
    assert retry_cursor.thread_id is None
    assert retried.snapshot.review is None
    context = store.load_json_artifact(run_id, retry_cursor.context_manifest)
    encoded = json.dumps(context)
    request = context["rework_request"]
    assert isinstance(request, dict)
    items = request["items"]
    assert isinstance(items, list)
    assert items[0]["id"] == "RF-001"
    assert "transcript" not in encoded.casefold()
    assert "ISSUE-002" not in encoded

    exhausted = replace(
        retried.snapshot,
        issues=tuple(
            replace(item, status=IssueStatus.CHANGES_REQUESTED)
            for item in retried.snapshot.issues
        ),
        outcome=StepOutcome.CHANGES_REQUESTED,
        development=replace(retry_cursor, attempt_id=AttemptId("attempt-003")),
        attempts=tuple(
            IssueAttemptRecord(
                retry_cursor.issue_id,
                number,
                AttemptStatus.CHANGES_REQUESTED,
                StepOutcome.CHANGES_REQUESTED,
                None,
                None,
                None,
                rework,
            )
            for number in range(1, 4)
        ),
    )
    store.record(exhausted, RunEventType.REVIEW_CHANGES_REQUESTED)

    with pytest.raises(ReworkLimitReachedError):
        service.prepare_rework(run_id)

    blocked = store.load(run_id)
    assert blocked.run_status is WorkflowRunStatus.PAUSED
    assert blocked.step_status is StepRunStatus.BLOCKED
    assert blocked.outcome is StepOutcome.BLOCKED
    assert blocked.issues[0].status is IssueStatus.BLOCKED


def test_scheduler_does_not_call_backend_after_rework_exhaustion_pauses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260712t120001-123456abcdef")
    config = _accepted_run(repository, run_id)
    prepared = WorkspaceDevelopmentService(config).prepare(
        run_id,
        WorkspaceChoice.CURRENT_CHECKOUT,
    )
    cursor = prepared.snapshot.development
    assert cursor is not None
    exhausted = replace(
        prepared.snapshot,
        outcome=StepOutcome.CHANGES_REQUESTED,
        issues=tuple(
            replace(item, status=IssueStatus.CHANGES_REQUESTED)
            for item in prepared.snapshot.issues
        ),
        development=replace(cursor, attempt_id=AttemptId("attempt-003")),
        attempts=tuple(
            IssueAttemptRecord(
                cursor.issue_id,
                number,
                AttemptStatus.CHANGES_REQUESTED,
                StepOutcome.CHANGES_REQUESTED,
                None,
                None,
                None,
                ArtifactRef(f"rework-{number}.json", f"rework-hash-{number}"),
            )
            for number in range(1, 4)
        ),
    )
    store = RunStore(config.paths.run_root)
    store.record(exhausted, RunEventType.REVIEW_CHANGES_REQUESTED)
    scheduler = WorkflowSchedulerService(config)
    backend_calls = 0

    def backend_must_not_start(**_: object) -> DevelopmentAgentOutput:
        nonlocal backend_calls
        backend_calls += 1
        raise AssertionError("paused preparation reached the development backend")

    monkeypatch.setattr(
        scheduler._development._development_runner,
        "run_turn",
        backend_must_not_start,
    )

    advanced = scheduler.advance(run_id)

    assert advanced.action is SchedulerAction.PAUSED
    assert advanced.snapshot.run_status is WorkflowRunStatus.PAUSED
    assert advanced.snapshot.issues[0].status is IssueStatus.BLOCKED
    assert backend_calls == 0


def test_scheduler_does_not_call_backend_after_failed_development_pauses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260712t120002-123456abcdef")
    config = _accepted_run(repository, run_id)
    development = WorkspaceDevelopmentService(config)
    development.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)

    def fail_development(**arguments: object) -> DevelopmentAgentOutput:
        thread_bound = arguments["on_thread_bound"]
        turn_started = arguments["on_turn_started"]
        assert callable(thread_bound)
        assert callable(turn_started)
        thread_bound(ExecutionThreadId("development-thread-failed-001"))
        turn_started(ExecutionTurnId("development-turn-failed-001"))
        raise RuntimeError("terminal development failure")

    monkeypatch.setattr(development._development_runner, "run_turn", fail_development)
    with pytest.raises(WorkspaceDevelopmentError, match="reset the Issue"):
        development.develop(run_id)

    scheduler = WorkflowSchedulerService(config)
    backend_calls = 0

    def backend_must_not_restart(**_: object) -> DevelopmentAgentOutput:
        nonlocal backend_calls
        backend_calls += 1
        raise AssertionError("failed Issue restarted without an explicit reset")

    monkeypatch.setattr(
        scheduler._development._development_runner,
        "run_turn",
        backend_must_not_restart,
    )

    advanced = scheduler.advance(run_id)

    assert advanced.action is SchedulerAction.PAUSED
    assert advanced.snapshot.issues[0].status is IssueStatus.FAILED
    assert backend_calls == 0

    with pytest.raises(WorkspaceDevelopmentError, match="not BLOCKED"):
        scheduler.retry_blocked_issue(run_id, IssueId("ISSUE-001"))

    reset = scheduler.reset_failed_issue(run_id, IssueId("ISSUE-001"))
    reset_cursor = reset.snapshot.development
    assert reset_cursor is not None
    assert reset_cursor.attempt_id == AttemptId("attempt-002")
    assert reset_cursor.thread_id is None
    reset_context = RunStore(config.paths.run_root).load_json_artifact(
        run_id,
        reset_cursor.context_manifest,
    )
    assert reset_context["attempt"]["authorization"]["kind"] == "FAILED_RESET"

    def complete_reset(**_: object) -> DevelopmentAgentOutput:
        (repository / "greeting.py").write_text(
            "def greeting(): return 'Hello'\n",
            encoding="utf-8",
        )
        return DevelopmentAgentOutput(
            ExecutionThreadId("development-thread-reset-001"),
            ExecutionTurnId("development-turn-reset-001"),
            (),
            (
                CriterionImplementation(
                    AcceptanceCriterionId("AC-ISSUE-001-001"),
                    CriterionImplementationStatus.IMPLEMENTED,
                    "The reset attempt completed successfully.",
                ),
            ),
            ("python -m unittest",),
            (),
            (),
            (),
            "Completed the explicit failed reset.",
        )

    monkeypatch.setattr(
        scheduler._development._development_runner,
        "run_turn",
        complete_reset,
    )

    resumed = scheduler.advance(run_id)

    assert resumed.action is SchedulerAction.DEVELOPMENT_COMPLETED
    assert resumed.snapshot.development is not None
    assert resumed.snapshot.development.thread_id == ExecutionThreadId(
        "development-thread-reset-001"
    )
    assert resumed.snapshot.attempts[0].development_thread == ExecutionThreadId(
        "development-thread-failed-001"
    )


def test_development_block_continues_independent_issue_before_scheduler_pauses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260712t120003-123456abcdef")
    config = _accepted_run(repository, run_id, payload=_blocked_dependency_payload())
    WorkspaceDevelopmentService(config).prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    scheduler = WorkflowSchedulerService(config)
    attempted_issues: list[str] = []

    def execute_issue(**arguments: object) -> DevelopmentAgentOutput:
        context = arguments["context_manifest"]
        assert isinstance(context, dict)
        issue = context["issue"]
        assert isinstance(issue, dict)
        issue_id = str(issue["id"])
        attempted_issues.append(issue_id)
        if issue_id == "ISSUE-001" and attempted_issues.count(issue_id) == 1:
            (repository / "greeting.py").write_text(
                "def greeting(): return 'Hello'\n",
                encoding="utf-8",
            )
            return DevelopmentAgentOutput(
                ExecutionThreadId("development-thread-blocked-001"),
                ExecutionTurnId("development-turn-blocked-001"),
                (),
                (
                    CriterionImplementation(
                        AcceptanceCriterionId("AC-ISSUE-001-001"),
                        CriterionImplementationStatus.NOT_IMPLEMENTED,
                        "Required external input is unavailable.",
                    ),
                ),
                (),
                (),
                (),
                (),
                "Development cannot proceed without the required input.",
                outcome=StepOutcome.BLOCKED,
                blocked_reason="Required external input is unavailable.",
            )
        if issue_id == "ISSUE-001":
            return DevelopmentAgentOutput(
                ExecutionThreadId("development-thread-retry-001"),
                ExecutionTurnId("development-turn-retry-001"),
                (),
                (
                    CriterionImplementation(
                        AcceptanceCriterionId("AC-ISSUE-001-001"),
                        CriterionImplementationStatus.IMPLEMENTED,
                        "The authorized retry completed the partial implementation.",
                    ),
                ),
                ("python -m unittest",),
                (),
                (),
                (),
                "Completed the explicitly retried Issue.",
            )
        (repository / "farewell.py").write_text(
            "def farewell(): return 'Goodbye'\n",
            encoding="utf-8",
        )
        return DevelopmentAgentOutput(
            ExecutionThreadId("development-thread-independent-002"),
            ExecutionTurnId("development-turn-independent-002"),
            (),
            (
                CriterionImplementation(
                    AcceptanceCriterionId("AC-ISSUE-002-001"),
                    CriterionImplementationStatus.IMPLEMENTED,
                    "The independent behavior is implemented.",
                ),
            ),
            ("python -m unittest",),
            (),
            (),
            (),
            "Implemented the independent Issue.",
        )

    monkeypatch.setattr(
        scheduler._development._development_runner,
        "run_turn",
        execute_issue,
    )

    advanced = scheduler.advance(run_id)

    assert advanced.action is SchedulerAction.DEVELOPMENT_COMPLETED
    assert attempted_issues == ["ISSUE-001", "ISSUE-002"]
    by_id = {item.issue_id.value: item.status for item in advanced.snapshot.issues}
    assert by_id == {
        "ISSUE-001": IssueStatus.BLOCKED,
        "ISSUE-002": IssueStatus.IN_REVIEW,
        "ISSUE-003": IssueStatus.PENDING,
    }
    board = scheduler.issue_board(run_id)
    assert board[0].current_step == StepInstanceId("development")
    assert board[2].dependency_readiness.value == "BLOCKED"

    store = RunStore(config.paths.run_root)
    completed_independent = replace(
        advanced.snapshot,
        active_step=StepInstanceId("development"),
        run_status=WorkflowRunStatus.RUNNING,
        step_status=StepRunStatus.NOT_STARTED,
        issues=tuple(
            replace(item, status=IssueStatus.COMPLETED)
            if item.issue_id.value == "ISSUE-002"
            else item
            for item in advanced.snapshot.issues
        ),
    )
    store.record(completed_independent, RunEventType.ISSUE_ATTEMPT_ARCHIVED)

    paused = scheduler.advance(run_id)

    assert paused.action is SchedulerAction.PAUSED
    assert attempted_issues == ["ISSUE-001", "ISSUE-002"]

    retried = scheduler.retry_blocked_issue(run_id, IssueId("ISSUE-001"))

    retry_cursor = retried.snapshot.development
    assert retry_cursor is not None
    assert retry_cursor.attempt_id == AttemptId("attempt-002")
    assert retry_cursor.thread_id is None
    retry_context = store.load_json_artifact(run_id, retry_cursor.context_manifest)
    assert retry_context["attempt"]["id"] == "attempt-002"
    assert "ISSUE-002" not in json.dumps(retry_context)

    resumed = scheduler.advance(run_id)

    assert resumed.action is SchedulerAction.DEVELOPMENT_COMPLETED
    assert resumed.development is not None
    assert [item.path for item in resumed.development.result.changed_files] == ["greeting.py"]
    assert attempted_issues == ["ISSUE-001", "ISSUE-002", "ISSUE-001"]


def test_acl_approval_is_limited_to_one_exact_workspace_path(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    target = workspace / "tests"
    target.mkdir()
    sid = "S-1-5-21-100-200-300-1001"

    assert is_safe_windows_acl_grant(
        f'icacls "{target}" /grant:r "*{sid}:(F)"', workspace, sid
    )
    assert not is_safe_windows_acl_grant(
        f'icacls "{workspace}" /grant:r "*{sid}:(F)"', workspace, sid
    )
    assert not is_safe_windows_acl_grant(
        f'icacls "{target}" /grant:r "*{sid}:(F)" /T', workspace, sid
    )
    assert not is_safe_windows_acl_grant(
        f'icacls "{target}" /grant:r "*{sid}:(F)"; whoami', workspace, sid
    )
    assert not is_safe_windows_acl_grant(
        f'icacls "$(Get-Location)" /grant:r "*{sid}:(F)"', workspace, sid
    )
    assert not is_safe_windows_acl_grant(
        f'icacls "{target}\\*" /grant:r "*{sid}:(F)"', workspace, sid
    )


def test_development_approval_does_not_auto_approve_inspection_or_tests(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    tests_path = workspace / "tests"
    outside = tmp_path.parent
    readme = workspace / "README.md"
    outside_secret = outside / "secret.txt"
    sid = "S-1-5-21-100-200-300-1001"

    assert not is_safe_development_command(
        f'git -C "{workspace}" status --short', workspace, sid
    )
    assert not is_safe_development_command(
        f'python -m unittest discover -s "{tests_path}"', workspace, sid
    )
    assert not is_safe_development_command(
        f'git -C "{workspace}" add .', workspace, sid
    )
    assert not is_safe_development_command(
        f'python -m unittest discover -s "{outside}"', workspace, sid
    )
    assert not is_safe_development_command(
        f'git -C "{workspace}" status; Remove-Item -Recurse .', workspace, sid
    )
    assert not is_safe_development_command("Get-Location", workspace, sid)
    assert not is_safe_development_command(
        f'Get-Content -Raw "{readme}"', workspace, sid
    )
    assert not is_safe_development_command("rg --files", workspace, sid)
    assert not is_safe_development_command(
        f'Get-Content -Raw "{outside_secret}"', workspace, sid
    )
    assert not is_safe_development_command(
        "Get-Content ..\\secret.txt", workspace, sid
    )
    assert not is_safe_development_command(
        "python -m unittest discover -s ..\\outside", workspace, sid
    )
    assert not is_safe_development_command("rg --pre malicious --files", workspace, sid)
    assert not is_safe_development_command("rg --pre=malicious --files", workspace, sid)
    assert not is_safe_development_command(
        "Get-Content $(Get-Location)", workspace, sid
    )


def test_development_approval_rejects_powershell_provider_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()

    approved = is_safe_development_command(
        "Get-Content Env:PATH",
        workspace,
        "S-1-5-21-100-200-300-1001",
    )

    assert not approved


def test_development_approval_rejects_git_output_with_attached_path(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path.parent / "captured.diff"

    approved = is_safe_development_command(
        f'git -C "{workspace}" diff --output="{outside}"',
        workspace,
        "S-1-5-21-100-200-300-1001",
    )

    assert not approved


def test_development_approval_rejects_explicit_executable_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()

    approved = is_safe_development_command(
        f'"{tmp_path.parent / "git"}" status --short',
        workspace,
        "S-1-5-21-100-200-300-1001",
    )

    assert not approved


def test_development_approval_rejects_pytest_output_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path.parent / "pytest-output"

    approved = is_safe_development_command(
        f'python -m pytest --basetemp="{outside}" tests',
        workspace,
        "S-1-5-21-100-200-300-1001",
    )

    assert not approved


@pytest.mark.integration
def test_real_development_attempt_advances_to_review_without_completing_issue(
    tmp_path: Path,
) -> None:
    if os.environ.get("DEVLOOP_REAL_DEVELOPMENT") != "1":
        pytest.skip("Set DEVLOOP_REAL_DEVELOPMENT=1 to run the real development gate.")
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260710t120013-123456abcdef")
    config = _accepted_run(repository, run_id)
    service = WorkspaceDevelopmentService(config)
    service.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)

    completed = service.develop(run_id)

    assert completed.snapshot.active_step.value == "code-review"
    assert completed.snapshot.issues[0].status.value == "IN_REVIEW"
    assert completed.result.changed_files
    assert completed.result.criteria[0].status is CriterionImplementationStatus.IMPLEMENTED


def test_review_and_qa_inputs_use_exact_minimal_allowlists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260711t015956-123456abcdef")
    config = _accepted_run(repository, run_id, payload=_dependency_payload())
    development = WorkspaceDevelopmentService(config)
    development.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    _seed_implementation(
        config,
        run_id,
        source='def greeting() -> str:\n    return "Hello"\n',
        test_source=(
            "from greeting import greeting\n\n"
            "def test_greeting() -> None:\n"
            '    assert greeting() == "Hello"\n'
        ),
    )
    review_qa = ReviewQaService(config)
    captured_review_input: dict[str, object] = {}
    captured_qa_input: dict[str, object] = {}

    def complete_review(**arguments: object) -> ReviewAgentOutput:
        review_input = arguments["review_input"]
        assert isinstance(review_input, dict)
        captured_review_input.update(review_input)
        thread_bound = arguments["on_thread_bound"]
        turn_started = arguments["on_turn_started"]
        assert callable(thread_bound)
        assert callable(turn_started)
        thread_bound(ExecutionThreadId("review-thread-minimal-input"))
        turn_started(ExecutionTurnId("review-turn-minimal-input"))
        return ReviewAgentOutput(
            ExecutionThreadId("review-thread-minimal-input"),
            ExecutionTurnId("review-turn-minimal-input"),
            (),
            (),
            "The implementation is accepted for QA.",
            None,
        )

    monkeypatch.setattr(review_qa._review_runner, "run_turn", complete_review)
    reviewed = review_qa.review(run_id)

    def complete_qa(**arguments: object) -> QaAgentOutput:
        qa_input = arguments["qa_input"]
        assert isinstance(qa_input, dict)
        captured_qa_input.update(qa_input)
        thread_bound = arguments["on_thread_bound"]
        turn_started = arguments["on_turn_started"]
        assert callable(thread_bound)
        assert callable(turn_started)
        thread_bound(ExecutionThreadId("qa-thread-minimal-input"))
        turn_started(ExecutionTurnId("qa-turn-minimal-input"))
        return QaAgentOutput(
            ExecutionThreadId("qa-thread-minimal-input"),
            ExecutionTurnId("qa-turn-minimal-input"),
            (),
            (
                QaCheck(
                    QaCheckId("QC-001"),
                    AcceptanceCriterionId("AC-ISSUE-001-001"),
                    QaCheckKind.TEST,
                    CheckRequirement.REQUIRED,
                    QaCheckStatus.PASSED,
                    "python -m pytest -q",
                    0,
                    1,
                    "The focused greeting test passed.",
                    "",
                    "The tested greeting returns Hello.",
                    "The focused test exits successfully.",
                ),
            ),
            (),
            "Every required QA check passed.",
        )

    monkeypatch.setattr(review_qa._qa_runner, "run_turn", complete_qa)
    verified = review_qa.qa(run_id)

    assert reviewed.outcome is StepOutcome.SUCCEEDED
    assert verified.outcome is StepOutcome.SUCCEEDED
    assert set(captured_review_input) == {
        "schema",
        "issue",
        "workspace",
        "implementation",
        "relevant_diff",
        "repository_constraints",
        "capability_profile",
    }
    assert set(captured_qa_input) == {
        "schema",
        "issue",
        "workspace",
        "implementation",
        "review",
        "repository_state",
        "repository_constraints",
        "capability_profile",
    }
    assert captured_review_input["capability_profile"] == ["review"]
    assert captured_qa_input["capability_profile"] == ["qa"]
    assert captured_review_input["issue"] == captured_qa_input["issue"]
    serialized_inputs = json.dumps(
        {"review": captured_review_input, "qa": captured_qa_input},
        ensure_ascii=False,
    )
    assert "ISSUE-002" not in serialized_inputs
    assert "transcript" not in serialized_inputs.casefold()
    assert "model_reasoning" not in serialized_inputs.casefold()


def test_qa_resume_checkpoints_running_before_app_server_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260711t015965-123456abcdef")
    config = _accepted_run(repository, run_id)
    development = WorkspaceDevelopmentService(config)
    development.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    _seed_implementation(
        config,
        run_id,
        source='def greeting() -> str:\n    return "Hello"\n',
        test_source=(
            "from greeting import greeting\n\n"
            "def test_greeting() -> None:\n"
            '    assert greeting() == "Hello"\n'
        ),
    )
    snapshot = _seed_accepted_review(config, run_id)
    active = snapshot.development
    assert active is not None
    store = RunStore(config.paths.run_root)
    qa_input = store.save_json_artifact(
        run_id,
        Path("qa-inputs") / f"{active.issue_id.value}-resume.json",
        {"schema": "devloop.qa-input/v1"},
    )
    interrupted = store.record(
        replace(
            snapshot,
            run_status=WorkflowRunStatus.PAUSED,
            step_status=StepRunStatus.RUNNING,
            qa=QaCursor(
                active.issue_id,
                active.attempt_id,
                qa_input,
                ExecutionThreadId("qa-thread-resume"),
                ExecutionTurnId("qa-turn-resume"),
            ),
        ),
        RunEventType.RUN_PAUSED,
    )
    store.release_lease(interrupted)
    review_qa = ReviewQaService(config)
    observed_statuses: list[WorkflowRunStatus] = []

    def observe_resume(**arguments: object) -> QaAgentOutput:
        observed_statuses.append(store.load(run_id).run_status)
        raise QaComponentError("Stop after observing QA resume checkpoint.")

    monkeypatch.setattr(review_qa._qa_runner, "recover_completed_turn", observe_resume)

    with pytest.raises(ReviewQaError):
        review_qa.resume_qa(run_id)

    assert observed_statuses == [WorkflowRunStatus.RUNNING]


def test_qa_pause_preserves_the_exact_cursor_and_marks_an_active_operation_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260711t015966-123456abcdef")
    config = _accepted_run(repository, run_id)
    development = WorkspaceDevelopmentService(config)
    development.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    _seed_implementation(
        config,
        run_id,
        source='def greeting() -> str:\n    return "Hello"\n',
        test_source=(
            "from greeting import greeting\n\n"
            "def test_greeting() -> None:\n"
            '    assert greeting() == "Hello"\n'
        ),
    )
    accepted_review = _seed_accepted_review(config, run_id)
    original_workspace = accepted_review.workspace
    original_attempt = accepted_review.development
    assert original_workspace is not None
    assert original_attempt is not None
    review_qa = ReviewQaService(config)

    def pause_qa(**arguments: object) -> object:
        thread_bound = arguments["on_thread_bound"]
        turn_started = arguments["on_turn_started"]
        item_started = arguments["on_item_started"]
        pause_requested = arguments["pause_requested"]
        assert callable(thread_bound)
        assert callable(turn_started)
        assert callable(item_started)
        assert callable(pause_requested)
        thread_bound(ExecutionThreadId("qa-thread-paused"))
        turn_started(ExecutionTurnId("qa-turn-paused"))
        item_started("qa-command-unknown")
        review_qa.request_pause(run_id)
        assert pause_requested()
        raise QaTurnPaused("Paused by the user.")

    monkeypatch.setattr(review_qa._qa_runner, "run_turn", pause_qa)

    with pytest.raises(ReviewQaPaused) as paused_error:
        review_qa.qa(run_id)

    paused = paused_error.value.snapshot
    assert paused.run_status is WorkflowRunStatus.PAUSED
    assert paused.step_status is StepRunStatus.RUNNING
    assert paused.operation == OperationState(
        "qa-command-unknown",
        OperationStatus.UNKNOWN,
    )
    assert paused.qa is not None
    assert paused.qa.issue_id == original_attempt.issue_id
    assert paused.qa.attempt_id == original_attempt.attempt_id
    assert paused.qa.thread_id == ExecutionThreadId("qa-thread-paused")
    assert paused.qa.turn_id == ExecutionTurnId("qa-turn-paused")
    assert paused.workspace == original_workspace


def test_review_rejects_a_must_fix_finding_with_unavailable_repository_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260711t015957-123456abcdef")
    config = _accepted_run(repository, run_id)
    development = WorkspaceDevelopmentService(config)
    development.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    _seed_implementation(
        config,
        run_id,
        source='def greeting() -> str:\n    return "Hello"\n',
        test_source=(
            "from greeting import greeting\n\n"
            "def test_greeting() -> None:\n"
            '    assert greeting() == "Hello"\n'
        ),
    )
    review_qa = ReviewQaService(config)

    def return_unsupported_finding(**arguments: object) -> ReviewAgentOutput:
        thread_bound = arguments["on_thread_bound"]
        turn_started = arguments["on_turn_started"]
        assert callable(thread_bound)
        assert callable(turn_started)
        thread_bound(ExecutionThreadId("review-thread-unsupported"))
        turn_started(ExecutionTurnId("review-turn-unsupported"))
        return ReviewAgentOutput(
            ExecutionThreadId("review-thread-unsupported"),
            ExecutionTurnId("review-turn-unsupported"),
            (),
            (
                ReviewFinding(
                    ReviewFindingId("RF-001"),
                    FindingSeverity.HIGH,
                    FindingDisposition.MUST_FIX,
                    "Unsupported finding",
                    "The claimed repository defect cannot be inspected.",
                    "The claimed evidence is not present in the workspace.",
                    "missing.py",
                    None,
                    "Review findings cite available repository evidence.",
                    "Remove or support the finding with a repository path.",
                ),
            ),
            "A finding was emitted before review became blocked.",
            "Additional repository inspection was blocked.",
        )

    monkeypatch.setattr(review_qa._review_runner, "run_turn", return_unsupported_finding)

    with pytest.raises(ReviewQaError, match="evidence is unavailable"):
        review_qa.review(run_id)

    failed = RunStore(config.paths.run_root).load(run_id)
    assert failed.issues[0].status is IssueStatus.FAILED
    assert failed.issues[0].current_step == StepInstanceId("code-review")


def test_qa_source_change_before_approval_blocks_without_reverting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260711t015959-123456abcdef")
    config = _accepted_run(repository, run_id)
    development = WorkspaceDevelopmentService(config)
    development.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    _seed_implementation(
        config,
        run_id,
        source='def greeting() -> str:\n    return "Hello"\n',
        test_source=(
            "from greeting import greeting\n\n"
            "def test_greeting() -> None:\n"
            '    assert greeting() == "Hello"\n'
        ),
    )
    _seed_accepted_review(config, run_id)
    review_qa = ReviewQaService(config)

    def change_source_then_request_approval(**arguments: object) -> object:
        thread_bound = arguments["on_thread_bound"]
        turn_started = arguments["on_turn_started"]
        assert callable(thread_bound)
        assert callable(turn_started)
        thread_bound(ExecutionThreadId("qa-thread-approval"))
        turn_started(ExecutionTurnId("qa-turn-approval"))
        (repository / "README.md").write_text("# Changed by QA\n", encoding="utf-8")
        raise AppServerApprovalRequired(
            AppServerApprovalRequest(
                "qa-approval-request-1",
                AppServerApprovalKind.COMMAND,
                AppServerRequestMethod.COMMAND_APPROVAL.value,
                "run another verification command",
                str(repository),
                "Additional verification requires approval.",
                ("accept", "decline", "cancel"),
                "qa-thread-approval",
                "qa-turn-approval",
                "qa-item-approval-1",
            )
        )

    monkeypatch.setattr(review_qa._qa_runner, "run_turn", change_source_then_request_approval)

    with pytest.raises(ReviewQaError, match="blocked without reverting"):
        review_qa.qa(run_id)

    blocked = RunStore(config.paths.run_root).load(run_id)
    assert blocked.run_status is WorkflowRunStatus.PAUSED
    assert blocked.step_status is StepRunStatus.BLOCKED
    assert blocked.issues[0].status is IssueStatus.BLOCKED
    assert blocked.issues[0].current_step == StepInstanceId("qa")
    assert (repository / "README.md").read_text(encoding="utf-8") == "# Changed by QA\n"


def test_qa_blocks_index_drift_before_starting_the_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "project"
    _git_repository(repository)
    (repository / "greeting.py").write_text(
        'def greeting() -> str:\n    return "Old"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "greeting.py"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add greeting"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    run_id = WorkflowRunId("run-20260711t015958-123456abcdef")
    config = _accepted_run(repository, run_id)
    development = WorkspaceDevelopmentService(config)
    development.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    _seed_implementation(
        config,
        run_id,
        source='def greeting() -> str:\n    return "Hello"\n',
        test_source=(
            "from greeting import greeting\n\n"
            "def test_greeting() -> None:\n"
            '    assert greeting() == "Hello"\n'
        ),
    )
    _seed_accepted_review(config, run_id)
    subprocess.run(["git", "add", "greeting.py"], cwd=repository, check=True)
    review_qa = ReviewQaService(config)
    agent_started = False

    def unexpected_agent_start(**arguments: object) -> object:
        nonlocal agent_started
        agent_started = True
        raise AssertionError("QA agent must not start against a drifted repository state.")

    monkeypatch.setattr(review_qa._qa_runner, "run_turn", unexpected_agent_start)

    with pytest.raises(ReviewQaError, match="blocked without reverting"):
        review_qa.qa(run_id)

    blocked = RunStore(config.paths.run_root).load(run_id)
    assert not agent_started
    assert blocked.step_status is StepRunStatus.BLOCKED
    assert blocked.issues[0].status is IssueStatus.BLOCKED
    assert blocked.issues[0].current_step == StepInstanceId("qa")
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert staged == ["greeting.py"]


@pytest.mark.integration
def test_real_development_review_qa_path_uses_distinct_threads_and_preserves_sources(
    tmp_path: Path,
) -> None:
    if os.environ.get("DEVLOOP_REAL_REVIEW_QA") != "1":
        pytest.skip("Set DEVLOOP_REAL_REVIEW_QA=1 to run the real review and QA gate.")
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260711t020000-123456abcdef")
    config = _accepted_run(repository, run_id)
    development_service = WorkspaceDevelopmentService(config)
    review_qa_service = ReviewQaService(config)
    development_service.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)

    developed = development_service.develop(run_id)
    workspace = developed.snapshot.workspace
    assert workspace is not None
    before_review = capture_worktree_changes(
        repository,
        workspace.base_commit,
        workspace.baseline,
    )
    reviewed = review_qa_service.review(run_id)
    after_review = capture_worktree_changes(
        repository,
        workspace.base_commit,
        workspace.baseline,
    )

    assert reviewed.outcome is StepOutcome.SUCCEEDED
    assert after_review == before_review
    assert reviewed.snapshot.review is not None
    assert reviewed.snapshot.development is not None
    assert reviewed.snapshot.review.thread_id != reviewed.snapshot.development.thread_id

    verified = review_qa_service.qa(run_id)
    after_qa = capture_worktree_changes(
        repository,
        workspace.base_commit,
        workspace.baseline,
    )

    assert verified.outcome is StepOutcome.SUCCEEDED
    assert after_qa == before_review
    assert verified.snapshot.qa is not None
    assert verified.snapshot.qa.thread_id != reviewed.snapshot.review.thread_id
    assert verified.snapshot.issues[0].status is IssueStatus.COMPLETED


@pytest.mark.integration
def test_real_review_requests_typed_rework_and_starts_a_fresh_attempt(
    tmp_path: Path,
) -> None:
    if os.environ.get("DEVLOOP_REAL_REWORK") != "1":
        pytest.skip("Set DEVLOOP_REAL_REWORK=1 to run the real rework gates.")
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260711t030001-123456abcdef")
    config = _accepted_run(repository, run_id)
    development = WorkspaceDevelopmentService(config)
    review_qa = ReviewQaService(config)
    development.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    seeded = _seed_implementation(
        config,
        run_id,
        source='def greeting() -> str:\n    return "Goodbye"\n',
        test_source=(
            "from greeting import greeting\n\n"
            "def test_greeting() -> None:\n"
            '    assert greeting() == "Goodbye"\n'
        ),
    )
    workspace = seeded.workspace
    assert workspace is not None
    before = capture_worktree_changes(
        repository,
        workspace.base_commit,
        workspace.baseline,
    )

    reviewed = review_qa.review(run_id)

    assert reviewed.outcome is StepOutcome.CHANGES_REQUESTED
    assert reviewed.snapshot.review is not None
    assert reviewed.snapshot.review.rework_request is not None
    after = capture_worktree_changes(
        repository,
        workspace.base_commit,
        workspace.baseline,
    )
    assert after == before
    request = RunStore(config.paths.run_root).load_json_artifact(
        run_id,
        reviewed.snapshot.review.rework_request,
    )
    items = request["items"]
    assert isinstance(items, list) and items
    assert all(
        set(item) == {"id", "evidence", "expected_behavior", "acceptance_condition"}
        for item in items
    )
    retried = development.prepare_rework(run_id)
    assert retried.snapshot.development is not None
    assert retried.snapshot.development.attempt_id == AttemptId("attempt-002")
    assert retried.snapshot.development.thread_id is None
    assert retried.snapshot.review is None
    assert retried.snapshot.qa is None


@pytest.mark.integration
def test_real_qa_requests_typed_rework_and_starts_a_fresh_attempt(
    tmp_path: Path,
) -> None:
    if os.environ.get("DEVLOOP_REAL_REWORK") != "1":
        pytest.skip("Set DEVLOOP_REAL_REWORK=1 to run the real rework gates.")
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260711t030002-123456abcdef")
    config = _accepted_run(repository, run_id)
    development = WorkspaceDevelopmentService(config)
    review_qa = ReviewQaService(config)
    development.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    _seed_implementation(
        config,
        run_id,
        source='def greeting() -> str:\n    return "Goodbye"\n',
        test_source=(
            "from greeting import greeting\n\n"
            "def test_greeting() -> None:\n"
            '    assert greeting() == "Hello"\n'
        ),
    )
    seeded = _seed_accepted_review(config, run_id)
    workspace = seeded.workspace
    assert workspace is not None
    before = capture_worktree_changes(
        repository,
        workspace.base_commit,
        workspace.baseline,
    )

    verified = review_qa.qa(run_id)

    assert verified.outcome is StepOutcome.CHANGES_REQUESTED
    assert verified.snapshot.qa is not None
    assert verified.snapshot.qa.rework_request is not None
    after = capture_worktree_changes(
        repository,
        workspace.base_commit,
        workspace.baseline,
    )
    assert after == before
    retried = development.prepare_rework(run_id)
    assert retried.snapshot.development is not None
    assert retried.snapshot.development.attempt_id == AttemptId("attempt-002")
    assert retried.snapshot.development.thread_id is None
    assert retried.snapshot.review is None
    assert retried.snapshot.qa is None


@pytest.mark.integration
def test_real_scheduler_completes_a_dependency_chain_with_isolated_threads(
    tmp_path: Path,
) -> None:
    if os.environ.get("DEVLOOP_REAL_SCHEDULER") != "1":
        pytest.skip("Set DEVLOOP_REAL_SCHEDULER=1 to run the real scheduler gate.")
    repository = tmp_path / "project"
    _git_repository(repository)
    run_id = WorkflowRunId("run-20260711t050000-123456abcdef")
    config = _accepted_run(repository, run_id, payload=_dependency_payload())
    WorkspaceDevelopmentService(config).prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    scheduler = WorkflowSchedulerService(config)

    result = scheduler.run_until_pause(run_id)

    assert result.action is SchedulerAction.WORKFLOW_DRAINED
    assert result.snapshot.active_step == StepInstanceId("workspace-finalization")
    assert [item.status for item in result.snapshot.issues] == [
        IssueStatus.COMPLETED,
        IssueStatus.COMPLETED,
    ]
    assert len(result.snapshot.attempts) == 2
    threads = {
        thread
        for attempt in result.snapshot.attempts
        for thread in (
            attempt.development_thread,
            attempt.review_thread,
            attempt.qa_thread,
        )
    }
    assert None not in threads
    assert len(threads) == 6
    completed = scheduler.completed_results(run_id)
    assert len(completed.implementations) == 2
    assert len(completed.reviews) == 2
    assert len(completed.qa_results) == 2

    finalized = FinalizationService(config).finalize(run_id)

    assert finalized.snapshot.run_status is WorkflowRunStatus.COMPLETED
    assert finalized.summary.completed_issues == (IssueId("ISSUE-001"), IssueId("ISSUE-002"))
    assert finalized.summary.workspace_disposition.value == "LEAVE_INTACT"
    assert finalized.summary.verification_evidence
