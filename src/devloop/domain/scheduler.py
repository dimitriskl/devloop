from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from devloop.domain.development import ArtifactRef, IssueRuntimeState, IssueStatus
from devloop.domain.identifiers import ExecutionThreadId, IssueId, StepInstanceId
from devloop.domain.outcomes import StepOutcome
from devloop.domain.planning import PlannedIssue, PlanningPackage


class AttemptStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class DependencyReadiness(str, Enum):
    READY = "READY"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class RetryPolicy:
    max_rework_cycles_per_issue: int
    max_transient_backend_retries: int

    def __post_init__(self) -> None:
        for value in (
            self.max_rework_cycles_per_issue,
            self.max_transient_backend_retries,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                raise ValueError("Workflow retry limits must be integers from 0 through 100.")


TRANSIENT_RETRY_BASE_SECONDS = 0.25
TRANSIENT_RETRY_MAX_SECONDS = 2.0


def transient_retry_delays(policy: RetryPolicy) -> tuple[float, ...]:
    return tuple(
        min(TRANSIENT_RETRY_BASE_SECONDS * (2**index), TRANSIENT_RETRY_MAX_SECONDS)
        for index in range(policy.max_transient_backend_retries)
    )


@dataclass(frozen=True)
class IssueAttemptRecord:
    issue_id: IssueId
    attempt_number: int
    status: AttemptStatus
    outcome: StepOutcome
    implementation: ArtifactRef | None
    review: ArtifactRef | None
    qa_result: ArtifactRef | None
    rework_request: ArtifactRef | None
    development_thread: ExecutionThreadId | None = None
    review_thread: ExecutionThreadId | None = None
    qa_thread: ExecutionThreadId | None = None


@dataclass(frozen=True)
class IssueBoardRow:
    issue_id: IssueId
    position: int
    title: str
    status: IssueStatus
    dependency_readiness: DependencyReadiness
    current_step: StepInstanceId | None
    attempt_number: int
    blocking_dependencies: tuple[IssueId, ...]


@dataclass(frozen=True)
class CompletedResultSets:
    implementations: tuple[ArtifactRef, ...]
    reviews: tuple[ArtifactRef, ...]
    qa_results: tuple[ArtifactRef, ...]


def refresh_issue_states(
    package: PlanningPackage,
    states: tuple[IssueRuntimeState, ...],
) -> tuple[IssueRuntimeState, ...]:
    by_id = {item.issue_id: item for item in states}
    result: list[IssueRuntimeState] = []
    for issue in package.issues:
        existing = by_id.get(issue.issue_id)
        status = IssueStatus.PLANNED if existing is None else existing.status
        if status in {
            IssueStatus.IN_DEVELOPMENT,
            IssueStatus.IN_REVIEW,
            IssueStatus.IN_QA,
            IssueStatus.CHANGES_REQUESTED,
            IssueStatus.BLOCKED,
            IssueStatus.FAILED,
            IssueStatus.COMPLETED,
        }:
            result.append(
                IssueRuntimeState(issue.issue_id, status)
                if existing is None
                else replace(existing, status=status)
            )
            continue
        dependencies_complete = all(
            by_id.get(item) is not None
            and by_id[item].status is IssueStatus.COMPLETED
            for item in issue.dependencies
        )
        next_status = IssueStatus.READY if dependencies_complete else IssueStatus.PENDING
        result.append(
            IssueRuntimeState(issue.issue_id, next_status)
            if existing is None
            else replace(existing, status=next_status)
        )
    return tuple(result)


def select_next_ready_issue(
    package: PlanningPackage,
    states: tuple[IssueRuntimeState, ...],
) -> PlannedIssue | None:
    by_id = {item.issue_id: item for item in states}
    return next(
        (
            item
            for item in package.issues
            if by_id.get(item.issue_id) is not None
            and by_id[item.issue_id].status is IssueStatus.READY
        ),
        None,
    )


def next_rework_attempt_number(
    policy: RetryPolicy,
    *,
    current_attempt_number: int,
    changes_requested_attempts: int | None = None,
) -> int | None:
    if current_attempt_number < 1:
        raise ValueError("Attempt number must be positive.")
    if changes_requested_attempts is None:
        rework_cycles_used = current_attempt_number - 1
        if rework_cycles_used >= policy.max_rework_cycles_per_issue:
            return None
    else:
        if changes_requested_attempts < 1:
            raise ValueError("Changes-requested attempt count must be positive.")
        if changes_requested_attempts > policy.max_rework_cycles_per_issue:
            return None
    return current_attempt_number + 1


def validate_attempt_record(record: IssueAttemptRecord) -> None:
    if record.attempt_number < 1:
        raise ValueError("Attempt number must be positive.")
    expected_outcome = {
        AttemptStatus.CHANGES_REQUESTED: StepOutcome.CHANGES_REQUESTED,
        AttemptStatus.BLOCKED: StepOutcome.BLOCKED,
        AttemptStatus.FAILED: StepOutcome.FAILED,
        AttemptStatus.COMPLETED: StepOutcome.SUCCEEDED,
    }
    if record.status is AttemptStatus.ACTIVE:
        raise ValueError("Active attempts cannot be archived.")
    if expected_outcome[record.status] is not record.outcome:
        raise ValueError("Attempt lifecycle and outcome are incompatible.")
    if record.status is AttemptStatus.COMPLETED and (
        record.implementation is None or record.review is None or record.qa_result is None
    ):
        raise ValueError("A completed attempt requires implementation, review, and QA results.")
    if record.status is AttemptStatus.COMPLETED:
        threads = (
            record.development_thread,
            record.review_thread,
            record.qa_thread,
        )
        if any(item is None for item in threads) or len(set(threads)) != len(threads):
            raise ValueError("A completed attempt requires distinct phase threads.")
    if record.status is AttemptStatus.CHANGES_REQUESTED and record.rework_request is None:
        raise ValueError("A changes-requested attempt requires a Rework Request.")


def validate_attempt_history(attempts: tuple[IssueAttemptRecord, ...]) -> None:
    identities: set[tuple[IssueId, int]] = set()
    threads: set[ExecutionThreadId] = set()
    for record in attempts:
        validate_attempt_record(record)
        identity = (record.issue_id, record.attempt_number)
        if identity in identities:
            raise ValueError("Issue attempt identities must be unique.")
        identities.add(identity)
        for thread in (
            record.development_thread,
            record.review_thread,
            record.qa_thread,
        ):
            if thread is None:
                continue
            if thread in threads:
                raise ValueError("Execution Threads cannot be reused across Issue attempts.")
            threads.add(thread)


def archived_execution_threads(
    attempts: tuple[IssueAttemptRecord, ...],
) -> frozenset[ExecutionThreadId]:
    return frozenset(
        thread
        for record in attempts
        for thread in (
            record.development_thread,
            record.review_thread,
            record.qa_thread,
        )
        if thread is not None
    )


def completed_result_sets(
    attempts: tuple[IssueAttemptRecord, ...],
) -> CompletedResultSets:
    completed = tuple(item for item in attempts if item.status is AttemptStatus.COMPLETED)
    for item in completed:
        validate_attempt_record(item)
    return CompletedResultSets(
        tuple(item.implementation for item in completed if item.implementation is not None),
        tuple(item.review for item in completed if item.review is not None),
        tuple(item.qa_result for item in completed if item.qa_result is not None),
    )


def project_issue_board(
    package: PlanningPackage,
    states: tuple[IssueRuntimeState, ...],
    attempts: tuple[IssueAttemptRecord, ...],
) -> tuple[IssueBoardRow, ...]:
    by_id = {item.issue_id: item for item in states}
    attempts_by_issue: dict[IssueId, int] = {}
    for attempt in attempts:
        attempts_by_issue[attempt.issue_id] = max(
            attempts_by_issue.get(attempt.issue_id, 0), attempt.attempt_number
        )
    rows: list[IssueBoardRow] = []
    for issue in package.issues:
        blockers = tuple(
            dependency
            for dependency in issue.dependencies
            if by_id.get(dependency) is None
            or by_id[dependency].status is not IssueStatus.COMPLETED
        )
        blocker_statuses = {
            None if by_id.get(item) is None else by_id[item].status for item in blockers
        }
        if not blockers:
            readiness = DependencyReadiness.READY
        elif IssueStatus.FAILED in blocker_statuses:
            readiness = DependencyReadiness.FAILED
        elif IssueStatus.BLOCKED in blocker_statuses:
            readiness = DependencyReadiness.BLOCKED
        else:
            readiness = DependencyReadiness.WAITING
        state = by_id.get(issue.issue_id)
        status = IssueStatus.PENDING if state is None else state.status
        archived_attempt = attempts_by_issue.get(issue.issue_id, 0)
        attempt_number = (
            archived_attempt + 1
            if status
            in {
                IssueStatus.IN_DEVELOPMENT,
                IssueStatus.IN_REVIEW,
                IssueStatus.IN_QA,
            }
            else archived_attempt
        )
        rows.append(
            IssueBoardRow(
                issue.issue_id,
                issue.position,
                issue.title,
                status,
                readiness,
                state.current_step
                if state is not None
                and status in {IssueStatus.BLOCKED, IssueStatus.FAILED}
                else _current_step(status),
                attempt_number,
                blockers,
            )
        )
    return tuple(rows)


def _current_step(status: IssueStatus) -> StepInstanceId | None:
    values = {
        IssueStatus.IN_DEVELOPMENT: StepInstanceId("development"),
        IssueStatus.CHANGES_REQUESTED: StepInstanceId("development"),
        IssueStatus.IN_REVIEW: StepInstanceId("code-review"),
        IssueStatus.IN_QA: StepInstanceId("qa"),
    }
    return values.get(status)
