from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Callable, Set
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from pathlib import Path

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Label, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState, get_current_worker

from devloop.application.analysis import (
    ANALYSIS_STEP_ID,
    AnalysisAcceptance,
    AnalysisRunResult,
    AnalysisWorkflowError,
    AnalysisWorkflowService,
)
from devloop.application.capabilities import (
    CapabilityProfileService,
    standard_capability_catalog,
)
from devloop.application.config import ApplicationConfig
from devloop.application.control import WorkflowControlError, WorkflowControlService
from devloop.application.development import (
    CODE_REVIEW_STEP_ID,
    DEVELOPMENT_STEP_ID,
    WORKSPACE_STEP_ID,
    DevelopmentBlocked,
    DevelopmentCompleted,
    DevelopmentInterrupted,
    DevelopmentPaused,
    WorkspaceDevelopmentError,
    WorkspaceDevelopmentService,
    WorkspacePrepared,
)
from devloop.application.finalization import FinalizationError
from devloop.application.recovery import (
    FINALIZATION_STEP_ID,
    RecoveryDisposition,
    RecoveryError,
    RecoveryService,
)
from devloop.application.review_qa import (
    QA_STEP_ID,
    QaCompleted,
    ReviewCompleted,
    ReviewQaError,
    ReviewQaInterrupted,
    ReviewQaPaused,
    ReviewQaService,
)
from devloop.application.scheduler import (
    SchedulerAction,
    WorkflowSchedulerError,
    WorkflowSchedulerService,
)
from devloop.components.workspace import WorkspacePreparationCancelled, WorkspaceProposal
from devloop.domain.commands import CommandScope, SlashCommand, SlashCommandRegistry
from devloop.domain.development import WorkspaceChoice
from devloop.domain.identifiers import AttemptId, IssueId, StepInstanceId, WorkflowRunId
from devloop.domain.language import LanguageTag
from devloop.domain.operations import (
    ApprovalDecision,
    ApprovalRequest,
    StopAction,
    StopRequest,
)
from devloop.domain.run import (
    AnalysisIntent,
    BackendActivity,
    StepOutcome,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.execution.app_server import (
    AppServerApprovalRequest,
    cooperative_cancellation,
)
from devloop.persistence.run_store import RunStoreError
from devloop.ui.analysis import AnalysisIntentSelected, AnalysisView
from devloop.ui.composer import Composer
from devloop.ui.development import DevelopmentView
from devloop.ui.finalization import FinalizationView
from devloop.ui.modals import (
    ApprovalModal,
    CancelRunConfirmationModal,
    CapabilityOptionsModal,
    LanguageModal,
    StopModal,
)
from devloop.ui.qa import QaView
from devloop.ui.review import CodeReviewView
from devloop.ui.shared import IssueBoard, WorkflowStatusBar, WorkflowStatusModel
from devloop.ui.workspace import WorkspaceChoiceSelected, WorkspacePreparationView

DEFAULT_LAUNCHER_SCOPES = frozenset({CommandScope.GLOBAL})


class MenuMode(str, Enum):
    COMMANDS = "COMMANDS"
    RESUME = "RESUME"
    RECOVERY = "RECOVERY"


@dataclass(frozen=True)
class ComposerEdit:
    """A text-only edit produced by choosing a Slash Command suggestion."""

    text: str
    cursor_location: tuple[int, int]


class LauncherViewModel:
    """Pure suggestion and editing behavior for the Run Launcher."""

    def __init__(
        self,
        registry: SlashCommandRegistry,
        *,
        active_scopes: Set[CommandScope] = DEFAULT_LAUNCHER_SCOPES,
    ) -> None:
        self._registry = registry
        self._active_scopes = frozenset(active_scopes)

    def suggestions(self, composer_text: str) -> tuple[SlashCommand, ...]:
        """Return registered commands for a composer containing only a slash prefix."""
        if not composer_text.startswith("/"):
            return ()
        if any(character.isspace() for character in composer_text):
            return ()
        return self._registry.matching(composer_text, active_scopes=self._active_scopes)

    def select(self, command: SlashCommand) -> ComposerEdit:
        """Turn a suggestion into a composer edit, never a Workflow Run request."""
        text = f"/{command.command_id} "
        return ComposerEdit(text=text, cursor_location=(0, len(text)))


class RunLauncherApp(App[None]):
    TITLE = "Dev Loop"
    BINDINGS = [Binding("ctrl+c", "request_stop", "Stop", priority=True)]
    CSS = """
    Screen { layout: vertical; }
    #identity { height: 3; padding: 0 1; background: $panel; }
    #application-shell { height: 1fr; }
    #launcher { width: 1fr; height: 1fr; padding: 1 2; }
    #activity {
        height: 2;
        min-height: 1;
        max-height: 2;
        background: $panel;
    }
    #command-menu {
        height: auto;
        max-height: 8;
        display: none;
        overlay: screen;
        constrain: inside inside;
    }
    #composer { height: 1fr; min-height: 4; max-height: 8; border: solid $accent; }
    """

    def __init__(
        self,
        repository: Path,
        commands: SlashCommandRegistry,
        *,
        on_feature: Callable[[str], None] | None = None,
        analysis_service: AnalysisWorkflowService | None = None,
        workspace_service: WorkspaceDevelopmentService | None = None,
        review_qa_service: ReviewQaService | None = None,
        scheduler_service: WorkflowSchedulerService | None = None,
        recovery_service: RecoveryService | None = None,
        capability_service: CapabilityProfileService | None = None,
        control_service: WorkflowControlService | None = None,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._commands = commands
        self._on_feature = on_feature
        self._analysis_service = analysis_service
        self._workspace_service = workspace_service
        self._review_qa_service = review_qa_service
        self._scheduler_service = scheduler_service
        self._recovery_service = recovery_service
        self._capability_service = capability_service
        self._control_service = control_service
        self._language = LanguageTag("en")
        self._started_at = time.monotonic()
        self._active_run_id: WorkflowRunId | None = None
        self._active_step: StepInstanceId | None = None
        self._active_scopes = set(DEFAULT_LAUNCHER_SCOPES)
        self._menu_mode = MenuMode.COMMANDS
        self._resume_steps: dict[WorkflowRunId, StepInstanceId] = {}
        self._pending_recovery: tuple[WorkflowRunId, StepInstanceId] | None = None
        self._busy = False
        self._active_worker: Worker[None] | None = None
        self._active_worker_terminal: WorkerState | None = None
        self._active_worker_exited = False
        self._pending_operation: Callable[[], None] | None = None
        self._lifecycle_active = False

    def compose(self) -> ComposeResult:
        yield Static(f"Dev Loop\n{self._repository}", id="identity")
        with Horizontal(id="application-shell"):
            with Vertical(id="launcher"):
                yield Label("What do you want to build?")
                yield RichLog(id="activity", wrap=True, markup=False)
                yield AnalysisView(id="analysis-view")
                yield WorkspacePreparationView(id="workspace-view")
                yield DevelopmentView(id="development-view")
                yield CodeReviewView(id="review-view")
                yield QaView(id="qa-view")
                yield FinalizationView(id="finalization-view")
                yield OptionList(id="command-menu")
                yield Composer(widget_id="composer")
            yield IssueBoard(id="issue-board")
        yield WorkflowStatusBar(id="status")

    def on_mount(self) -> None:
        self._lifecycle_active = True
        self.query_one("#issue-board", IssueBoard).display = False
        self.query_one("#composer", Composer).focus()

    def on_unmount(self) -> None:
        self._lifecycle_active = False
        self._pending_operation = None
        worker = self._active_worker
        if worker is not None and not worker.is_finished:
            worker.cancel()

    def on_resize(self, event: events.Resize) -> None:
        if self._active_run_id is not None:
            self._refresh_issue_board(force=False)

    def _transition_to(
        self,
        run_id: WorkflowRunId | None,
        step: StepInstanceId | None,
    ) -> None:
        self._active_run_id = run_id
        self._active_step = step
        scopes = set(DEFAULT_LAUNCHER_SCOPES)
        if run_id is not None:
            scopes.add(CommandScope.WORKFLOW)
        if step == ANALYSIS_STEP_ID:
            scopes.add(CommandScope.STEP)
        self._active_scopes = scopes
        self.query_one("#activity", RichLog).display = True
        menu = self.query_one("#command-menu", OptionList)
        menu.clear_options()
        menu.display = False
        if self._lifecycle_active:
            self.call_later(self._refresh_issue_board_after_transition)

    def _refresh_issue_board_after_transition(self) -> None:
        if self._lifecycle_active:
            self._refresh_issue_board(force=False)

    def _start_operation(self, operation: Callable[[], None]) -> bool:
        if self._busy or not self._lifecycle_active:
            return False
        self._busy = True
        self._set_operation_controls(disabled=True)
        try:
            self._launch_operation(operation)
        except Exception:
            self._busy = False
            self._set_operation_controls(disabled=False)
            raise
        return True

    def _launch_operation(self, operation: Callable[[], None]) -> None:
        if not self._lifecycle_active:
            return
        self._active_worker_terminal = None
        self._active_worker_exited = False
        self._active_worker = self._run_operation_worker(operation)

    @work(thread=True, group="workflow")
    def _run_operation_worker(self, operation: Callable[[], None]) -> None:
        worker = get_current_worker()
        try:
            with cooperative_cancellation(
                lambda: worker.is_cancelled or not self._lifecycle_active
            ):
                operation()
        finally:
            if self._lifecycle_active:
                try:
                    self.call_from_thread(self._operation_callable_exited, worker)
                except RuntimeError:
                    pass

    def _queue_operation(self, operation: Callable[[], None]) -> None:
        if not self._lifecycle_active:
            return
        if self._active_worker is not None and self._active_worker.is_cancelled:
            return
        if not self._busy:
            self._start_operation(operation)
            return
        if self._pending_operation is not None:
            raise RuntimeError("Only one Workflow operation may be queued at a time.")
        self._pending_operation = operation

    @on(Worker.StateChanged)
    def finish_operation(self, event: Worker.StateChanged) -> None:
        if not self._lifecycle_active:
            return
        if event.worker is not self._active_worker:
            return
        if event.state not in {
            WorkerState.SUCCESS,
            WorkerState.ERROR,
            WorkerState.CANCELLED,
        }:
            return
        self._active_worker_terminal = event.state
        if event.state is WorkerState.CANCELLED:
            self._pending_operation = None
        self._complete_operation_if_stopped(event.worker)

    def _operation_callable_exited(self, worker: Worker[None]) -> None:
        if not self._lifecycle_active:
            return
        if worker is not self._active_worker:
            return
        self._active_worker_exited = True
        self._complete_operation_if_stopped(worker)

    def _complete_operation_if_stopped(self, worker: Worker[None]) -> None:
        if not self._lifecycle_active:
            self._pending_operation = None
            self._active_worker = None
            self._active_worker_terminal = None
            self._active_worker_exited = False
            self._busy = False
            return
        if (
            worker is not self._active_worker
            or not self._active_worker_exited
            or self._active_worker_terminal is None
        ):
            return
        terminal = self._active_worker_terminal
        pending = self._pending_operation
        self._pending_operation = None
        if terminal is WorkerState.SUCCESS and pending is not None:
            try:
                self._launch_operation(pending)
            except Exception:
                self._active_worker = None
                self._busy = False
                self._set_operation_controls(disabled=False)
                raise
            return
        self._active_worker = None
        self._active_worker_terminal = None
        self._active_worker_exited = False
        self._busy = False
        self._set_operation_controls(disabled=False)

    def _call_from_operation_thread(
        self,
        callback: Callable[..., None],
        *args: object,
    ) -> None:
        worker = get_current_worker()
        if worker.is_cancelled or not self._lifecycle_active:
            return
        try:
            self.call_from_thread(self._deliver_operation_callback, worker, callback, args)
        except RuntimeError:
            pass

    def _deliver_operation_callback(
        self,
        worker: Worker[None],
        callback: Callable[..., None],
        args: tuple[object, ...],
    ) -> None:
        if (
            self._lifecycle_active
            and worker is self._active_worker
            and not worker.is_cancelled
        ):
            callback(*args)

    def _set_operation_controls(self, *, disabled: bool) -> None:
        try:
            composer = self.query_one("#composer", Composer)
            menu = self.query_one("#command-menu", OptionList)
            analysis_view = self.query_one("#analysis-view", AnalysisView)
            workspace_view = self.query_one("#workspace-view", WorkspacePreparationView)
        except NoMatches:
            return
        composer.disabled = disabled
        menu.disabled = disabled
        if disabled:
            menu.display = False
        analysis_view.set_busy(disabled)
        workspace_view.set_busy(disabled)

    @on(TextArea.Changed, "#composer")
    def update_command_menu(self, event: TextArea.Changed) -> None:
        value = event.text_area.text.lstrip()
        menu = self.query_one("#command-menu", OptionList)
        menu.clear_options()
        if self._busy:
            menu.display = False
            return
        if not value.startswith("/") or "\n" in value:
            menu.display = False
            return
        self._menu_mode = MenuMode.COMMANDS
        matches = self._commands.matching(value, active_scopes=self._active_scopes)
        menu.add_options(
            [
                Option(f"/{command.command_id}  {command.title}", id=command.command_id.value)
                for command in matches
            ]
        )
        menu.display = bool(matches)

    @on(OptionList.OptionSelected, "#command-menu")
    def select_command(self, event: OptionList.OptionSelected) -> None:
        if self._busy:
            return
        option_id = event.option.id
        if option_id is None:
            return
        if self._menu_mode is MenuMode.RECOVERY:
            pending = self._pending_recovery
            if option_id != "fresh-recovery-attempt" or pending is None:
                self._show_error("The Recovery Attempt choice is no longer available.")
                return
            run_id, recovery_step = pending
            menu = self.query_one("#command-menu", OptionList)
            menu.clear_options()
            menu.display = False
            self._pending_recovery = None
            self._transition_to(run_id, recovery_step)
            self._start_operation(
                lambda: self._run_fresh_recovery(run_id, recovery_step)
            )
            return
        if self._menu_mode is MenuMode.RESUME:
            try:
                run_id = WorkflowRunId(option_id)
            except ValueError:
                self._show_error("The selected Workflow Run ID is invalid.")
                return
            self.query_one("#command-menu", OptionList).display = False
            step = self._resume_steps.get(run_id)
            if step is None:
                self._show_error("The selected Workflow Run has no resumable step.")
                return
            recovery = self._recovery_service
            if recovery is None:
                self._show_error("Resume service is unavailable.")
                return
            try:
                plan = recovery.inspect(run_id)
            except (OSError, RunStoreError, ValueError) as error:
                self._show_error(str(error))
                return
            if plan.disposition is RecoveryDisposition.REFUSE:
                diagnostic = " ".join(plan.diagnostics)
                self._show_error(diagnostic or "The Workflow Run is unsafe to resume.")
                return
            if plan.disposition is RecoveryDisposition.FRESH_ATTEMPT:
                diagnostic = " ".join(plan.diagnostics)
                menu = self.query_one("#command-menu", OptionList)
                menu.clear_options()
                menu.add_option(
                    Option(
                        "Start transcript-free Recovery Attempt from locked structured context",
                        id="fresh-recovery-attempt",
                    )
                )
                menu.display = True
                menu.focus()
                self._menu_mode = MenuMode.RECOVERY
                self._pending_recovery = (run_id, step)
                self.query_one("#activity", RichLog).write(
                    diagnostic
                    or "The App Server thread is unavailable; choose a fresh Recovery Attempt."
                )
                return
            if plan.snapshot.active_step != step:
                self._show_error("The Workflow Run cursor changed; open /resume again.")
                return
            self._transition_to(run_id, step)
            if step == ANALYSIS_STEP_ID:
                self._start_operation(lambda: self._resume_analysis(run_id))
            elif step == WORKSPACE_STEP_ID:
                self._start_operation(lambda: self._resume_workspace(run_id))
            elif step == DEVELOPMENT_STEP_ID:
                self._start_operation(lambda: self._resume_development(run_id))
            elif step == CODE_REVIEW_STEP_ID:
                self._start_operation(lambda: self._run_review(run_id, resume=True))
            elif step == QA_STEP_ID:
                self._start_operation(lambda: self._run_qa(run_id, resume=True))
            elif step == FINALIZATION_STEP_ID:
                if self._scheduler_service is None:
                    self._show_finalization_resume(plan.snapshot)
                else:
                    self._start_operation(lambda: self._advance_scheduler(run_id))
            else:
                self._show_error("This Workflow step is not resumable in the current slice.")
            return
        view_model = LauncherViewModel(self._commands, active_scopes=self._active_scopes)
        command = next(
            (
                candidate
                for candidate in view_model.suggestions(f"/{option_id}")
                if candidate.command_id.value == option_id
            ),
            None,
        )
        if command is None:
            self._show_error("The selected Slash Command is unavailable in this context.")
            return
        edit = view_model.select(command)
        composer = self.query_one("#composer", Composer)
        composer.load_text(edit.text)
        composer.move_cursor(edit.cursor_location)
        composer.focus()

    @on(Composer.Submitted)
    def submit_feature(self, event: Composer.Submitted) -> None:
        if self._busy:
            self._handle_busy_submission(event.text)
            return
        menu = self.query_one("#command-menu", OptionList)
        menu.display = False
        activity = self.query_one("#activity", RichLog)
        if event.text.startswith("/"):
            self._handle_command(event.text)
            return
        run_id = self._active_run_id
        if run_id is not None:
            if self._active_step != ANALYSIS_STEP_ID or self._analysis_service is None:
                self._show_error("Composer input is only available during active analysis.")
                return
            self.query_one("#analysis-view", AnalysisView).append_activity(
                "Submitting clarification or requested changes to the persisted analysis thread."
            )
            self._start_operation(
                lambda: self._continue_analysis(run_id, event.text)
            )
            return
        if self._on_feature is not None:
            self._on_feature(event.text)
            activity.write("Feature request captured.")
            return
        if self._analysis_service is None:
            activity.write("Analysis service is unavailable.")
            return
        activity.write("Creating Workflow Run and starting real Codex analysis.")
        self._show_step_status(
            WorkflowRunStatus.RUNNING,
            ANALYSIS_STEP_ID,
            None,
            BackendActivity.STARTING,
        )
        self._start_operation(lambda: self._start_analysis(event.text))

    def _handle_busy_submission(self, text: str) -> None:
        if text.strip().casefold() != "/pause":
            return
        run_id = self._active_run_id
        if run_id is None:
            return
        if self._active_step == DEVELOPMENT_STEP_ID:
            workspace_service = self._workspace_service
            if workspace_service is None:
                return
            try:
                workspace_service.request_development_pause(run_id)
            except (WorkspaceDevelopmentError, OSError, ValueError) as error:
                self._show_error(str(error))
                return
            self.query_one("#status", Static).update(
                "PAUSING | DEVELOPMENT | INTERRUPTING ACTIVE TURN"
            )
            return
        if self._active_step in {CODE_REVIEW_STEP_ID, QA_STEP_ID}:
            review_qa_service = self._review_qa_service
            if review_qa_service is None:
                return
            try:
                review_qa_service.request_pause(run_id)
            except (ReviewQaError, OSError, ValueError) as error:
                self._show_error(str(error))
                return
            self.query_one("#status", Static).update(
                f"PAUSING | {self._active_step.value.upper()} | INTERRUPTING ACTIVE TURN"
            )

    @on(AnalysisIntentSelected)
    def handle_analysis_intent(self, event: AnalysisIntentSelected) -> None:
        if self._busy:
            return
        run_id = self._active_run_id
        if run_id is None or self._active_step != ANALYSIS_STEP_ID:
            self._show_error("There is no active analysis run.")
            return
        if event.intent is AnalysisIntent.REQUEST_CHANGES:
            composer = self.query_one("#composer", Composer)
            composer.focus()
            self.query_one("#analysis-view", AnalysisView).append_activity(
                "Describe the requested changes in the Composer."
            )
            return
        self._start_operation(lambda: self._accept_analysis(run_id))

    @on(WorkspaceChoiceSelected)
    def handle_workspace_choice(self, event: WorkspaceChoiceSelected) -> None:
        if self._busy:
            return
        run_id = self._active_run_id
        if run_id is None or self._active_step != WORKSPACE_STEP_ID:
            self._show_error("There is no active Workflow Run.")
            return
        self.query_one("#status", Static).update(f"RUNNING | WORKSPACE | {event.choice.value}")
        self._start_operation(
            lambda: self._prepare_and_develop(run_id, event.choice)
        )

    def _handle_command(self, command_text: str) -> None:
        if self._busy:
            return
        parts = command_text.split()
        command = parts[0].casefold()
        if command == "/resume":
            self._show_resume_menu()
            return
        if command == "/runs":
            if self._recovery_service is None:
                self._show_error("Workflow Run registry is unavailable.")
                return
            runs = self._recovery_service.list_runs()
            output = self.query_one("#activity", RichLog)
            if not runs:
                output.write("No Workflow Runs exist for the current project.")
                return
            for run in runs:
                issue = "no active Issue" if run.issue_id is None else run.issue_id.value
                output.write(
                    f"{run.run_id.value} | {run.status} | {run.step.value} | "
                    f"{issue} | {run.workspace or 'workspace not selected'}"
                )
            return
        if command == "/options":
            if self._capability_service is None:
                self._show_error("Capability options are unavailable.")
                return
            self.push_screen(CapabilityOptionsModal(self._capability_service.begin()))
            return
        if command == "/issues":
            if self._active_run_id is None:
                self._show_error("There is no active Workflow Run Issue Board.")
                return
            self._refresh_issue_board(force=True)
            return
        if command == "/status":
            self.query_one("#activity", RichLog).write(
                str(self.query_one("#status", WorkflowStatusBar).render())
            )
            return
        if command == "/language":
            if len(parts) == 2:
                try:
                    self._language = LanguageTag(parts[1])
                except ValueError as error:
                    self._show_error(str(error))
                    return
                self.query_one("#activity", RichLog).write(
                    f"Content language: {self._language.value}. Machine tokens are unchanged."
                )
            else:
                self.push_screen(LanguageModal(), self._language_selected)
            return
        if command == "/cancel":
            self.action_request_stop()
            return
        if command == "/pause":
            run_id = self._active_run_id
            if run_id is None or self._active_step != ANALYSIS_STEP_ID:
                self._show_error("There is no active analysis run to pause.")
                return
            pause_run_id = run_id

            def pause_active_analysis() -> None:
                self._pause_analysis(pause_run_id)

            self._start_operation(pause_active_analysis)
            return
        if command == "/accept":
            run_id = self._active_run_id
            if run_id is None or self._active_step != ANALYSIS_STEP_ID:
                self._show_error("There is no active analysis run to accept.")
                return
            accept_run_id = run_id
            self._start_operation(lambda: self._accept_analysis(accept_run_id))
            return
        if command == "/finalize":
            run_id = self._active_run_id
            if run_id is None or self._active_step != FINALIZATION_STEP_ID:
                self._show_error("There is no Workflow Run ready for finalization.")
                return
            finalize_run_id = run_id
            self._start_operation(lambda: self._advance_scheduler(finalize_run_id))
            return
        if command == "/request-changes":
            if self._active_run_id is None or self._active_step != ANALYSIS_STEP_ID:
                self._show_error("There is no active analysis run to revise.")
                return
            self.query_one("#analysis-view", AnalysisView).append_activity(
                "Describe the requested changes in the Composer."
            )
            self.query_one("#composer", Composer).focus()
            return
        if command in {"/retry", "/reset"}:
            run_id = self._active_run_id
            if run_id is None or self._scheduler_service is None:
                self._show_error("There is no active Workflow Run to update.")
                return
            if len(parts) != 2:
                self._show_error(f"Usage: {command} ISSUE-ID")
                return
            try:
                issue_id = IssueId(parts[1].upper())
            except ValueError as error:
                self._show_error(str(error))
                return
            self._start_operation(
                lambda: self._authorize_issue_transition(
                    run_id,
                    issue_id,
                    reset=command == "/reset",
                )
            )
            return
        self.query_one("#activity", RichLog).write(f"Command not implemented: {command}")

    def action_request_stop(self) -> None:
        issue_id = self._active_issue_id()
        request = StopRequest(
            self._active_step,
            issue_id,
            has_active_turn=self._busy and self._active_run_id is not None,
            has_active_run=self._active_run_id is not None,
        )
        self.push_screen(StopModal(request), self._stop_selected)

    def _stop_selected(self, action: StopAction | None) -> None:
        if action is None:
            return
        if action is StopAction.CONTINUE:
            self.query_one("#composer", Composer).focus()
            return
        if action is StopAction.CANCEL_RUN:
            self.push_screen(CancelRunConfirmationModal(), self._cancel_confirmed)
            return
        run_id = self._active_run_id
        if run_id is None:
            self._show_error("There is no active Workflow Run.")
            return
        if self._active_step == DEVELOPMENT_STEP_ID and self._workspace_service is not None:
            try:
                if action is StopAction.INTERRUPT_TURN:
                    self._workspace_service.request_development_interrupt(run_id)
                else:
                    self._workspace_service.request_development_pause(run_id)
            except (WorkspaceDevelopmentError, OSError, ValueError) as error:
                self._show_error(str(error))
                return
            label = "INTERRUPTING TURN" if action is StopAction.INTERRUPT_TURN else "PAUSING RUN"
            self.query_one("#status", WorkflowStatusBar).update(
                f"RUNNING | DEVELOPMENT | {label}"
            )
            return
        if (
            self._active_step in {CODE_REVIEW_STEP_ID, QA_STEP_ID}
            and self._review_qa_service is not None
        ):
            try:
                if action is StopAction.INTERRUPT_TURN:
                    self._review_qa_service.request_interrupt(run_id)
                else:
                    self._review_qa_service.request_pause(run_id)
            except (ReviewQaError, OSError, ValueError) as error:
                self._show_error(str(error))
                return
            label = "INTERRUPTING TURN" if action is StopAction.INTERRUPT_TURN else "PAUSING RUN"
            self.query_one("#status", WorkflowStatusBar).update(
                f"RUNNING | {self._active_step.value.upper()} | {label}"
            )
            return
        if action is StopAction.PAUSE_RUN and self._active_step == ANALYSIS_STEP_ID:
            if self._busy:
                self._show_error("Analysis will pause when its active turn reaches a checkpoint.")
            else:
                self._start_operation(lambda: self._pause_analysis(run_id))
            return
        self._show_error("The selected stop action is unavailable for this active step.")

    def _cancel_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        run_id = self._active_run_id
        if run_id is None or self._control_service is None:
            self._show_error("Run cancellation is unavailable.")
            return
        if self._busy:
            if self._active_step == DEVELOPMENT_STEP_ID and self._workspace_service is not None:
                try:
                    self._workspace_service.request_development_pause(run_id)
                except (WorkspaceDevelopmentError, OSError, ValueError) as error:
                    self._show_error(str(error))
                    return
            elif (
                self._active_step in {CODE_REVIEW_STEP_ID, QA_STEP_ID}
                and self._review_qa_service is not None
            ):
                try:
                    self._review_qa_service.request_pause(run_id)
                except (ReviewQaError, OSError, ValueError) as error:
                    self._show_error(str(error))
                    return
            else:
                self._show_error("Interrupt the active turn before cancelling this run.")
                return
            self._queue_operation(lambda: self._cancel_run(run_id))
            return
        self._start_operation(lambda: self._cancel_run(run_id))

    def _cancel_run(self, run_id: WorkflowRunId) -> None:
        service = self._control_service
        if service is None:
            return
        try:
            snapshot = service.cancel(run_id)
        except (WorkflowControlError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        self._call_from_operation_thread(self._show_cancelled, snapshot)

    def _show_cancelled(self, snapshot: WorkflowRunSnapshot) -> None:
        self._transition_to(None, None)
        self.query_one("#issue-board", IssueBoard).display = False
        self.query_one("#activity", RichLog).write(
            f"Workflow Run {snapshot.run_id.value} cancelled. Workspace and Git topology intact."
        )
        self.query_one("#status", WorkflowStatusBar).update(
            "CANCELLED | WORKFLOW | NO ISSUE | NO ATTEMPT | IDLE | 00:00:00"
        )

    def _language_selected(self, language: LanguageTag | None) -> None:
        if language is None:
            return
        self._language = language
        self.query_one("#activity", RichLog).write(
            f"Content language: {language.value}. Machine tokens are unchanged."
        )

    def _await_approval(self, request: AppServerApprovalRequest) -> str | None:
        """Bridge the synchronous App Server worker to an explicit Textual modal."""
        worker = get_current_worker()
        if not self._lifecycle_active:
            return None
        completed = threading.Event()
        selected: list[ApprovalDecision | None] = []
        try:
            self.call_from_thread(
                self._show_approval,
                worker,
                request,
                completed,
                selected,
            )
        except RuntimeError:
            return None
        while not completed.wait(timeout=0.1):
            if worker.is_cancelled or not self._lifecycle_active:
                return None
        decision = selected[0] if selected else None
        return None if decision is None else decision.value

    @staticmethod
    def _accepts_approval(operation: Callable[..., object]) -> bool:
        return "on_approval" in inspect.signature(operation).parameters

    def _show_approval(
        self,
        worker: Worker[None],
        request: AppServerApprovalRequest,
        completed: threading.Event,
        selected: list[ApprovalDecision | None],
    ) -> None:
        if (
            not self._lifecycle_active
            or worker is not self._active_worker
            or worker.is_cancelled
        ):
            completed.set()
            return
        supported: list[ApprovalDecision] = []
        for value in request.supported_decisions:
            try:
                supported.append(ApprovalDecision(value))
            except ValueError:
                continue
        step = self._active_step
        if step is None:
            completed.set()
            return
        approval = ApprovalRequest(
            step,
            self._active_issue_id(),
            request.action,
            request.target,
            request.reason,
            tuple(supported),
        )
        if not approval.supported_decisions:
            self._show_error("The backend did not advertise a supported approval decision.")
            completed.set()
            return
        self.push_screen(
            ApprovalModal(approval),
            lambda decision: self._approval_selected(
                decision,
                completed,
                selected,
            ),
        )

    @staticmethod
    def _approval_selected(
        decision: ApprovalDecision | None,
        completed: threading.Event,
        selected: list[ApprovalDecision | None],
    ) -> None:
        selected.append(decision)
        completed.set()

    def _active_issue_id(self) -> IssueId | None:
        if self._active_run_id is None or self._scheduler_service is None:
            return None
        issue_board = getattr(self._scheduler_service, "issue_board", None)
        if not callable(issue_board):
            return None
        try:
            rows = issue_board(self._active_run_id)
        except (RunStoreError, WorkflowSchedulerError, OSError, ValueError):
            return None
        return next(
            (
                row.issue_id
                for row in rows
                if row.current_step == self._active_step
            ),
            None,
        )

    def _refresh_issue_board(self, *, force: bool) -> None:
        board = self.query_one("#issue-board", IssueBoard)
        run_id = self._active_run_id
        service = self._scheduler_service
        if run_id is None or service is None:
            board.display = False
            return
        issue_board = getattr(service, "issue_board", None)
        if not callable(issue_board):
            board.display = False
            return
        try:
            rows = issue_board(run_id)
        except (RunStoreError, WorkflowSchedulerError, OSError, ValueError):
            board.display = False
            return
        board.show_rows(rows)
        board.display = force or self.size.width >= 100

    def _show_resume_menu(self) -> None:
        if self._recovery_service is None:
            self._show_error("Resume service is unavailable.")
            return
        runs = self._recovery_service.list_candidates()
        menu = self.query_one("#command-menu", OptionList)
        menu.clear_options()
        self._menu_mode = MenuMode.RESUME
        self._resume_steps = {run.run_id: run.step for run in runs}
        menu.add_options(
            [
                Option(
                    f"{run.feature} | {run.workflow} | {run.step.value} | "
                    f"{run.issue_id.value if run.issue_id is not None else '-'} | "
                    f"{run.status} | {run.workspace or '-'} | {run.last_activity} | "
                    f"{run.validation.value}",
                    id=run.run_id.value,
                )
                for run in runs
            ]
        )
        menu.display = bool(runs)
        if not runs:
            self.query_one("#activity", RichLog).write(
                "No unfinished Workflow Runs exist for this project."
            )

    def _start_analysis(self, feature_request: str) -> None:
        service = self._analysis_service
        if service is None:
            return

        def activity(delta: str) -> None:
            self._call_from_operation_thread(self._show_activity_delta, delta)

        try:
            result = service.start(feature_request, on_activity=activity)
        except (AnalysisWorkflowError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        self._call_from_operation_thread(self._show_analysis, result)

    def _continue_analysis(self, run_id: WorkflowRunId, message: str) -> None:
        service = self._analysis_service
        if service is None:
            return

        def activity(delta: str) -> None:
            self._call_from_operation_thread(self._show_activity_delta, delta)

        try:
            result = service.continue_analysis(run_id, message, on_activity=activity)
        except (AnalysisWorkflowError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        self._call_from_operation_thread(self._show_analysis, result)

    def _resume_analysis(self, run_id: WorkflowRunId) -> None:
        service = self._analysis_service
        if service is None:
            return

        def activity(delta: str) -> None:
            self._call_from_operation_thread(self._show_activity_delta, delta)

        try:
            result = service.resume(run_id, on_activity=activity)
        except (AnalysisWorkflowError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        self._call_from_operation_thread(self._show_analysis, result)

    def _pause_analysis(self, run_id: WorkflowRunId) -> None:
        service = self._analysis_service
        if service is None:
            return
        try:
            snapshot = service.pause(run_id)
        except (AnalysisWorkflowError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        self._call_from_operation_thread(self._show_analysis_paused, snapshot)

    def _accept_analysis(self, run_id: WorkflowRunId) -> None:
        service = self._analysis_service
        if service is None:
            return
        try:
            result = service.accept(run_id)
        except (AnalysisWorkflowError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        self._call_from_operation_thread(self._show_acceptance, result)

    def _load_workspace_proposal(self, run_id: WorkflowRunId) -> None:
        service = self._workspace_service
        if service is None:
            return
        try:
            proposal = service.proposal(run_id)
        except (WorkspaceDevelopmentError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        self._call_from_operation_thread(self._show_workspace, run_id, proposal)

    def _resume_workspace(self, run_id: WorkflowRunId) -> None:
        service = self._workspace_service
        if service is None:
            return
        try:
            proposal = service.resume_workspace(run_id)
        except (WorkspaceDevelopmentError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        self._call_from_operation_thread(self._show_workspace, run_id, proposal)

    def _prepare_and_develop(self, run_id: WorkflowRunId, choice: WorkspaceChoice) -> None:
        service = self._workspace_service
        if service is None:
            return
        try:
            prepared = service.prepare(run_id, choice)
        except WorkspacePreparationCancelled as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        except (WorkspaceDevelopmentError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        self._call_from_operation_thread(self._show_development_prepared, prepared)

        def activity(delta: str) -> None:
            self._call_from_operation_thread(self._show_development_activity, delta)

        try:
            if self._accepts_approval(service.develop):
                completed = service.develop(
                    run_id,
                    on_activity=activity,
                    on_approval=self._await_approval,
                )
            else:
                completed = service.develop(run_id, on_activity=activity)
        except DevelopmentPaused as paused:
            self._call_from_operation_thread(
                self._show_development_paused,
                paused.snapshot,
            )
            return
        except DevelopmentInterrupted as interrupted:
            self._call_from_operation_thread(
                self._show_development_interrupted,
                interrupted.snapshot,
            )
            return
        except (WorkspaceDevelopmentError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        if isinstance(completed, DevelopmentBlocked):
            self._call_from_operation_thread(
                self._show_scheduler_status,
                completed.snapshot.run_id,
                completed.snapshot.active_step,
                "PAUSED | DEVELOPMENT BLOCKED | CHECKING INDEPENDENT ISSUES",
            )
            self._advance_scheduler(run_id)
            return
        self._call_from_operation_thread(self._show_development_completed, completed)

    def _resume_development(self, run_id: WorkflowRunId) -> None:
        service = self._workspace_service
        if service is None:
            return

        def activity(delta: str) -> None:
            self._call_from_operation_thread(self._show_development_activity, delta)

        try:
            if self._accepts_approval(service.resume_development):
                completed = service.resume_development(
                    run_id,
                    on_activity=activity,
                    on_approval=self._await_approval,
                )
            else:
                completed = service.resume_development(run_id, on_activity=activity)
        except DevelopmentPaused as paused:
            self._call_from_operation_thread(
                self._show_development_paused,
                paused.snapshot,
            )
            return
        except DevelopmentInterrupted as interrupted:
            self._call_from_operation_thread(
                self._show_development_interrupted,
                interrupted.snapshot,
            )
            return
        except (WorkspaceDevelopmentError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        if isinstance(completed, DevelopmentBlocked):
            self._call_from_operation_thread(
                self._show_scheduler_status,
                completed.snapshot.run_id,
                completed.snapshot.active_step,
                "PAUSED | DEVELOPMENT BLOCKED | CHECKING INDEPENDENT ISSUES",
            )
            self._advance_scheduler(run_id)
            return
        self._call_from_operation_thread(self._show_development_completed, completed)

    def _run_fresh_recovery(
        self,
        run_id: WorkflowRunId,
        step: StepInstanceId,
    ) -> None:
        recovery = self._recovery_service
        if recovery is None:
            return
        try:
            snapshot = recovery.start_fresh_attempt(run_id)
        except (OSError, RecoveryError, RunStoreError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        if snapshot.active_step != step:
            self._call_from_operation_thread(
                self._show_error,
                "The Workflow Run cursor changed before the Recovery Attempt started.",
            )
            return
        if step == ANALYSIS_STEP_ID:
            self._run_analysis_recovery(run_id)
        elif step == DEVELOPMENT_STEP_ID:
            self._run_development_recovery(run_id)
        elif step == CODE_REVIEW_STEP_ID:
            self._run_review(run_id)
        elif step == QA_STEP_ID:
            self._run_qa(run_id)
        else:
            self._call_from_operation_thread(
                self._show_error,
                "This Workflow phase cannot start a transcript-free Recovery Attempt.",
            )

    def _run_analysis_recovery(self, run_id: WorkflowRunId) -> None:
        service = self._analysis_service
        if service is None:
            return

        def activity(delta: str) -> None:
            self._call_from_operation_thread(self._show_activity_delta, delta)

        try:
            result = service.recover_fresh(run_id, on_activity=activity)
        except (AnalysisWorkflowError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        self._call_from_operation_thread(self._show_analysis, result)

    def _run_development_recovery(self, run_id: WorkflowRunId) -> None:
        service = self._workspace_service
        if service is None:
            return

        def activity(delta: str) -> None:
            self._call_from_operation_thread(self._show_development_activity, delta)

        try:
            if self._accepts_approval(service.develop):
                completed = service.develop(
                    run_id,
                    on_activity=activity,
                    on_approval=self._await_approval,
                )
            else:
                completed = service.develop(run_id, on_activity=activity)
        except DevelopmentPaused as paused:
            self._call_from_operation_thread(
                self._show_development_paused,
                paused.snapshot,
            )
            return
        except DevelopmentInterrupted as interrupted:
            self._call_from_operation_thread(
                self._show_development_interrupted,
                interrupted.snapshot,
            )
            return
        except (WorkspaceDevelopmentError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        if isinstance(completed, DevelopmentBlocked):
            self._call_from_operation_thread(
                self._show_scheduler_status,
                completed.snapshot.run_id,
                completed.snapshot.active_step,
                "PAUSED | DEVELOPMENT BLOCKED | CHECKING INDEPENDENT ISSUES",
            )
            self._advance_scheduler(run_id)
            return
        self._call_from_operation_thread(self._show_development_completed, completed)

    def _run_review(self, run_id: WorkflowRunId, *, resume: bool = False) -> None:
        service = self._review_qa_service
        if service is None:
            return
        try:
            issue_id = service.active_issue_id(run_id)
        except (ReviewQaError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        self._call_from_operation_thread(self._show_review_running, run_id, issue_id)

        def activity(delta: str) -> None:
            self._call_from_operation_thread(self._show_review_activity, delta)

        try:
            review = service.resume_review if resume else service.review
            if self._accepts_approval(review):
                completed = review(
                    run_id,
                    on_activity=activity,
                    on_approval=self._await_approval,
                )
            else:
                completed = review(run_id, on_activity=activity)
        except ReviewQaPaused as paused:
            self._call_from_operation_thread(self._show_verification_paused, paused.snapshot)
            return
        except ReviewQaInterrupted as interrupted:
            self._call_from_operation_thread(
                self._show_verification_interrupted,
                interrupted.snapshot,
            )
            return
        except (ReviewQaError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        self._call_from_operation_thread(self._show_review_completed, completed)

    def _run_qa(self, run_id: WorkflowRunId, *, resume: bool = False) -> None:
        service = self._review_qa_service
        if service is None:
            return
        try:
            issue_id = service.active_issue_id(run_id)
        except (ReviewQaError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        self._call_from_operation_thread(self._show_qa_running, run_id, issue_id)

        def activity(delta: str) -> None:
            self._call_from_operation_thread(self._show_qa_activity, delta)

        try:
            qa = service.resume_qa if resume else service.qa
            if self._accepts_approval(qa):
                completed = qa(
                    run_id,
                    on_activity=activity,
                    on_approval=self._await_approval,
                )
            else:
                completed = qa(run_id, on_activity=activity)
        except ReviewQaPaused as paused:
            self._call_from_operation_thread(self._show_verification_paused, paused.snapshot)
            return
        except ReviewQaInterrupted as interrupted:
            self._call_from_operation_thread(
                self._show_verification_interrupted,
                interrupted.snapshot,
            )
            return
        except (ReviewQaError, OSError, ValueError) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        self._call_from_operation_thread(self._show_qa_completed, completed)

    def _advance_scheduler(self, run_id: WorkflowRunId) -> None:
        service = self._scheduler_service
        if service is None:
            return

        def activity(delta: str) -> None:
            self._call_from_operation_thread(self._show_development_activity, delta)

        try:
            if self._accepts_approval(service.advance):
                advanced = service.advance(
                    run_id,
                    on_activity=activity,
                    on_approval=self._await_approval,
                )
            else:
                advanced = service.advance(run_id, on_activity=activity)
        except (
            WorkflowSchedulerError,
            FinalizationError,
            WorkspaceDevelopmentError,
            ReviewQaError,
            OSError,
            ValueError,
        ) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        if advanced.finalization is not None:
            self._call_from_operation_thread(
                self._show_finalization_completed,
                advanced.finalization.snapshot,
                advanced.finalization.summary,
            )
            return
        if advanced.development is not None:
            self._call_from_operation_thread(
                self._show_development_completed,
                advanced.development,
            )
            return
        if advanced.action is SchedulerAction.WORKFLOW_DRAINED:
            self._call_from_operation_thread(self._show_finalization_resume, advanced.snapshot)
        elif advanced.action is SchedulerAction.PAUSED:
            self._call_from_operation_thread(
                self._show_scheduler_status,
                advanced.snapshot.run_id,
                advanced.snapshot.active_step,
                "PAUSED | SCHEDULER | BLOCKED ISSUES REQUIRE /RETRY",
            )

    def _authorize_issue_transition(
        self,
        run_id: WorkflowRunId,
        issue_id: IssueId,
        *,
        reset: bool,
    ) -> None:
        service = self._scheduler_service
        if service is None:
            return
        try:
            prepared = (
                service.reset_failed_issue(run_id, issue_id)
                if reset
                else service.retry_blocked_issue(run_id, issue_id)
            )
        except (
            WorkflowSchedulerError,
            WorkspaceDevelopmentError,
            OSError,
            ValueError,
        ) as error:
            self._call_from_operation_thread(self._show_error, str(error))
            return
        self._call_from_operation_thread(
            self._show_scheduler_status,
            run_id,
            prepared.snapshot.active_step,
            f"RUNNING | DEVELOPMENT | {issue_id.value} | EXPLICIT "
            f"{'RESET' if reset else 'RETRY'} AUTHORIZED",
        )
        self._advance_scheduler(run_id)

    def _show_analysis(self, result: AnalysisRunResult) -> None:
        self._transition_to(result.snapshot.run_id, result.snapshot.active_step)
        self.query_one("#launcher > Label", Label).display = False
        self.query_one("#analysis-view", AnalysisView).show_result(result)
        self._show_typed_status(result.snapshot, BackendActivity.IDLE)

    def _show_analysis_paused(self, snapshot: WorkflowRunSnapshot) -> None:
        self._transition_to(None, None)
        self.query_one("#analysis-view", AnalysisView).append_activity(
            f"Workflow Run {snapshot.run_id.value} paused. Use /resume to continue."
        )
        self._show_typed_status(snapshot, BackendActivity.IDLE)

    def _show_acceptance(self, acceptance: AnalysisAcceptance) -> None:
        self._transition_to(acceptance.snapshot.run_id, acceptance.snapshot.active_step)
        view = self.query_one("#analysis-view", AnalysisView)
        view.append_activity(f"Published PRD Package: {acceptance.package.root}")
        self._show_typed_status(acceptance.snapshot, BackendActivity.IDLE)
        self._queue_operation(
            lambda: self._load_workspace_proposal(acceptance.snapshot.run_id)
        )

    def _show_workspace(self, run_id: WorkflowRunId, proposal: WorkspaceProposal) -> None:
        self._transition_to(run_id, WORKSPACE_STEP_ID)
        self.query_one("#analysis-view", AnalysisView).display = False
        self.query_one("#development-view", DevelopmentView).display = False
        self.query_one("#review-view", CodeReviewView).display = False
        self.query_one("#qa-view", QaView).display = False
        self.query_one("#workspace-view", WorkspacePreparationView).show_proposal(proposal)
        self._show_step_status(
            WorkflowRunStatus.AWAITING_USER,
            WORKSPACE_STEP_ID,
            None,
            BackendActivity.IDLE,
        )

    def _show_development_prepared(self, prepared: WorkspacePrepared) -> None:
        self._transition_to(prepared.snapshot.run_id, prepared.snapshot.active_step)
        self.query_one("#workspace-view", WorkspacePreparationView).display = False
        self.query_one("#development-view", DevelopmentView).show_prepared(prepared)
        self.query_one("#review-view", CodeReviewView).display = False
        self.query_one("#qa-view", QaView).display = False
        composer = self.query_one("#composer", Composer)
        composer.disabled = False
        composer.focus()
        self._show_typed_status(prepared.snapshot, BackendActivity.STARTING)

    def _show_development_paused(self, snapshot: WorkflowRunSnapshot) -> None:
        self._transition_to(snapshot.run_id, snapshot.active_step)
        self.query_one("#development-view", DevelopmentView).display = True
        self._show_typed_status(snapshot, BackendActivity.IDLE)

    def _show_development_interrupted(self, snapshot: WorkflowRunSnapshot) -> None:
        self._transition_to(snapshot.run_id, snapshot.active_step)
        self.query_one("#development-view", DevelopmentView).display = True
        self.query_one("#development-view", DevelopmentView).append_activity(
            "Active turn interrupted. Use /resume to continue this run on its thread."
        )
        self._show_typed_status(snapshot, BackendActivity.IDLE)

    def _show_verification_paused(self, snapshot: WorkflowRunSnapshot) -> None:
        self._transition_to(snapshot.run_id, snapshot.active_step)
        self.query_one("#activity", RichLog).write(
            "Verification paused. Use /resume to recover the exact workflow cursor."
        )
        self._show_typed_status(snapshot, BackendActivity.IDLE)

    def _show_verification_interrupted(self, snapshot: WorkflowRunSnapshot) -> None:
        self._transition_to(snapshot.run_id, snapshot.active_step)
        self.query_one("#activity", RichLog).write(
            "Verification turn interrupted. Use /resume to continue this run."
        )
        self._show_typed_status(snapshot, BackendActivity.IDLE)

    def _show_finalization_resume(self, snapshot: WorkflowRunSnapshot) -> None:
        self._transition_to(snapshot.run_id, FINALIZATION_STEP_ID)
        self.query_one("#analysis-view", AnalysisView).display = False
        self.query_one("#workspace-view", WorkspacePreparationView).display = False
        self.query_one("#development-view", DevelopmentView).display = False
        self.query_one("#review-view", CodeReviewView).display = False
        self.query_one("#qa-view", QaView).display = False
        self.query_one("#finalization-view", FinalizationView).show_snapshot(snapshot)
        self.query_one("#activity", RichLog).write(
            "Workspace finalization is ready. Run /finalize to create the Handoff Summary and "
            "review the final handoff while leaving the "
            "workspace intact; no merge, push, branch deletion, or worktree removal "
            "will run automatically."
        )
        self.query_one("#status", Static).update(
            "AWAITING_USER | WORKSPACE FINALIZATION | RUN /FINALIZE"
        )
        self._show_typed_status(snapshot, BackendActivity.IDLE)

    def _show_finalization_completed(self, snapshot: WorkflowRunSnapshot, handoff: object) -> None:
        from devloop.domain.finalization import HandoffSummary

        if not isinstance(handoff, HandoffSummary):
            self._show_error("Workspace finalization returned an invalid Handoff Summary.")
            return
        self._transition_to(snapshot.run_id, FINALIZATION_STEP_ID)
        self.query_one("#finalization-view", FinalizationView).show_completed(snapshot, handoff)
        self.query_one("#activity", RichLog).write(
            "Workflow completed. The workspace was left intact for explicit publication."
        )
        self._show_typed_status(snapshot, BackendActivity.IDLE)

    def _show_development_activity(self, delta: str) -> None:
        self.query_one("#development-view", DevelopmentView).append_activity(delta)

    def _show_development_completed(self, completed: DevelopmentCompleted) -> None:
        self._transition_to(completed.snapshot.run_id, completed.snapshot.active_step)
        view = self.query_one("#development-view", DevelopmentView)
        view.display = True
        self.query_one("#workspace-view", WorkspacePreparationView).display = False
        view.show_completed(completed)
        self._show_typed_status(completed.snapshot, BackendActivity.STARTING)
        self._queue_operation(lambda: self._run_review(completed.snapshot.run_id))

    def _show_review_running(self, run_id: WorkflowRunId, issue_id: IssueId) -> None:
        self._transition_to(run_id, CODE_REVIEW_STEP_ID)
        self.query_one("#development-view", DevelopmentView).display = False
        self.query_one("#qa-view", QaView).display = False
        self.query_one("#review-view", CodeReviewView).show_running(issue_id)
        self._show_step_status(
            WorkflowRunStatus.RUNNING,
            CODE_REVIEW_STEP_ID,
            issue_id,
            BackendActivity.STREAMING,
        )

    def _show_review_activity(self, delta: str) -> None:
        self.query_one("#review-view", CodeReviewView).append_activity(delta)

    def _show_review_completed(self, completed: ReviewCompleted) -> None:
        self._transition_to(completed.snapshot.run_id, completed.snapshot.active_step)
        view = self.query_one("#review-view", CodeReviewView)
        view.show_completed(completed)
        self._show_typed_status(completed.snapshot, BackendActivity.IDLE)
        if completed.outcome is StepOutcome.SUCCEEDED:
            self._queue_operation(lambda: self._run_qa(completed.snapshot.run_id))
        else:
            self._queue_operation(
                lambda: self._advance_scheduler(completed.snapshot.run_id)
            )

    def _show_qa_running(self, run_id: WorkflowRunId, issue_id: IssueId) -> None:
        self._transition_to(run_id, QA_STEP_ID)
        self.query_one("#review-view", CodeReviewView).display = False
        self.query_one("#qa-view", QaView).show_running(issue_id)
        self._show_step_status(
            WorkflowRunStatus.RUNNING,
            QA_STEP_ID,
            issue_id,
            BackendActivity.STREAMING,
        )

    def _show_qa_activity(self, delta: str) -> None:
        self.query_one("#qa-view", QaView).append_activity(delta)

    def _show_qa_completed(self, completed: QaCompleted) -> None:
        self._transition_to(completed.snapshot.run_id, completed.snapshot.active_step)
        view = self.query_one("#qa-view", QaView)
        view.show_completed(completed)
        self._show_typed_status(completed.snapshot, BackendActivity.IDLE)
        self._queue_operation(lambda: self._advance_scheduler(completed.snapshot.run_id))

    def _show_scheduler_status(
        self,
        run_id: WorkflowRunId,
        step: StepInstanceId,
        value: str,
    ) -> None:
        self._transition_to(run_id, step)
        self.query_one("#status", Static).update(value)

    def _show_typed_status(
        self,
        snapshot: WorkflowRunSnapshot,
        backend_activity: BackendActivity,
    ) -> None:
        self.query_one("#status", WorkflowStatusBar).show_status(
            WorkflowStatusModel.from_snapshot(
                snapshot,
                backend_activity=backend_activity,
                elapsed=timedelta(seconds=time.monotonic() - self._started_at),
            )
        )

    def _show_step_status(
        self,
        workflow_status: WorkflowRunStatus,
        step: StepInstanceId,
        issue_id: IssueId | None,
        backend_activity: BackendActivity,
    ) -> None:
        issue_position: int | None = None
        issue_total: int | None = None
        issue_status = None
        attempt = None
        service = self._scheduler_service
        issue_board = None if service is None else getattr(service, "issue_board", None)
        if issue_id is not None and callable(issue_board) and self._active_run_id is not None:
            try:
                rows = issue_board(self._active_run_id)
            except (RunStoreError, WorkflowSchedulerError, OSError, ValueError):
                rows = ()
            row = next((item for item in rows if item.issue_id == issue_id), None)
            if row is not None:
                issue_position = row.position
                issue_total = len(rows)
                issue_status = row.status
                if row.attempt_number > 0:
                    attempt = AttemptId(f"attempt-{row.attempt_number:03d}")
        self.query_one("#status", WorkflowStatusBar).show_status(
            WorkflowStatusModel(
                workflow_status,
                step,
                issue_id,
                issue_position,
                issue_total,
                issue_status,
                attempt,
                backend_activity,
                timedelta(seconds=time.monotonic() - self._started_at),
            )
        )

    def _show_activity_delta(self, delta: str) -> None:
        view = self.query_one("#analysis-view", AnalysisView)
        if not view.display:
            view.display = True
        view.append_activity(delta)

    def _show_error(self, message: str) -> None:
        output = self.query_one("#activity", RichLog)
        output.display = True
        output.write(f"ERROR: {message}")
        self._show_step_status(
            WorkflowRunStatus.PAUSED,
            self._active_step or StepInstanceId("launcher"),
            self._active_issue_id(),
            BackendActivity.FAILED,
        )


def run_launcher(config: ApplicationConfig, commands: SlashCommandRegistry) -> None:
    RunLauncherApp(
        config.repository,
        commands,
        analysis_service=AnalysisWorkflowService(config),
        workspace_service=WorkspaceDevelopmentService(config),
        review_qa_service=ReviewQaService(config),
        scheduler_service=WorkflowSchedulerService(config),
        recovery_service=RecoveryService(config),
        capability_service=CapabilityProfileService(
            config.paths.user_config,
            standard_capability_catalog(),
        ),
        control_service=WorkflowControlService(config),
    ).run()
