from __future__ import annotations

from dataclasses import replace

import pytest

from devloop.domain.identifiers import (
    AcceptanceCriterionId,
    AttemptId,
    QaCheckId,
    ReviewFindingId,
)
from devloop.domain.review_qa import (
    CheckRequirement,
    FindingDisposition,
    FindingSeverity,
    QaCheck,
    QaCheckKind,
    QaCheckStatus,
    QaResult,
    ReviewFinding,
    ReviewResult,
    qa_outcome,
    review_outcome,
    validate_qa_result,
    validate_review_result,
)
from devloop.domain.run import StepOutcome


def _finding(disposition: FindingDisposition) -> ReviewFinding:
    return ReviewFinding(
        finding_id=ReviewFindingId("RF-001"),
        severity=FindingSeverity.HIGH,
        disposition=disposition,
        title="Incorrect total",
        rationale="The reducer drops the final grocery item.",
        evidence="Line 24 slices the list before summing it.",
        file_path="src/pricing.py",
        line=24,
        expected_behavior="All groceries contribute to the total.",
        acceptance_condition="The total includes every requested grocery item.",
    )


def _check(status: QaCheckStatus) -> QaCheck:
    return QaCheck(
        check_id=QaCheckId("QC-001"),
        criterion_id=AcceptanceCriterionId("AC-ISSUE-001-001"),
        kind=QaCheckKind.TEST,
        requirement=CheckRequirement.REQUIRED,
        status=status,
        command="pytest -q",
        exit_code=0 if status is QaCheckStatus.PASSED else 1,
        duration_ms=25,
        evidence="Focused pricing test result.",
        reason="" if status is QaCheckStatus.PASSED else "The total assertion failed.",
        expected_behavior="All groceries contribute to the total.",
        acceptance_condition="The focused pricing test passes.",
    )


def test_review_outcome_is_derived_only_from_validated_findings() -> None:
    advisory = ReviewResult(
        "devloop.review-result/v1",
        AttemptId("attempt-001"),
        "diff",
        (_finding(FindingDisposition.ADVISORY),),
        "ok",
    )
    must_fix = ReviewResult(
        "devloop.review-result/v1",
        AttemptId("attempt-001"),
        "diff",
        (_finding(FindingDisposition.MUST_FIX),),
        "fix",
    )

    validate_review_result(advisory)
    validate_review_result(must_fix)

    assert review_outcome(advisory) is StepOutcome.SUCCEEDED
    assert review_outcome(must_fix) is StepOutcome.CHANGES_REQUESTED


def test_review_rejects_unsupported_or_duplicate_findings() -> None:
    unsupported = _finding(FindingDisposition.MUST_FIX)
    unsupported = ReviewFinding(
        unsupported.finding_id,
        unsupported.severity,
        unsupported.disposition,
        unsupported.title,
        unsupported.rationale,
        "",
        unsupported.file_path,
        unsupported.line,
        unsupported.expected_behavior,
        unsupported.acceptance_condition,
    )

    with pytest.raises(ValueError, match="evidence"):
        validate_review_result(
            ReviewResult(
                "devloop.review-result/v1", AttemptId("attempt-001"), "diff", (unsupported,), "bad"
            )
        )
    with pytest.raises(ValueError, match="unique"):
        validate_review_result(
            ReviewResult(
                "devloop.review-result/v1",
                AttemptId("attempt-001"),
                "diff",
                (_finding(FindingDisposition.ADVISORY), _finding(FindingDisposition.ADVISORY)),
                "bad",
            )
        )


def test_review_must_fix_takes_precedence_over_a_blocked_reason() -> None:
    result = ReviewResult(
        "devloop.review-result/v1",
        AttemptId("attempt-001"),
        "diff",
        (_finding(FindingDisposition.MUST_FIX),),
        "A supported must-fix finding was recorded before review became blocked.",
        blocked_reason="Additional repository evidence was unavailable.",
    )

    validate_review_result(result)

    assert review_outcome(result) is StepOutcome.CHANGES_REQUESTED


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (QaCheckStatus.PASSED, StepOutcome.SUCCEEDED),
        (QaCheckStatus.FAILED, StepOutcome.CHANGES_REQUESTED),
        (QaCheckStatus.BLOCKED, StepOutcome.BLOCKED),
        (QaCheckStatus.SKIPPED, StepOutcome.BLOCKED),
        (QaCheckStatus.UNKNOWN, StepOutcome.BLOCKED),
    ],
)
def test_qa_outcome_is_derived_from_required_checks(
    status: QaCheckStatus,
    expected: StepOutcome,
) -> None:
    result = QaResult(
        "devloop.qa-result/v1", AttemptId("attempt-001"), "diff", (_check(status),), (), "qa"
    )
    criteria = (AcceptanceCriterionId("AC-ISSUE-001-001"),)

    validate_qa_result(result, criteria)

    assert qa_outcome(result) is expected


def test_qa_requires_at_least_one_required_check_per_acceptance_criterion() -> None:
    result = QaResult(
        "devloop.qa-result/v1",
        AttemptId("attempt-001"),
        "diff",
        (_check(QaCheckStatus.PASSED),),
        (),
        "qa",
    )

    with pytest.raises(ValueError, match="required QA Check"):
        validate_qa_result(
            result,
            (
                AcceptanceCriterionId("AC-ISSUE-001-001"),
                AcceptanceCriterionId("AC-ISSUE-001-002"),
            ),
        )


def test_qa_rejects_passed_executable_check_with_nonzero_exit_code() -> None:
    result = QaResult(
        "devloop.qa-result/v1",
        AttemptId("attempt-001"),
        "diff",
        (replace(_check(QaCheckStatus.PASSED), exit_code=1),),
        (),
        "qa",
    )

    with pytest.raises(ValueError, match="exit code"):
        validate_qa_result(result, (AcceptanceCriterionId("AC-ISSUE-001-001"),))


@pytest.mark.parametrize(
    ("command", "exit_code"),
    [
        (None, 0),
        ("pytest -q", None),
    ],
)
def test_qa_requires_execution_data_for_passed_executable_checks(
    command: str | None,
    exit_code: int | None,
) -> None:
    result = QaResult(
        "devloop.qa-result/v1",
        AttemptId("attempt-001"),
        "diff",
        (replace(_check(QaCheckStatus.PASSED), command=command, exit_code=exit_code),),
        (),
        "qa",
    )

    with pytest.raises(ValueError, match="execution data"):
        validate_qa_result(result, (AcceptanceCriterionId("AC-ISSUE-001-001"),))


@pytest.mark.parametrize(
    ("command", "exit_code"),
    [
        (None, 1),
        ("pytest -q", None),
    ],
)
def test_qa_requires_execution_data_for_failed_executable_checks(
    command: str | None,
    exit_code: int | None,
) -> None:
    result = QaResult(
        "devloop.qa-result/v1",
        AttemptId("attempt-001"),
        "diff",
        (
            replace(
                _check(QaCheckStatus.FAILED),
                command=command,
                exit_code=exit_code,
            ),
        ),
        (),
        "qa",
    )

    with pytest.raises(ValueError, match="execution data"):
        validate_qa_result(result, (AcceptanceCriterionId("AC-ISSUE-001-001"),))


def test_qa_source_state_drift_blocks_even_when_required_checks_pass() -> None:
    result = QaResult(
        "devloop.qa-result/v1",
        AttemptId("attempt-001"),
        "diff",
        (_check(QaCheckStatus.PASSED),),
        (),
        "qa",
        source_state_changed=True,
        state_change_evidence="QA changed src/pricing.py.",
    )
    criteria = (AcceptanceCriterionId("AC-ISSUE-001-001"),)

    validate_qa_result(result, criteria)

    assert qa_outcome(result) is StepOutcome.BLOCKED


def test_optional_qa_failure_requires_a_residual_risk() -> None:
    required = _check(QaCheckStatus.PASSED)
    optional = replace(
        _check(QaCheckStatus.FAILED),
        check_id=QaCheckId("QC-002"),
        requirement=CheckRequirement.OPTIONAL,
    )
    result = QaResult(
        "devloop.qa-result/v1",
        AttemptId("attempt-001"),
        "diff",
        (required, optional),
        (),
        "qa",
    )

    with pytest.raises(ValueError, match="residual risk"):
        validate_qa_result(result, (AcceptanceCriterionId("AC-ISSUE-001-001"),))
