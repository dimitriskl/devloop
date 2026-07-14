from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from devloop.domain.identifiers import (
    AcceptanceCriterionId,
    FeatureSlug,
    IssueId,
    RequirementId,
    WorkflowRunId,
)
from devloop.domain.planning import (
    ANALYSIS_DRAFT_SCHEMA,
    ISSUE_MARKER,
    ISSUE_SECTION_MARKERS,
    ISSUE_SET_SCHEMA,
    PRD_MARKER,
    PRD_SECTION_MARKERS,
    AcceptanceCriterion,
    AnalysisDraft,
    IssueDraft,
    PublishedPackage,
    ValidationCode,
    ValidationFinding,
)

PRD_DIRECTORY = "prd"
ISSUES_DIRECTORY = "issues"
ISSUE_INDEX_FILENAME = "index.json"
_REQUIREMENT_ID_TOKEN = re.compile(r"(?<![A-Za-z0-9-])REQ-[0-9]{3,}(?![A-Za-z0-9-])")
_ACCEPTANCE_ID_TOKEN = re.compile(
    r"(?<![A-Za-z0-9-])AC-ISSUE-[0-9]{3,}-[0-9]{3,}(?![A-Za-z0-9-])"
)
_MAX_PORTABLE_FILENAME_BYTES = 255


class AnalysisDraftError(ValueError):
    pass


class AnalysisPublicationError(RuntimeError):
    pass


def parse_analysis_draft(payload: Mapping[str, object], run_id: WorkflowRunId) -> AnalysisDraft:
    if payload.get("schema") != ANALYSIS_DRAFT_SCHEMA:
        raise AnalysisDraftError("Analysis output uses an unsupported schema.")
    persisted_run_id = payload.get("run_id")
    if persisted_run_id is not None and persisted_run_id != run_id.value:
        raise AnalysisDraftError("Analysis Draft belongs to a different Workflow Run.")
    requirements_value = payload.get("requirements")
    issues_value = payload.get("issues")
    if not isinstance(requirements_value, list) or not isinstance(issues_value, list):
        raise AnalysisDraftError("Analysis output requires requirements and issues lists.")
    requirements = tuple(RequirementId(_typed_string(item)) for item in requirements_value)
    issues = tuple(_parse_issue(item) for item in issues_value)
    revision = payload.get("revision", 1)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise AnalysisDraftError("Analysis draft revision must be a positive integer.")
    return AnalysisDraft(
        schema=ANALYSIS_DRAFT_SCHEMA,
        run_id=run_id,
        feature_title=_field_string(payload, "feature_title"),
        feature_slug=FeatureSlug(_field_string(payload, "feature_slug")),
        prd_markdown=_field_string(payload, "prd_markdown"),
        requirement_ids=requirements,
        issues=issues,
        revision=revision,
    )


def analysis_draft_to_dict(draft: AnalysisDraft) -> dict[str, object]:
    return {
        "schema": draft.schema,
        "run_id": draft.run_id.value,
        "feature_title": draft.feature_title,
        "feature_slug": draft.feature_slug.value,
        "prd_markdown": draft.prd_markdown,
        "requirements": [item.value for item in draft.requirement_ids],
        "issues": [
            {
                "id": issue.issue_id.value,
                "slug": issue.slug.value,
                "title": issue.title,
                "requirements": [item.value for item in issue.requirement_ids],
                "dependencies": [item.value for item in issue.dependencies],
                "acceptance_criteria": [
                    {"id": criterion.criterion_id.value, "text": criterion.text}
                    for criterion in issue.acceptance_criteria
                ],
                "markdown": issue.markdown,
            }
            for issue in draft.issues
        ],
        "revision": draft.revision,
    }


def validate_analysis_draft(draft: AnalysisDraft) -> tuple[ValidationFinding, ...]:
    findings: list[ValidationFinding] = []
    if draft.schema != ANALYSIS_DRAFT_SCHEMA:
        findings.append(ValidationFinding(ValidationCode.SCHEMA, "Unsupported draft schema."))
    if PRD_MARKER not in draft.prd_markdown:
        findings.append(ValidationFinding(ValidationCode.PRD_SECTION, "PRD marker is missing."))
    for marker in PRD_SECTION_MARKERS:
        if marker not in draft.prd_markdown:
            findings.append(
                ValidationFinding(ValidationCode.PRD_SECTION, f"PRD section is missing: {marker}.")
            )
    prd_markers = (PRD_MARKER, *PRD_SECTION_MARKERS)
    if all(marker in draft.prd_markdown for marker in prd_markers) and not _markers_are_ordered(
        draft.prd_markdown,
        prd_markers,
    ):
        findings.append(
            ValidationFinding(
                ValidationCode.PRD_SECTION,
                "PRD markers must appear exactly once in schema order.",
            )
        )

    requirement_values = [item.value for item in draft.requirement_ids]
    if not requirement_values or len(requirement_values) != len(set(requirement_values)):
        findings.append(
            ValidationFinding(
                ValidationCode.REQUIREMENT_ID,
                "Requirement IDs must be present and unique.",
            )
        )
    requirements_marker = PRD_SECTION_MARKERS[-1]
    if requirements_marker in draft.prd_markdown:
        documented_requirements = set(
            _REQUIREMENT_ID_TOKEN.findall(
                draft.prd_markdown.split(requirements_marker, maxsplit=1)[1]
            )
        )
        declared_requirements = set(requirement_values)
        if documented_requirements != declared_requirements:
            missing = sorted(declared_requirements.difference(documented_requirements))
            undeclared = sorted(documented_requirements.difference(declared_requirements))
            findings.append(
                ValidationFinding(
                    ValidationCode.REQUIREMENT_ID,
                    "PRD Requirement IDs do not match the declared IDs; "
                    f"missing={missing}, undeclared={undeclared}.",
                )
            )

    issue_ids = [issue.issue_id for issue in draft.issues]
    if not issue_ids or len(issue_ids) != len(set(issue_ids)):
        findings.append(
            ValidationFinding(ValidationCode.ISSUE_ID, "Issue IDs must be present and unique.")
        )
    known_issues = set(issue_ids)
    covered_requirements: set[RequirementId] = set()
    for issue in draft.issues:
        covered_requirements.update(issue.requirement_ids)
        findings.extend(_validate_issue(issue, known_issues))
    if set(draft.requirement_ids) != covered_requirements:
        missing = sorted(
            item.value for item in set(draft.requirement_ids).difference(covered_requirements)
        )
        extra = sorted(
            item.value for item in covered_requirements.difference(draft.requirement_ids)
        )
        findings.append(
            ValidationFinding(
                ValidationCode.REQUIREMENT_COVERAGE,
                f"Requirement coverage mismatch; missing={missing}, unknown={extra}.",
            )
        )
    if _has_dependency_cycle(draft.issues):
        findings.append(
            ValidationFinding(
                ValidationCode.DEPENDENCY_CYCLE,
                "Issue dependencies contain a cycle.",
            )
        )
    return tuple(findings)


def publish_analysis_package(repository: Path, draft: AnalysisDraft) -> PublishedPackage:
    findings = validate_analysis_draft(draft)
    if findings:
        raise AnalysisPublicationError(
            "Analysis draft cannot be published: " + "; ".join(item.message for item in findings)
        )
    prd_root = repository / PRD_DIRECTORY
    target = prd_root / draft.feature_slug.value
    prd_root.mkdir(parents=True, exist_ok=True)
    if not prd_root.resolve().is_relative_to(repository.resolve()):
        raise AnalysisPublicationError("The PRD directory resolves outside the repository.")
    if target.is_symlink():
        raise AnalysisPublicationError("The PRD Package target cannot be a symbolic link.")
    if target.exists():
        existing = _existing_owned_package(target, draft)
        if existing is not None:
            return existing
        raise AnalysisPublicationError(
            f"Publication target already exists and will not be overwritten: {target}."
        )
    staging = prd_root / f".{draft.feature_slug.value}.staging-{uuid.uuid4().hex}"
    try:
        issues_root = staging / ISSUES_DIRECTORY
        issues_root.mkdir(parents=True)
        prd_bytes = _document_bytes(draft.prd_markdown)
        prd_hash = hashlib.sha256(prd_bytes).hexdigest()
        _write_new(staging / f"{draft.feature_slug.value}.md", prd_bytes)

        issue_metadata: list[dict[str, object]] = []
        for position, issue in enumerate(draft.issues, start=1):
            issue_bytes = _document_bytes(issue.markdown)
            issue_hash = hashlib.sha256(issue_bytes).hexdigest()
            _write_new(issues_root / issue.filename, issue_bytes)
            issue_metadata.append(
                {
                    "position": position,
                    "id": issue.issue_id.value,
                    "title": issue.title,
                    "slug": issue.slug.value,
                    "filename": issue.filename,
                    "dependencies": [item.value for item in issue.dependencies],
                    "requirements": [item.value for item in issue.requirement_ids],
                    "acceptance_criteria": [
                        item.criterion_id.value for item in issue.acceptance_criteria
                    ],
                    "hash": issue_hash,
                }
            )
        index = {
            "schema": ISSUE_SET_SCHEMA,
            "owner_run_id": draft.run_id.value,
            "prd": {"filename": f"{draft.feature_slug.value}.md", "hash": prd_hash},
            "issues": issue_metadata,
        }
        index_bytes = (
            json.dumps(
                index,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        issue_set_hash = hashlib.sha256(index_bytes).hexdigest()
        _write_new(issues_root / ISSUE_INDEX_FILENAME, index_bytes)
        os.replace(staging, target)
        return PublishedPackage(str(target), prd_hash, issue_set_hash)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _parse_issue(value: object) -> IssueDraft:
    if not isinstance(value, dict):
        raise AnalysisDraftError("Each Issue must be an object.")
    data = cast(dict[str, object], value)
    requirements = _string_list(data.get("requirements"), "Issue requirements")
    dependencies = _string_list(data.get("dependencies"), "Issue dependencies")
    criteria_value = data.get("acceptance_criteria")
    if not isinstance(criteria_value, list):
        raise AnalysisDraftError("Issue acceptance criteria must be a list.")
    criteria: list[AcceptanceCriterion] = []
    for criterion_value in criteria_value:
        if not isinstance(criterion_value, dict):
            raise AnalysisDraftError("Acceptance criteria must be objects.")
        criterion = cast(dict[str, object], criterion_value)
        criteria.append(
            AcceptanceCriterion(
                AcceptanceCriterionId(_field_string(criterion, "id")),
                _field_string(criterion, "text"),
            )
        )
    return IssueDraft(
        issue_id=IssueId(_field_string(data, "id")),
        slug=FeatureSlug(_field_string(data, "slug")),
        title=_field_string(data, "title"),
        requirement_ids=tuple(RequirementId(item) for item in requirements),
        dependencies=tuple(IssueId(item) for item in dependencies),
        acceptance_criteria=tuple(criteria),
        markdown=_field_string(data, "markdown"),
    )


def _validate_issue(
    issue: IssueDraft,
    known_issues: set[IssueId],
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if len(issue.filename.encode("utf-8")) > _MAX_PORTABLE_FILENAME_BYTES:
        findings.append(
            ValidationFinding(
                ValidationCode.ISSUE_FILENAME,
                f"{issue.issue_id} filename exceeds the portable component limit.",
            )
        )
    if ISSUE_MARKER not in issue.markdown:
        findings.append(
            ValidationFinding(
                ValidationCode.ISSUE_SECTION,
                f"{issue.issue_id} is missing its Issue marker.",
            )
        )
    for marker in ISSUE_SECTION_MARKERS:
        if marker not in issue.markdown:
            findings.append(
                ValidationFinding(
                    ValidationCode.ISSUE_SECTION,
                    f"{issue.issue_id} is missing section {marker}.",
                )
            )
    issue_markers = (ISSUE_MARKER, *ISSUE_SECTION_MARKERS)
    if all(marker in issue.markdown for marker in issue_markers) and not _markers_are_ordered(
        issue.markdown,
        issue_markers,
    ):
        findings.append(
            ValidationFinding(
                ValidationCode.ISSUE_SECTION,
                f"{issue.issue_id} markers must appear exactly once in schema order.",
            )
        )
    criterion_ids = [item.criterion_id for item in issue.acceptance_criteria]
    if not criterion_ids or len(criterion_ids) != len(set(criterion_ids)):
        findings.append(
            ValidationFinding(
                ValidationCode.ACCEPTANCE_ID,
                f"{issue.issue_id} acceptance criterion IDs must be present and unique.",
            )
        )
    for criterion in issue.acceptance_criteria:
        expected_prefix = f"AC-{issue.issue_id.value}-"
        if not criterion.criterion_id.value.startswith(expected_prefix):
            findings.append(
                ValidationFinding(
                    ValidationCode.ACCEPTANCE_ID,
                    f"{criterion.criterion_id} does not belong to {issue.issue_id}.",
                )
            )
    acceptance_marker = ISSUE_SECTION_MARKERS[-1]
    if acceptance_marker in issue.markdown:
        documented_criteria = _ACCEPTANCE_ID_TOKEN.findall(
            issue.markdown.split(acceptance_marker, maxsplit=1)[1]
        )
        declared_criteria = {item.value for item in criterion_ids}
        documented_set = set(documented_criteria)
        if len(documented_criteria) != len(documented_set) or documented_set != declared_criteria:
            missing = sorted(declared_criteria.difference(documented_set))
            undeclared = sorted(documented_set.difference(declared_criteria))
            findings.append(
                ValidationFinding(
                    ValidationCode.ACCEPTANCE_ID,
                    f"{issue.issue_id} acceptance IDs do not match its declared criteria; "
                    f"missing={missing}, undeclared={undeclared}.",
                )
            )
    for dependency in issue.dependencies:
        if dependency not in known_issues or dependency == issue.issue_id:
            findings.append(
                ValidationFinding(
                    ValidationCode.DEPENDENCY_REFERENCE,
                    f"{issue.issue_id} has invalid dependency {dependency}.",
                )
            )
    return findings


def _has_dependency_cycle(issues: tuple[IssueDraft, ...]) -> bool:
    graph = {issue.issue_id: issue.dependencies for issue in issues}
    visiting: set[IssueId] = set()
    visited: set[IssueId] = set()

    def visit(issue_id: IssueId) -> bool:
        if issue_id in visiting:
            return True
        if issue_id in visited or issue_id not in graph:
            return False
        visiting.add(issue_id)
        if any(visit(dependency) for dependency in graph[issue_id]):
            return True
        visiting.remove(issue_id)
        visited.add(issue_id)
        return False

    return any(visit(issue_id) for issue_id in graph)


def _markers_are_ordered(markdown: str, markers: tuple[str, ...]) -> bool:
    if any(markdown.count(marker) != 1 for marker in markers):
        return False
    positions = tuple(markdown.index(marker) for marker in markers)
    return positions == tuple(sorted(positions))


def _document_bytes(value: str) -> bytes:
    return value.rstrip().encode("utf-8") + b"\n"


def _existing_owned_package(target: Path, draft: AnalysisDraft) -> PublishedPackage | None:
    index_path = target / ISSUES_DIRECTORY / ISSUE_INDEX_FILENAME
    prd_path = target / f"{draft.feature_slug.value}.md"
    try:
        index_value = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(index_value, dict):
            return None
        index = cast(dict[str, object], index_value)
        prd = index.get("prd")
        if not isinstance(prd, dict):
            return None
        prd_data = cast(dict[str, object], prd)
        expected_prd_hash = hashlib.sha256(_document_bytes(draft.prd_markdown)).hexdigest()
        issues_value = index.get("issues")
        if (
            index.get("schema") != ISSUE_SET_SCHEMA
            or index.get("owner_run_id") != draft.run_id.value
            or prd_data.get("filename") != f"{draft.feature_slug.value}.md"
            or prd_data.get("hash") != expected_prd_hash
            or hashlib.sha256(prd_path.read_bytes()).hexdigest() != expected_prd_hash
            or not isinstance(issues_value, list)
            or len(issues_value) != len(draft.issues)
        ):
            return None
        for position, (metadata_value, issue) in enumerate(
            zip(issues_value, draft.issues, strict=True),
            start=1,
        ):
            if not isinstance(metadata_value, dict):
                return None
            metadata = cast(dict[str, object], metadata_value)
            expected_hash = hashlib.sha256(_document_bytes(issue.markdown)).hexdigest()
            expected = {
                "position": position,
                "id": issue.issue_id.value,
                "title": issue.title,
                "slug": issue.slug.value,
                "filename": issue.filename,
                "dependencies": [item.value for item in issue.dependencies],
                "requirements": [item.value for item in issue.requirement_ids],
                "acceptance_criteria": [
                    item.criterion_id.value for item in issue.acceptance_criteria
                ],
                "hash": expected_hash,
            }
            if metadata != expected:
                return None
            issue_path = target / ISSUES_DIRECTORY / issue.filename
            if hashlib.sha256(issue_path.read_bytes()).hexdigest() != expected_hash:
                return None
        return PublishedPackage(
            str(target),
            expected_prd_hash,
            hashlib.sha256(index_path.read_bytes()).hexdigest(),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _field_string(data: Mapping[str, object], name: str) -> str:
    return _typed_string(data.get(name))


def _typed_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalysisDraftError("Analysis output contains a missing string value.")
    return value


def _string_list(value: object, title: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AnalysisDraftError(f"{title} must be a list of strings.")
    return tuple(cast(list[str], value))
