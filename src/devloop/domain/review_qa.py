from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath

from devloop.domain.development import ArtifactRef
from devloop.domain.identifiers import (
    AcceptanceCriterionId,
    AttemptId,
    ExecutionThreadId,
    ExecutionTurnId,
    IssueId,
    QaCheckId,
    ReviewFindingId,
)
from devloop.domain.outcomes import StepOutcome

REVIEW_RESULT_SCHEMA = "devloop.review-result/v1"
QA_RESULT_SCHEMA = "devloop.qa-result/v1"
REWORK_REQUEST_SCHEMA = "devloop.rework-request/v1"


class FindingSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class FindingDisposition(str, Enum):
    MUST_FIX = "MUST_FIX"
    ADVISORY = "ADVISORY"


class QaCheckKind(str, Enum):
    BUILD = "BUILD"
    TEST = "TEST"
    LINT = "LINT"
    TYPE_CHECK = "TYPE_CHECK"
    SECURITY = "SECURITY"
    MANUAL_INSPECTION = "MANUAL_INSPECTION"


class CheckRequirement(str, Enum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"


class QaCheckStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"
    UNKNOWN = "UNKNOWN"


_EXECUTABLE_QA_KINDS = {
    QaCheckKind.BUILD,
    QaCheckKind.TEST,
    QaCheckKind.LINT,
    QaCheckKind.TYPE_CHECK,
    QaCheckKind.SECURITY,
}


class ReworkSource(str, Enum):
    CODE_REVIEW = "CODE_REVIEW"
    QA = "QA"


@dataclass(frozen=True)
class ReviewFinding:
    finding_id: ReviewFindingId
    severity: FindingSeverity
    disposition: FindingDisposition
    title: str
    rationale: str
    evidence: str
    file_path: str
    line: int | None
    expected_behavior: str
    acceptance_condition: str


@dataclass(frozen=True)
class ReviewResult:
    schema: str
    attempt_id: AttemptId
    implementation_diff_hash: str
    findings: tuple[ReviewFinding, ...]
    summary: str
    blocked_reason: str | None = None


@dataclass(frozen=True)
class QaCheck:
    check_id: QaCheckId
    criterion_id: AcceptanceCriterionId
    kind: QaCheckKind
    requirement: CheckRequirement
    status: QaCheckStatus
    command: str | None
    exit_code: int | None
    duration_ms: int
    evidence: str
    reason: str
    expected_behavior: str
    acceptance_condition: str


@dataclass(frozen=True)
class QaResult:
    schema: str
    attempt_id: AttemptId
    implementation_diff_hash: str
    checks: tuple[QaCheck, ...]
    residual_risks: tuple[str, ...]
    summary: str
    source_state_changed: bool = False
    state_change_evidence: str = ""


@dataclass(frozen=True)
class ReworkItem:
    item_id: str
    evidence: str
    expected_behavior: str
    acceptance_condition: str


@dataclass(frozen=True)
class ReworkRequest:
    schema: str
    source: ReworkSource
    attempt_id: AttemptId
    items: tuple[ReworkItem, ...]


@dataclass(frozen=True)
class ReviewCursor:
    issue_id: IssueId
    attempt_id: AttemptId
    input_manifest: ArtifactRef
    thread_id: ExecutionThreadId | None = None
    turn_id: ExecutionTurnId | None = None
    completed_item_ids: tuple[str, ...] = ()
    review_result: ArtifactRef | None = None
    rework_request: ArtifactRef | None = None
    transient_retries: int = 0


@dataclass(frozen=True)
class QaCursor:
    issue_id: IssueId
    attempt_id: AttemptId
    input_manifest: ArtifactRef
    thread_id: ExecutionThreadId | None = None
    turn_id: ExecutionTurnId | None = None
    completed_item_ids: tuple[str, ...] = ()
    qa_result: ArtifactRef | None = None
    rework_request: ArtifactRef | None = None
    transient_retries: int = 0


def validate_review_result(result: ReviewResult) -> None:
    if result.schema != REVIEW_RESULT_SCHEMA:
        raise ValueError("Review Result schema is unsupported.")
    if not result.implementation_diff_hash:
        raise ValueError("Review Result implementation diff hash is missing.")
    _required(result.summary, "Review Result summary")
    if result.blocked_reason is not None:
        _required(result.blocked_reason, "Review Result blocked reason")
    finding_ids = [item.finding_id for item in result.findings]
    if len(finding_ids) != len(set(finding_ids)):
        raise ValueError("Review Finding IDs must be unique.")
    for finding in result.findings:
        _required(finding.title, "Review Finding title")
        _required(finding.rationale, "Review Finding rationale")
        _required(finding.evidence, "Review Finding evidence")
        _relative_path(finding.file_path)
        if finding.line is not None and finding.line < 1:
            raise ValueError("Review Finding line must be positive when supplied.")
        _required(finding.expected_behavior, "Review Finding expected behavior")
        _required(finding.acceptance_condition, "Review Finding acceptance condition")


def review_outcome(result: ReviewResult) -> StepOutcome:
    validate_review_result(result)
    if any(item.disposition is FindingDisposition.MUST_FIX for item in result.findings):
        return StepOutcome.CHANGES_REQUESTED
    if result.blocked_reason is not None:
        return StepOutcome.BLOCKED
    return StepOutcome.SUCCEEDED


def validate_qa_result(
    result: QaResult,
    criterion_ids: tuple[AcceptanceCriterionId, ...],
) -> None:
    if result.schema != QA_RESULT_SCHEMA:
        raise ValueError("QA Result schema is unsupported.")
    if not result.implementation_diff_hash:
        raise ValueError("QA Result implementation diff hash is missing.")
    _required(result.summary, "QA Result summary")
    check_ids = [item.check_id for item in result.checks]
    if len(check_ids) != len(set(check_ids)):
        raise ValueError("QA Check IDs must be unique.")
    expected = set(criterion_ids)
    for check in result.checks:
        if check.criterion_id not in expected:
            raise ValueError("QA Check maps to an unknown acceptance criterion.")
        if check.duration_ms < 0:
            raise ValueError("QA Check duration cannot be negative.")
        if check.status is QaCheckStatus.PASSED and check.exit_code not in {None, 0}:
            raise ValueError("A passed QA Check cannot have a nonzero exit code.")
        if (
            check.status in {QaCheckStatus.PASSED, QaCheckStatus.FAILED}
            and check.kind in _EXECUTABLE_QA_KINDS
            and (
                check.command is None
                or not check.command.strip()
                or check.exit_code is None
            )
        ):
            raise ValueError("A terminal executable QA Check requires complete execution data.")
        _required(check.evidence, "QA Check evidence")
        _required(check.expected_behavior, "QA Check expected behavior")
        _required(check.acceptance_condition, "QA Check acceptance condition")
        if check.status is not QaCheckStatus.PASSED:
            _required(check.reason, "QA Check reason")
    required_coverage = {
        item.criterion_id for item in result.checks if item.requirement is CheckRequirement.REQUIRED
    }
    if required_coverage != expected:
        raise ValueError("Every acceptance criterion requires at least one required QA Check.")
    optional_risks = {
        QaCheckStatus.FAILED,
        QaCheckStatus.BLOCKED,
        QaCheckStatus.SKIPPED,
        QaCheckStatus.UNKNOWN,
    }
    if any(
        item.requirement is CheckRequirement.OPTIONAL and item.status in optional_risks
        for item in result.checks
    ) and not result.residual_risks:
        raise ValueError("Optional QA failures require a residual risk.")
    if result.source_state_changed:
        _required(result.state_change_evidence, "QA source-state change evidence")


def qa_outcome(result: QaResult) -> StepOutcome:
    if result.source_state_changed:
        return StepOutcome.BLOCKED
    required = tuple(
        item for item in result.checks if item.requirement is CheckRequirement.REQUIRED
    )
    if any(item.status is QaCheckStatus.FAILED for item in required):
        return StepOutcome.CHANGES_REQUESTED
    blocking = {
        QaCheckStatus.PENDING,
        QaCheckStatus.RUNNING,
        QaCheckStatus.BLOCKED,
        QaCheckStatus.SKIPPED,
        QaCheckStatus.UNKNOWN,
    }
    if any(item.status in blocking for item in required):
        return StepOutcome.BLOCKED
    return StepOutcome.SUCCEEDED


def _required(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} is missing.")


def _relative_path(value: str) -> None:
    path = PurePosixPath(value.replace("\\", "/"))
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError("Review Finding file path must be repository-relative.")
