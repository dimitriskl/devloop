from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from devloop.domain.identifiers import (
    AcceptanceCriterionId,
    AttemptId,
    CapabilityId,
    ExecutionThreadId,
    ExecutionTurnId,
    IssueId,
    StepInstanceId,
)


class WorkspaceChoice(str, Enum):
    CURRENT_CHECKOUT = "CURRENT_CHECKOUT"
    DEDICATED_WORKTREE = "DEDICATED_WORKTREE"
    CANCEL = "CANCEL"


class WorkspaceKind(str, Enum):
    CURRENT_CHECKOUT = "CURRENT_CHECKOUT"
    DEDICATED_WORKTREE = "DEDICATED_WORKTREE"


class IssueStatus(str, Enum):
    PLANNED = "PLANNED"
    PENDING = "PENDING"
    READY = "READY"
    IN_DEVELOPMENT = "IN_DEVELOPMENT"
    IN_REVIEW = "IN_REVIEW"
    IN_QA = "IN_QA"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class ChangeKind(str, Enum):
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"
    RENAMED = "RENAMED"
    UNTRACKED = "UNTRACKED"


class CriterionImplementationStatus(str, Enum):
    IMPLEMENTED = "IMPLEMENTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class ReworkResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNRESOLVED = "UNRESOLVED"


class ApprovalKind(str, Enum):
    COMMAND = "COMMAND"
    FILE_CHANGE = "FILE_CHANGE"
    PERMISSIONS = "PERMISSIONS"
    TOOL_INPUT = "TOOL_INPUT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PlanningPackageRef:
    root: str
    prd_hash: str
    issue_set_hash: str


@dataclass(frozen=True)
class WorkspaceBaselineEntry:
    path: str
    kind: ChangeKind
    content_hash: str | None


@dataclass(frozen=True)
class WorkspaceRef:
    kind: WorkspaceKind
    repository_root: str
    path: str
    branch: str | None
    base_commit: str
    baseline: tuple[WorkspaceBaselineEntry, ...] = ()


@dataclass(frozen=True)
class IssueRuntimeState:
    issue_id: IssueId
    status: IssueStatus
    current_step: StepInstanceId | None = None
    repository_baseline: tuple[WorkspaceBaselineEntry, ...] | None = None
    owned_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityProfile:
    capabilities: tuple[CapabilityId, ...]


@dataclass(frozen=True)
class ContextManifestRef:
    path: str
    content_hash: str


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    content_hash: str


@dataclass(frozen=True)
class DevelopmentCursor:
    issue_id: IssueId
    position: int
    total: int
    attempt_id: AttemptId
    context_manifest: ContextManifestRef
    thread_id: ExecutionThreadId | None = None
    turn_id: ExecutionTurnId | None = None
    completed_item_ids: tuple[str, ...] = ()
    implementation_result: ArtifactRef | None = None
    approval_request: ArtifactRef | None = None
    transient_retries: int = 0


@dataclass(frozen=True)
class ChangedFile:
    path: str
    kind: ChangeKind


@dataclass(frozen=True)
class CriterionImplementation:
    criterion_id: AcceptanceCriterionId
    status: CriterionImplementationStatus
    evidence: str


@dataclass(frozen=True)
class ReworkResolution:
    rework_id: str
    status: ReworkResolutionStatus
    evidence: str


def validate_rework_resolutions(
    expected_ids: tuple[str, ...],
    resolutions: tuple[ReworkResolution, ...],
) -> None:
    if len(set(expected_ids)) != len(expected_ids):
        raise ValueError("Rework Request item IDs must be unique.")
    actual_ids = tuple(item.rework_id for item in resolutions)
    if len(set(actual_ids)) != len(actual_ids):
        raise ValueError("Rework Resolution item IDs must be unique.")
    if set(actual_ids) != set(expected_ids):
        raise ValueError("Rework Resolutions must cover every requested item exactly.")
    if any(
        item.status
        not in {
            ReworkResolutionStatus.RESOLVED,
            ReworkResolutionStatus.NOT_APPLICABLE,
        }
        for item in resolutions
    ):
        raise ValueError("Development left a rework item unresolved.")


@dataclass(frozen=True)
class ImplementationResult:
    schema: str
    attempt_id: AttemptId
    base_state: str
    result_state: str
    diff_hash: str
    repository_state_hash: str
    changed_files: tuple[ChangedFile, ...]
    criteria: tuple[CriterionImplementation, ...]
    commands: tuple[str, ...]
    rework_resolutions: tuple[ReworkResolution, ...]
    assumptions: tuple[str, ...]
    risks: tuple[str, ...]
    summary: str
