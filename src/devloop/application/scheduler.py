from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum

from devloop.application.config import ApplicationConfig
from devloop.application.development import (
    DevelopmentBlocked,
    DevelopmentCompleted,
    DevelopmentPrepared,
    NoReadyIssueError,
    ReworkLimitReachedError,
    WorkspaceDevelopmentService,
)
from devloop.application.finalization import FinalizationCompleted, FinalizationService
from devloop.application.review_qa import QaCompleted, ReviewCompleted, ReviewQaService
from devloop.domain.development import IssueStatus
from devloop.domain.identifiers import IssueId, StepInstanceId, WorkflowRunId
from devloop.domain.outcomes import StepOutcome
from devloop.domain.planning import PlanningPackage
from devloop.domain.run import (
    RunEventType,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.domain.scheduler import (
    CompletedResultSets,
    IssueBoardRow,
    completed_result_sets,
    project_issue_board,
    refresh_issue_states,
    select_next_ready_issue,
)
from devloop.execution.app_server import AppServerApprovalRequest
from devloop.persistence.run_store import RunStore
from devloop.planning.package_reader import load_planning_package
from devloop.workflow.definition import load_standard_workflow

DEVELOPMENT_STEP_ID = StepInstanceId("development")
CODE_REVIEW_STEP_ID = StepInstanceId("code-review")
QA_STEP_ID = StepInstanceId("qa")


class SchedulerAction(str, Enum):
    ISSUE_PREPARED = "ISSUE_PREPARED"
    DEVELOPMENT_COMPLETED = "DEVELOPMENT_COMPLETED"
    REVIEW_COMPLETED = "REVIEW_COMPLETED"
    QA_COMPLETED = "QA_COMPLETED"
    WORKFLOW_DRAINED = "WORKFLOW_DRAINED"
    PAUSED = "PAUSED"


@dataclass(frozen=True)
class SchedulerAdvance:
    snapshot: WorkflowRunSnapshot
    action: SchedulerAction
    development: DevelopmentCompleted | None = None
    review: ReviewCompleted | None = None
    qa: QaCompleted | None = None
    finalization: FinalizationCompleted | None = None


class WorkflowSchedulerError(RuntimeError):
    pass


class WorkflowSchedulerService:
    def __init__(
        self,
        config: ApplicationConfig,
        *,
        development_service: WorkspaceDevelopmentService | None = None,
        review_qa_service: ReviewQaService | None = None,
        finalization_service: FinalizationService | None = None,
    ) -> None:
        self._config = config
        self._store = RunStore(config.paths.run_root)
        self._workflow = load_standard_workflow()
        self._development = development_service or WorkspaceDevelopmentService(config)
        self._review_qa = review_qa_service or ReviewQaService(config)
        self._finalization = finalization_service or FinalizationService(config)

    def issue_board(self, run_id: WorkflowRunId) -> tuple[IssueBoardRow, ...]:
        snapshot = self._store.load(run_id)
        package = self._package(snapshot)
        states = refresh_issue_states(package, snapshot.issues)
        return project_issue_board(package, states, snapshot.attempts)

    def completed_results(self, run_id: WorkflowRunId) -> CompletedResultSets:
        return completed_result_sets(self._store.load(run_id).attempts)

    def retry_blocked_issue(
        self,
        run_id: WorkflowRunId,
        issue_id: IssueId,
    ) -> DevelopmentPrepared:
        return self._development.retry_blocked_issue(run_id, issue_id)

    def reset_failed_issue(
        self,
        run_id: WorkflowRunId,
        issue_id: IssueId,
    ) -> DevelopmentPrepared:
        return self._development.reset_failed_issue(run_id, issue_id)

    def advance(
        self,
        run_id: WorkflowRunId,
        *,
        on_activity: Callable[[str], None] | None = None,
        on_approval: Callable[[AppServerApprovalRequest], str | None] | None = None,
    ) -> SchedulerAdvance:
        snapshot = self._store.load(run_id)
        if snapshot.workflow.definition_hash != self._workflow.definition_hash:
            raise WorkflowSchedulerError("The locked Workflow Definition has changed.")
        if snapshot.active_step == self._workflow.completion_step():
            finalized = self._finalization.finalize(run_id)
            return SchedulerAdvance(
                finalized.snapshot,
                SchedulerAction.WORKFLOW_DRAINED,
                finalization=finalized,
            )
        if snapshot.active_step == DEVELOPMENT_STEP_ID:
            snapshot = self._prepare_development_if_needed(snapshot)
            if snapshot.active_step == self._workflow.completion_step():
                return SchedulerAdvance(snapshot, SchedulerAction.WORKFLOW_DRAINED)
            if snapshot.run_status is WorkflowRunStatus.PAUSED:
                return SchedulerAdvance(snapshot, SchedulerAction.PAUSED)
            developed = self._development.develop(
                run_id,
                on_activity=on_activity,
                on_approval=on_approval,
            )
            if isinstance(developed, DevelopmentBlocked):
                return self._continue_after_block(
                    developed.snapshot,
                    on_activity=on_activity,
                    on_approval=on_approval,
                )
            return SchedulerAdvance(
                developed.snapshot,
                SchedulerAction.DEVELOPMENT_COMPLETED,
                development=developed,
            )
        if snapshot.active_step == CODE_REVIEW_STEP_ID:
            if _active_issue_status(snapshot) is IssueStatus.BLOCKED:
                return self._continue_after_block(
                    snapshot,
                    on_activity=on_activity,
                    on_approval=on_approval,
                )
            reviewed = self._review_qa.review(
                run_id,
                on_activity=on_activity,
                on_approval=on_approval,
            )
            if reviewed.outcome is StepOutcome.BLOCKED:
                return self._continue_after_block(
                    reviewed.snapshot,
                    on_activity=on_activity,
                    on_approval=on_approval,
                )
            return SchedulerAdvance(
                reviewed.snapshot,
                SchedulerAction.REVIEW_COMPLETED,
                review=reviewed,
            )
        if snapshot.active_step == QA_STEP_ID:
            if _active_issue_status(snapshot) is IssueStatus.BLOCKED:
                return self._continue_after_block(
                    snapshot,
                    on_activity=on_activity,
                    on_approval=on_approval,
                )
            verified = self._review_qa.qa(
                run_id,
                on_activity=on_activity,
                on_approval=on_approval,
            )
            if verified.outcome is StepOutcome.BLOCKED:
                return self._continue_after_block(
                    verified.snapshot,
                    on_activity=on_activity,
                    on_approval=on_approval,
                )
            return SchedulerAdvance(
                verified.snapshot,
                SchedulerAction.QA_COMPLETED,
                qa=verified,
            )
        raise WorkflowSchedulerError(f"Unsupported scheduler step: {snapshot.active_step}.")

    def run_until_pause(
        self,
        run_id: WorkflowRunId,
        *,
        on_activity: Callable[[str], None] | None = None,
        on_approval: Callable[[AppServerApprovalRequest], str | None] | None = None,
    ) -> SchedulerAdvance:
        package = self._package(self._store.load(run_id))
        maximum_advances = len(package.issues) * (
            3 * (1 + self._workflow.retry_policy.max_rework_cycles_per_issue) + 1
        )
        for _ in range(maximum_advances):
            result = self.advance(
                run_id,
                on_activity=on_activity,
                on_approval=on_approval,
            )
            if result.action in {SchedulerAction.PAUSED, SchedulerAction.WORKFLOW_DRAINED}:
                return result
        raise WorkflowSchedulerError("Scheduler exceeded the versioned bounded transition budget.")

    def _prepare_development_if_needed(
        self,
        snapshot: WorkflowRunSnapshot,
    ) -> WorkflowRunSnapshot:
        cursor = snapshot.development
        status = None if cursor is None else _active_issue_status(snapshot)
        if status is IssueStatus.CHANGES_REQUESTED:
            try:
                return self._development.prepare_rework(snapshot.run_id).snapshot
            except ReworkLimitReachedError:
                return self._drain_or_pause(self._store.load(snapshot.run_id)).snapshot
        if cursor is None or status in {
            IssueStatus.COMPLETED,
            IssueStatus.BLOCKED,
        }:
            try:
                return self._development.prepare_next_ready(snapshot.run_id).snapshot
            except NoReadyIssueError:
                return self._drain_or_pause(self._store.load(snapshot.run_id)).snapshot
        if status is IssueStatus.FAILED:
            return snapshot
        return snapshot

    def _continue_after_block(
        self,
        snapshot: WorkflowRunSnapshot,
        *,
        on_activity: Callable[[str], None] | None,
        on_approval: Callable[[AppServerApprovalRequest], str | None] | None,
    ) -> SchedulerAdvance:
        current = snapshot
        while True:
            try:
                self._development.prepare_next_ready(current.run_id)
            except NoReadyIssueError:
                return self._drain_or_pause(self._store.load(current.run_id))
            completed = self._development.develop(
                current.run_id,
                on_activity=on_activity,
                on_approval=on_approval,
            )
            if isinstance(completed, DevelopmentBlocked):
                current = completed.snapshot
                continue
            return SchedulerAdvance(
                completed.snapshot,
                SchedulerAction.DEVELOPMENT_COMPLETED,
                development=completed,
            )

    def _drain_or_pause(self, snapshot: WorkflowRunSnapshot) -> SchedulerAdvance:
        package = self._package(snapshot)
        states = refresh_issue_states(package, snapshot.issues)
        if all(item.status is IssueStatus.COMPLETED for item in states):
            drained = replace(
                snapshot,
                issues=states,
                active_step=self._workflow.completion_step(),
                step_status=StepRunStatus.NOT_STARTED,
                outcome=StepOutcome.SUCCEEDED,
                run_status=WorkflowRunStatus.AWAITING_USER,
            )
            drained = self._store.record(drained, RunEventType.SCHEDULER_DRAINED)
            return SchedulerAdvance(drained, SchedulerAction.WORKFLOW_DRAINED)
        if select_next_ready_issue(package, states) is not None:
            prepared = self._development.prepare_next_ready(snapshot.run_id)
            return SchedulerAdvance(prepared.snapshot, SchedulerAction.ISSUE_PREPARED)
        if (
            snapshot.run_status is WorkflowRunStatus.PAUSED
            and snapshot.step_status is StepRunStatus.BLOCKED
            and snapshot.outcome is StepOutcome.BLOCKED
        ):
            return SchedulerAdvance(snapshot, SchedulerAction.PAUSED)
        paused = replace(
            snapshot,
            issues=states,
            run_status=WorkflowRunStatus.PAUSED,
            step_status=StepRunStatus.BLOCKED,
            outcome=StepOutcome.BLOCKED,
        )
        paused = self._store.record(paused, RunEventType.SCHEDULER_PAUSED)
        self._store.release_lease(paused)
        return SchedulerAdvance(paused, SchedulerAction.PAUSED)

    def _pause(self, snapshot: WorkflowRunSnapshot) -> SchedulerAdvance:
        paused = replace(snapshot, run_status=WorkflowRunStatus.PAUSED)
        paused = self._store.record(paused, RunEventType.SCHEDULER_PAUSED)
        self._store.release_lease(paused)
        return SchedulerAdvance(paused, SchedulerAction.PAUSED)

    def _package(self, snapshot: WorkflowRunSnapshot) -> PlanningPackage:
        if snapshot.planning_package is None:
            raise WorkflowSchedulerError("Scheduler requires an accepted PRD Package.")
        return load_planning_package(
            self._config.repository,
            snapshot.planning_package,
            snapshot.run_id,
        )


def _active_issue_status(snapshot: WorkflowRunSnapshot) -> IssueStatus:
    if snapshot.development is None:
        raise WorkflowSchedulerError("Scheduler has no active Issue cursor.")
    state = next(
        (
            item
            for item in snapshot.issues
            if item.issue_id == snapshot.development.issue_id
        ),
        None,
    )
    if state is None:
        raise WorkflowSchedulerError("Scheduler has no active Issue state.")
    return state.status
