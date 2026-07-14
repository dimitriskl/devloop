from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from devloop.domain.development import ArtifactRef, IssueRuntimeState, IssueStatus
from devloop.domain.identifiers import ExecutionThreadId, IssueId, StepInstanceId
from devloop.domain.outcomes import StepOutcome
from devloop.domain.planning import PlannedIssue, PlanningPackage
from devloop.domain.scheduler import (
    AttemptStatus,
    IssueAttemptRecord,
    RetryPolicy,
    completed_result_sets,
    next_rework_attempt_number,
    project_issue_board,
    refresh_issue_states,
    select_next_ready_issue,
    transient_retry_delays,
    validate_attempt_history,
    validate_attempt_record,
)


def _issue(value: int, dependencies: tuple[int, ...] = ()) -> PlannedIssue:
    issue_id = IssueId(f"ISSUE-{value:03}")
    return PlannedIssue(
        issue_id,
        value,
        f"Issue {value}",
        f"ISSUE-{value:03}-issue-{value}.md",
        f"hash-{value}",
        (),
        tuple(IssueId(f"ISSUE-{item:03}") for item in dependencies),
        (),
        f"Issue {value}",
    )


def _package(*issues: PlannedIssue) -> PlanningPackage:
    return PlanningPackage("root", "prd", "prd-hash", "issue-hash", tuple(issues))


def test_dependency_chain_becomes_ready_only_after_every_dependency_completes() -> None:
    package = _package(_issue(1), _issue(2, (1,)), _issue(3, (1, 2)))
    states = tuple(IssueRuntimeState(item.issue_id, IssueStatus.PLANNED) for item in package.issues)

    first = refresh_issue_states(package, states)
    selected = select_next_ready_issue(package, first)
    assert selected is not None
    assert selected.issue_id == IssueId("ISSUE-001")

    second = refresh_issue_states(
        package,
        tuple(
            replace(item, status=IssueStatus.COMPLETED)
            if item.issue_id == IssueId("ISSUE-001")
            else item
            for item in first
        ),
    )
    selected = select_next_ready_issue(package, second)
    assert selected is not None
    assert selected.issue_id == IssueId("ISSUE-002")
    board = project_issue_board(package, second, ())
    assert board[2].blocking_dependencies == (IssueId("ISSUE-002"),)


def test_blocked_issue_does_not_stop_an_independent_ready_issue() -> None:
    package = _package(_issue(1), _issue(2), _issue(3, (1,)))
    states = (
        IssueRuntimeState(IssueId("ISSUE-001"), IssueStatus.BLOCKED),
        IssueRuntimeState(IssueId("ISSUE-002"), IssueStatus.PLANNED),
        IssueRuntimeState(IssueId("ISSUE-003"), IssueStatus.PLANNED),
    )

    refreshed = refresh_issue_states(package, states)

    selected = select_next_ready_issue(package, refreshed)
    assert selected is not None
    assert selected.issue_id == IssueId("ISSUE-002")
    board = project_issue_board(package, refreshed, ())
    assert board[2].blocking_dependencies == (IssueId("ISSUE-001"),)


def test_issue_board_shows_the_active_attempt_after_archived_rework() -> None:
    package = _package(_issue(1))
    archived = IssueAttemptRecord(
        IssueId("ISSUE-001"),
        1,
        AttemptStatus.CHANGES_REQUESTED,
        StepOutcome.CHANGES_REQUESTED,
        ArtifactRef("implementation.json", "implementation-hash"),
        ArtifactRef("review.json", "review-hash"),
        None,
        ArtifactRef("rework.json", "rework-hash"),
    )

    board = project_issue_board(
        package,
        (IssueRuntimeState(IssueId("ISSUE-001"), IssueStatus.IN_DEVELOPMENT),),
        (archived,),
    )

    assert board[0].attempt_number == 2
    assert board[0].current_step == StepInstanceId("development")


def test_issue_board_preserves_terminal_step_provenance() -> None:
    package = _package(_issue(1), _issue(2))
    states = (
        IssueRuntimeState(
            IssueId("ISSUE-001"),
            IssueStatus.BLOCKED,
            StepInstanceId("code-review"),
        ),
        IssueRuntimeState(
            IssueId("ISSUE-002"),
            IssueStatus.FAILED,
            StepInstanceId("qa"),
        ),
    )

    board = project_issue_board(package, states, ())

    assert board[0].current_step == StepInstanceId("code-review")
    assert board[1].current_step == StepInstanceId("qa")


def test_rework_attempts_are_bounded_by_versioned_policy() -> None:
    policy = RetryPolicy(max_rework_cycles_per_issue=2, max_transient_backend_retries=1)

    assert next_rework_attempt_number(policy, current_attempt_number=1) == 2
    assert next_rework_attempt_number(policy, current_attempt_number=2) == 3
    assert next_rework_attempt_number(policy, current_attempt_number=3) is None
    assert transient_retry_delays(policy) == (0.25,)


def test_user_authorized_attempts_do_not_consume_automatic_rework_budget() -> None:
    policy = RetryPolicy(max_rework_cycles_per_issue=2, max_transient_backend_retries=0)

    assert next_rework_attempt_number(
        policy,
        current_attempt_number=2,
        changes_requested_attempts=1,
    ) == 3
    assert next_rework_attempt_number(
        policy,
        current_attempt_number=3,
        changes_requested_attempts=2,
    ) == 4
    assert (
        next_rework_attempt_number(
            policy,
            current_attempt_number=4,
            changes_requested_attempts=3,
        )
        is None
    )


def test_attempt_history_rejects_thread_reuse_across_attempts() -> None:
    shared = ExecutionThreadId("shared-development-thread")
    first = IssueAttemptRecord(
        IssueId("ISSUE-001"),
        1,
        AttemptStatus.FAILED,
        StepOutcome.FAILED,
        None,
        None,
        None,
        None,
        shared,
    )
    second = IssueAttemptRecord(
        IssueId("ISSUE-001"),
        2,
        AttemptStatus.FAILED,
        StepOutcome.FAILED,
        None,
        None,
        None,
        None,
        shared,
    )

    with pytest.raises(ValueError, match="cannot be reused"):
        validate_attempt_history((first, second))


def test_illegal_completed_attempt_without_all_results_is_rejected() -> None:
    record = IssueAttemptRecord(
        IssueId("ISSUE-001"),
        1,
        AttemptStatus.COMPLETED,
        StepOutcome.SUCCEEDED,
        ArtifactRef("implementation.json", "implementation-hash"),
        None,
        None,
        None,
    )

    with pytest.raises(ValueError, match="completed attempt"):
        validate_attempt_record(record)


def test_aggregates_include_only_completed_attempt_outputs() -> None:
    implementation = ArtifactRef("implementation.json", "implementation-hash")
    review = ArtifactRef("review.json", "review-hash")
    qa = ArtifactRef("qa.json", "qa-hash")
    completed = IssueAttemptRecord(
        IssueId("ISSUE-001"),
        1,
        AttemptStatus.COMPLETED,
        StepOutcome.SUCCEEDED,
        implementation,
        review,
        qa,
        None,
        ExecutionThreadId("development-thread"),
        ExecutionThreadId("review-thread"),
        ExecutionThreadId("qa-thread"),
    )
    blocked = IssueAttemptRecord(
        IssueId("ISSUE-002"),
        1,
        AttemptStatus.BLOCKED,
        StepOutcome.BLOCKED,
        implementation,
        review,
        None,
        None,
    )

    result = completed_result_sets((completed, blocked))

    assert result.implementations == (implementation,)
    assert result.reviews == (review,)
    assert result.qa_results == (qa,)


@given(
    max_cycles=st.integers(min_value=0, max_value=20),
    attempt_number=st.integers(min_value=1, max_value=30),
)
def test_property_retry_never_exceeds_configured_bound(
    max_cycles: int,
    attempt_number: int,
) -> None:
    policy = RetryPolicy(max_cycles, 0)

    next_number = next_rework_attempt_number(
        policy, current_attempt_number=attempt_number
    )

    if next_number is not None:
        assert next_number == attempt_number + 1
        assert next_number <= max_cycles + 1
    else:
        assert attempt_number >= max_cycles + 1


@given(
    size=st.integers(min_value=1, max_value=20),
    completed=st.integers(min_value=0, max_value=20),
)
def test_property_chain_selects_only_the_first_incomplete_dependency_ready_issue(
    size: int,
    completed: int,
) -> None:
    completed = min(completed, size)
    package = _package(
        *(_issue(index, () if index == 1 else (index - 1,)) for index in range(1, size + 1))
    )
    states = tuple(
        IssueRuntimeState(
            issue.issue_id,
            IssueStatus.COMPLETED if issue.position <= completed else IssueStatus.PLANNED,
        )
        for issue in package.issues
    )

    refreshed = refresh_issue_states(package, states)
    selected = select_next_ready_issue(package, refreshed)

    if completed == size:
        assert selected is None
    else:
        assert selected is not None
        assert selected.position == completed + 1


@given(
    size=st.integers(min_value=1, max_value=8),
    edges=st.sets(
        st.tuples(
            st.integers(min_value=1, max_value=8),
            st.integers(min_value=1, max_value=8),
        ),
        max_size=28,
    ),
    completed_positions=st.sets(st.integers(min_value=1, max_value=8)),
)
def test_property_dependency_dag_exposes_only_nodes_with_completed_parents(
    size: int,
    edges: set[tuple[int, int]],
    completed_positions: set[int],
) -> None:
    dependencies = {
        child: tuple(
            sorted(
                parent
                for parent, candidate_child in edges
                if candidate_child == child and parent < child and parent <= size
            )
        )
        for child in range(1, size + 1)
    }
    package = _package(
        *(_issue(position, dependencies[position]) for position in range(1, size + 1))
    )
    states = tuple(
        IssueRuntimeState(
            issue.issue_id,
            IssueStatus.COMPLETED
            if issue.position in completed_positions
            else IssueStatus.PLANNED,
        )
        for issue in package.issues
    )

    refreshed = refresh_issue_states(package, states)
    by_id = {item.issue_id: item.status for item in refreshed}
    expected_ready = [
        issue
        for issue in package.issues
        if issue.position not in completed_positions
        and all(parent in completed_positions for parent in dependencies[issue.position])
    ]

    assert all(
        all(by_id[dependency] is IssueStatus.COMPLETED for dependency in issue.dependencies)
        for issue in package.issues
        if by_id[issue.issue_id] is IssueStatus.READY
    )
    selected = select_next_ready_issue(package, refreshed)
    assert selected == (expected_ready[0] if expected_ready else None)


@given(
    status=st.sampled_from(
        [
            AttemptStatus.CHANGES_REQUESTED,
            AttemptStatus.BLOCKED,
            AttemptStatus.FAILED,
            AttemptStatus.COMPLETED,
        ]
    ),
    outcome=st.sampled_from(
        [
            StepOutcome.SUCCEEDED,
            StepOutcome.CHANGES_REQUESTED,
            StepOutcome.BLOCKED,
            StepOutcome.FAILED,
        ]
    ),
)
def test_property_illegal_lifecycle_outcome_combinations_are_rejected(
    status: AttemptStatus,
    outcome: StepOutcome,
) -> None:
    expected = {
        AttemptStatus.CHANGES_REQUESTED: StepOutcome.CHANGES_REQUESTED,
        AttemptStatus.BLOCKED: StepOutcome.BLOCKED,
        AttemptStatus.FAILED: StepOutcome.FAILED,
        AttemptStatus.COMPLETED: StepOutcome.SUCCEEDED,
    }
    implementation = ArtifactRef("implementation.json", "implementation-hash")
    review = ArtifactRef("review.json", "review-hash")
    qa = ArtifactRef("qa.json", "qa-hash")
    rework = ArtifactRef("rework.json", "rework-hash")
    record = IssueAttemptRecord(
        IssueId("ISSUE-001"),
        1,
        status,
        outcome,
        implementation,
        review,
        qa if status is AttemptStatus.COMPLETED else None,
        rework if status is AttemptStatus.CHANGES_REQUESTED else None,
        ExecutionThreadId("development-thread")
        if status is AttemptStatus.COMPLETED
        else None,
        ExecutionThreadId("review-thread") if status is AttemptStatus.COMPLETED else None,
        ExecutionThreadId("qa-thread") if status is AttemptStatus.COMPLETED else None,
    )

    if outcome is expected[status]:
        validate_attempt_record(record)
    else:
        with pytest.raises(ValueError, match="incompatible"):
            validate_attempt_record(record)
