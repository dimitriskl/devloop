from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from devloop.domain.identifiers import (
    AcceptanceCriterionId,
    FeatureSlug,
    IssueId,
    RequirementId,
    WorkflowRunId,
)

ANALYSIS_DRAFT_SCHEMA = "devloop.analysis-draft/v1"
ANALYSIS_CLARIFICATION_MAX_LENGTH = 4_000
ANALYSIS_FEATURE_TITLE_MAX_LENGTH = 500
ANALYSIS_PRD_MARKDOWN_MAX_LENGTH = 1_000_000
ANALYSIS_ISSUE_TITLE_MAX_LENGTH = 500
ANALYSIS_ACCEPTANCE_TEXT_MAX_LENGTH = 10_000
ANALYSIS_ISSUE_MARKDOWN_MAX_LENGTH = 250_000
ISSUE_SET_SCHEMA = "devloop.issue-set/v1"
PRD_MARKER = "<!-- devloop:prd:v1 -->"
PRD_SECTION_MARKERS = (
    "<!-- devloop:section:problem -->",
    "<!-- devloop:section:solution -->",
    "<!-- devloop:section:requirements -->",
)
ISSUE_MARKER = "<!-- devloop:issue:v1 -->"
ISSUE_SECTION_MARKERS = (
    "<!-- devloop:section:description -->",
    "<!-- devloop:section:acceptance -->",
)


class ValidationCode(str, Enum):
    SCHEMA = "SCHEMA"
    PRD_SECTION = "PRD_SECTION"
    REQUIREMENT_ID = "REQUIREMENT_ID"
    ISSUE_ID = "ISSUE_ID"
    ISSUE_FILENAME = "ISSUE_FILENAME"
    ISSUE_SECTION = "ISSUE_SECTION"
    ACCEPTANCE_ID = "ACCEPTANCE_ID"
    DEPENDENCY_REFERENCE = "DEPENDENCY_REFERENCE"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    REQUIREMENT_COVERAGE = "REQUIREMENT_COVERAGE"
    PORT_CONTRACT = "PORT_CONTRACT"
    PUBLICATION_CONFLICT = "PUBLICATION_CONFLICT"


class PlanningAuthority(str, Enum):
    LEGACY_MIXED = "LEGACY_MIXED"
    STRUCTURED_RENDERER = "STRUCTURED_RENDERER"


@dataclass(frozen=True)
class ValidationFinding:
    code: ValidationCode
    message: str


@dataclass(frozen=True)
class AcceptanceCriterion:
    criterion_id: AcceptanceCriterionId
    text: str


@dataclass(frozen=True)
class IssueDraft:
    issue_id: IssueId
    slug: FeatureSlug
    title: str
    requirement_ids: tuple[RequirementId, ...]
    dependencies: tuple[IssueId, ...]
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    markdown: str

    @property
    def filename(self) -> str:
        return f"{self.issue_id.value}-{self.slug.value}.md"


@dataclass(frozen=True)
class AnalysisDraft:
    schema: str
    run_id: WorkflowRunId
    feature_title: str
    feature_slug: FeatureSlug
    prd_markdown: str
    requirement_ids: tuple[RequirementId, ...]
    issues: tuple[IssueDraft, ...]
    revision: int
    authority: PlanningAuthority = PlanningAuthority.LEGACY_MIXED


@dataclass(frozen=True)
class PublishedPackage:
    root: str
    prd_hash: str
    issue_set_hash: str


@dataclass(frozen=True)
class PlannedIssue:
    issue_id: IssueId
    position: int
    title: str
    filename: str
    content_hash: str
    requirement_ids: tuple[RequirementId, ...]
    dependencies: tuple[IssueId, ...]
    acceptance_criterion_ids: tuple[AcceptanceCriterionId, ...]
    markdown: str


@dataclass(frozen=True)
class PlanningPackage:
    root: str
    prd_markdown: str
    prd_hash: str
    issue_set_hash: str
    issues: tuple[PlannedIssue, ...]
