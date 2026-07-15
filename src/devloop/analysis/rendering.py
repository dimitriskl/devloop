from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from devloop.domain.identifiers import (
    AcceptanceCriterionId,
    FeatureSlug,
    IssueId,
    RequirementId,
    WorkflowRunId,
)
from devloop.domain.planning import (
    ANALYSIS_ACCEPTANCE_TEXT_MAX_LENGTH,
    ANALYSIS_DRAFT_SCHEMA,
    ANALYSIS_FEATURE_TITLE_MAX_LENGTH,
    ANALYSIS_ISSUE_TITLE_MAX_LENGTH,
    ISSUE_MARKER,
    ISSUE_SECTION_MARKERS,
    PRD_MARKER,
    PRD_SECTION_MARKERS,
    AcceptanceCriterion,
    AnalysisDraft,
    IssueDraft,
    PlanningAuthority,
)

ANALYSIS_CONTENT_SCHEMA = "devloop.analysis-content/v1"
_MAX_SECTION_LENGTH = 250_000
_MAX_LABEL_LENGTH = 200
_MAX_REQUIREMENTS = 500
_MAX_ISSUES = 200
_MAX_CRITERIA = 200
_MACHINE_MARKER_PREFIX = "<!-- devloop:"
_SLUG_SEPARATOR = re.compile(r"[^a-z0-9]+")


class AnalysisContentError(ValueError):
    pass


@dataclass(frozen=True)
class PlanningLabels:
    problem: str
    solution: str
    requirements: str
    description: str
    acceptance: str


@dataclass(frozen=True)
class RequirementContent:
    text: str


@dataclass(frozen=True)
class AcceptanceContent:
    text: str


@dataclass(frozen=True)
class IssueContent:
    title: str
    description: str
    requirement_numbers: tuple[int, ...]
    dependency_numbers: tuple[int, ...]
    acceptance_criteria: tuple[AcceptanceContent, ...]


@dataclass(frozen=True)
class AnalysisContent:
    schema: str
    feature_title: str
    labels: PlanningLabels
    problem: str
    solution: str
    requirements: tuple[RequirementContent, ...]
    issues: tuple[IssueContent, ...]
    revision: int


def parse_analysis_content(payload: Mapping[str, object]) -> AnalysisContent:
    _require_keys(
        payload,
        {
            "schema",
            "feature_title",
            "labels",
            "problem",
            "solution",
            "requirements",
            "issues",
            "revision",
        },
        "analysis content",
    )
    if payload.get("schema") != ANALYSIS_CONTENT_SCHEMA:
        raise AnalysisContentError("Analysis content uses an unsupported schema.")
    labels_value = _mapping(payload.get("labels"), "planning labels")
    _require_keys(
        labels_value,
        {"problem", "solution", "requirements", "description", "acceptance"},
        "planning labels",
    )
    labels = PlanningLabels(
        _content_string(labels_value, "problem", _MAX_LABEL_LENGTH),
        _content_string(labels_value, "solution", _MAX_LABEL_LENGTH),
        _content_string(labels_value, "requirements", _MAX_LABEL_LENGTH),
        _content_string(labels_value, "description", _MAX_LABEL_LENGTH),
        _content_string(labels_value, "acceptance", _MAX_LABEL_LENGTH),
    )
    requirements_value = _bounded_list(
        payload.get("requirements"),
        "requirements",
        minimum=1,
        maximum=_MAX_REQUIREMENTS,
    )
    requirements: list[RequirementContent] = []
    for value in requirements_value:
        requirement = _mapping(value, "requirement")
        _require_keys(requirement, {"text"}, "requirement")
        requirements.append(
            RequirementContent(
                _content_string(requirement, "text", ANALYSIS_ACCEPTANCE_TEXT_MAX_LENGTH)
            )
        )
    issues_value = _bounded_list(
        payload.get("issues"),
        "issues",
        minimum=1,
        maximum=_MAX_ISSUES,
    )
    issues = tuple(
        _parse_issue_content(value, len(requirements), len(issues_value), position)
        for position, value in enumerate(issues_value, start=1)
    )
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise AnalysisContentError("Analysis content revision must be a positive integer.")
    return AnalysisContent(
        ANALYSIS_CONTENT_SCHEMA,
        _content_string(payload, "feature_title", ANALYSIS_FEATURE_TITLE_MAX_LENGTH),
        labels,
        _content_string(payload, "problem", _MAX_SECTION_LENGTH),
        _content_string(payload, "solution", _MAX_SECTION_LENGTH),
        tuple(requirements),
        issues,
        revision,
    )


def render_analysis_content(
    content: AnalysisContent,
    run_id: WorkflowRunId,
) -> AnalysisDraft:
    """Assign identity once and render every machine-controlled planning surface."""

    if content.schema != ANALYSIS_CONTENT_SCHEMA:
        raise AnalysisContentError("Analysis content uses an unsupported schema.")
    requirement_ids = tuple(
        RequirementId(f"REQ-{position:03d}")
        for position in range(1, len(content.requirements) + 1)
    )
    issue_ids = tuple(
        IssueId(f"ISSUE-{position:03d}")
        for position in range(1, len(content.issues) + 1)
    )
    issues: list[IssueDraft] = []
    for position, issue in enumerate(content.issues, start=1):
        issue_id = issue_ids[position - 1]
        assigned_requirements = tuple(
            requirement_ids[number - 1] for number in issue.requirement_numbers
        )
        dependencies = tuple(issue_ids[number - 1] for number in issue.dependency_numbers)
        criteria = tuple(
            AcceptanceCriterion(
                AcceptanceCriterionId(f"AC-{issue_id.value}-{criterion_position:03d}"),
                criterion.text,
            )
            for criterion_position, criterion in enumerate(
                issue.acceptance_criteria,
                start=1,
            )
        )
        slug = FeatureSlug(_slug(issue.title, fallback=f"issue-{position:03d}"))
        issues.append(
            IssueDraft(
                issue_id,
                slug,
                issue.title,
                assigned_requirements,
                dependencies,
                criteria,
                _render_issue_markdown(
                    issue,
                    content.labels,
                    assigned_requirements,
                    dependencies,
                    criteria,
                ),
            )
        )
    feature_slug = FeatureSlug(
        _slug(
            content.feature_title,
            fallback=(
                "feature-"
                + hashlib.sha256(content.feature_title.encode("utf-8")).hexdigest()[:8]
            ),
        )
    )
    return AnalysisDraft(
        ANALYSIS_DRAFT_SCHEMA,
        run_id,
        content.feature_title,
        feature_slug,
        _render_prd_markdown(content, requirement_ids),
        requirement_ids,
        tuple(issues),
        content.revision,
        PlanningAuthority.STRUCTURED_RENDERER,
    )


def _parse_issue_content(
    value: object,
    requirement_count: int,
    issue_count: int,
    position: int,
) -> IssueContent:
    issue = _mapping(value, f"Issue {position}")
    _require_keys(
        issue,
        {
            "title",
            "description",
            "requirement_numbers",
            "dependency_numbers",
            "acceptance_criteria",
        },
        f"Issue {position}",
    )
    requirement_numbers = _number_references(
        issue.get("requirement_numbers"),
        label=f"Issue {position} requirement",
        maximum=requirement_count,
        allow_empty=False,
    )
    dependency_numbers = _number_references(
        issue.get("dependency_numbers"),
        label=f"Issue {position} dependency",
        maximum=issue_count,
        allow_empty=True,
    )
    if position in dependency_numbers:
        raise AnalysisContentError(f"Issue {position} cannot depend on itself.")
    criteria_value = _bounded_list(
        issue.get("acceptance_criteria"),
        f"Issue {position} acceptance criteria",
        minimum=1,
        maximum=_MAX_CRITERIA,
    )
    criteria: list[AcceptanceContent] = []
    for value in criteria_value:
        criterion = _mapping(value, f"Issue {position} acceptance criterion")
        _require_keys(criterion, {"text"}, f"Issue {position} acceptance criterion")
        criteria.append(
            AcceptanceContent(
                _content_string(criterion, "text", ANALYSIS_ACCEPTANCE_TEXT_MAX_LENGTH)
            )
        )
    return IssueContent(
        _content_string(issue, "title", ANALYSIS_ISSUE_TITLE_MAX_LENGTH),
        _content_string(issue, "description", _MAX_SECTION_LENGTH),
        requirement_numbers,
        dependency_numbers,
        tuple(criteria),
    )


def _render_prd_markdown(
    content: AnalysisContent,
    requirement_ids: tuple[RequirementId, ...],
) -> str:
    lines = [
        PRD_MARKER,
        f"# {content.feature_title}",
        "",
        PRD_SECTION_MARKERS[0],
        f"## {content.labels.problem}",
        "",
        content.problem,
        "",
        PRD_SECTION_MARKERS[1],
        f"## {content.labels.solution}",
        "",
        content.solution,
        "",
        PRD_SECTION_MARKERS[2],
        f"## {content.labels.requirements}",
        "",
    ]
    lines.extend(
        f"- **{requirement_id.value}**: {requirement.text}"
        for requirement_id, requirement in zip(
            requirement_ids,
            content.requirements,
            strict=True,
        )
    )
    return "\n".join(lines)


def _render_issue_markdown(
    issue: IssueContent,
    labels: PlanningLabels,
    requirement_ids: tuple[RequirementId, ...],
    dependencies: tuple[IssueId, ...],
    criteria: tuple[AcceptanceCriterion, ...],
) -> str:
    requirements = ", ".join(f"`{item.value}`" for item in requirement_ids)
    dependency_text = (
        ", ".join(f"`{item.value}`" for item in dependencies) if dependencies else "None"
    )
    lines = [
        ISSUE_MARKER,
        f"# {issue.title}",
        "",
        ISSUE_SECTION_MARKERS[0],
        f"## {labels.description}",
        "",
        issue.description,
        "",
        f"**Requirements:** {requirements}",
        f"**Dependencies:** {dependency_text}",
        "",
        ISSUE_SECTION_MARKERS[1],
        f"## {labels.acceptance}",
        "",
    ]
    lines.extend(f"- **{item.criterion_id.value}**: {item.text}" for item in criteria)
    return "\n".join(lines)


def _slug(value: str, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = _SLUG_SEPARATOR.sub("-", normalized.casefold()).strip("-")
    if not slug:
        slug = fallback
    return slug[:100].rstrip("-")


def _content_string(data: Mapping[str, object], key: str, limit: int) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise AnalysisContentError(f"Analysis content is missing {key}.")
    canonical = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in canonical.split("\n")).strip()
    if not normalized or len(normalized) > limit:
        raise AnalysisContentError(f"Analysis content {key} has an invalid length.")
    if _MACHINE_MARKER_PREFIX in normalized.casefold():
        raise AnalysisContentError(f"Analysis content {key} contains a reserved marker.")
    return normalized


def _number_references(
    value: object,
    *,
    label: str,
    maximum: int,
    allow_empty: bool,
) -> tuple[int, ...]:
    values = _bounded_list(
        value,
        f"{label} numbers",
        minimum=0 if allow_empty else 1,
        maximum=maximum,
    )
    if any(
        isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= maximum
        for item in values
    ):
        raise AnalysisContentError(f"{label} number is outside the structured plan.")
    typed = tuple(cast(list[int], values))
    if len(typed) != len(set(typed)):
        raise AnalysisContentError(f"{label} numbers must be unique.")
    return typed


def _bounded_list(
    value: object,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> list[object]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise AnalysisContentError(
            f"Analysis content {label} must contain between {minimum} and {maximum} items."
        )
    return cast(list[object], value)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise AnalysisContentError(f"Analysis content {label} must be an object.")
    return cast(dict[str, object], value)


def _require_keys(
    value: Mapping[str, object],
    expected: set[str],
    label: str,
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise AnalysisContentError(
            f"Analysis content {label} fields mismatch; missing={missing}, unknown={unknown}."
        )
