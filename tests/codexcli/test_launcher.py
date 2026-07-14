from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual import events
from textual.widgets import Button, OptionList, RichLog, Static

from devloop.application.analysis import AnalysisRunResult, AnalysisWorkflowError
from devloop.application.commands import launcher_command_registry
from devloop.application.development import WorkspaceDevelopmentError
from devloop.application.recovery import (
    RecoveryDisposition,
    RecoveryPlan,
    RecoveryValidation,
    ResumeCandidate,
)
from devloop.application.review_qa import ReviewQaError
from devloop.components.workspace import WorkspaceProposal
from devloop.domain.development import WorkspaceChoice
from devloop.domain.identifiers import (
    FeatureSlug,
    IssueId,
    StepInstanceId,
    WorkflowId,
    WorkflowRunId,
)
from devloop.domain.operations import StopAction
from devloop.domain.run import (
    AnalysisCursor,
    ResolvedWorkflow,
    RunLease,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.ui.app import RunLauncherApp
from devloop.ui.composer import Composer
from devloop.ui.workspace import WorkspacePreparationView


class _BlockingAnalysisService:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.calls = 0
        self.active_calls = 0
        self.max_active_calls = 0

    def start(self, feature_request: str, *, on_activity: object = None) -> None:
        with self._lock:
            self.calls += 1
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
            self.started.set()
        try:
            self.release.wait(timeout=5.0)
        finally:
            with self._lock:
                self.active_calls -= 1
        raise AnalysisWorkflowError("Stop the blocking test operation.")


class _TeardownAnalysisService:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.started = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()

    def start(self, feature_request: str, *, on_activity: object = None) -> AnalysisRunResult:
        self.started.set()
        self.release.wait(timeout=5.0)
        try:
            if callable(on_activity):
                on_activity(f"late {self.outcome} activity")
            if self.outcome == "error":
                raise AnalysisWorkflowError("late teardown error")
            return _analysis_result()
        finally:
            self.exited.set()


class _BlockingResumeService(_BlockingAnalysisService):
    def __init__(self) -> None:
        super().__init__()
        self.run_id = WorkflowRunId("run-20260711t120000-123456abcdef")

    def list_resumable(self) -> tuple[SimpleNamespace, ...]:
        return (
            SimpleNamespace(
                run_id=self.run_id,
                active_step=StepInstanceId("analysis"),
                run_status=SimpleNamespace(value="AWAITING_USER"),
                feature_title="Resume me",
                updated_at="2026-07-11T12:00:00Z",
            ),
        )

    def resume(self, run_id: WorkflowRunId, *, on_activity: object = None) -> None:
        assert run_id == self.run_id
        self.start("resume")


class _DisplayRecoveryService:
    def list_candidates(self) -> tuple[ResumeCandidate, ...]:
        return (
            ResumeCandidate(
                run_id=WorkflowRunId("run-20260711t120000-123456abcdef"),
                feature="Recover checkout",
                workflow="standard@1.0.0",
                step=StepInstanceId("qa"),
                issue_id=IssueId("ISSUE-003"),
                status="PAUSED",
                workspace="E:/worktrees/recover-checkout",
                last_activity="2026-07-11T12:00:00Z",
                validation=RecoveryValidation.UNKNOWN_OPERATION,
            ),
        )


class _RefusingRecoveryService(_DisplayRecoveryService):
    def __init__(self) -> None:
        self.inspected: list[WorkflowRunId] = []

    def inspect(self, run_id: WorkflowRunId) -> RecoveryPlan:
        self.inspected.append(run_id)
        return RecoveryPlan(
            replace(_snapshot("qa"), run_id=run_id),
            RecoveryDisposition.REFUSE,
            RecoveryValidation.DRIFT,
            ("Workspace HEAD differs from the checkpoint.",),
        )


class _FreshRecoveryService(_DisplayRecoveryService):
    def __init__(self) -> None:
        self.started: list[WorkflowRunId] = []

    def inspect(self, run_id: WorkflowRunId) -> RecoveryPlan:
        return RecoveryPlan(
            replace(_snapshot("qa"), run_id=run_id),
            RecoveryDisposition.FRESH_ATTEMPT,
            RecoveryValidation.UNKNOWN_OPERATION,
            (
                "An interrupted operation has unknown effects and will not be replayed. ",
            ),
        )

    def start_fresh_attempt(self, run_id: WorkflowRunId) -> WorkflowRunSnapshot:
        self.started.append(run_id)
        return replace(_snapshot("qa"), run_id=run_id)


class _RecordingQaResumeService:
    def __init__(self) -> None:
        self.calls = 0
        self.fresh_calls = 0

    def active_issue_id(self, run_id: WorkflowRunId) -> IssueId:
        return IssueId("ISSUE-003")

    def resume_qa(self, run_id: WorkflowRunId, *, on_activity: object = None) -> None:
        self.calls += 1
        raise ReviewQaError("QA resume should have been refused.")

    def qa(self, run_id: WorkflowRunId, *, on_activity: object = None) -> None:
        self.fresh_calls += 1
        raise ReviewQaError("Stop after starting the fresh QA attempt.")


class _RecordingVerificationStopService:
    def __init__(self) -> None:
        self.paused: list[WorkflowRunId] = []
        self.interrupted: list[WorkflowRunId] = []

    def request_pause(self, run_id: WorkflowRunId) -> None:
        self.paused.append(run_id)

    def request_interrupt(self, run_id: WorkflowRunId) -> None:
        self.interrupted.append(run_id)


TRANSITION_RUN_ID = WorkflowRunId("run-20260711t130000-123456abcdef")


def _snapshot(step: str) -> WorkflowRunSnapshot:
    return WorkflowRunSnapshot(
        "devloop.run-snapshot/v1",
        TRANSITION_RUN_ID,
        str(Path.cwd()),
        "Transition feature",
        FeatureSlug("transition-feature"),
        ResolvedWorkflow(WorkflowId("standard"), "1.0.0", "workflow-hash"),
        (),
        StepInstanceId(step),
        WorkflowRunStatus.AWAITING_USER,
        StepRunStatus.AWAITING_USER,
        None,
        AnalysisCursor(draft_revision=1),
        RunLease("lease", 1, datetime.now(timezone.utc).isoformat()),
        1,
        datetime.now(timezone.utc).isoformat(),
    )


def _analysis_result() -> AnalysisRunResult:
    draft = SimpleNamespace(prd_markdown="# Transition feature", issues=())
    return AnalysisRunResult(_snapshot("analysis"), draft, (), None)  # type: ignore[arg-type]


class _SnapshotRecoveryService:
    def __init__(self, checkpoint: WorkflowRunSnapshot) -> None:
        self.checkpoint = checkpoint

    def list_candidates(self) -> tuple[ResumeCandidate, ...]:
        snapshot = self.checkpoint
        return (
            ResumeCandidate(
                snapshot.run_id,
                snapshot.feature_title,
                f"{snapshot.workflow.workflow_id.value}@{snapshot.workflow.version}",
                snapshot.active_step,
                None,
                snapshot.run_status.value,
                None,
                snapshot.updated_at,
                RecoveryValidation.VALID,
            ),
        )

    def inspect(self, run_id: WorkflowRunId) -> RecoveryPlan:
        assert run_id == self.checkpoint.run_id
        disposition = (
            RecoveryDisposition.CONTINUE_WORKFLOW
            if self.checkpoint.active_step
            in {
                StepInstanceId("workspace-preparation"),
                StepInstanceId("workspace-finalization"),
            }
            else RecoveryDisposition.CONTINUE_THREAD
        )
        return RecoveryPlan(
            self.checkpoint,
            disposition,
            RecoveryValidation.VALID,
            (),
        )


def _workspace_proposal() -> WorkspaceProposal:
    repository = Path.cwd()
    return WorkspaceProposal(
        repository,
        repository,
        repository.parent / "transition-worktree",
        "devloop/transition-feature",
        "abc123",
    )


class _TransitionAnalysisService:
    def __init__(self) -> None:
        self.accepted: list[WorkflowRunId] = []
        self.continued: list[tuple[WorkflowRunId, str]] = []

    def start(self, feature_request: str, *, on_activity: object = None) -> AnalysisRunResult:
        return _analysis_result()

    def list_resumable(self) -> tuple[WorkflowRunSnapshot, ...]:
        return (_snapshot("workspace-preparation"),)

    def accept(self, run_id: WorkflowRunId) -> None:
        self.accepted.append(run_id)
        raise AnalysisWorkflowError("Acceptance should not run outside analysis.")

    def continue_analysis(
        self,
        run_id: WorkflowRunId,
        message: str,
        *,
        on_activity: object = None,
    ) -> None:
        self.continued.append((run_id, message))
        raise AnalysisWorkflowError("Continuation should not run outside analysis.")


class _PauseAnalysisService(_TransitionAnalysisService):
    def __init__(self) -> None:
        super().__init__()
        self.paused: list[WorkflowRunId] = []

    def pause(self, run_id: WorkflowRunId) -> WorkflowRunSnapshot:
        self.paused.append(run_id)
        return replace(_snapshot("analysis"), run_status=WorkflowRunStatus.PAUSED)


class _WorkspaceResumeService:
    def __init__(self) -> None:
        self.prepared: list[tuple[WorkflowRunId, WorkspaceChoice]] = []

    def resume_workspace(self, run_id: WorkflowRunId) -> WorkspaceProposal:
        assert run_id == TRANSITION_RUN_ID
        return _workspace_proposal()

    def prepare(self, run_id: WorkflowRunId, choice: WorkspaceChoice) -> None:
        self.prepared.append((run_id, choice))
        raise WorkspaceDevelopmentError("Stop after recording the workspace choice.")


class _BlockingAcceptanceService(_TransitionAnalysisService):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()

    def accept(self, run_id: WorkflowRunId) -> SimpleNamespace:
        assert run_id == TRANSITION_RUN_ID
        self.started.set()
        try:
            self.release.wait(timeout=5.0)
        finally:
            self.exited.set()
        return SimpleNamespace(
            snapshot=_snapshot("workspace-preparation"),
            package=SimpleNamespace(root=Path.cwd() / "prd" / "transition-feature"),
        )


class _ProgressionWorkspaceService(_WorkspaceResumeService):
    def __init__(self) -> None:
        super().__init__()
        self.proposal_calls: list[WorkflowRunId] = []

    def proposal(self, run_id: WorkflowRunId) -> WorkspaceProposal:
        self.proposal_calls.append(run_id)
        return _workspace_proposal()


@pytest.mark.asyncio
async def test_launcher_starts_idle_and_submits_unicode_only_after_user_action(
    tmp_path: Path,
) -> None:
    submitted: list[str] = []
    app = RunLauncherApp(
        tmp_path,
        launcher_command_registry(),
        on_feature=submitted.append,
    )

    async with app.run_test(size=(100, 32)) as pilot:
        assert submitted == []
        assert "NO WORKFLOW RUN" in app.query_one("#status", Static).content
        composer = app.query_one("#composer", Composer)
        composer.load_text("/r")
        await pilot.pause()

        menu = app.query_one("#command-menu", OptionList)
        assert menu.display is True
        assert menu.option_count == 1

        composer.load_text("Σύγκρινε τιμές για ψώνια")
        await pilot.pause()
        await pilot.press("ctrl+enter")
        await pilot.pause()

    assert submitted == ["Σύγκρινε τιμές για ψώνια"]


@pytest.mark.asyncio
async def test_launcher_allows_only_one_submission_until_the_worker_thread_exits() -> None:
    service = _BlockingAnalysisService()
    app = RunLauncherApp(
        Path.cwd(),
        launcher_command_registry(),
        analysis_service=service,  # type: ignore[arg-type]
    )

    async with app.run_test(size=(80, 24)) as pilot:
        composer = app.query_one("#composer", Composer)
        menu = app.query_one("#command-menu", OptionList)
        try:
            composer.load_text("first request")
            composer.action_submit()
            for _ in range(20):
                await pilot.pause()
                if service.started.is_set():
                    break
            assert service.started.is_set()

            app.post_message(Composer.Submitted("duplicate request"))
            await pilot.pause()

            assert service.calls == 1
            assert service.max_active_calls == 1
            assert composer.disabled is True
            assert menu.disabled is True
            assert all(button.disabled for button in app.query(Button))

            service.release.set()
            for _ in range(20):
                await pilot.pause()
                if not composer.disabled:
                    break

            assert composer.disabled is False
            assert menu.disabled is False
            assert app.query_one("#analysis-request-changes", Button).disabled is False
            assert app.query_one("#analysis-accept", Button).disabled is True
            assert all(
                not app.query_one(selector, Button).disabled
                for selector in (
                    "#workspace-cancel",
                    "#workspace-current-choice",
                    "#workspace-dedicated-choice",
                )
            )
            assert service.calls == 1
        finally:
            service.release.set()


@pytest.mark.asyncio
async def test_launcher_ignores_a_duplicate_resume_action_while_work_is_busy() -> None:
    service = _BlockingResumeService()
    app = RunLauncherApp(
        Path.cwd(),
        launcher_command_registry(),
        analysis_service=service,  # type: ignore[arg-type]
        recovery_service=_SnapshotRecoveryService(
            replace(_snapshot("analysis"), run_id=service.run_id)
        ),  # type: ignore[arg-type]
    )

    async with app.run_test(size=(80, 24)) as pilot:
        composer = app.query_one("#composer", Composer)
        menu = app.query_one("#command-menu", OptionList)
        try:
            app.post_message(Composer.Submitted("/resume"))
            await pilot.pause()
            option = menu.get_option_at_index(0)
            menu.highlighted = 0
            menu.focus()
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause()
                if service.started.is_set():
                    break
            assert service.started.is_set()

            app.post_message(OptionList.OptionSelected(menu, option, 0))
            await pilot.pause()

            assert service.calls == 1
            assert service.max_active_calls == 1
            assert composer.disabled is True
            assert menu.disabled is True

            service.release.set()
            for _ in range(20):
                await pilot.pause()
                if not composer.disabled:
                    break

            assert composer.disabled is False
            assert menu.disabled is False
            assert service.calls == 1
        finally:
            service.release.set()


@pytest.mark.asyncio
async def test_resume_menu_shows_the_exact_checkpoint_and_validation_condition() -> None:
    app = RunLauncherApp(
        Path.cwd(),
        launcher_command_registry(),
        recovery_service=_DisplayRecoveryService(),  # type: ignore[arg-type]
    )

    async with app.run_test(size=(140, 24)) as pilot:
        app.post_message(Composer.Submitted("/resume"))
        await pilot.pause()

        menu = app.query_one("#command-menu", OptionList)
        assert menu.option_count == 1
        prompt = str(menu.get_option_at_index(0).prompt)
        assert "Recover checkout" in prompt
        assert "standard@1.0.0" in prompt
        assert "qa" in prompt
        assert "ISSUE-003" in prompt
        assert "PAUSED" in prompt
        assert "E:/worktrees/recover-checkout" in prompt
        assert "2026-07-11T12:00:00Z" in prompt
        assert "UNKNOWN_OPERATION" in prompt


@pytest.mark.asyncio
async def test_resume_selection_refuses_drift_before_starting_the_backend() -> None:
    recovery = _RefusingRecoveryService()
    qa = _RecordingQaResumeService()
    app = RunLauncherApp(
        Path.cwd(),
        launcher_command_registry(),
        review_qa_service=qa,  # type: ignore[arg-type]
        recovery_service=recovery,  # type: ignore[arg-type]
    )

    async with app.run_test(size=(140, 24)) as pilot:
        app.post_message(Composer.Submitted("/resume"))
        await pilot.pause()
        menu = app.query_one("#command-menu", OptionList)
        menu.highlighted = 0
        menu.focus()
        await pilot.press("enter")
        await pilot.pause()

        output = app.query_one("#activity", RichLog)
        assert recovery.inspected == [
            WorkflowRunId("run-20260711t120000-123456abcdef")
        ]
        assert qa.calls == 0
        assert any(
            "Workspace HEAD differs from the checkpoint." in line.text
            for line in output.lines
        )


@pytest.mark.asyncio
async def test_unknown_operation_offers_an_explicit_transcript_free_recovery_attempt() -> None:
    qa = _RecordingQaResumeService()
    recovery = _FreshRecoveryService()
    app = RunLauncherApp(
        Path.cwd(),
        launcher_command_registry(),
        review_qa_service=qa,  # type: ignore[arg-type]
        recovery_service=recovery,  # type: ignore[arg-type]
    )

    async with app.run_test(size=(140, 24)) as pilot:
        app.post_message(Composer.Submitted("/resume"))
        await pilot.pause()
        menu = app.query_one("#command-menu", OptionList)
        menu.highlighted = 0
        menu.focus()
        await pilot.press("enter")
        await pilot.pause()

        assert qa.calls == 0
        assert menu.option_count == 1
        assert "transcript-free Recovery Attempt" in str(
            menu.get_option_at_index(0).prompt
        )

        menu.highlighted = 0
        menu.focus()
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if qa.fresh_calls:
                break

        run_id = WorkflowRunId("run-20260711t120000-123456abcdef")
        assert recovery.started == [run_id]
        assert qa.fresh_calls == 1
        assert qa.calls == 0


@pytest.mark.asyncio
async def test_pause_command_pauses_the_active_analysis_run() -> None:
    service = _PauseAnalysisService()
    app = RunLauncherApp(
        Path.cwd(),
        launcher_command_registry(),
        analysis_service=service,  # type: ignore[arg-type]
    )

    async with app.run_test(size=(100, 32)) as pilot:
        composer = app.query_one("#composer", Composer)
        composer.load_text("Start analysis")
        composer.action_submit()
        for _ in range(20):
            await pilot.pause()
            if not composer.disabled:
                break

        app.post_message(Composer.Submitted("/pause"))
        for _ in range(20):
            await pilot.pause()
            if service.paused:
                break

        assert service.paused == [TRANSITION_RUN_ID]
        assert "PAUSED | ANALYSIS" in app.query_one("#status", Static).content


@pytest.mark.asyncio
async def test_busy_qa_pause_and_ctrl_c_interrupt_reach_the_live_verification_service() -> None:
    service = _RecordingVerificationStopService()
    app = RunLauncherApp(
        Path.cwd(),
        launcher_command_registry(),
        review_qa_service=service,  # type: ignore[arg-type]
        control_service=object(),  # type: ignore[arg-type]
    )

    async with app.run_test(size=(100, 32)) as pilot:
        app._transition_to(TRANSITION_RUN_ID, StepInstanceId("qa"))
        app._busy = True
        app.post_message(Composer.Submitted("/pause"))
        await pilot.pause()

        app._stop_selected(StopAction.INTERRUPT_TURN)
        await pilot.pause()

        app._cancel_confirmed(True)
        await pilot.pause()

        assert service.paused == [TRANSITION_RUN_ID, TRANSITION_RUN_ID]
        assert service.interrupted == [TRANSITION_RUN_ID]
        assert app._pending_operation is not None


@pytest.mark.asyncio
async def test_cancelled_worker_waits_for_thread_exit_and_discards_late_progression() -> None:
    analysis = _BlockingAcceptanceService()
    workspace = _ProgressionWorkspaceService()
    app = RunLauncherApp(
        Path.cwd(),
        launcher_command_registry(),
        analysis_service=analysis,  # type: ignore[arg-type]
        workspace_service=workspace,  # type: ignore[arg-type]
    )

    async with app.run_test(size=(100, 32)) as pilot:
        composer = app.query_one("#composer", Composer)
        try:
            composer.load_text("Start analysis")
            composer.action_submit()
            for _ in range(20):
                await pilot.pause()
                if not composer.disabled:
                    break

            app.post_message(Composer.Submitted("/accept"))
            for _ in range(20):
                await pilot.pause()
                if analysis.started.is_set():
                    break
            assert analysis.started.is_set()

            cancelled = app.workers.cancel_group(app, "workflow")
            assert len(cancelled) == 1
            await pilot.pause()
            assert composer.disabled is True

            analysis.release.set()
            for _ in range(40):
                await pilot.pause()
                if analysis.exited.is_set() and not composer.disabled:
                    break

            assert analysis.exited.is_set()
            assert composer.disabled is False
            assert workspace.proposal_calls == []
            assert app.query_one("#workspace-view", WorkspacePreparationView).display is False
        finally:
            analysis.release.set()


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
@pytest.mark.asyncio
async def test_unmount_discards_late_worker_results_callbacks_and_queued_work(
    outcome: str,
) -> None:
    analysis = _TeardownAnalysisService(outcome)
    app = RunLauncherApp(
        Path.cwd(),
        launcher_command_registry(),
        analysis_service=analysis,  # type: ignore[arg-type]
    )

    async with app.run_test(size=(100, 32)) as pilot:
        composer = app.query_one("#composer", Composer)
        composer.load_text("Start analysis")
        composer.action_submit()
        for _ in range(20):
            await pilot.pause()
            if analysis.started.is_set():
                break
        assert analysis.started.is_set()

        app._pending_operation = lambda: (_ for _ in ()).throw(
            AssertionError("queued work ran after unmount")
        )
        if outcome == "cancel":
            app.workers.cancel_group(app, "workflow")
        app.on_unmount()
        analysis.release.set()
        for _ in range(40):
            await pilot.pause()
            if analysis.exited.is_set():
                break

        assert analysis.exited.is_set()
        assert app._pending_operation is None
        assert app._active_run_id is None
        assert all(
            f"late {outcome}" not in line.text
            for line in app.query_one("#activity", RichLog).lines
        )


@pytest.mark.asyncio
async def test_workspace_transition_removes_analysis_actions() -> None:
    analysis = _TransitionAnalysisService()
    app = RunLauncherApp(
        Path.cwd(),
        launcher_command_registry(),
        analysis_service=analysis,  # type: ignore[arg-type]
        workspace_service=_WorkspaceResumeService(),  # type: ignore[arg-type]
        recovery_service=_SnapshotRecoveryService(
            _snapshot("workspace-preparation")
        ),  # type: ignore[arg-type]
    )

    async with app.run_test(size=(100, 32)) as pilot:
        composer = app.query_one("#composer", Composer)
        composer.load_text("Start analysis")
        composer.action_submit()
        for _ in range(20):
            await pilot.pause()
            if not composer.disabled:
                break

        app.post_message(Composer.Submitted("/resume"))
        await pilot.pause()
        menu = app.query_one("#command-menu", OptionList)
        menu.highlighted = 0
        menu.focus()
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if app.query_one("#workspace-view", WorkspacePreparationView).display:
                break

        composer.load_text("/acc")
        await pilot.pause()
        assert menu.option_count == 0
        assert menu.display is False

        app.post_message(Composer.Submitted("/accept"))
        app.post_message(Composer.Submitted("Revise the already accepted plan"))
        await pilot.pause()

        output = app.query_one("#activity", RichLog)
        assert output.display is True
        assert any("only available during active analysis" in line.text for line in output.lines)

    assert analysis.accepted == []
    assert analysis.continued == []


@pytest.mark.asyncio
async def test_workspace_resume_choice_targets_the_selected_run() -> None:
    workspace = _WorkspaceResumeService()
    app = RunLauncherApp(
        Path.cwd(),
        launcher_command_registry(),
        analysis_service=_TransitionAnalysisService(),  # type: ignore[arg-type]
        workspace_service=workspace,  # type: ignore[arg-type]
        recovery_service=_SnapshotRecoveryService(
            _snapshot("workspace-preparation")
        ),  # type: ignore[arg-type]
    )

    async with app.run_test(size=(100, 32)) as pilot:
        app.post_message(Composer.Submitted("/resume"))
        await pilot.pause()
        menu = app.query_one("#command-menu", OptionList)
        menu.highlighted = 0
        menu.focus()
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if (
                app.query_one("#workspace-view", WorkspacePreparationView).display
                and not app.query_one("#workspace-current-choice", Button).disabled
            ):
                break

        app.query_one("#workspace-current-choice", Button).press()
        for _ in range(20):
            await pilot.pause()
            if workspace.prepared:
                break

    assert workspace.prepared == [(TRANSITION_RUN_ID, WorkspaceChoice.CURRENT_CHECKOUT)]


@pytest.mark.asyncio
async def test_workspace_finalization_resume_routes_to_a_safe_presentation() -> None:
    checkpoint = _snapshot("workspace-finalization")
    app = RunLauncherApp(
        Path.cwd(),
        launcher_command_registry(),
        recovery_service=_SnapshotRecoveryService(checkpoint),  # type: ignore[arg-type]
    )

    async with app.run_test(size=(100, 32)) as pilot:
        app.post_message(Composer.Submitted("/resume"))
        await pilot.pause()
        menu = app.query_one("#command-menu", OptionList)
        menu.highlighted = 0
        menu.focus()
        await pilot.press("enter")
        await pilot.pause()

        status = str(app.query_one("#status", Static).render())
        output = app.query_one("#activity", RichLog)
        assert "WORKSPACE FINALIZATION" in status
        assert any("final handoff" in line.text for line in output.lines)


@pytest.mark.asyncio
async def test_option_selection_places_followup_typing_after_the_selected_command() -> None:
    app = RunLauncherApp(Path.cwd(), launcher_command_registry())

    async with app.run_test(size=(80, 24)) as pilot:
        composer = app.query_one("#composer", Composer)
        composer.load_text("/sta")
        await pilot.pause()

        menu = app.query_one("#command-menu", OptionList)
        menu.highlighted = 0
        menu.focus()
        await pilot.press("enter")
        await pilot.press("n", "o", "w")

        assert composer.text == "/status now"


@pytest.mark.asyncio
async def test_composer_keeps_multiline_history() -> None:
    app = RunLauncherApp(Path.cwd(), launcher_command_registry())

    async with app.run_test(size=(80, 24)) as pilot:
        composer = app.query_one("#composer", Composer)
        composer.load_text("first line\nsecond line")
        composer.action_submit()
        await pilot.pause()
        composer.action_previous_history()

        assert composer.text == "first line\nsecond line"
        await pilot.press("!")
        assert composer.text == "first line\nsecond line!"


@pytest.mark.asyncio
async def test_composer_preserves_selection_undo_redo_and_paste() -> None:
    app = RunLauncherApp(Path.cwd(), launcher_command_registry())

    async with app.run_test(size=(80, 24)) as pilot:
        composer = app.query_one("#composer", Composer)
        composer.load_text("abc")
        composer.move_cursor((0, 3))

        await pilot.press("shift+left")
        assert composer.selected_text == "c"

        await pilot.press("x")
        assert composer.text == "abx"
        await pilot.press("ctrl+z")
        assert composer.text == "abc"
        await pilot.press("ctrl+y")
        assert composer.text == "abx"

        app.post_message(events.Paste(" pasted"))
        await pilot.pause()
        assert composer.text == "abx pasted"


@pytest.mark.asyncio
async def test_launcher_remains_usable_after_resizing_to_a_short_terminal() -> None:
    submitted: list[str] = []
    app = RunLauncherApp(
        Path.cwd(),
        launcher_command_registry(),
        on_feature=submitted.append,
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.resize_terminal(40, 12)
        await pilot.pause()

        composer = app.query_one("#composer", Composer)
        status = app.query_one("#status", Static)
        assert composer.region.bottom <= status.region.y
        assert composer.has_focus

        composer.load_text("Build from a short terminal")
        await pilot.press("ctrl+enter")

    assert submitted == ["Build from a short terminal"]


@pytest.mark.asyncio
async def test_slash_menu_does_not_push_composer_below_status_in_a_short_terminal() -> None:
    app = RunLauncherApp(Path.cwd(), launcher_command_registry())

    async with app.run_test(size=(40, 12)) as pilot:
        composer = app.query_one("#composer", Composer)
        composer.load_text("/")
        await pilot.pause()

        menu = app.query_one("#command-menu", OptionList)
        status = app.query_one("#status", Static)
        assert menu.display is True
        assert composer.region.bottom <= status.region.y
