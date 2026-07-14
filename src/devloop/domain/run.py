from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from devloop.domain.capabilities import ResolvedCapabilityProfile
from devloop.domain.development import (
    DevelopmentCursor,
    IssueRuntimeState,
    PlanningPackageRef,
    WorkspaceRef,
)
from devloop.domain.finalization import FinalizationCursor
from devloop.domain.identifiers import (
    ExecutionThreadId,
    ExecutionTurnId,
    FeatureSlug,
    StepComponentId,
    StepInstanceId,
    WorkflowId,
    WorkflowRunId,
)
from devloop.domain.outcomes import StepOutcome as StepOutcome
from devloop.domain.review_qa import QaCursor, ReviewCursor
from devloop.domain.scheduler import IssueAttemptRecord


class WorkflowRunStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    AWAITING_USER = "AWAITING_USER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepRunStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    AWAITING_USER = "AWAITING_USER"
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AnalysisIntent(str, Enum):
    REQUEST_CHANGES = "REQUEST_CHANGES"
    ACCEPT = "ACCEPT"


class AnalysisResponseKind(str, Enum):
    CLARIFICATION = "CLARIFICATION"
    DRAFT = "DRAFT"


class RunEventType(str, Enum):
    RUN_CREATED = "RUN_CREATED"
    ANALYSIS_ATTEMPT_STARTED = "ANALYSIS_ATTEMPT_STARTED"
    ANALYSIS_THREAD_BOUND = "ANALYSIS_THREAD_BOUND"
    ANALYSIS_TURN_STARTED = "ANALYSIS_TURN_STARTED"
    ANALYSIS_CLARIFICATION_REQUESTED = "ANALYSIS_CLARIFICATION_REQUESTED"
    ANALYSIS_DRAFT_SAVED = "ANALYSIS_DRAFT_SAVED"
    RUN_PAUSED = "RUN_PAUSED"
    RUN_RESUMED = "RUN_RESUMED"
    ANALYSIS_ACCEPTED = "ANALYSIS_ACCEPTED"
    WORKSPACE_PREPARATION_STARTED = "WORKSPACE_PREPARATION_STARTED"
    WORKSPACE_PREPARED = "WORKSPACE_PREPARED"
    CONTEXT_MANIFEST_SAVED = "CONTEXT_MANIFEST_SAVED"
    DEVELOPMENT_THREAD_BOUND = "DEVELOPMENT_THREAD_BOUND"
    DEVELOPMENT_TURN_STARTED = "DEVELOPMENT_TURN_STARTED"
    DEVELOPMENT_SUCCEEDED = "DEVELOPMENT_SUCCEEDED"
    DEVELOPMENT_BLOCKED = "DEVELOPMENT_BLOCKED"
    DEVELOPMENT_APPROVAL_REQUIRED = "DEVELOPMENT_APPROVAL_REQUIRED"
    DEVELOPMENT_TURN_INTERRUPTED = "DEVELOPMENT_TURN_INTERRUPTED"
    VERIFICATION_TURN_INTERRUPTED = "VERIFICATION_TURN_INTERRUPTED"
    ISSUE_ATTEMPT_STARTED = "ISSUE_ATTEMPT_STARTED"
    ISSUE_RETRY_LIMIT_REACHED = "ISSUE_RETRY_LIMIT_REACHED"
    BLOCKED_RETRY_AUTHORIZED = "BLOCKED_RETRY_AUTHORIZED"
    FAILED_RESET_AUTHORIZED = "FAILED_RESET_AUTHORIZED"
    TRANSIENT_BACKEND_RETRY_SCHEDULED = "TRANSIENT_BACKEND_RETRY_SCHEDULED"
    ISSUE_ATTEMPT_ARCHIVED = "ISSUE_ATTEMPT_ARCHIVED"
    SCHEDULER_DRAINED = "SCHEDULER_DRAINED"
    SCHEDULER_PAUSED = "SCHEDULER_PAUSED"
    DEVELOPMENT_FAILED = "DEVELOPMENT_FAILED"
    REVIEW_INPUT_SAVED = "REVIEW_INPUT_SAVED"
    REVIEW_THREAD_BOUND = "REVIEW_THREAD_BOUND"
    REVIEW_TURN_STARTED = "REVIEW_TURN_STARTED"
    REVIEW_SUCCEEDED = "REVIEW_SUCCEEDED"
    REVIEW_CHANGES_REQUESTED = "REVIEW_CHANGES_REQUESTED"
    REVIEW_BLOCKED = "REVIEW_BLOCKED"
    REVIEW_FAILED = "REVIEW_FAILED"
    QA_INPUT_SAVED = "QA_INPUT_SAVED"
    QA_THREAD_BOUND = "QA_THREAD_BOUND"
    QA_TURN_STARTED = "QA_TURN_STARTED"
    QA_SUCCEEDED = "QA_SUCCEEDED"
    QA_CHANGES_REQUESTED = "QA_CHANGES_REQUESTED"
    QA_BLOCKED = "QA_BLOCKED"
    QA_FAILED = "QA_FAILED"
    FINALIZATION_STARTED = "FINALIZATION_STARTED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_CANCELLED = "RUN_CANCELLED"
    OPERATION_STARTED = "OPERATION_STARTED"
    OPERATION_COMPLETED = "OPERATION_COMPLETED"
    OPERATION_UNKNOWN = "OPERATION_UNKNOWN"
    RECOVERY_ATTEMPT_STARTED = "RECOVERY_ATTEMPT_STARTED"


class BackendActivity(str, Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    STREAMING = "STREAMING"
    WAITING = "WAITING"
    FAILED = "FAILED"


class OperationStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class OperationState:
    item_id: str | None = None
    status: OperationStatus = OperationStatus.IDLE


@dataclass(frozen=True)
class RunLease:
    lease_id: str
    process_id: int
    acquired_at: str


@dataclass(frozen=True)
class ComponentLock:
    component_id: StepComponentId
    version: str
    distribution: str
    package_hash: str


@dataclass(frozen=True)
class ResolvedWorkflow:
    workflow_id: WorkflowId
    version: str
    definition_hash: str


@dataclass(frozen=True)
class AnalysisCursor:
    thread_id: ExecutionThreadId | None = None
    turn_id: ExecutionTurnId | None = None
    draft_revision: int = 0
    clarification: str | None = None
    completed_item_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowRunSnapshot:
    schema: str
    run_id: WorkflowRunId
    repository: str
    feature_title: str
    feature_slug: FeatureSlug
    workflow: ResolvedWorkflow
    component_locks: tuple[ComponentLock, ...]
    active_step: StepInstanceId
    run_status: WorkflowRunStatus
    step_status: StepRunStatus
    outcome: StepOutcome | None
    analysis: AnalysisCursor
    lease: RunLease
    event_sequence: int
    updated_at: str
    planning_package: PlanningPackageRef | None = None
    workspace: WorkspaceRef | None = None
    issues: tuple[IssueRuntimeState, ...] = ()
    development: DevelopmentCursor | None = None
    review: ReviewCursor | None = None
    qa: QaCursor | None = None
    finalization: FinalizationCursor | None = None
    attempts: tuple[IssueAttemptRecord, ...] = ()
    operation: OperationState = OperationState()
    workspace_state_hash: str | None = None
    capability_profiles: tuple[ResolvedCapabilityProfile, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.run_status in {
            WorkflowRunStatus.COMPLETED,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        }
