from __future__ import annotations

import re
from dataclasses import dataclass

_SLASH_COMMAND_ID = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_REGISTERED_ID = re.compile(r"[a-z][a-z0-9.-]{0,126}\Z")
_CONTRACT_ID = re.compile(r"[a-z][a-z0-9./-]{0,126}\Z")
_RUN_ID = re.compile(r"run-[0-9]{8}t[0-9]{6}-[a-f0-9]{12}\Z")
_FEATURE_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_ISSUE_ID = re.compile(r"ISSUE-[0-9]{3,}\Z")
_REQUIREMENT_ID = re.compile(r"REQ-[0-9]{3,}\Z")
_ACCEPTANCE_ID = re.compile(r"AC-ISSUE-[0-9]{3,}-[0-9]{3,}\Z")
_EXECUTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,126}\Z")
_CAPABILITY_ID = re.compile(r"[a-z][a-z0-9.-]{0,126}\Z")
_ATTEMPT_ID = re.compile(r"attempt-[0-9]{3,}\Z")
_REVIEW_FINDING_ID = re.compile(r"RF-[0-9]{3,}\Z")
_QA_CHECK_ID = re.compile(r"QC-[0-9]{3,}\Z")


@dataclass(frozen=True, order=True)
class SlashCommandId:
    """Validated open identity for a registered Slash Command."""

    value: str

    def __post_init__(self) -> None:
        if _SLASH_COMMAND_ID.fullmatch(self.value) is None:
            raise ValueError(
                "Slash Command ID must start with a lowercase letter and contain only "
                "lowercase letters, digits, or hyphens."
            )

    def __str__(self) -> str:
        return self.value


def _validate(value: str, pattern: re.Pattern[str], title: str) -> None:
    if pattern.fullmatch(value) is None:
        raise ValueError(f"Invalid {title}: {value!r}.")


@dataclass(frozen=True, order=True)
class WorkflowRunId:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, _RUN_ID, "Workflow Run ID")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class WorkflowId:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, _REGISTERED_ID, "Workflow ID")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class StepComponentId:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, _REGISTERED_ID, "Step Component ID")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class StepInstanceId:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, _REGISTERED_ID, "Step Instance ID")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class DataContractId:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, _CONTRACT_ID, "Data Contract ID")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class FeatureSlug:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, _FEATURE_SLUG, "Feature Slug")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class IssueId:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, _ISSUE_ID, "Issue ID")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class RequirementId:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, _REQUIREMENT_ID, "Requirement ID")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class AcceptanceCriterionId:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, _ACCEPTANCE_ID, "Acceptance Criterion ID")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class ExecutionThreadId:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, _EXECUTION_ID, "Execution Thread ID")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class ExecutionTurnId:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, _EXECUTION_ID, "Execution Turn ID")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class CapabilityId:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, _CAPABILITY_ID, "Capability ID")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class AttemptId:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, _ATTEMPT_ID, "Attempt ID")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class ReviewFindingId:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, _REVIEW_FINDING_ID, "Review Finding ID")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, order=True)
class QaCheckId:
    value: str

    def __post_init__(self) -> None:
        _validate(self.value, _QA_CHECK_ID, "QA Check ID")

    def __str__(self) -> str:
        return self.value
