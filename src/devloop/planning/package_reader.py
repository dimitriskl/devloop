from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from devloop.domain.development import IssueRuntimeState, IssueStatus, PlanningPackageRef
from devloop.domain.identifiers import (
    AcceptanceCriterionId,
    FeatureSlug,
    IssueId,
    RequirementId,
    WorkflowRunId,
)
from devloop.domain.planning import ISSUE_SET_SCHEMA, PlannedIssue, PlanningPackage

ISSUES_DIRECTORY = "issues"
ISSUE_INDEX_FILENAME = "index.json"
MAX_PACKAGE_FILE_BYTES = 2 * 1024 * 1024


class PlanningPackageError(ValueError):
    pass


def load_planning_package(
    repository: Path,
    package_ref: PlanningPackageRef,
    run_id: WorkflowRunId,
) -> PlanningPackage:
    root = Path(package_ref.root).resolve()
    if not root.is_relative_to((repository / "prd").resolve()):
        raise PlanningPackageError("PRD Package resolves outside the project PRD directory.")
    index_path = root / ISSUES_DIRECTORY / ISSUE_INDEX_FILENAME
    index_bytes = _read_bounded(index_path)
    if hashlib.sha256(index_bytes).hexdigest() != package_ref.issue_set_hash:
        raise PlanningPackageError("IssueSet index hash does not match the accepted package.")
    try:
        decoded = json.loads(index_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlanningPackageError("IssueSet index is not valid UTF-8 JSON.") from error
    if not isinstance(decoded, dict):
        raise PlanningPackageError("IssueSet index must be a JSON object.")
    index = cast(dict[str, object], decoded)
    if index.get("schema") != ISSUE_SET_SCHEMA or index.get("owner_run_id") != run_id.value:
        raise PlanningPackageError("IssueSet schema or owning Run ID does not match.")
    prd_value = index.get("prd")
    issues_value = index.get("issues")
    if not isinstance(prd_value, dict) or not isinstance(issues_value, list) or not issues_value:
        raise PlanningPackageError("IssueSet index is missing PRD or Issue metadata.")
    prd = cast(dict[str, object], prd_value)
    prd_filename = _string(prd, "filename")
    if Path(prd_filename).name != prd_filename:
        raise PlanningPackageError("PRD filename is unsafe.")
    prd_bytes = _read_bounded(root / prd_filename)
    prd_hash = hashlib.sha256(prd_bytes).hexdigest()
    if prd_hash != package_ref.prd_hash or prd.get("hash") != prd_hash:
        raise PlanningPackageError("PRD hash does not match the accepted package.")
    prd_markdown = prd_bytes.decode("utf-8")

    issues = tuple(
        _load_issue(root, value, expected_position, prd_markdown)
        for expected_position, value in enumerate(issues_value, start=1)
    )
    identities = {issue.issue_id for issue in issues}
    if len(identities) != len(issues):
        raise PlanningPackageError("IssueSet contains duplicate Issue IDs.")
    for issue in issues:
        if any(
            dependency not in identities or dependency == issue.issue_id
            for dependency in issue.dependencies
        ):
            raise PlanningPackageError(f"{issue.issue_id} has an invalid dependency.")
    if _has_cycle(issues):
        raise PlanningPackageError("IssueSet dependency graph contains a cycle.")
    return PlanningPackage(
        str(root),
        prd_markdown,
        prd_hash,
        package_ref.issue_set_hash,
        issues,
    )


def initial_issue_states(package: PlanningPackage) -> tuple[IssueRuntimeState, ...]:
    return tuple(IssueRuntimeState(issue.issue_id, IssueStatus.PLANNED) for issue in package.issues)


def select_dependency_ready_issue(
    package: PlanningPackage,
    states: tuple[IssueRuntimeState, ...],
) -> PlannedIssue | None:
    status_by_id = {state.issue_id: state.status for state in states}
    for issue in package.issues:
        status = status_by_id.get(issue.issue_id, IssueStatus.PLANNED)
        if status in {
            IssueStatus.COMPLETED,
            IssueStatus.IN_DEVELOPMENT,
            IssueStatus.IN_REVIEW,
            IssueStatus.IN_QA,
        }:
            continue
        if all(
            status_by_id.get(dependency) is IssueStatus.COMPLETED
            for dependency in issue.dependencies
        ):
            return issue
    return None


def _load_issue(
    root: Path,
    value: object,
    expected_position: int,
    prd_markdown: str,
) -> PlannedIssue:
    if not isinstance(value, dict):
        raise PlanningPackageError("Issue metadata must be an object.")
    data = cast(dict[str, object], value)
    if "status" in data:
        raise PlanningPackageError("IssueSet planning metadata cannot contain runtime status.")
    position = data.get("position")
    if position != expected_position:
        raise PlanningPackageError("IssueSet positions must be stable and contiguous.")
    issue_id = IssueId(_string(data, "id"))
    slug = FeatureSlug(_string(data, "slug"))
    filename = _string(data, "filename")
    expected_filename = f"{issue_id.value}-{slug.value}.md"
    if filename != expected_filename:
        raise PlanningPackageError(
            f"Issue filename does not match its stable identity: {issue_id}."
        )
    expected_hash = _string(data, "hash")
    issue_bytes = _read_bounded(root / ISSUES_DIRECTORY / filename)
    if hashlib.sha256(issue_bytes).hexdigest() != expected_hash:
        raise PlanningPackageError(f"Issue hash does not match: {issue_id}.")
    markdown = issue_bytes.decode("utf-8")
    requirements = tuple(RequirementId(item) for item in _string_list(data, "requirements"))
    dependencies = tuple(IssueId(item) for item in _string_list(data, "dependencies"))
    criteria = tuple(
        AcceptanceCriterionId(item) for item in _string_list(data, "acceptance_criteria")
    )
    if not criteria or any(item.value not in markdown for item in criteria):
        raise PlanningPackageError(f"Issue acceptance criteria are incomplete: {issue_id}.")
    if any(item.value not in prd_markdown for item in requirements):
        raise PlanningPackageError(f"Issue references an unknown requirement: {issue_id}.")
    return PlannedIssue(
        issue_id,
        expected_position,
        _string(data, "title"),
        filename,
        expected_hash,
        requirements,
        dependencies,
        criteria,
        markdown,
    )


def _has_cycle(issues: tuple[PlannedIssue, ...]) -> bool:
    graph = {issue.issue_id: issue.dependencies for issue in issues}
    visiting: set[IssueId] = set()
    visited: set[IssueId] = set()

    def visit(issue_id: IssueId) -> bool:
        if issue_id in visiting:
            return True
        if issue_id in visited:
            return False
        visiting.add(issue_id)
        if any(visit(dependency) for dependency in graph[issue_id]):
            return True
        visiting.remove(issue_id)
        visited.add(issue_id)
        return False

    return any(visit(issue_id) for issue_id in graph)


def _read_bounded(path: Path) -> bytes:
    try:
        if path.stat().st_size > MAX_PACKAGE_FILE_BYTES:
            raise PlanningPackageError(f"Planning package file is too large: {path.name}.")
        return path.read_bytes()
    except OSError as error:
        raise PlanningPackageError(f"Planning package file is unavailable: {path.name}.") from error


def _string(data: Mapping[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise PlanningPackageError(f"IssueSet field is missing: {name}.")
    return value


def _string_list(data: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = data.get(name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PlanningPackageError(f"IssueSet list is invalid: {name}.")
    return tuple(cast(list[str], value))
