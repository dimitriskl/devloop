from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from devloop.analysis.package import (
    AnalysisPublicationError,
    parse_analysis_draft,
    publish_analysis_package,
    validate_analysis_draft,
)
from devloop.domain.identifiers import WorkflowRunId
from devloop.domain.planning import ISSUE_SET_SCHEMA, ValidationCode

RUN_ID = WorkflowRunId("run-20260710t120000-123456abcdef")


def valid_payload() -> dict[str, object]:
    return {
        "schema": "devloop.analysis-draft/v1",
        "feature_title": "Price comparison",
        "feature_slug": "price-comparison",
        "prd_markdown": """<!-- devloop:prd:v1 -->
# Price comparison
<!-- devloop:section:problem -->
## Problem
REQ-001: Compare totals.
<!-- devloop:section:solution -->
## Solution
Collect prices safely.
<!-- devloop:section:requirements -->
## Requirements
- REQ-001: Compare totals.
""",
        "requirements": ["REQ-001"],
        "issues": [
            {
                "id": "ISSUE-001",
                "slug": "compare-totals",
                "title": "Compare totals",
                "requirements": ["REQ-001"],
                "dependencies": [],
                "acceptance_criteria": [
                    {"id": "AC-ISSUE-001-001", "text": "The lowest total is selected."}
                ],
                "markdown": """<!-- devloop:issue:v1 -->
# Compare totals
<!-- devloop:section:description -->
Implement REQ-001.
<!-- devloop:section:acceptance -->
- AC-ISSUE-001-001: The lowest total is selected.
""",
            }
        ],
        "revision": 1,
    }


def test_valid_draft_publishes_hash_locked_issue_set_without_statuses(tmp_path: Path) -> None:
    draft = parse_analysis_draft(valid_payload(), RUN_ID)

    assert validate_analysis_draft(draft) == ()
    published = publish_analysis_package(tmp_path, draft)

    root = Path(published.root)
    index_path = root / "issues" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    issue_path = root / "issues" / index["issues"][0]["filename"]
    assert index["schema"] == ISSUE_SET_SCHEMA
    assert index["owner_run_id"] == RUN_ID.value
    assert "status" not in json.dumps(index).casefold()
    assert index["issues"][0]["hash"] == hashlib.sha256(issue_path.read_bytes()).hexdigest()
    assert published.issue_set_hash == hashlib.sha256(index_path.read_bytes()).hexdigest()

    repeated = publish_analysis_package(tmp_path, draft)
    assert repeated == published

    issue_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(AnalysisPublicationError, match="will not be overwritten"):
        publish_analysis_package(tmp_path, draft)

    unrelated = parse_analysis_draft(
        {**valid_payload(), "revision": 2}, WorkflowRunId("run-20260710t120003-123456abcdef")
    )
    with pytest.raises(AnalysisPublicationError, match="will not be overwritten"):
        publish_analysis_package(tmp_path, unrelated)


def test_dependency_cycles_and_requirement_gaps_are_validation_findings() -> None:
    payload = valid_payload()
    issues = payload["issues"]
    assert isinstance(issues, list)
    first = issues[0]
    assert isinstance(first, dict)
    first["dependencies"] = ["ISSUE-002"]
    first["requirements"] = []
    issues.append(
        {
            "id": "ISSUE-002",
            "slug": "second",
            "title": "Second",
            "requirements": [],
            "dependencies": ["ISSUE-001"],
            "acceptance_criteria": [{"id": "AC-ISSUE-002-001", "text": "Second criterion."}],
            "markdown": """<!-- devloop:issue:v1 -->
<!-- devloop:section:description -->
Second.
<!-- devloop:section:acceptance -->
AC-ISSUE-002-001: Second criterion.
""",
        }
    )

    findings = validate_analysis_draft(parse_analysis_draft(payload, RUN_ID))
    codes = {finding.code for finding in findings}

    assert ValidationCode.DEPENDENCY_CYCLE in codes
    assert ValidationCode.REQUIREMENT_COVERAGE in codes


def test_prd_requirement_section_cannot_hide_an_undeclared_uncovered_requirement() -> None:
    payload = valid_payload()
    prd_markdown = payload["prd_markdown"]
    assert isinstance(prd_markdown, str)
    payload["prd_markdown"] = prd_markdown.replace(
        "- REQ-001: Compare totals.",
        "- REQ-001: Compare totals.\n- REQ-002: Preserve a comparison history.",
    )

    findings = validate_analysis_draft(parse_analysis_draft(payload, RUN_ID))

    assert any(
        finding.code is ValidationCode.REQUIREMENT_ID and "REQ-002" in finding.message
        for finding in findings
    )


def test_issue_acceptance_section_requires_exact_criterion_ids() -> None:
    payload = valid_payload()
    issues = payload["issues"]
    assert isinstance(issues, list)
    issue = issues[0]
    assert isinstance(issue, dict)
    markdown = issue["markdown"]
    assert isinstance(markdown, str)
    issue["markdown"] = markdown.replace(
        "AC-ISSUE-001-001:",
        "AC-ISSUE-001-0010:",
    )

    findings = validate_analysis_draft(parse_analysis_draft(payload, RUN_ID))

    assert any(
        finding.code is ValidationCode.ACCEPTANCE_ID
        and "AC-ISSUE-001-001" in finding.message
        for finding in findings
    )


def test_prd_section_markers_must_be_unique_and_in_schema_order() -> None:
    payload = valid_payload()
    markdown = payload["prd_markdown"]
    assert isinstance(markdown, str)
    payload["prd_markdown"] = markdown.replace(
        "<!-- devloop:section:problem -->",
        "<!-- devloop:section:swap -->",
        1,
    ).replace(
        "<!-- devloop:section:solution -->",
        "<!-- devloop:section:problem -->",
        1,
    ).replace(
        "<!-- devloop:section:swap -->",
        "<!-- devloop:section:solution -->",
        1,
    )

    findings = validate_analysis_draft(parse_analysis_draft(payload, RUN_ID))

    assert any(finding.code is ValidationCode.PRD_SECTION for finding in findings)


def test_issue_filename_must_fit_a_portable_filesystem_component() -> None:
    payload = valid_payload()
    issues = payload["issues"]
    assert isinstance(issues, list)
    issue = issues[0]
    assert isinstance(issue, dict)
    long_issue_id = "ISSUE-" + ("1" * 240)
    criterion_id = f"AC-{long_issue_id}-001"
    issue["id"] = long_issue_id
    criteria = issue["acceptance_criteria"]
    assert isinstance(criteria, list)
    criterion = criteria[0]
    assert isinstance(criterion, dict)
    criterion["id"] = criterion_id
    markdown = issue["markdown"]
    assert isinstance(markdown, str)
    issue["markdown"] = markdown.replace("AC-ISSUE-001-001", criterion_id)

    findings = validate_analysis_draft(parse_analysis_draft(payload, RUN_ID))

    assert any(finding.code is ValidationCode.ISSUE_FILENAME for finding in findings)
