from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from devloop.application.approvals import (
    classify_backend_approval,
    persist_approval_decision,
)
from devloop.application.config import ApplicationConfig
from devloop.application.retry import run_with_transient_retries
from devloop.application.telemetry import ExecutionTelemetryRecorder
from devloop.components.builtin import installed_component_registry
from devloop.components.qa import (
    QA_COMPONENT_ID,
    QaAgentOutput,
    QaComponentRunner,
    QaTurnInterrupted,
    QaTurnPaused,
)
from devloop.components.review import (
    CODE_REVIEW_COMPONENT_ID,
    ReviewAgentOutput,
    ReviewComponentRunner,
    ReviewTurnInterrupted,
    ReviewTurnPaused,
)
from devloop.domain.capabilities import capabilities_for
from devloop.domain.approval import locked_approval_policy
from devloop.domain.development import (
    ArtifactRef,
    CapabilityProfile,
    ChangedFile,
    ChangeKind,
    IssueRuntimeState,
    IssueStatus,
    WorkspaceBaselineEntry,
    WorkspaceRef,
)
from devloop.domain.doctor import redact_diagnostic
from devloop.domain.execution import ExecutionPhase, locked_execution_profile
from devloop.domain.identifiers import (
    AttemptId,
    CapabilityId,
    ExecutionThreadId,
    ExecutionTurnId,
    IssueId,
    ReviewFindingId,
    StepComponentId,
    StepInstanceId,
    WorkflowRunId,
)
from devloop.domain.outcomes import StepOutcome
from devloop.domain.planning import PlannedIssue, PlanningPackage
from devloop.domain.review_qa import (
    QA_RESULT_SCHEMA,
    REVIEW_RESULT_SCHEMA,
    REWORK_REQUEST_SCHEMA,
    CheckRequirement,
    FindingDisposition,
    FindingSeverity,
    QaCheckStatus,
    QaCursor,
    QaResult,
    ReviewCursor,
    ReviewFinding,
    ReviewResult,
    ReworkItem,
    ReworkRequest,
    ReworkSource,
    qa_outcome,
    review_outcome,
    validate_qa_result,
    validate_review_result,
)
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
    validate_attempt_record,
)
from devloop.execution.app_server import AppServerApprovalRequest, AppServerApprovalRequired
from devloop.infrastructure.git import (
    GitOperationError,
    WorktreeChanges,
    capture_worktree_changes,
    render_relevant_diff,
)
from devloop.infrastructure.paths import (
    QA_INPUTS_DIRECTORY,
    QA_RESULTS_DIRECTORY,
    REVIEW_INPUTS_DIRECTORY,
    REVIEW_RESULTS_DIRECTORY,
    REWORK_REQUESTS_DIRECTORY,
)
from devloop.persistence.run_store import RunStore, RunStoreError
from devloop.planning.package_reader import PlanningPackageError, load_planning_package
from devloop.workflow.definition import load_standard_workflow, validate_component_ports

CODE_REVIEW_STEP_ID = StepInstanceId("code-review")
QA_STEP_ID = StepInstanceId("qa")
DEVELOPMENT_STEP_ID = StepInstanceId("development")
REVIEW_CAPABILITIES = CapabilityProfile((CapabilityId("review"),))
QA_CAPABILITIES = CapabilityProfile((CapabilityId("qa"),))
MAX_REPOSITORY_INSTRUCTIONS_CHARS = 20_000


class ReviewQaError(RuntimeError):
    pass


class ReviewQaPaused(ReviewQaError):
    def __init__(self, snapshot: WorkflowRunSnapshot) -> None:
        super().__init__("Verification paused after interrupting the active App Server turn.")
        self.snapshot = snapshot


class ReviewQaInterrupted(ReviewQaError):
    def __init__(self, snapshot: WorkflowRunSnapshot) -> None:
        super().__init__("Verification active turn was interrupted without pausing the run.")
        self.snapshot = snapshot


@dataclass(frozen=True)
class ReviewCompleted:
    snapshot: WorkflowRunSnapshot
    result: ReviewResult
    outcome: StepOutcome
    rework_request: ReworkRequest | None


@dataclass(frozen=True)
class QaCompleted:
    snapshot: WorkflowRunSnapshot
    result: QaResult
    outcome: StepOutcome
    rework_request: ReworkRequest | None


@dataclass(frozen=True)
class _ImplementationIdentity:
    attempt_id: AttemptId
    diff_hash: str
    repository_state_hash: str
    changed_files: tuple[ChangedFile, ...]


class ReviewQaService:
    def __init__(self, config: ApplicationConfig) -> None:
        self._config = config
        self._store = RunStore(config.paths.run_root)
        self._workflow = load_standard_workflow()
        self._registry = installed_component_registry()
        review_manifest, review_runner = self._registry.resolve(CODE_REVIEW_COMPONENT_ID)
        qa_manifest, qa_runner = self._registry.resolve(QA_COMPONENT_ID)
        if not isinstance(review_runner, ReviewComponentRunner) or not isinstance(
            qa_runner, QaComponentRunner
        ):
            raise ReviewQaError("Built-in review or QA runner is invalid.")
        validate_component_ports(self._workflow.step(CODE_REVIEW_STEP_ID), review_manifest)
        validate_component_ports(self._workflow.step(QA_STEP_ID), qa_manifest)
        self._review_runner = review_runner
        self._qa_runner = qa_runner
        self._review_manifest = review_manifest
        self._qa_manifest = qa_manifest
        self._telemetry = ExecutionTelemetryRecorder(self._store)
        self._stop_lock = threading.Lock()
        self._pause_requests: set[WorkflowRunId] = set()
        self._interrupt_requests: set[WorkflowRunId] = set()

    def active_issue_id(self, run_id: WorkflowRunId) -> IssueId:
        snapshot = self._store.load(run_id)
        if snapshot.development is None:
            raise ReviewQaError("Workflow Run has no active Issue.")
        return snapshot.development.issue_id

    def request_pause(self, run_id: WorkflowRunId) -> None:
        self._validate_stop_request(run_id, require_turn=False)
        with self._stop_lock:
            self._pause_requests.add(run_id)

    def request_interrupt(self, run_id: WorkflowRunId) -> None:
        self._validate_stop_request(run_id, require_turn=True)
        with self._stop_lock:
            self._interrupt_requests.add(run_id)

    def review(
        self,
        run_id: WorkflowRunId,
        *,
        on_activity: Callable[[str], None] | None = None,
        on_approval: Callable[[AppServerApprovalRequest], str | None] | None = None,
    ) -> ReviewCompleted:
        return self._review(
            run_id,
            recover_completed=False,
            on_activity=on_activity,
            on_approval=on_approval,
        )

    def resume_review(
        self,
        run_id: WorkflowRunId,
        *,
        on_activity: Callable[[str], None] | None = None,
        on_approval: Callable[[AppServerApprovalRequest], str | None] | None = None,
    ) -> ReviewCompleted:
        return self._review(
            run_id,
            recover_completed=True,
            on_activity=on_activity,
            on_approval=on_approval,
        )

    def qa(
        self,
        run_id: WorkflowRunId,
        *,
        on_activity: Callable[[str], None] | None = None,
        on_approval: Callable[[AppServerApprovalRequest], str | None] | None = None,
    ) -> QaCompleted:
        return self._qa(
            run_id,
            recover_completed=False,
            on_activity=on_activity,
            on_approval=on_approval,
        )

    def resume_qa(
        self,
        run_id: WorkflowRunId,
        *,
        on_activity: Callable[[str], None] | None = None,
        on_approval: Callable[[AppServerApprovalRequest], str | None] | None = None,
    ) -> QaCompleted:
        return self._qa(
            run_id,
            recover_completed=True,
            on_activity=on_activity,
            on_approval=on_approval,
        )

    def _review(
        self,
        run_id: WorkflowRunId,
        *,
        recover_completed: bool,
        on_activity: Callable[[str], None] | None,
        on_approval: Callable[[AppServerApprovalRequest], str | None] | None,
    ) -> ReviewCompleted:
        snapshot = self._store.load(run_id)
        if recover_completed and snapshot.operation.status is OperationStatus.UNKNOWN:
            raise ReviewQaError(
                "An unknown operation requires an explicit transcript-free Recovery Attempt."
            )
        snapshot = self._ensure_lease(snapshot)
        package = self._validate_preflight(snapshot)
        issue, workspace, implementation, implementation_payload = self._review_prerequisites(
            snapshot, package
        )
        repository_baseline = _issue_repository_baseline(snapshot, issue.issue_id)
        before = self._verified_changes(workspace, implementation, repository_baseline)
        current = self._ensure_review_input(
            snapshot,
            issue,
            workspace,
            implementation,
            implementation_payload,
            before,
        )
        if recover_completed:
            current = replace(
                current,
                run_status=WorkflowRunStatus.RUNNING,
                step_status=StepRunStatus.RUNNING,
                outcome=None,
            )
            current = self._store.record(current, RunEventType.RUN_RESUMED)
        cursor = current.review
        if cursor is None:
            raise ReviewQaError("Review cursor was not checkpointed.")
        review_input = self._store.load_json_artifact(current.run_id, cursor.input_manifest)
        attempt_key = f"{cursor.issue_id.value}:{cursor.attempt_id.value}"
        current = self._telemetry.record(
            current,
            CODE_REVIEW_COMPONENT_ID.value,
            attempt_key,
            ExecutionPhase.CONTEXT_LOADED,
        )

        def phase(value: ExecutionPhase, *, applicable: bool = True) -> None:
            nonlocal current
            current = self._telemetry.record(
                current,
                CODE_REVIEW_COMPONENT_ID.value,
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
            if thread_id in archived_execution_threads(current.attempts):
                raise ReviewQaError("Review reused an archived Execution Thread.")
            if current.development is not None and current.development.thread_id == thread_id:
                raise ReviewQaError("Review reused the development Execution Thread.")
            value = current.review
            if value is None or value.thread_id == thread_id:
                return
            current = replace(current, review=replace(value, thread_id=thread_id))
            current = self._store.record(current, RunEventType.REVIEW_THREAD_BOUND)

        def turn_started(turn_id: ExecutionTurnId) -> None:
            nonlocal current
            value = current.review
            if value is None:
                raise ReviewQaError("Review cursor disappeared.")
            current = replace(current, review=replace(value, turn_id=turn_id))
            current = self._store.record(current, RunEventType.REVIEW_TURN_STARTED)

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
            value = current.review
            if value is None:
                raise ReviewQaError("Review cursor disappeared.")
            completed = tuple(dict.fromkeys((*value.completed_item_ids, item_id)))
            current = replace(
                current,
                review=replace(value, completed_item_ids=completed),
                operation=OperationState(),
            )
            current = self._store.record(current, RunEventType.OPERATION_COMPLETED)

        def retry_scheduled(attempt: int, delay: float) -> None:
            nonlocal current
            value = current.review
            if value is None:
                raise ReviewQaError("Review cursor disappeared.")
            current = replace(current, review=replace(value, transient_retries=attempt))
            current = self._store.record(
                current,
                RunEventType.TRANSIENT_BACKEND_RETRY_SCHEDULED,
            )
            _report_retry(on_activity, "code review", attempt, delay)

        def approval_handler(request: AppServerApprovalRequest) -> str | None:
            nonlocal current
            manifest_policy = self._review_manifest.approval_policy
            if manifest_policy is None:
                raise ReviewQaError("Code-review approval policy is missing.")
            policy = locked_approval_policy(
                current.approval_policies,
                CODE_REVIEW_COMPONENT_ID.value,
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
                    component_id=CODE_REVIEW_COMPONENT_ID.value,
                    issue_id=issue.issue_id.value,
                    attempt_id=cursor.attempt_id.value,
                    request=classified_request,
                    classification=classification,
                    selected_decision=decision,
                )
            return decision

        def execute(retry_recovery: bool) -> ReviewAgentOutput:
            active = current.review
            if active is None:
                raise ReviewQaError("Review cursor disappeared.")
            if (
                (recover_completed or retry_recovery)
                and active.thread_id is not None
                and active.turn_id is not None
            ):
                return self._review_runner.recover_completed_turn(
                    workspace=Path(workspace.path),
                    thread_id=active.thread_id,
                    turn_id=active.turn_id,
                    on_item_started=item_started,
                    on_item_completed=item_completed,
                )
            return self._review_runner.run_turn(
                workspace=Path(workspace.path),
                review_input=review_input,
                thread_id=active.thread_id,
                on_thread_bound=thread_bound,
                on_turn_started=turn_started,
                on_item_started=item_started,
                on_item_completed=item_completed,
                on_activity=activity,
                pause_requested=lambda: self._pause_requested(run_id),
                interrupt_requested=lambda: self._interrupt_requested(run_id),
                on_approval=approval_handler,
                execution_profile=locked_execution_profile(
                    current.execution_profiles,
                    CODE_REVIEW_COMPONENT_ID.value,
                    self._review_manifest.execution_profiles[0],
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
            phase(ExecutionPhase.VERIFICATION_STARTED)
            phase(ExecutionPhase.STRUCTURED_OUTPUT)
            after = self._verified_changes(workspace, implementation, repository_baseline)
            if after != before:
                raise ReviewQaError("Read-only code review changed the implementation state.")
            result = self._finalize_review(current, issue, workspace, implementation, output)
            completed = self._telemetry.record(
                result.snapshot,
                CODE_REVIEW_COMPONENT_ID.value,
                attempt_key,
                ExecutionPhase.COMPLETED,
            )
            return replace(result, snapshot=completed)
        except ReviewTurnPaused as error:
            paused = self._checkpoint_interrupted_turn(current, pause_run=True)
            raise ReviewQaPaused(paused) from error
        except ReviewTurnInterrupted as error:
            interrupted = self._checkpoint_interrupted_turn(current, pause_run=False)
            raise ReviewQaInterrupted(interrupted) from error
        except AppServerApprovalRequired as error:
            self._pause(current)
            raise ReviewQaError("Code review paused for an explicit approval decision.") from error
        except Exception as error:
            self._fail_attempt(current, issue, RunEventType.REVIEW_FAILED)
            if isinstance(error, ReviewQaError):
                raise
            raise ReviewQaError("The real code-review turn failed; reset the Issue.") from error
        finally:
            self._clear_stop_requests(run_id)

    def _qa(
        self,
        run_id: WorkflowRunId,
        *,
        recover_completed: bool,
        on_activity: Callable[[str], None] | None,
        on_approval: Callable[[AppServerApprovalRequest], str | None] | None,
    ) -> QaCompleted:
        snapshot = self._store.load(run_id)
        if recover_completed and snapshot.operation.status is OperationStatus.UNKNOWN:
            raise ReviewQaError(
                "An unknown operation requires an explicit transcript-free Recovery Attempt."
            )
        snapshot = self._ensure_lease(snapshot)
        package = self._validate_preflight(snapshot)
        issue, workspace, implementation, implementation_payload, review_payload = (
            self._qa_prerequisites(snapshot, package)
        )
        repository_baseline = _issue_repository_baseline(snapshot, issue.issue_id)
        try:
            before = self._verified_changes(workspace, implementation, repository_baseline)
        except (GitOperationError, ReviewQaError) as error:
            self._block_qa(snapshot, issue, str(error))
            raise ReviewQaError(
                "QA changed source-controlled state and was blocked without reverting it."
            ) from error
        current = self._ensure_qa_input(
            snapshot,
            issue,
            workspace,
            implementation,
            implementation_payload,
            review_payload,
            before,
        )
        if recover_completed:
            current = replace(
                current,
                run_status=WorkflowRunStatus.RUNNING,
                step_status=StepRunStatus.RUNNING,
                outcome=None,
            )
            current = self._store.record(current, RunEventType.RUN_RESUMED)
        cursor = current.qa
        if cursor is None:
            raise ReviewQaError("QA cursor was not checkpointed.")
        qa_input = self._store.load_json_artifact(current.run_id, cursor.input_manifest)
        attempt_key = f"{cursor.issue_id.value}:{cursor.attempt_id.value}"
        current = self._telemetry.record(
            current,
            QA_COMPONENT_ID.value,
            attempt_key,
            ExecutionPhase.CONTEXT_LOADED,
        )

        def phase(value: ExecutionPhase, *, applicable: bool = True) -> None:
            nonlocal current
            current = self._telemetry.record(
                current,
                QA_COMPONENT_ID.value,
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
            if thread_id in archived_execution_threads(current.attempts):
                raise ReviewQaError("QA reused an archived Execution Thread.")
            prior_threads = {
                item
                for item in (
                    current.development.thread_id if current.development else None,
                    current.review.thread_id if current.review else None,
                )
                if item is not None
            }
            if thread_id in prior_threads:
                raise ReviewQaError("QA reused a development or review Execution Thread.")
            value = current.qa
            if value is None or value.thread_id == thread_id:
                return
            current = replace(current, qa=replace(value, thread_id=thread_id))
            current = self._store.record(current, RunEventType.QA_THREAD_BOUND)

        def turn_started(turn_id: ExecutionTurnId) -> None:
            nonlocal current
            value = current.qa
            if value is None:
                raise ReviewQaError("QA cursor disappeared.")
            current = replace(current, qa=replace(value, turn_id=turn_id))
            current = self._store.record(current, RunEventType.QA_TURN_STARTED)

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
            value = current.qa
            if value is None:
                raise ReviewQaError("QA cursor disappeared.")
            completed = tuple(dict.fromkeys((*value.completed_item_ids, item_id)))
            current = replace(
                current,
                qa=replace(value, completed_item_ids=completed),
                operation=OperationState(),
            )
            current = self._store.record(current, RunEventType.OPERATION_COMPLETED)

        def retry_scheduled(attempt: int, delay: float) -> None:
            nonlocal current
            value = current.qa
            if value is None:
                raise ReviewQaError("QA cursor disappeared.")
            current = replace(current, qa=replace(value, transient_retries=attempt))
            current = self._store.record(
                current,
                RunEventType.TRANSIENT_BACKEND_RETRY_SCHEDULED,
            )
            _report_retry(on_activity, "QA", attempt, delay)

        def approval_handler(request: AppServerApprovalRequest) -> str | None:
            nonlocal current
            manifest_policy = self._qa_manifest.approval_policy
            if manifest_policy is None:
                raise ReviewQaError("QA approval policy is missing.")
            policy = locked_approval_policy(
                current.approval_policies,
                QA_COMPONENT_ID.value,
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
                    component_id=QA_COMPONENT_ID.value,
                    issue_id=issue.issue_id.value,
                    attempt_id=cursor.attempt_id.value,
                    request=classified_request,
                    classification=classification,
                    selected_decision=decision,
                )
            return decision

        def execute(retry_recovery: bool) -> QaAgentOutput:
            active = current.qa
            if active is None:
                raise ReviewQaError("QA cursor disappeared.")
            if (
                (recover_completed or retry_recovery)
                and active.thread_id is not None
                and active.turn_id is not None
            ):
                return self._qa_runner.recover_completed_turn(
                    workspace=Path(workspace.path),
                    thread_id=active.thread_id,
                    turn_id=active.turn_id,
                    on_item_started=item_started,
                    on_item_completed=item_completed,
                )
            return self._qa_runner.run_turn(
                workspace=Path(workspace.path),
                qa_input=qa_input,
                criterion_ids=issue.acceptance_criterion_ids,
                thread_id=active.thread_id,
                on_thread_bound=thread_bound,
                on_turn_started=turn_started,
                on_item_started=item_started,
                on_item_completed=item_completed,
                on_activity=activity,
                pause_requested=lambda: self._pause_requested(run_id),
                interrupt_requested=lambda: self._interrupt_requested(run_id),
                on_approval=approval_handler,
                execution_profile=locked_execution_profile(
                    current.execution_profiles,
                    QA_COMPONENT_ID.value,
                    self._qa_manifest.execution_profiles[0],
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
            phase(ExecutionPhase.VERIFICATION_STARTED)
            phase(ExecutionPhase.STRUCTURED_OUTPUT)
            after, drift = self._qa_state_after(
                workspace,
                implementation,
                before,
                repository_baseline,
            )
            result = self._finalize_qa(
                current,
                issue,
                implementation,
                output,
                source_state_changed=bool(drift),
                state_change_evidence=drift,
            )
            completed = self._telemetry.record(
                result.snapshot,
                QA_COMPONENT_ID.value,
                attempt_key,
                ExecutionPhase.COMPLETED,
            )
            return replace(result, snapshot=completed)
        except (QaTurnPaused, QaTurnInterrupted) as error:
            blocked = self._qa_changed_after_failure(
                workspace,
                implementation,
                before,
                repository_baseline,
            )
            if blocked:
                self._block_qa(current, issue, blocked)
                raise ReviewQaError(
                    "QA changed source-controlled state and was blocked without reverting it."
                ) from error
            interrupted = self._checkpoint_interrupted_turn(
                current,
                pause_run=isinstance(error, QaTurnPaused),
            )
            if isinstance(error, QaTurnPaused):
                raise ReviewQaPaused(interrupted) from error
            raise ReviewQaInterrupted(interrupted) from error
        except AppServerApprovalRequired as error:
            blocked = self._qa_changed_after_failure(
                workspace,
                implementation,
                before,
                repository_baseline,
            )
            if blocked:
                self._block_qa(current, issue, blocked)
                raise ReviewQaError(
                    "QA changed source-controlled state and was blocked without reverting it."
                ) from error
            self._pause(current)
            raise ReviewQaError("QA paused for an explicit approval decision.") from error
        except Exception as error:
            blocked = self._qa_changed_after_failure(
                workspace,
                implementation,
                before,
                repository_baseline,
            )
            if blocked:
                self._block_qa(current, issue, blocked)
                raise ReviewQaError(
                    "QA changed source-controlled state and was blocked without reverting it."
                ) from error
            self._fail_attempt(current, issue, RunEventType.QA_FAILED)
            if isinstance(error, ReviewQaError):
                raise
            raise ReviewQaError("The real QA turn failed; reset the Issue.") from error
        finally:
            self._clear_stop_requests(run_id)

    def _finalize_review(
        self,
        snapshot: WorkflowRunSnapshot,
        issue: PlannedIssue,
        workspace: WorkspaceRef,
        implementation: _ImplementationIdentity,
        output: ReviewAgentOutput,
    ) -> ReviewCompleted:
        result = ReviewResult(
            REVIEW_RESULT_SCHEMA,
            implementation.attempt_id,
            implementation.diff_hash,
            tuple(
                replace(
                    item,
                    title=redact_diagnostic(item.title, limit=500),
                    rationale=redact_diagnostic(item.rationale, limit=4000),
                    evidence=redact_diagnostic(item.evidence, limit=4000),
                    expected_behavior=redact_diagnostic(
                        item.expected_behavior, limit=4000
                    ),
                    acceptance_condition=redact_diagnostic(item.acceptance_condition, limit=4000),
                )
                for item in output.findings
            ),
            redact_diagnostic(output.summary, limit=8000),
            None
            if output.blocked_reason is None
            else redact_diagnostic(output.blocked_reason, limit=4000),
        )
        validate_review_result(result)
        self._validate_review_evidence(Path(workspace.path), implementation, result)
        outcome = review_outcome(result)
        result_ref = self._store.save_json_artifact(
            snapshot.run_id,
            Path(REVIEW_RESULTS_DIRECTORY)
            / f"{issue.issue_id.value}-{implementation.attempt_id.value}.json",
            review_result_to_dict(result),
        )
        rework = _review_rework(result) if outcome is StepOutcome.CHANGES_REQUESTED else None
        rework_ref = self._save_rework(snapshot, issue.issue_id, rework)
        cursor = snapshot.review
        if cursor is None:
            raise ReviewQaError("Review cursor disappeared during finalization.")
        updated_cursor = replace(
            cursor,
            thread_id=output.thread_id,
            turn_id=output.turn_id,
            completed_item_ids=output.completed_item_ids,
            review_result=result_ref,
            rework_request=rework_ref,
        )
        if outcome is StepOutcome.SUCCEEDED:
            target = self._workflow.transition_target(CODE_REVIEW_STEP_ID, outcome)
            if target is None:
                raise ReviewQaError("Successful code review has no Workflow transition.")
            updated = replace(
                snapshot,
                active_step=target,
                step_status=StepRunStatus.NOT_STARTED,
                outcome=outcome,
                issues=_replace_issue_status(
                    snapshot.issues,
                    issue.issue_id,
                    IssueStatus.IN_QA,
                    current_step=target,
                ),
                review=updated_cursor,
            )
            updated = self._store.record(updated, RunEventType.REVIEW_SUCCEEDED)
        elif outcome is StepOutcome.CHANGES_REQUESTED:
            target = self._workflow.transition_target(CODE_REVIEW_STEP_ID, outcome)
            if target is None:
                raise ReviewQaError("Review changes have no Workflow transition.")
            attempts = _archive_attempt(
                snapshot,
                issue.issue_id,
                AttemptStatus.CHANGES_REQUESTED,
                outcome,
                review=result_ref,
                rework_request=rework_ref,
            )
            updated = replace(
                snapshot,
                active_step=target,
                step_status=StepRunStatus.NOT_STARTED,
                outcome=outcome,
                issues=_replace_issue_status(
                    snapshot.issues,
                    issue.issue_id,
                    IssueStatus.CHANGES_REQUESTED,
                    current_step=target,
                ),
                review=updated_cursor,
                attempts=attempts,
            )
            updated = self._store.record(updated, RunEventType.REVIEW_CHANGES_REQUESTED)
        else:
            attempts = _archive_attempt(
                snapshot,
                issue.issue_id,
                AttemptStatus.BLOCKED,
                outcome,
                review=result_ref,
                rework_request=rework_ref,
            )
            updated = replace(
                snapshot,
                run_status=WorkflowRunStatus.PAUSED,
                step_status=StepRunStatus.BLOCKED,
                outcome=outcome,
                issues=_replace_issue_status(
                    snapshot.issues,
                    issue.issue_id,
                    IssueStatus.BLOCKED,
                    owned_paths=tuple(item.path for item in implementation.changed_files),
                ),
                review=updated_cursor,
                attempts=attempts,
            )
            updated = self._store.record(updated, RunEventType.REVIEW_BLOCKED)
            self._store.release_lease(updated)
        return ReviewCompleted(updated, result, outcome, rework)

    def _finalize_qa(
        self,
        snapshot: WorkflowRunSnapshot,
        issue: PlannedIssue,
        implementation: _ImplementationIdentity,
        output: QaAgentOutput,
        *,
        source_state_changed: bool,
        state_change_evidence: str,
    ) -> QaCompleted:
        result = QaResult(
            QA_RESULT_SCHEMA,
            implementation.attempt_id,
            implementation.diff_hash,
            tuple(
                replace(
                    item,
                    evidence=redact_diagnostic(item.evidence, limit=4000),
                    reason=redact_diagnostic(item.reason, limit=4000),
                    expected_behavior=redact_diagnostic(item.expected_behavior, limit=4000),
                    acceptance_condition=redact_diagnostic(item.acceptance_condition, limit=4000),
                    command=None
                    if item.command is None
                    else redact_diagnostic(item.command, limit=2000),
                )
                for item in output.checks
            ),
            tuple(redact_diagnostic(item, limit=4000) for item in output.residual_risks),
            redact_diagnostic(output.summary, limit=8000),
            source_state_changed,
            redact_diagnostic(state_change_evidence, limit=20_000),
        )
        validate_qa_result(result, issue.acceptance_criterion_ids)
        outcome = qa_outcome(result)
        result_ref = self._store.save_json_artifact(
            snapshot.run_id,
            Path(QA_RESULTS_DIRECTORY)
            / f"{issue.issue_id.value}-{implementation.attempt_id.value}.json",
            qa_result_to_dict(result),
        )
        rework = _qa_rework(result) if outcome is StepOutcome.CHANGES_REQUESTED else None
        rework_ref = self._save_rework(snapshot, issue.issue_id, rework)
        cursor = snapshot.qa
        if cursor is None:
            raise ReviewQaError("QA cursor disappeared during finalization.")
        updated_cursor = replace(
            cursor,
            thread_id=output.thread_id,
            turn_id=output.turn_id,
            completed_item_ids=output.completed_item_ids,
            qa_result=result_ref,
            rework_request=rework_ref,
        )
        if outcome is StepOutcome.SUCCEEDED:
            target = self._workflow.transition_target(QA_STEP_ID, outcome)
            if target is None:
                raise ReviewQaError("Successful QA has no Workflow transition.")
            attempts = _archive_attempt(
                snapshot,
                issue.issue_id,
                AttemptStatus.COMPLETED,
                outcome,
                qa_result=result_ref,
            )
            updated = replace(
                snapshot,
                active_step=target,
                step_status=StepRunStatus.NOT_STARTED,
                outcome=outcome,
                issues=_replace_issue_status(
                    snapshot.issues, issue.issue_id, IssueStatus.COMPLETED
                ),
                qa=updated_cursor,
                attempts=attempts,
            )
            updated = self._store.record(updated, RunEventType.QA_SUCCEEDED)
        elif outcome is StepOutcome.CHANGES_REQUESTED:
            target = self._workflow.transition_target(QA_STEP_ID, outcome)
            if target is None:
                raise ReviewQaError("QA changes have no Workflow transition.")
            attempts = _archive_attempt(
                snapshot,
                issue.issue_id,
                AttemptStatus.CHANGES_REQUESTED,
                outcome,
                qa_result=result_ref,
                rework_request=rework_ref,
            )
            updated = replace(
                snapshot,
                active_step=target,
                step_status=StepRunStatus.NOT_STARTED,
                outcome=outcome,
                issues=_replace_issue_status(
                    snapshot.issues,
                    issue.issue_id,
                    IssueStatus.CHANGES_REQUESTED,
                    current_step=target,
                ),
                qa=updated_cursor,
                attempts=attempts,
            )
            updated = self._store.record(updated, RunEventType.QA_CHANGES_REQUESTED)
        else:
            attempts = _archive_attempt(
                snapshot,
                issue.issue_id,
                AttemptStatus.BLOCKED,
                outcome,
                qa_result=result_ref,
            )
            updated = replace(
                snapshot,
                run_status=WorkflowRunStatus.PAUSED,
                step_status=StepRunStatus.BLOCKED,
                outcome=outcome,
                issues=_replace_issue_status(
                    snapshot.issues,
                    issue.issue_id,
                    IssueStatus.BLOCKED,
                    owned_paths=tuple(item.path for item in implementation.changed_files),
                ),
                qa=updated_cursor,
                attempts=attempts,
            )
            updated = self._store.record(updated, RunEventType.QA_BLOCKED)
            self._store.release_lease(updated)
        return QaCompleted(updated, result, outcome, rework)

    def _review_prerequisites(
        self,
        snapshot: WorkflowRunSnapshot,
        package: PlanningPackage,
    ) -> tuple[PlannedIssue, WorkspaceRef, _ImplementationIdentity, Mapping[str, object]]:
        if snapshot.active_step != CODE_REVIEW_STEP_ID:
            raise ReviewQaError("Workflow Run is not at code review.")
        workspace = snapshot.workspace
        development = snapshot.development
        if workspace is None or development is None or development.implementation_result is None:
            raise ReviewQaError("Review requires a Workspace and Implementation Result.")
        issue = _package_issue(package, development.issue_id)
        if _issue_status(snapshot, issue.issue_id) is not IssueStatus.IN_REVIEW:
            raise ReviewQaError("Issue is not awaiting code review.")
        payload = self._store.load_json_artifact(snapshot.run_id, development.implementation_result)
        implementation = _implementation_identity(payload)
        if implementation.attempt_id != development.attempt_id:
            raise ReviewQaError("Implementation Result belongs to a different attempt.")
        return issue, workspace, implementation, payload

    def _qa_prerequisites(
        self,
        snapshot: WorkflowRunSnapshot,
        package: PlanningPackage,
    ) -> tuple[
        PlannedIssue,
        WorkspaceRef,
        _ImplementationIdentity,
        Mapping[str, object],
        Mapping[str, object],
    ]:
        if snapshot.active_step != QA_STEP_ID:
            raise ReviewQaError("Workflow Run is not at QA.")
        workspace = snapshot.workspace
        development = snapshot.development
        review = snapshot.review
        if (
            workspace is None
            or development is None
            or development.implementation_result is None
            or review is None
            or review.review_result is None
        ):
            raise ReviewQaError("QA requires accepted implementation and review results.")
        issue = _package_issue(package, development.issue_id)
        if _issue_status(snapshot, issue.issue_id) is not IssueStatus.IN_QA:
            raise ReviewQaError("Issue is not awaiting QA.")
        if review.issue_id != development.issue_id or review.attempt_id != development.attempt_id:
            raise ReviewQaError("Review cursor belongs to a different Issue attempt.")
        implementation_payload = self._store.load_json_artifact(
            snapshot.run_id, development.implementation_result
        )
        implementation = _implementation_identity(implementation_payload)
        if implementation.attempt_id != development.attempt_id:
            raise ReviewQaError("Implementation Result belongs to a different attempt.")
        review_payload = self._store.load_json_artifact(snapshot.run_id, review.review_result)
        accepted_review = review_result_from_dict(review_payload)
        if accepted_review.attempt_id != implementation.attempt_id:
            raise ReviewQaError("Review Result belongs to a different attempt.")
        if review_outcome(accepted_review) is not StepOutcome.SUCCEEDED:
            raise ReviewQaError("QA cannot consume an unaccepted Review Result.")
        if accepted_review.implementation_diff_hash != implementation.diff_hash:
            raise ReviewQaError("Review Result references a different implementation state.")
        return issue, workspace, implementation, implementation_payload, review_payload

    def _ensure_review_input(
        self,
        snapshot: WorkflowRunSnapshot,
        issue: PlannedIssue,
        workspace: WorkspaceRef,
        implementation: _ImplementationIdentity,
        implementation_payload: Mapping[str, object],
        changes: WorktreeChanges,
    ) -> WorkflowRunSnapshot:
        if snapshot.review is not None:
            if snapshot.review.attempt_id != implementation.attempt_id:
                raise ReviewQaError("Review cursor belongs to a different attempt.")
            return snapshot
        payload = {
            "schema": "devloop.review-input/v1",
            "issue": _issue_payload(issue),
            "workspace": _workspace_payload(workspace),
            "implementation": dict(implementation_payload),
            "relevant_diff": render_relevant_diff(
                Path(workspace.path), workspace.base_commit, implementation.changed_files
            ),
            "repository_constraints": _repository_constraints(Path(workspace.path)),
            "capability_profile": [
                item.value
                for item in capabilities_for(
                    snapshot.capability_profiles,
                    StepComponentId("code-review"),
                    fallback=REVIEW_CAPABILITIES.capabilities,
                )
            ],
        }
        artifact = self._store.save_json_artifact(
            snapshot.run_id,
            Path(REVIEW_INPUTS_DIRECTORY)
            / f"{issue.issue_id.value}-{implementation.attempt_id.value}.json",
            payload,
        )
        updated = replace(
            snapshot,
            step_status=StepRunStatus.RUNNING,
            outcome=None,
            review=ReviewCursor(issue.issue_id, implementation.attempt_id, artifact),
        )
        return self._store.record(updated, RunEventType.REVIEW_INPUT_SAVED)

    def _ensure_qa_input(
        self,
        snapshot: WorkflowRunSnapshot,
        issue: PlannedIssue,
        workspace: WorkspaceRef,
        implementation: _ImplementationIdentity,
        implementation_payload: Mapping[str, object],
        review_payload: Mapping[str, object],
        changes: WorktreeChanges,
    ) -> WorkflowRunSnapshot:
        if snapshot.qa is not None:
            if snapshot.qa.attempt_id != implementation.attempt_id:
                raise ReviewQaError("QA cursor belongs to a different attempt.")
            return snapshot
        payload = {
            "schema": "devloop.qa-input/v1",
            "issue": _issue_payload(issue),
            "workspace": _workspace_payload(workspace),
            "implementation": dict(implementation_payload),
            "review": dict(review_payload),
            "repository_state": {
                "base_state": changes.base_state,
                "result_state": changes.result_state,
                "diff_hash": changes.diff_hash,
                "repository_state_hash": changes.repository_state_hash,
                "changed_files": [
                    {"path": item.path, "kind": item.kind.value} for item in changes.changed_files
                ],
                "relevant_diff": render_relevant_diff(
                    Path(workspace.path), workspace.base_commit, implementation.changed_files
                ),
            },
            "repository_constraints": _repository_constraints(Path(workspace.path)),
            "capability_profile": [
                item.value
                for item in capabilities_for(
                    snapshot.capability_profiles,
                    StepComponentId("qa"),
                    fallback=QA_CAPABILITIES.capabilities,
                )
            ],
        }
        artifact = self._store.save_json_artifact(
            snapshot.run_id,
            Path(QA_INPUTS_DIRECTORY)
            / f"{issue.issue_id.value}-{implementation.attempt_id.value}.json",
            payload,
        )
        updated = replace(
            snapshot,
            step_status=StepRunStatus.RUNNING,
            outcome=None,
            qa=QaCursor(issue.issue_id, implementation.attempt_id, artifact),
        )
        return self._store.record(updated, RunEventType.QA_INPUT_SAVED)

    def _validate_preflight(self, snapshot: WorkflowRunSnapshot) -> PlanningPackage:
        if snapshot.planning_package is None:
            raise ReviewQaError("An accepted PRD Package is required.")
        if snapshot.workflow.definition_hash != self._workflow.definition_hash:
            raise ReviewQaError("The locked Workflow Definition has changed.")
        expected = tuple(
            ComponentLock(
                manifest.component_id,
                manifest.version,
                manifest.distribution,
                manifest.package_hash,
            )
            for manifest in self._registry.manifests
        )
        if snapshot.component_locks != expected:
            raise ReviewQaError("A locked Workflow component changed or is missing.")
        for manifest in self._registry.manifests:
            validate_component_ports(
                self._workflow.step(StepInstanceId(manifest.component_id.value)), manifest
            )
        review_capabilities = capabilities_for(
            snapshot.capability_profiles,
            StepComponentId("code-review"),
            fallback=REVIEW_CAPABILITIES.capabilities,
        )
        qa_capabilities = capabilities_for(
            snapshot.capability_profiles,
            StepComponentId("qa"),
            fallback=QA_CAPABILITIES.capabilities,
        )
        if not review_capabilities or not qa_capabilities:
            raise ReviewQaError("Review and QA capability profiles are required.")
        try:
            return load_planning_package(
                self._config.repository, snapshot.planning_package, snapshot.run_id
            )
        except PlanningPackageError as error:
            raise ReviewQaError(str(error)) from error

    def _verified_changes(
        self,
        workspace: WorkspaceRef,
        implementation: _ImplementationIdentity,
        repository_baseline: tuple[WorkspaceBaselineEntry, ...],
    ) -> WorktreeChanges:
        changes = capture_worktree_changes(
            Path(workspace.path), workspace.base_commit, repository_baseline
        )
        if (
            changes.diff_hash != implementation.diff_hash
            or changes.repository_state_hash != implementation.repository_state_hash
            or changes.changed_files != implementation.changed_files
        ):
            raise ReviewQaError("Repository state no longer matches the Implementation Result.")
        return changes

    def _qa_state_after(
        self,
        workspace: WorkspaceRef,
        implementation: _ImplementationIdentity,
        before: WorktreeChanges,
        repository_baseline: tuple[WorkspaceBaselineEntry, ...],
    ) -> tuple[WorktreeChanges, str]:
        try:
            after = capture_worktree_changes(
                Path(workspace.path), workspace.base_commit, repository_baseline
            )
        except GitOperationError:
            return before, "QA source state could not be inspected after verification."
        if after == before:
            return after, ""
        changed = ", ".join(item.path for item in after.changed_files)
        return after, f"QA changed source-controlled state: {changed}"

    def _qa_changed_after_failure(
        self,
        workspace: WorkspaceRef,
        implementation: _ImplementationIdentity,
        before: WorktreeChanges,
        repository_baseline: tuple[WorkspaceBaselineEntry, ...],
    ) -> str:
        try:
            after = capture_worktree_changes(
                Path(workspace.path), workspace.base_commit, repository_baseline
            )
        except GitOperationError:
            return "QA source state could not be inspected after failure."
        if after == before:
            return ""
        return "QA changed source-controlled state before failing: " + ", ".join(
            item.path for item in after.changed_files
        )

    def _validate_review_evidence(
        self,
        workspace: Path,
        implementation: _ImplementationIdentity,
        result: ReviewResult,
    ) -> None:
        deleted = {
            item.path for item in implementation.changed_files if item.kind is ChangeKind.DELETED
        }
        root = workspace.resolve()
        for finding in result.findings:
            path = (root / finding.file_path).resolve()
            if path != root and not path.is_relative_to(root):
                raise ReviewQaError("Review Finding evidence path escapes the workspace.")
            if not path.is_file():
                if finding.file_path not in deleted:
                    raise ReviewQaError("Review Finding repository evidence is unavailable.")
                continue
            if finding.line is not None:
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError as error:
                    raise ReviewQaError(
                        "Review Finding repository evidence cannot be read."
                    ) from error
                if finding.line > len(lines):
                    raise ReviewQaError("Review Finding line is outside the evidence file.")

    def _save_rework(
        self,
        snapshot: WorkflowRunSnapshot,
        issue_id: IssueId,
        request: ReworkRequest | None,
    ) -> ArtifactRef | None:
        if request is None:
            return None
        return self._store.save_json_artifact(
            snapshot.run_id,
            Path(REWORK_REQUESTS_DIRECTORY)
            / (
                f"{issue_id.value}-{request.attempt_id.value}-"
                f"{request.source.value.casefold()}.json"
            ),
            rework_request_to_dict(request),
        )

    def _block_qa(
        self,
        snapshot: WorkflowRunSnapshot,
        issue: PlannedIssue,
        evidence: str,
    ) -> None:
        development = snapshot.development
        if development is None:
            raise ReviewQaError("QA source-state block requires a development cursor.")
        attempts = _archive_attempt(
            snapshot,
            issue.issue_id,
            AttemptStatus.BLOCKED,
            StepOutcome.BLOCKED,
        )
        owned_paths = _capture_issue_owned_paths(snapshot, issue.issue_id)
        blocked = replace(
            snapshot,
            run_status=WorkflowRunStatus.PAUSED,
            step_status=StepRunStatus.BLOCKED,
            outcome=StepOutcome.BLOCKED,
            issues=_replace_issue_status(
                snapshot.issues,
                issue.issue_id,
                IssueStatus.BLOCKED,
                owned_paths=owned_paths,
            ),
            attempts=attempts,
        )
        blocked = self._store.record(blocked, RunEventType.QA_BLOCKED)
        self._store.save_json_artifact(
            blocked.run_id,
            Path(QA_RESULTS_DIRECTORY)
            / f"{issue.issue_id.value}-{development.attempt_id.value}-source-blocked.json",
            {"schema": "devloop.qa-source-state/v1", "evidence": evidence},
        )
        self._store.release_lease(blocked)

    def _pause(self, snapshot: WorkflowRunSnapshot) -> None:
        paused = replace(snapshot, run_status=WorkflowRunStatus.PAUSED)
        paused = self._store.record(paused, RunEventType.RUN_PAUSED)
        self._store.release_lease(paused)

    def _validate_stop_request(
        self,
        run_id: WorkflowRunId,
        *,
        require_turn: bool,
    ) -> None:
        snapshot = self._store.load(run_id)
        cursor = snapshot.review if snapshot.active_step == CODE_REVIEW_STEP_ID else snapshot.qa
        if (
            snapshot.active_step not in {CODE_REVIEW_STEP_ID, QA_STEP_ID}
            or snapshot.run_status is not WorkflowRunStatus.RUNNING
            or cursor is None
            or (require_turn and cursor.turn_id is None)
        ):
            raise ReviewQaError("No active review or QA turn can be stopped.")

    def _pause_requested(self, run_id: WorkflowRunId) -> bool:
        with self._stop_lock:
            return run_id in self._pause_requests

    def _interrupt_requested(self, run_id: WorkflowRunId) -> bool:
        with self._stop_lock:
            return run_id in self._interrupt_requests

    def _clear_stop_requests(self, run_id: WorkflowRunId) -> None:
        with self._stop_lock:
            self._pause_requests.discard(run_id)
            self._interrupt_requests.discard(run_id)

    def _checkpoint_interrupted_turn(
        self,
        snapshot: WorkflowRunSnapshot,
        *,
        pause_run: bool,
    ) -> WorkflowRunSnapshot:
        operation = snapshot.operation
        if operation.status is OperationStatus.RUNNING:
            operation = replace(operation, status=OperationStatus.UNKNOWN)
        interrupted = replace(
            snapshot,
            run_status=(
                WorkflowRunStatus.PAUSED if pause_run else WorkflowRunStatus.AWAITING_USER
            ),
            step_status=(
                snapshot.step_status if pause_run else StepRunStatus.AWAITING_USER
            ),
            operation=operation,
        )
        event_type = (
            RunEventType.RUN_PAUSED
            if pause_run
            else RunEventType.VERIFICATION_TURN_INTERRUPTED
        )
        interrupted = self._store.record(interrupted, event_type)
        self._store.release_lease(interrupted)
        return interrupted

    def _fail_attempt(
        self,
        snapshot: WorkflowRunSnapshot,
        issue: PlannedIssue,
        event_type: RunEventType,
    ) -> None:
        attempts = _archive_attempt(
            snapshot,
            issue.issue_id,
            AttemptStatus.FAILED,
            StepOutcome.FAILED,
        )
        owned_paths = _capture_issue_owned_paths(snapshot, issue.issue_id)
        failed = replace(
            snapshot,
            run_status=WorkflowRunStatus.PAUSED,
            step_status=StepRunStatus.FAILED,
            outcome=StepOutcome.FAILED,
            issues=_replace_issue_status(
                snapshot.issues,
                issue.issue_id,
                IssueStatus.FAILED,
                owned_paths=owned_paths,
            ),
            attempts=attempts,
        )
        failed = self._store.record(failed, event_type)
        self._store.release_lease(failed)

    def _ensure_lease(self, snapshot: WorkflowRunSnapshot) -> WorkflowRunSnapshot:
        try:
            self._store.validate_lease(snapshot)
            return snapshot
        except (RunStoreError, OSError, ValueError):
            return self._store.take_lease(snapshot)


def review_result_to_dict(result: ReviewResult) -> dict[str, object]:
    return {
        "schema": result.schema,
        "attempt_id": result.attempt_id.value,
        "implementation_diff_hash": result.implementation_diff_hash,
        "findings": [
            {
                "id": item.finding_id.value,
                "severity": item.severity.value,
                "disposition": item.disposition.value,
                "title": item.title,
                "rationale": item.rationale,
                "evidence": item.evidence,
                "file_path": item.file_path,
                "line": item.line,
                "expected_behavior": item.expected_behavior,
                "acceptance_condition": item.acceptance_condition,
            }
            for item in result.findings
        ],
        "summary": result.summary,
        "blocked_reason": result.blocked_reason,
    }


def review_result_from_dict(value: Mapping[str, object]) -> ReviewResult:
    findings_value = value.get("findings")
    if not isinstance(findings_value, list):
        raise ReviewQaError("Persisted Review Result findings are invalid.")
    findings: list[ReviewFinding] = []
    for item_value in findings_value:
        if not isinstance(item_value, dict):
            raise ReviewQaError("Persisted Review Finding is invalid.")
        item = cast(dict[str, object], item_value)
        line = item.get("line")
        if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
            raise ReviewQaError("Persisted Review Finding line is invalid.")
        findings.append(
            ReviewFinding(
                ReviewFindingId(_string(item, "id")),
                FindingSeverity(_string(item, "severity")),
                FindingDisposition(_string(item, "disposition")),
                _string(item, "title"),
                _string(item, "rationale"),
                _string(item, "evidence"),
                _string(item, "file_path"),
                line,
                _string(item, "expected_behavior"),
                _string(item, "acceptance_condition"),
            )
        )
    blocked = value.get("blocked_reason")
    if blocked is not None and not isinstance(blocked, str):
        raise ReviewQaError("Persisted Review Result blocked reason is invalid.")
    result = ReviewResult(
        _string(value, "schema"),
        AttemptId(_string(value, "attempt_id")),
        _string(value, "implementation_diff_hash"),
        tuple(findings),
        _string(value, "summary"),
        blocked,
    )
    validate_review_result(result)
    return result


def qa_result_to_dict(result: QaResult) -> dict[str, object]:
    return {
        "schema": result.schema,
        "attempt_id": result.attempt_id.value,
        "implementation_diff_hash": result.implementation_diff_hash,
        "checks": [
            {
                "id": item.check_id.value,
                "criterion_id": item.criterion_id.value,
                "kind": item.kind.value,
                "requirement": item.requirement.value,
                "status": item.status.value,
                "command": item.command,
                "exit_code": item.exit_code,
                "duration_ms": item.duration_ms,
                "evidence": item.evidence,
                "reason": item.reason,
                "expected_behavior": item.expected_behavior,
                "acceptance_condition": item.acceptance_condition,
            }
            for item in result.checks
        ],
        "residual_risks": list(result.residual_risks),
        "summary": result.summary,
        "source_state_changed": result.source_state_changed,
        "state_change_evidence": result.state_change_evidence,
    }


def rework_request_to_dict(request: ReworkRequest) -> dict[str, object]:
    return {
        "schema": request.schema,
        "source": request.source.value,
        "attempt_id": request.attempt_id.value,
        "items": [
            {
                "id": item.item_id,
                "evidence": item.evidence,
                "expected_behavior": item.expected_behavior,
                "acceptance_condition": item.acceptance_condition,
            }
            for item in request.items
        ],
    }


def _review_rework(result: ReviewResult) -> ReworkRequest:
    return ReworkRequest(
        REWORK_REQUEST_SCHEMA,
        ReworkSource.CODE_REVIEW,
        result.attempt_id,
        tuple(
            ReworkItem(
                item.finding_id.value,
                item.evidence,
                item.expected_behavior,
                item.acceptance_condition,
            )
            for item in result.findings
            if item.disposition is FindingDisposition.MUST_FIX
        ),
    )


def _qa_rework(result: QaResult) -> ReworkRequest:
    return ReworkRequest(
        REWORK_REQUEST_SCHEMA,
        ReworkSource.QA,
        result.attempt_id,
        tuple(
            ReworkItem(
                item.check_id.value,
                item.evidence,
                item.expected_behavior,
                item.acceptance_condition,
            )
            for item in result.checks
            if item.requirement is CheckRequirement.REQUIRED and item.status is QaCheckStatus.FAILED
        ),
    )


def _implementation_identity(value: Mapping[str, object]) -> _ImplementationIdentity:
    if value.get("schema") != "devloop.implementation-result/v1":
        raise ReviewQaError("Implementation Result schema is unsupported.")
    changed_value = value.get("changed_files")
    if not isinstance(changed_value, list):
        raise ReviewQaError("Implementation Result changed files are invalid.")
    changed: list[ChangedFile] = []
    for item_value in changed_value:
        if not isinstance(item_value, dict):
            raise ReviewQaError("Implementation Result changed file is invalid.")
        item = cast(dict[str, object], item_value)
        changed.append(ChangedFile(_string(item, "path"), ChangeKind(_string(item, "kind"))))
    return _ImplementationIdentity(
        AttemptId(_string(value, "attempt_id")),
        _string(value, "diff_hash"),
        _string(value, "repository_state_hash"),
        tuple(changed),
    )


def _issue_payload(issue: PlannedIssue) -> dict[str, object]:
    return {
        "id": issue.issue_id.value,
        "position": issue.position,
        "markdown": issue.markdown,
        "requirements": [item.value for item in issue.requirement_ids],
        "acceptance_criteria": [item.value for item in issue.acceptance_criterion_ids],
    }


def _workspace_payload(workspace: WorkspaceRef) -> dict[str, object]:
    return {
        "kind": workspace.kind.value,
        "path": workspace.path,
        "branch": workspace.branch,
        "base_commit": workspace.base_commit,
    }


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
            "source-change",
            "commit",
            "push",
            "merge",
            "pull-request",
            "branch-delete",
            "worktree-remove",
        ],
    }


def _package_issue(package: PlanningPackage, issue_id: IssueId) -> PlannedIssue:
    issue = next((item for item in package.issues if item.issue_id == issue_id), None)
    if issue is None:
        raise ReviewQaError("Checkpointed Issue is not in the accepted package.")
    return issue


def _issue_status(snapshot: WorkflowRunSnapshot, issue_id: IssueId) -> IssueStatus:
    state = next((item for item in snapshot.issues if item.issue_id == issue_id), None)
    if state is None:
        raise ReviewQaError("Workflow Run does not contain the active Issue state.")
    return state.status


def _issue_repository_baseline(
    snapshot: WorkflowRunSnapshot,
    issue_id: IssueId,
) -> tuple[WorkspaceBaselineEntry, ...]:
    state = next((item for item in snapshot.issues if item.issue_id == issue_id), None)
    if state is None or state.repository_baseline is None:
        raise ReviewQaError("Issue repository baseline is missing.")
    return state.repository_baseline


def _replace_issue_status(
    states: tuple[IssueRuntimeState, ...],
    issue_id: IssueId,
    status: IssueStatus,
    *,
    current_step: StepInstanceId | None = None,
    owned_paths: tuple[str, ...] | None = None,
) -> tuple[IssueRuntimeState, ...]:
    return tuple(
        replace(
            item,
            status=status,
            current_step=item.current_step if current_step is None else current_step,
            owned_paths=item.owned_paths if owned_paths is None else owned_paths,
        )
        if item.issue_id == issue_id
        else item
        for item in states
    )


def _capture_issue_owned_paths(
    snapshot: WorkflowRunSnapshot,
    issue_id: IssueId,
) -> tuple[str, ...]:
    state = next((item for item in snapshot.issues if item.issue_id == issue_id), None)
    workspace = snapshot.workspace
    if state is None or workspace is None or state.repository_baseline is None:
        return () if state is None else state.owned_paths
    try:
        changes = capture_worktree_changes(
            Path(workspace.path),
            workspace.base_commit,
            state.repository_baseline,
        )
    except GitOperationError:
        return state.owned_paths
    return tuple(item.path for item in changes.changed_files)


def _archive_attempt(
    snapshot: WorkflowRunSnapshot,
    issue_id: IssueId,
    status: AttemptStatus,
    outcome: StepOutcome,
    *,
    review: ArtifactRef | None = None,
    qa_result: ArtifactRef | None = None,
    rework_request: ArtifactRef | None = None,
) -> tuple[IssueAttemptRecord, ...]:
    development = snapshot.development
    if development is None or development.issue_id != issue_id:
        raise ReviewQaError("The active development attempt cannot be archived.")
    identity = (issue_id, _attempt_number(development.attempt_id))
    if any((item.issue_id, item.attempt_number) == identity for item in snapshot.attempts):
        raise ReviewQaError("The active Issue attempt was already archived.")
    record = IssueAttemptRecord(
        issue_id,
        identity[1],
        status,
        outcome,
        development.implementation_result,
        review
        if review is not None
        else snapshot.review.review_result
        if snapshot.review is not None
        else None,
        qa_result,
        rework_request,
        development.thread_id,
        snapshot.review.thread_id if snapshot.review is not None else None,
        snapshot.qa.thread_id if snapshot.qa is not None else None,
    )
    validate_attempt_record(record)
    return (*snapshot.attempts, record)


def _report_retry(
    on_activity: Callable[[str], None] | None,
    phase: str,
    attempt: int,
    delay: float,
) -> None:
    if on_activity is None:
        return
    on_activity(
        f"Retrying transient {phase} backend failure ({attempt}) after {delay:.2f}s."
    )


def _attempt_number(attempt_id: AttemptId) -> int:
    return int(attempt_id.value.rsplit("-", 1)[1])


def _string(value: Mapping[str, object], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise ReviewQaError(f"Persisted artifact is missing {name}.")
    return item
