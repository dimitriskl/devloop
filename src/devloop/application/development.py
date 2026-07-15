from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from devloop.application.approvals import (
    classify_backend_approval,
    persist_approval_decision,
)
from devloop.application.config import ApplicationConfig
from devloop.application.retry import run_with_transient_retries
from devloop.application.telemetry import ExecutionTelemetryRecorder
from devloop.application.workspace_preflight import (
    LocalWorkspacePreflight,
    WorkspacePreflight,
    WorkspacePreflightError,
)
from devloop.components.builtin import installed_component_registry
from devloop.components.development import (
    DEVELOPMENT_COMPONENT_ID,
    DevelopmentAgentOutput,
    DevelopmentComponentError,
    DevelopmentComponentRunner,
    DevelopmentTurnInterrupted,
    DevelopmentTurnPaused,
    DevelopmentTurnStalled,
)
from devloop.components.workspace import (
    WORKSPACE_COMPONENT_ID,
    WorkspaceComponentRunner,
    WorkspacePreparationCancelled,
    WorkspaceProposal,
)
from devloop.domain.capabilities import capabilities_for
from devloop.domain.approval import locked_approval_policy
from devloop.domain.development import (
    ArtifactRef,
    CapabilityProfile,
    ContextManifestRef,
    CriterionImplementationStatus,
    DevelopmentCursor,
    ImplementationResult,
    IssueRuntimeState,
    IssueStatus,
    WorkspaceBaselineEntry,
    WorkspaceChoice,
    WorkspaceRef,
    validate_rework_resolutions,
)
from devloop.domain.doctor import redact_diagnostic
from devloop.domain.execution import ExecutionPhase, locked_execution_profile
from devloop.domain.identifiers import (
    AttemptId,
    CapabilityId,
    ExecutionThreadId,
    ExecutionTurnId,
    IssueId,
    StepComponentId,
    StepInstanceId,
    WorkflowRunId,
)
from devloop.domain.outcomes import StepOutcome
from devloop.domain.planning import PlannedIssue, PlanningPackage
from devloop.domain.review_qa import REWORK_REQUEST_SCHEMA
from devloop.domain.run import (
    ComponentLock,
    OperationState,
    OperationStatus,
    RunEventType,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.domain.scheduler import (
    AttemptStatus,
    IssueAttemptRecord,
    archived_execution_threads,
    next_rework_attempt_number,
    refresh_issue_states,
    select_next_ready_issue,
)
from devloop.execution.app_server import (
    AppServerApprovalRequest,
    AppServerApprovalRequired,
    AppServerTransientError,
)
from devloop.infrastructure.git import (
    GitOperationError,
    capture_repository_state_hash,
    capture_workspace_baseline,
    capture_worktree_changes,
    current_branch,
    head_commit,
    repository_root,
)
from devloop.infrastructure.paths import (
    CONTEXT_MANIFESTS_DIRECTORY,
    IMPLEMENTATION_RESULTS_DIRECTORY,
)
from devloop.persistence.run_store import RunStore, RunStoreError
from devloop.planning.package_reader import (
    PlanningPackageError,
    initial_issue_states,
    load_planning_package,
)
from devloop.workflow.definition import load_standard_workflow, validate_component_ports

CONTEXT_MANIFEST_SCHEMA = "devloop.context-manifest/v1"
IMPLEMENTATION_RESULT_SCHEMA = "devloop.implementation-result/v1"
DEVELOPMENT_STEP_ID = StepInstanceId("development")
CODE_REVIEW_STEP_ID = StepInstanceId("code-review")
WORKSPACE_STEP_ID = StepInstanceId("workspace-preparation")
DEVELOPMENT_CAPABILITIES = CapabilityProfile((CapabilityId("implement"),))
MAX_REPOSITORY_INSTRUCTIONS_CHARS = 20_000


class WorkspaceDevelopmentError(RuntimeError):
    pass


class NoReadyIssueError(WorkspaceDevelopmentError):
    pass


class ReworkLimitReachedError(WorkspaceDevelopmentError):
    pass


class DevelopmentPaused(WorkspaceDevelopmentError):
    def __init__(self, snapshot: WorkflowRunSnapshot) -> None:
        super().__init__("Development paused after interrupting the active App Server turn.")
        self.snapshot = snapshot


class DevelopmentInterrupted(WorkspaceDevelopmentError):
    def __init__(self, snapshot: WorkflowRunSnapshot) -> None:
        super().__init__("Development active turn was interrupted without pausing the run.")
        self.snapshot = snapshot


@dataclass(frozen=True)
class WorkspacePrepared:
    snapshot: WorkflowRunSnapshot
    proposal: WorkspaceProposal
    workspace: WorkspaceRef
    issue: PlannedIssue


@dataclass(frozen=True)
class DevelopmentCompleted:
    snapshot: WorkflowRunSnapshot
    result: ImplementationResult


@dataclass(frozen=True)
class DevelopmentBlocked:
    snapshot: WorkflowRunSnapshot
    blocked_reason: str
    summary: str
    outcome: StepOutcome = StepOutcome.BLOCKED


@dataclass(frozen=True)
class DevelopmentPrepared:
    snapshot: WorkflowRunSnapshot
    issue: PlannedIssue


class WorkspaceDevelopmentService:
    def __init__(
        self,
        config: ApplicationConfig,
        *,
        workspace_preflight: WorkspacePreflight | None = None,
    ) -> None:
        self._config = config
        self._store = RunStore(config.paths.run_root)
        self._workflow = load_standard_workflow()
        self._registry = installed_component_registry()
        workspace_manifest, workspace_runner = self._registry.resolve(WORKSPACE_COMPONENT_ID)
        development_manifest, development_runner = self._registry.resolve(DEVELOPMENT_COMPONENT_ID)
        if not isinstance(workspace_runner, WorkspaceComponentRunner) or not isinstance(
            development_runner, DevelopmentComponentRunner
        ):
            raise WorkspaceDevelopmentError("Built-in workspace or development runner is invalid.")
        validate_component_ports(self._workflow.step(WORKSPACE_STEP_ID), workspace_manifest)
        validate_component_ports(self._workflow.step(DEVELOPMENT_STEP_ID), development_manifest)
        self._workspace_runner = workspace_runner
        self._development_runner = development_runner
        self._development_manifest = development_manifest
        self._telemetry = ExecutionTelemetryRecorder(self._store)
        self._workspace_preflight = workspace_preflight or LocalWorkspacePreflight()
        self._pause_lock = threading.Lock()
        self._pause_requests: set[WorkflowRunId] = set()
        self._interrupt_requests: set[WorkflowRunId] = set()

    def request_development_pause(self, run_id: WorkflowRunId) -> None:
        snapshot = self._store.load(run_id)
        if (
            snapshot.active_step != DEVELOPMENT_STEP_ID
            or snapshot.development is None
            or snapshot.run_status is not WorkflowRunStatus.RUNNING
        ):
            raise WorkspaceDevelopmentError("No active development turn can be paused.")
        with self._pause_lock:
            self._pause_requests.add(run_id)

    def request_development_interrupt(self, run_id: WorkflowRunId) -> None:
        snapshot = self._store.load(run_id)
        cursor = snapshot.development
        if (
            snapshot.active_step != DEVELOPMENT_STEP_ID
            or snapshot.run_status is not WorkflowRunStatus.RUNNING
            or cursor is None
            or cursor.turn_id is None
        ):
            raise WorkspaceDevelopmentError("No active development turn can be interrupted.")
        with self._pause_lock:
            self._interrupt_requests.add(run_id)

    def proposal(
        self,
        run_id: WorkflowRunId,
        *,
        worktree_parent: Path | None = None,
    ) -> WorkspaceProposal:
        snapshot = self._store.load(run_id)
        self._validate_preflight(snapshot)
        return self._workspace_runner.propose(
            self._config.repository,
            snapshot.feature_slug.value,
            worktree_parent=worktree_parent,
        )

    def resume_workspace(
        self,
        run_id: WorkflowRunId,
        *,
        worktree_parent: Path | None = None,
    ) -> WorkspaceProposal:
        snapshot = self._store.take_lease(self._store.load(run_id))
        self._validate_preflight(snapshot)
        resumed = replace(snapshot, run_status=WorkflowRunStatus.AWAITING_USER)
        self._store.record(resumed, RunEventType.RUN_RESUMED)
        return self._workspace_runner.propose(
            self._config.repository,
            snapshot.feature_slug.value,
            worktree_parent=worktree_parent,
        )

    def prepare(
        self,
        run_id: WorkflowRunId,
        choice: WorkspaceChoice,
        *,
        worktree_parent: Path | None = None,
    ) -> WorkspacePrepared:
        snapshot = self._ensure_lease(self._store.load(run_id))
        package = self._validate_preflight(snapshot)
        proposal = self._workspace_runner.propose(
            self._config.repository,
            snapshot.feature_slug.value,
            worktree_parent=worktree_parent,
        )
        snapshot = replace(
            snapshot,
            run_status=WorkflowRunStatus.RUNNING,
            step_status=StepRunStatus.RUNNING,
            outcome=None,
        )
        snapshot = self._store.record(
            snapshot,
            RunEventType.WORKSPACE_PREPARATION_STARTED,
        )
        try:
            workspace = self._workspace_runner.prepare(proposal, choice)
        except WorkspacePreparationCancelled:
            paused = replace(snapshot, run_status=WorkflowRunStatus.PAUSED)
            paused = self._store.record(paused, RunEventType.RUN_PAUSED)
            self._store.release_lease(paused)
            raise
        try:
            permission_profile = self._workspace_preflight.probe(
                Path(workspace.path),
                run_id,
            )
            workspace = replace(workspace, permission_profile=permission_profile)
        except WorkspacePreflightError as error:
            paused = replace(
                snapshot,
                workspace=workspace,
                run_status=WorkflowRunStatus.PAUSED,
                step_status=StepRunStatus.BLOCKED,
            )
            paused = self._store.record(paused, RunEventType.WORKSPACE_PREFLIGHT_FAILED)
            self._store.release_lease(paused)
            raise WorkspaceDevelopmentError(str(error)) from error
        self._validate_workspace(workspace)
        states = refresh_issue_states(package, initial_issue_states(package))
        issue = select_next_ready_issue(package, states)
        if issue is None:
            raise NoReadyIssueError("No dependency-ready Issue is available.")
        states = _replace_issue_status(
            states,
            issue,
            IssueStatus.IN_DEVELOPMENT,
            current_step=DEVELOPMENT_STEP_ID,
            repository_baseline=capture_workspace_baseline(Path(workspace.path)),
        )
        prepared = replace(
            snapshot,
            workspace=workspace,
            issues=states,
            active_step=self._workflow.required_transition_target(
                WORKSPACE_STEP_ID,
                StepOutcome.SUCCEEDED,
            ),
            run_status=WorkflowRunStatus.RUNNING,
            step_status=StepRunStatus.RUNNING,
            outcome=None,
            workspace_state_hash=capture_repository_state_hash(Path(workspace.path)),
        )
        prepared = self._store.record(prepared, RunEventType.WORKSPACE_PREPARED)
        attempt_id = AttemptId("attempt-001")
        context_ref = self._save_context_manifest(
            prepared, package, issue, attempt_id, rework_request=None
        )
        cursor = DevelopmentCursor(
            issue.issue_id,
            issue.position,
            len(package.issues),
            attempt_id,
            context_ref,
        )
        prepared = replace(prepared, development=cursor)
        prepared = self._store.record(prepared, RunEventType.CONTEXT_MANIFEST_SAVED)
        return WorkspacePrepared(prepared, proposal, workspace, issue)

    def prepare_next_ready(self, run_id: WorkflowRunId) -> DevelopmentPrepared:
        snapshot = self._ensure_lease(self._store.load(run_id))
        package = self._validate_preflight(snapshot)
        if snapshot.workspace is None:
            raise WorkspaceDevelopmentError("A prepared Workspace is required.")
        states = refresh_issue_states(package, snapshot.issues)
        issue = select_next_ready_issue(package, states)
        if issue is None:
            raise NoReadyIssueError("No dependency-ready Issue is available.")
        return self._prepare_issue_attempt(
            replace(snapshot, issues=states),
            package,
            issue,
            AttemptId("attempt-001"),
            rework_request=None,
        )

    def prepare_rework(self, run_id: WorkflowRunId) -> DevelopmentPrepared:
        snapshot = self._ensure_lease(self._store.load(run_id))
        package = self._validate_preflight(snapshot)
        cursor = snapshot.development
        if cursor is None:
            raise WorkspaceDevelopmentError("A development attempt is required for rework.")
        if _issue_status(snapshot.issues, cursor.issue_id) is not IssueStatus.CHANGES_REQUESTED:
            raise WorkspaceDevelopmentError("The active Issue has not requested changes.")
        current_number = _latest_attempt_number(snapshot, cursor.issue_id)
        changes_requested_attempts = max(
            1,
            sum(
                item.status is AttemptStatus.CHANGES_REQUESTED
                for item in snapshot.attempts
                if item.issue_id == cursor.issue_id
            ),
        )
        next_number = next_rework_attempt_number(
            self._workflow.retry_policy,
            current_attempt_number=current_number,
            changes_requested_attempts=changes_requested_attempts,
        )
        if next_number is None:
            owned_paths = _capture_issue_owned_paths(snapshot, cursor.issue_id)
            blocked = replace(
                snapshot,
                run_status=WorkflowRunStatus.PAUSED,
                step_status=StepRunStatus.BLOCKED,
                outcome=StepOutcome.BLOCKED,
                issues=_replace_issue_status_by_id(
                    snapshot.issues,
                    cursor.issue_id,
                    IssueStatus.BLOCKED,
                    owned_paths=owned_paths,
                ),
            )
            blocked = self._store.record(blocked, RunEventType.ISSUE_RETRY_LIMIT_REACHED)
            self._store.release_lease(blocked)
            raise ReworkLimitReachedError("The configured Issue rework limit was reached.")
        rework = _latest_rework_request(snapshot)
        issue = next(
            (item for item in package.issues if item.issue_id == cursor.issue_id),
            None,
        )
        if issue is None:
            raise WorkspaceDevelopmentError("The rework Issue is not in the accepted package.")
        return self._prepare_issue_attempt(
            snapshot,
            package,
            issue,
            AttemptId(f"attempt-{next_number:03}"),
            rework_request=rework,
        )

    def retry_blocked_issue(
        self,
        run_id: WorkflowRunId,
        issue_id: IssueId,
    ) -> DevelopmentPrepared:
        return self._prepare_authorized_attempt(
            run_id,
            issue_id,
            expected_status=IssueStatus.BLOCKED,
            event_type=RunEventType.BLOCKED_RETRY_AUTHORIZED,
            authorization="BLOCKED_RETRY",
        )

    def reset_failed_issue(
        self,
        run_id: WorkflowRunId,
        issue_id: IssueId,
    ) -> DevelopmentPrepared:
        return self._prepare_authorized_attempt(
            run_id,
            issue_id,
            expected_status=IssueStatus.FAILED,
            event_type=RunEventType.FAILED_RESET_AUTHORIZED,
            authorization="FAILED_RESET",
        )

    def _prepare_authorized_attempt(
        self,
        run_id: WorkflowRunId,
        issue_id: IssueId,
        *,
        expected_status: IssueStatus,
        event_type: RunEventType,
        authorization: str,
    ) -> DevelopmentPrepared:
        snapshot = self._ensure_lease(self._store.load(run_id))
        package = self._validate_preflight(snapshot)
        if snapshot.run_status is not WorkflowRunStatus.PAUSED:
            raise WorkspaceDevelopmentError(
                "Issue retry or reset requires an explicitly paused Workflow Run."
            )
        state = _issue_state(snapshot.issues, issue_id)
        if state.status is not expected_status:
            raise WorkspaceDevelopmentError(
                f"Issue {issue_id.value} is not {expected_status.value}."
            )
        issue = next((item for item in package.issues if item.issue_id == issue_id), None)
        if issue is None:
            raise WorkspaceDevelopmentError("Authorized Issue is not in the accepted package.")
        current_number = _latest_attempt_number(snapshot, issue_id)
        rebased = _rebase_issue_repository_baseline(snapshot, state)
        states = tuple(
            replace(item, repository_baseline=rebased)
            if item.issue_id == issue_id
            else item
            for item in snapshot.issues
        )
        rework_request = _latest_issue_rework_request(snapshot, issue_id)
        return self._prepare_issue_attempt(
            replace(snapshot, issues=states),
            package,
            issue,
            AttemptId(f"attempt-{current_number + 1:03}"),
            rework_request=rework_request,
            authorization={
                "kind": authorization,
                "from_attempt": current_number,
                "outcome": snapshot.outcome.value if snapshot.outcome is not None else None,
                "step": state.current_step.value if state.current_step is not None else None,
            },
            event_type=event_type,
        )

    def develop(
        self,
        run_id: WorkflowRunId,
        *,
        on_activity: Callable[[str], None] | None = None,
        on_approval: Callable[[AppServerApprovalRequest], str | None] | None = None,
    ) -> DevelopmentCompleted | DevelopmentBlocked:
        snapshot = self._ensure_lease(self._store.load(run_id))
        package = self._validate_preflight(snapshot)
        return self._run_development(
            snapshot,
            package,
            on_activity=on_activity,
            on_approval=on_approval,
        )

    def resume_development(
        self,
        run_id: WorkflowRunId,
        *,
        on_activity: Callable[[str], None] | None = None,
        on_approval: Callable[[AppServerApprovalRequest], str | None] | None = None,
    ) -> DevelopmentCompleted | DevelopmentBlocked:
        snapshot = self._store.load(run_id)
        if snapshot.operation.status is OperationStatus.UNKNOWN:
            raise WorkspaceDevelopmentError(
                "An unknown operation requires an explicit transcript-free Recovery Attempt."
            )
        snapshot = self._store.take_lease(snapshot)
        package = self._validate_preflight(snapshot)
        cursor = snapshot.development
        workspace = snapshot.workspace
        if cursor is None or workspace is None or cursor.thread_id is None:
            raise WorkspaceDevelopmentError("Development cursor cannot resume its real thread.")
        self._validate_workspace(workspace)
        self._validate_workspace_state(snapshot)
        issue = next(
            (item for item in package.issues if item.issue_id == cursor.issue_id),
            None,
        )
        if issue is None:
            raise WorkspaceDevelopmentError("Checkpointed Issue is not in the accepted package.")
        resumed = replace(snapshot, run_status=WorkflowRunStatus.RUNNING)
        current = self._store.record(resumed, RunEventType.RUN_RESUMED)

        def item_started(item_id: str) -> None:
            nonlocal current
            current = replace(
                current,
                operation=OperationState(item_id, OperationStatus.RUNNING),
            )
            current = self._store.record(current, RunEventType.OPERATION_STARTED)

        def item_completed(item_id: str) -> None:
            nonlocal current
            development = current.development
            if development is None:
                raise WorkspaceDevelopmentError("Development cursor disappeared.")
            completed = tuple(dict.fromkeys((*development.completed_item_ids, item_id)))
            current = replace(
                current,
                development=replace(development, completed_item_ids=completed),
                operation=OperationState(),
                workspace_state_hash=capture_repository_state_hash(Path(workspace.path)),
            )
            current = self._store.record(current, RunEventType.OPERATION_COMPLETED)

        recovered: DevelopmentAgentOutput | None = None
        try:
            if cursor.turn_id is not None:
                recovered = self._development_runner.recover_completed_turn(
                    workspace=Path(workspace.path),
                    thread_id=cursor.thread_id,
                    turn_id=cursor.turn_id,
                    criterion_ids=issue.acceptance_criterion_ids,
                    on_item_started=item_started,
                    on_item_completed=item_completed,
                )
        except Exception:
            paused = replace(current, run_status=WorkflowRunStatus.PAUSED)
            paused = self._store.record(paused, RunEventType.RUN_PAUSED)
            self._store.release_lease(paused)
            raise
        if recovered is not None:
            return self._finalize_development(
                current,
                issue,
                workspace,
                recovered,
            )
        self._development_runner.validate_resume(Path(workspace.path), cursor.thread_id)
        return self._run_development(
            current,
            package,
            on_activity=on_activity,
            on_approval=on_approval,
        )

    def _run_development(
        self,
        snapshot: WorkflowRunSnapshot,
        package: PlanningPackage,
        *,
        on_activity: Callable[[str], None] | None,
        on_approval: Callable[[AppServerApprovalRequest], str | None] | None,
    ) -> DevelopmentCompleted | DevelopmentBlocked:
        cursor = snapshot.development
        workspace = snapshot.workspace
        if cursor is None or workspace is None:
            raise WorkspaceDevelopmentError("Workspace and development cursor are required.")
        self._validate_workspace(workspace)
        self._validate_workspace_state(snapshot)
        issue = next(
            (item for item in package.issues if item.issue_id == cursor.issue_id),
            None,
        )
        if issue is None:
            raise WorkspaceDevelopmentError(
                "The checkpointed Issue is not in the accepted package."
            )
        context = self._store.load_json_artifact(snapshot.run_id, cursor.context_manifest)
        current = snapshot
        attempt_key = f"{cursor.issue_id.value}:{cursor.attempt_id.value}"
        current = self._telemetry.record(
            current,
            DEVELOPMENT_COMPONENT_ID.value,
            attempt_key,
            ExecutionPhase.CONTEXT_LOADED,
        )

        def phase(value: ExecutionPhase, *, applicable: bool = True) -> None:
            nonlocal current
            current = self._telemetry.record(
                current,
                DEVELOPMENT_COMPONENT_ID.value,
                attempt_key,
                value,
                applicable=applicable,
            )

        def activity(delta: str) -> None:
            phase(ExecutionPhase.FIRST_ACTIVITY)
            if on_activity is not None:
                on_activity(delta)

        def thread_bound(thread_id: ExecutionThreadId) -> None:
            nonlocal current
            development = current.development
            if thread_id in archived_execution_threads(current.attempts):
                raise WorkspaceDevelopmentError(
                    "Development reused an archived Execution Thread."
                )
            if development is None or development.thread_id == thread_id:
                return
            current = replace(current, development=replace(development, thread_id=thread_id))
            current = self._store.record(current, RunEventType.DEVELOPMENT_THREAD_BOUND)

        def turn_started(turn_id: ExecutionTurnId) -> None:
            nonlocal current
            development = current.development
            if development is None:
                raise WorkspaceDevelopmentError("Development cursor disappeared.")
            current = replace(current, development=replace(development, turn_id=turn_id))
            current = self._store.record(current, RunEventType.DEVELOPMENT_TURN_STARTED)

        def item_started(item_id: str) -> None:
            nonlocal current
            phase(ExecutionPhase.FIRST_ACTIVITY)
            current = replace(
                current,
                operation=OperationState(item_id, OperationStatus.RUNNING),
            )
            current = self._store.record(current, RunEventType.OPERATION_STARTED)

        def item_completed(item_id: str) -> None:
            nonlocal current
            development = current.development
            if development is None:
                raise WorkspaceDevelopmentError("Development cursor disappeared.")
            completed = tuple(dict.fromkeys((*development.completed_item_ids, item_id)))
            current = replace(
                current,
                development=replace(development, completed_item_ids=completed),
                operation=OperationState(),
                workspace_state_hash=capture_repository_state_hash(Path(workspace.path)),
            )
            current = self._store.record(current, RunEventType.OPERATION_COMPLETED)

        def file_changed(_item_id: str) -> None:
            phase(ExecutionPhase.FIRST_ACTIVITY)
            phase(ExecutionPhase.FIRST_FILE_CHANGE)

        def retry_scheduled(attempt: int, delay: float) -> None:
            nonlocal current
            development = current.development
            if development is None:
                raise WorkspaceDevelopmentError("Development cursor disappeared.")
            current = replace(
                current,
                development=replace(development, transient_retries=attempt),
            )
            current = self._store.record(
                current,
                RunEventType.TRANSIENT_BACKEND_RETRY_SCHEDULED,
            )
            if on_activity is not None:
                on_activity(
                    f"Retrying transient development backend failure "
                    f"({attempt}) after {delay:.2f}s."
                )

        def approval_handler(request: AppServerApprovalRequest) -> str | None:
            nonlocal current
            manifest_policy = self._development_manifest.approval_policy
            if manifest_policy is None:
                raise WorkspaceDevelopmentError("Development approval policy is missing.")
            policy = locked_approval_policy(
                current.approval_policies,
                DEVELOPMENT_COMPONENT_ID.value,
                manifest_policy,
            )
            classified_request, classification = classify_backend_approval(
                request,
                Path(workspace.path),
                policy,
            )
            if on_approval is None:
                return None
            decision = on_approval(classified_request)
            if decision is not None:
                current = persist_approval_decision(
                    self._store,
                    current,
                    component_id=DEVELOPMENT_COMPONENT_ID.value,
                    issue_id=cursor.issue_id.value,
                    attempt_id=cursor.attempt_id.value,
                    request=classified_request,
                    classification=classification,
                    selected_decision=decision,
                )
            return decision

        def execute(recover: bool) -> DevelopmentAgentOutput:
            active = current.development
            if active is None:
                raise WorkspaceDevelopmentError("Development cursor disappeared.")
            if recover and active.thread_id is not None and active.turn_id is not None:
                try:
                    return self._development_runner.recover_completed_turn(
                        workspace=Path(workspace.path),
                        thread_id=active.thread_id,
                        turn_id=active.turn_id,
                        criterion_ids=issue.acceptance_criterion_ids,
                    )
                except (AppServerTransientError, DevelopmentComponentError):
                    # A terminal transport failure cannot yield structured output. Start a
                    # replacement turn on the same thread within this bounded retry attempt.
                    pass
            return self._development_runner.run_turn(
                workspace=Path(workspace.path),
                context_manifest=context,
                criterion_ids=issue.acceptance_criterion_ids,
                thread_id=active.thread_id,
                on_thread_bound=thread_bound,
                on_turn_started=turn_started,
                on_item_started=item_started,
                on_item_completed=item_completed,
                on_file_change=file_changed,
                on_activity=activity,
                pause_requested=lambda: self._pause_requested(snapshot.run_id),
                interrupt_requested=lambda: self._interrupt_requested(snapshot.run_id),
                on_approval=approval_handler,
                execution_profile=locked_execution_profile(
                    current.execution_profiles,
                    DEVELOPMENT_COMPONENT_ID.value,
                    self._development_manifest.execution_profiles[0],
                ),
            )

        try:
            output = run_with_transient_retries(
                execute,
                self._workflow.retry_policy,
                retries_used=cursor.transient_retries,
                on_retry=retry_scheduled,
            )
            phase(ExecutionPhase.FIRST_ACTIVITY)
            phase(ExecutionPhase.FIRST_FILE_CHANGE, applicable=False)
            phase(
                ExecutionPhase.VERIFICATION_STARTED,
                applicable=bool(output.commands),
            )
            phase(ExecutionPhase.STRUCTURED_OUTPUT)
        except DevelopmentTurnStalled as error:
            development = current.development
            if development is None:
                raise WorkspaceDevelopmentError("Development cursor disappeared.") from error
            completed = tuple(
                dict.fromkeys((*development.completed_item_ids, *error.completed_item_ids))
            )
            stalled = replace(
                current,
                run_status=WorkflowRunStatus.PAUSED,
                step_status=StepRunStatus.RUNNING,
                development=replace(
                    development,
                    thread_id=error.thread_id,
                    turn_id=error.turn_id,
                    completed_item_ids=completed,
                ),
                operation=OperationState(),
                workspace_state_hash=capture_repository_state_hash(Path(workspace.path)),
            )
            stalled = self._store.record(stalled, RunEventType.EXECUTION_STALLED)
            self._store.release_lease(stalled)
            self._clear_stop_requests(snapshot.run_id)
            raise DevelopmentPaused(stalled) from error
        except DevelopmentTurnPaused as error:
            development = current.development
            if development is None:
                raise WorkspaceDevelopmentError("Development cursor disappeared.") from error
            operation = current.operation
            if operation.status is OperationStatus.RUNNING:
                operation = replace(operation, status=OperationStatus.UNKNOWN)
            paused = replace(
                current,
                run_status=WorkflowRunStatus.PAUSED,
                development=replace(development, turn_id=None),
                operation=operation,
            )
            paused = self._store.record(paused, RunEventType.RUN_PAUSED)
            self._store.release_lease(paused)
            self._clear_stop_requests(snapshot.run_id)
            raise DevelopmentPaused(paused) from error
        except DevelopmentTurnInterrupted as error:
            development = current.development
            if development is None:
                raise WorkspaceDevelopmentError("Development cursor disappeared.") from error
            operation = current.operation
            if operation.status is OperationStatus.RUNNING:
                operation = replace(operation, status=OperationStatus.UNKNOWN)
            interrupted = replace(
                current,
                run_status=WorkflowRunStatus.AWAITING_USER,
                step_status=StepRunStatus.AWAITING_USER,
                development=replace(development, turn_id=None),
                operation=operation,
            )
            interrupted = self._store.record(
                interrupted,
                RunEventType.DEVELOPMENT_TURN_INTERRUPTED,
            )
            self._store.release_lease(interrupted)
            self._clear_stop_requests(snapshot.run_id)
            raise DevelopmentInterrupted(interrupted) from error
        except AppServerApprovalRequired as error:
            self._clear_stop_requests(snapshot.run_id)
            approval = self._save_approval_request(current, error)
            development = current.development
            if development is None:
                raise WorkspaceDevelopmentError("Development cursor disappeared.") from error
            paused = replace(
                current,
                run_status=WorkflowRunStatus.PAUSED,
                development=replace(development, approval_request=approval),
            )
            paused = self._store.record(paused, RunEventType.DEVELOPMENT_APPROVAL_REQUIRED)
            self._store.release_lease(paused)
            raise WorkspaceDevelopmentError(
                "Development paused for an explicit Codex approval decision."
            ) from error
        except Exception as error:
            self._clear_stop_requests(snapshot.run_id)
            self._fail_attempt(current)
            raise WorkspaceDevelopmentError(
                "The real development turn failed; reset the Issue before retrying."
            ) from error
        self._clear_stop_requests(snapshot.run_id)
        completed = self._finalize_development(current, issue, workspace, output)
        completed_snapshot = self._telemetry.record(
            completed.snapshot,
            DEVELOPMENT_COMPONENT_ID.value,
            attempt_key,
            ExecutionPhase.COMPLETED,
        )
        return replace(completed, snapshot=completed_snapshot)

    def _finalize_development(
        self,
        current: WorkflowRunSnapshot,
        issue: PlannedIssue,
        workspace: WorkspaceRef,
        output: DevelopmentAgentOutput,
    ) -> DevelopmentCompleted | DevelopmentBlocked:
        if output.outcome is StepOutcome.BLOCKED:
            if output.blocked_reason is None:
                raise WorkspaceDevelopmentError("Blocked development requires a reason.")
            return self._block_development(current, issue, output)
        if output.outcome is not StepOutcome.SUCCEEDED:
            raise WorkspaceDevelopmentError("Development returned an unsupported outcome.")
        try:
            result = self._build_result(current, workspace, output)
            if any(
                item.status is not CriterionImplementationStatus.IMPLEMENTED
                for item in result.criteria
            ):
                raise ValueError("Development did not implement every criterion.")
            validate_rework_resolutions(
                self._expected_rework_ids(current),
                result.rework_resolutions,
            )
        except Exception as error:
            self._fail_attempt(current)
            raise WorkspaceDevelopmentError(
                "Development output failed safe inspection; reset the failed Issue."
            ) from error
        artifact = self._save_implementation_result(current, result)
        development = current.development
        if development is None:
            raise WorkspaceDevelopmentError("Development cursor disappeared.")
        states = _replace_issue_status(
            current.issues,
            issue,
            IssueStatus.IN_REVIEW,
            current_step=self._workflow.transition_target(
                DEVELOPMENT_STEP_ID,
                StepOutcome.SUCCEEDED,
            ),
        )
        completed = replace(
            current,
            active_step=self._workflow.required_transition_target(
                DEVELOPMENT_STEP_ID,
                StepOutcome.SUCCEEDED,
            ),
            step_status=StepRunStatus.NOT_STARTED,
            run_status=WorkflowRunStatus.RUNNING,
            issues=states,
            development=replace(
                development,
                thread_id=output.thread_id,
                turn_id=output.turn_id,
                completed_item_ids=output.completed_item_ids,
                implementation_result=artifact,
            ),
            review=None,
            qa=None,
            workspace_state_hash=result.repository_state_hash,
        )
        completed = self._store.record(completed, RunEventType.DEVELOPMENT_SUCCEEDED)
        return DevelopmentCompleted(completed, result)

    def _block_development(
        self,
        snapshot: WorkflowRunSnapshot,
        issue: PlannedIssue,
        output: DevelopmentAgentOutput,
    ) -> DevelopmentBlocked:
        cursor = snapshot.development
        if cursor is None or output.blocked_reason is None:
            raise WorkspaceDevelopmentError("Blocked development lost its typed outcome.")
        owned_paths = _capture_issue_owned_paths(snapshot, cursor.issue_id)
        record = IssueAttemptRecord(
            cursor.issue_id,
            _attempt_number(cursor.attempt_id),
            AttemptStatus.BLOCKED,
            StepOutcome.BLOCKED,
            None,
            None,
            None,
            None,
            output.thread_id,
        )
        blocked = replace(
            snapshot,
            run_status=WorkflowRunStatus.PAUSED,
            step_status=StepRunStatus.BLOCKED,
            outcome=StepOutcome.BLOCKED,
            issues=_replace_issue_status(
                snapshot.issues,
                issue,
                IssueStatus.BLOCKED,
                current_step=DEVELOPMENT_STEP_ID,
                owned_paths=owned_paths,
            ),
            development=replace(
                cursor,
                thread_id=output.thread_id,
                turn_id=output.turn_id,
                completed_item_ids=output.completed_item_ids,
            ),
            attempts=(*snapshot.attempts, record),
        )
        blocked = self._store.record(blocked, RunEventType.DEVELOPMENT_BLOCKED)
        self._store.release_lease(blocked)
        return DevelopmentBlocked(
            blocked,
            redact_diagnostic(output.blocked_reason, limit=4000),
            redact_diagnostic(output.summary, limit=8000),
        )

    def _expected_rework_ids(self, snapshot: WorkflowRunSnapshot) -> tuple[str, ...]:
        cursor = snapshot.development
        if cursor is None:
            raise WorkspaceDevelopmentError("Development cursor disappeared.")
        context = self._store.load_json_artifact(snapshot.run_id, cursor.context_manifest)
        value = context.get("rework_request")
        if value is None:
            return ()
        if not isinstance(value, dict) or value.get("schema") != REWORK_REQUEST_SCHEMA:
            raise WorkspaceDevelopmentError("Context contains an invalid Rework Request.")
        items = value.get("items")
        if not isinstance(items, list):
            raise WorkspaceDevelopmentError("Rework Request items are invalid.")
        result: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                raise WorkspaceDevelopmentError("Rework Request item is invalid.")
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise WorkspaceDevelopmentError("Rework Request item ID is invalid.")
            result.append(item_id)
        return tuple(result)

    def _validate_preflight(self, snapshot: WorkflowRunSnapshot) -> PlanningPackage:
        if snapshot.planning_package is None:
            raise WorkspaceDevelopmentError("An accepted PRD Package is required.")
        if snapshot.workflow.definition_hash != self._workflow.definition_hash:
            raise WorkspaceDevelopmentError("The locked Workflow Definition has changed.")
        expected_locks = tuple(
            ComponentLock(
                manifest.component_id,
                manifest.version,
                manifest.distribution,
                manifest.package_hash,
            )
            for manifest in self._registry.manifests
        )
        if snapshot.component_locks != expected_locks:
            raise WorkspaceDevelopmentError("A locked Workflow component changed or is missing.")
        for manifest in self._registry.manifests:
            validate_component_ports(
                self._workflow.step(StepInstanceId(manifest.component_id.value)),
                manifest,
            )
        if not capabilities_for(
            snapshot.capability_profiles,
            StepComponentId("development"),
            fallback=DEVELOPMENT_CAPABILITIES.capabilities,
        ):
            raise WorkspaceDevelopmentError("Development capability profile is empty.")
        try:
            return load_planning_package(
                self._config.repository,
                snapshot.planning_package,
                snapshot.run_id,
            )
        except PlanningPackageError as error:
            raise WorkspaceDevelopmentError(str(error)) from error

    def _validate_workspace(self, workspace: WorkspaceRef) -> None:
        path = Path(workspace.path)
        configured_repository = repository_root(self._config.repository)
        recorded_repository = Path(workspace.repository_root).resolve()
        if (
            repository_root(recorded_repository) != recorded_repository
            or recorded_repository != configured_repository
        ):
            raise WorkspaceDevelopmentError(
                "Workspace repository identity differs from the selected repository."
            )
        if repository_root(path) != path.resolve():
            raise WorkspaceDevelopmentError("Workspace is not a Git worktree root.")
        if current_branch(path) != workspace.branch:
            raise WorkspaceDevelopmentError(
                "Workspace branch does not match the selected Workspace Ref."
            )
        if head_commit(path) != workspace.base_commit:
            raise WorkspaceDevelopmentError(
                "Workspace HEAD does not match the selected base commit."
            )
        permission_profile = workspace.permission_profile
        if permission_profile is not None and (
            not permission_profile.ready
            or Path(permission_profile.canonical_root) != path.resolve()
        ):
            raise WorkspaceDevelopmentError(
                "Workspace session permission profile is invalid or no longer ready."
            )

    def _validate_workspace_state(self, snapshot: WorkflowRunSnapshot) -> None:
        workspace = snapshot.workspace
        if workspace is None or snapshot.workspace_state_hash is None:
            raise WorkspaceDevelopmentError("Workspace source state checkpoint is missing.")
        if (
            capture_repository_state_hash(Path(workspace.path))
            != snapshot.workspace_state_hash
        ):
            raise WorkspaceDevelopmentError(
                "Workspace source state differs from the active development checkpoint."
            )

    def _save_context_manifest(
        self,
        snapshot: WorkflowRunSnapshot,
        package: PlanningPackage,
        issue: PlannedIssue,
        attempt_id: AttemptId,
        rework_request: ArtifactRef | None,
        authorization: dict[str, object] | None = None,
    ) -> ContextManifestRef:
        workspace = snapshot.workspace
        if workspace is None:
            raise WorkspaceDevelopmentError("Workspace Ref is required for context selection.")
        payload: dict[str, object] = {
            "schema": CONTEXT_MANIFEST_SCHEMA,
            "run_id": snapshot.run_id.value,
            "attempt": {
                "id": attempt_id.value,
                "authorization": authorization,
            },
            "issue": {
                "id": issue.issue_id.value,
                "position": issue.position,
                "total": len(package.issues),
                "markdown": issue.markdown,
                "requirements": [item.value for item in issue.requirement_ids],
                "acceptance_criteria": [
                    item.value for item in issue.acceptance_criterion_ids
                ],
            },
            "prd_sections": _relevant_prd(package.prd_markdown, issue),
            "repository_constraints": _repository_constraints(Path(workspace.path)),
            "capability_profile": [
                item.value
                for item in capabilities_for(
                    snapshot.capability_profiles,
                    StepComponentId("development"),
                    fallback=DEVELOPMENT_CAPABILITIES.capabilities,
                )
            ],
            "workspace": {
                "kind": workspace.kind.value,
                "path": workspace.path,
                "branch": workspace.branch,
                "base_commit": workspace.base_commit,
                "requires_windows_acl_handoff": bool(
                    workspace.permission_profile is not None
                    and workspace.permission_profile.requires_windows_acl_handoff
                ),
            },
            "rework_request": None
            if rework_request is None
            else self._store.load_json_artifact(snapshot.run_id, rework_request),
        }
        relative = (
            Path(CONTEXT_MANIFESTS_DIRECTORY)
            / f"{issue.issue_id.value}-{attempt_id.value}.json"
        )
        artifact = self._store.save_json_artifact(snapshot.run_id, relative, payload)
        return ContextManifestRef(artifact.path, artifact.content_hash)

    def _build_result(
        self,
        snapshot: WorkflowRunSnapshot,
        workspace: WorkspaceRef,
        output: DevelopmentAgentOutput,
    ) -> ImplementationResult:
        cursor = snapshot.development
        if cursor is None:
            raise WorkspaceDevelopmentError("Development cursor is required.")
        changes = capture_worktree_changes(
            Path(workspace.path),
            workspace.base_commit,
            _issue_repository_baseline(snapshot.issues, cursor.issue_id),
        )
        if not changes.changed_files:
            raise WorkspaceDevelopmentError("Development completed without repository changes.")
        return ImplementationResult(
            IMPLEMENTATION_RESULT_SCHEMA,
            cursor.attempt_id,
            changes.base_state,
            changes.result_state,
            changes.diff_hash,
            changes.repository_state_hash,
            changes.changed_files,
            tuple(
                replace(item, evidence=redact_diagnostic(item.evidence, limit=4000))
                for item in output.criteria
            ),
            tuple(redact_diagnostic(item, limit=2000) for item in output.commands),
            tuple(
                replace(item, evidence=redact_diagnostic(item.evidence, limit=4000))
                for item in output.rework_resolutions
            ),
            tuple(redact_diagnostic(item, limit=4000) for item in output.assumptions),
            tuple(redact_diagnostic(item, limit=4000) for item in output.risks),
            redact_diagnostic(output.summary, limit=8000),
        )

    def _save_implementation_result(
        self,
        snapshot: WorkflowRunSnapshot,
        result: ImplementationResult,
    ) -> ArtifactRef:
        payload = implementation_result_to_dict(result)
        cursor = snapshot.development
        if cursor is None:
            raise WorkspaceDevelopmentError("Development cursor is required for result storage.")
        relative = (
            Path(IMPLEMENTATION_RESULTS_DIRECTORY)
            / f"{cursor.issue_id.value}-{result.attempt_id.value}.json"
        )
        return self._store.save_json_artifact(snapshot.run_id, relative, payload)

    def _prepare_issue_attempt(
        self,
        snapshot: WorkflowRunSnapshot,
        package: PlanningPackage,
        issue: PlannedIssue,
        attempt_id: AttemptId,
        *,
        rework_request: ArtifactRef | None,
        authorization: dict[str, object] | None = None,
        event_type: RunEventType = RunEventType.ISSUE_ATTEMPT_STARTED,
    ) -> DevelopmentPrepared:
        baseline = _issue_repository_baseline_or_none(snapshot.issues, issue.issue_id)
        if baseline is None:
            workspace = snapshot.workspace
            if workspace is None:
                raise WorkspaceDevelopmentError(
                    "Workspace Ref is required for Issue baseline capture."
                )
            baseline = capture_workspace_baseline(Path(workspace.path))
        states = _replace_issue_status(
            snapshot.issues,
            issue,
            IssueStatus.IN_DEVELOPMENT,
            current_step=DEVELOPMENT_STEP_ID,
            repository_baseline=baseline,
        )
        prepared = replace(
            snapshot,
            issues=states,
            active_step=self._workflow.step_for_component(DEVELOPMENT_COMPONENT_ID).step_id,
            run_status=WorkflowRunStatus.RUNNING,
            step_status=StepRunStatus.RUNNING,
            outcome=None,
            review=None,
            qa=None,
            workspace_state_hash=capture_repository_state_hash(
                Path(snapshot.workspace.path)
                if snapshot.workspace is not None
                else self._config.repository
            ),
        )
        context_ref = self._save_context_manifest(
            prepared,
            package,
            issue,
            attempt_id,
            rework_request=rework_request,
            authorization=authorization,
        )
        prepared = replace(
            prepared,
            development=DevelopmentCursor(
                issue.issue_id,
                issue.position,
                len(package.issues),
                attempt_id,
                context_ref,
            ),
        )
        prepared = self._store.record(prepared, event_type)
        return DevelopmentPrepared(prepared, issue)

    def _save_approval_request(
        self,
        snapshot: WorkflowRunSnapshot,
        error: AppServerApprovalRequired,
    ) -> ArtifactRef:
        cursor = snapshot.development
        if cursor is None:
            raise WorkspaceDevelopmentError(
                "Development approval requires an active attempt."
            )
        request = error.request
        workspace = snapshot.workspace
        manifest_policy = self._development_manifest.approval_policy
        if workspace is None or manifest_policy is None:
            raise WorkspaceDevelopmentError("Development approval policy context is missing.")
        policy = locked_approval_policy(
            snapshot.approval_policies,
            DEVELOPMENT_COMPONENT_ID.value,
            manifest_policy,
        )
        classified_request, classification = classify_backend_approval(
            request,
            Path(workspace.path),
            policy,
        )
        payload = {
            "schema": "devloop.approval-request/v2",
            "step_id": DEVELOPMENT_STEP_ID.value,
            "issue_id": cursor.issue_id.value,
            "attempt_id": cursor.attempt_id.value,
            "request_id": request.request_id,
            "kind": request.kind.value,
            "method": request.method,
            "parsed_action": classified_request.action,
            "command_family": classification.family.value,
            "workspace_boundary": classification.boundary.value,
            "classification": classification.classification.value,
            "policy_version": policy.version,
            "policy_hash": policy.policy_hash,
            "policy_reason": classification.reason,
            "command_hash": classification.command_hash,
            "reason": None
            if request.reason is None
            else redact_diagnostic(request.reason, limit=4000),
            "supported_decisions": list(classified_request.supported_decisions),
            "decision": None,
            "thread_id": request.thread_id,
            "turn_id": request.turn_id,
            "item_id": request.item_id,
        }
        request_token = _approval_request_artifact_token(request.request_id)
        relative = Path("approvals") / f"{cursor.attempt_id.value}-{request_token}.json"
        return self._store.save_json_artifact(snapshot.run_id, relative, payload)

    def _ensure_lease(self, snapshot: WorkflowRunSnapshot) -> WorkflowRunSnapshot:
        try:
            self._store.validate_lease(snapshot)
            return snapshot
        except (RunStoreError, OSError, ValueError):
            return self._store.take_lease(snapshot)

    def _pause_requested(self, run_id: WorkflowRunId) -> bool:
        with self._pause_lock:
            return run_id in self._pause_requests

    def _interrupt_requested(self, run_id: WorkflowRunId) -> bool:
        with self._pause_lock:
            return run_id in self._interrupt_requests

    def _clear_stop_requests(self, run_id: WorkflowRunId) -> None:
        with self._pause_lock:
            self._pause_requests.discard(run_id)
            self._interrupt_requests.discard(run_id)

    def _fail_attempt(self, snapshot: WorkflowRunSnapshot) -> None:
        cursor = snapshot.development
        if cursor is None:
            raise WorkspaceDevelopmentError("A development cursor is required for failure.")
        owned_paths = _capture_issue_owned_paths(snapshot, cursor.issue_id)
        record = IssueAttemptRecord(
            cursor.issue_id,
            _attempt_number(cursor.attempt_id),
            AttemptStatus.FAILED,
            StepOutcome.FAILED,
            cursor.implementation_result,
            None,
            None,
            None,
            cursor.thread_id,
        )
        failed = replace(
            snapshot,
            run_status=WorkflowRunStatus.PAUSED,
            step_status=StepRunStatus.FAILED,
            outcome=StepOutcome.FAILED,
            issues=_replace_issue_status_by_id(
                snapshot.issues,
                cursor.issue_id,
                IssueStatus.FAILED,
                owned_paths=owned_paths,
            ),
            attempts=(*snapshot.attempts, record),
        )
        failed = self._store.record(failed, RunEventType.DEVELOPMENT_FAILED)
        self._store.release_lease(failed)


def implementation_result_to_dict(result: ImplementationResult) -> dict[str, object]:
    return {
        "schema": result.schema,
        "attempt_id": result.attempt_id.value,
        "base_state": result.base_state,
        "result_state": result.result_state,
        "diff_hash": result.diff_hash,
        "repository_state_hash": result.repository_state_hash,
        "changed_files": [
            {"path": item.path, "kind": item.kind.value} for item in result.changed_files
        ],
        "criteria": [
            {"id": item.criterion_id.value, "status": item.status.value, "evidence": item.evidence}
            for item in result.criteria
        ],
        "commands": list(result.commands),
        "rework_resolutions": [
            {"id": item.rework_id, "status": item.status.value, "evidence": item.evidence}
            for item in result.rework_resolutions
        ],
        "assumptions": list(result.assumptions),
        "risks": list(result.risks),
        "summary": result.summary,
    }


def _replace_issue_status(
    states: tuple[IssueRuntimeState, ...],
    issue: PlannedIssue,
    status: IssueStatus,
    *,
    current_step: StepInstanceId | None = None,
    repository_baseline: tuple[WorkspaceBaselineEntry, ...] | None = None,
    owned_paths: tuple[str, ...] | None = None,
) -> tuple[IssueRuntimeState, ...]:
    return tuple(
        replace(
            item,
            status=status,
            current_step=item.current_step if current_step is None else current_step,
            repository_baseline=item.repository_baseline
            if repository_baseline is None
            else repository_baseline,
            owned_paths=item.owned_paths if owned_paths is None else owned_paths,
        )
        if item.issue_id == issue.issue_id
        else item
        for item in states
    )


def _replace_issue_status_by_id(
    states: tuple[IssueRuntimeState, ...],
    issue_id: object,
    status: IssueStatus,
    *,
    owned_paths: tuple[str, ...] | None = None,
) -> tuple[IssueRuntimeState, ...]:
    return tuple(
        replace(
            item,
            status=status,
            owned_paths=item.owned_paths if owned_paths is None else owned_paths,
        )
        if item.issue_id == issue_id
        else item
        for item in states
    )


def _issue_repository_baseline_or_none(
    states: tuple[IssueRuntimeState, ...],
    issue_id: object,
) -> tuple[WorkspaceBaselineEntry, ...] | None:
    state = next((item for item in states if item.issue_id == issue_id), None)
    if state is None:
        raise WorkspaceDevelopmentError("The active Issue state is missing.")
    return state.repository_baseline


def _issue_repository_baseline(
    states: tuple[IssueRuntimeState, ...],
    issue_id: object,
) -> tuple[WorkspaceBaselineEntry, ...]:
    baseline = _issue_repository_baseline_or_none(states, issue_id)
    if baseline is None:
        raise WorkspaceDevelopmentError("The active Issue repository baseline is missing.")
    return baseline


def _issue_state(
    states: tuple[IssueRuntimeState, ...],
    issue_id: IssueId,
) -> IssueRuntimeState:
    state = next((item for item in states if item.issue_id == issue_id), None)
    if state is None:
        raise WorkspaceDevelopmentError("The requested Issue state is missing.")
    return state


def _latest_attempt_number(snapshot: WorkflowRunSnapshot, issue_id: IssueId) -> int:
    numbers = [item.attempt_number for item in snapshot.attempts if item.issue_id == issue_id]
    if snapshot.development is not None and snapshot.development.issue_id == issue_id:
        numbers.append(_attempt_number(snapshot.development.attempt_id))
    if not numbers:
        raise WorkspaceDevelopmentError("The requested Issue has no prior attempt.")
    return max(numbers)


def _latest_issue_rework_request(
    snapshot: WorkflowRunSnapshot,
    issue_id: IssueId,
) -> ArtifactRef | None:
    return next(
        (
            item.rework_request
            for item in reversed(snapshot.attempts)
            if item.issue_id == issue_id and item.rework_request is not None
        ),
        None,
    )


def _capture_issue_owned_paths(
    snapshot: WorkflowRunSnapshot,
    issue_id: IssueId,
) -> tuple[str, ...]:
    state = _issue_state(snapshot.issues, issue_id)
    workspace = snapshot.workspace
    if workspace is None or state.repository_baseline is None:
        return state.owned_paths
    try:
        changes = capture_worktree_changes(
            Path(workspace.path),
            workspace.base_commit,
            state.repository_baseline,
        )
    except GitOperationError:
        return state.owned_paths
    return tuple(item.path for item in changes.changed_files)


def _rebase_issue_repository_baseline(
    snapshot: WorkflowRunSnapshot,
    state: IssueRuntimeState,
) -> tuple[WorkspaceBaselineEntry, ...]:
    workspace = snapshot.workspace
    original = state.repository_baseline
    if workspace is None or original is None:
        raise WorkspaceDevelopmentError("Authorized retry requires an Issue repository baseline.")
    current = {item.path: item for item in capture_workspace_baseline(Path(workspace.path))}
    original_by_path = {item.path: item for item in original}
    for path in state.owned_paths:
        prior = original_by_path.get(path)
        if prior is None:
            current.pop(path, None)
        else:
            current[path] = prior
    return tuple(current[path] for path in sorted(current))


def _issue_status(states: tuple[IssueRuntimeState, ...], issue_id: object) -> IssueStatus:
    state = next((item for item in states if item.issue_id == issue_id), None)
    if state is None:
        raise WorkspaceDevelopmentError("The active Issue state is missing.")
    return state.status


def _attempt_number(attempt_id: AttemptId) -> int:
    return int(attempt_id.value.rsplit("-", 1)[1])


def _approval_request_artifact_token(request_id: int | str) -> str:
    if isinstance(request_id, int):
        return str(request_id)
    return hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]


def _latest_rework_request(snapshot: WorkflowRunSnapshot) -> ArtifactRef:
    if snapshot.qa is not None and snapshot.qa.rework_request is not None:
        return snapshot.qa.rework_request
    if snapshot.review is not None and snapshot.review.rework_request is not None:
        return snapshot.review.rework_request
    raise WorkspaceDevelopmentError("A typed Rework Request is required for a new attempt.")


def _relevant_prd(prd_markdown: str, issue: PlannedIssue) -> str:
    requirement_values = {item.value for item in issue.requirement_ids}
    overview = ""
    problem_marker = "<!-- devloop:section:problem -->"
    requirements_marker = "<!-- devloop:section:requirements -->"
    problem_index = prd_markdown.find(problem_marker)
    requirements_index = prd_markdown.find(requirements_marker)
    if 0 <= problem_index < requirements_index:
        overview = prd_markdown[problem_index:requirements_index].strip()[:20_000]
    selected: list[str] = [overview] if overview else []
    for line in prd_markdown.splitlines():
        if any(requirement in line for requirement in requirement_values):
            selected.append(line)
    return "\n".join(selected)[:50_000]


def _repository_constraints(workspace: Path) -> dict[str, object]:
    instructions_path = workspace / "AGENTS.md"
    instructions = ""
    if (
        instructions_path.is_file()
        and instructions_path.stat().st_size <= MAX_REPOSITORY_INSTRUCTIONS_CHARS
    ):
        instructions = instructions_path.read_text(encoding="utf-8", errors="replace")
    return {
        "instructions": instructions,
        "prohibited_operations": [
            "push",
            "merge",
            "pull-request",
            "branch-delete",
            "worktree-remove",
        ],
    }
