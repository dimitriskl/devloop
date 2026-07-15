from __future__ import annotations

import json
from pathlib import Path

import pytest

from devloop.analysis.rendering import (
    ANALYSIS_CONTENT_SCHEMA,
    AnalysisContentError,
    parse_analysis_content,
    render_analysis_content,
)
from devloop.components.analysis import ANALYSIS_OUTPUT_SCHEMA
from devloop.domain.identifiers import WorkflowRunId

RUN_ID = WorkflowRunId("run-20260714t120000-123456abcdef")


def _content_payload() -> dict[str, object]:
    return {
        "schema": ANALYSIS_CONTENT_SCHEMA,
        "feature_title": "Σύγκριση τιμών",
        "labels": {
            "problem": "Πρόβλημα",
            "solution": "Λύση",
            "requirements": "Απαιτήσεις",
            "description": "Περιγραφή",
            "acceptance": "Κριτήρια αποδοχής",
        },
        "problem": (
            "Οι αγοραστές δεν γνωρίζουν το χαμηλότερο σύνολο."
        ),
        "solution": "Σύγκρινε τα σύνολα με σταθερή σειρά.",
        "requirements": [
            {"text": "Να συγκρίνονται όλα τα σύνολα."},
            {"text": "Να διατηρείται η σειρά εισαγωγής."},
        ],
        "issues": [
            {
                "title": "Υπολογισμός συνόλων",
                "description": "Υπολόγισε το σύνολο κάθε καλαθιού.",
                "requirement_numbers": [1],
                "dependency_numbers": [],
                "acceptance_criteria": [
                    {"text": "Επιστρέφεται το σωστό σύνολο."},
                ],
            },
            {
                "title": "Επιλογή χαμηλότερου",
                "description": "Επίλεξε το χαμηλότερο σύνολο.",
                "requirement_numbers": [1, 2],
                "dependency_numbers": [1],
                "acceptance_criteria": [
                    {
                        "text": "Σε ισοπαλία διατηρείται η σειρά εισαγωγής."
                    },
                ],
            },
        ],
        "revision": 1,
    }


def test_structured_content_assigns_all_machine_identity_and_renders_repeatably() -> None:
    content = parse_analysis_content(_content_payload())

    first = render_analysis_content(content, RUN_ID)
    second = render_analysis_content(content, RUN_ID)

    assert first == second
    assert first.requirement_ids[0].value == "REQ-001"
    assert [issue.issue_id.value for issue in first.issues] == ["ISSUE-001", "ISSUE-002"]
    assert first.issues[1].dependencies[0].value == "ISSUE-001"
    assert first.issues[1].acceptance_criteria[0].criterion_id.value == (
        "AC-ISSUE-002-001"
    )
    assert first.issues[0].filename.startswith("ISSUE-001-")
    assert "Πρόβλημα" in first.prd_markdown
    assert first.prd_markdown.endswith("\n") is False
    assert "<!-- devloop:section:acceptance -->" in first.issues[0].markdown


def test_agent_output_contract_has_no_machine_identity_or_markdown_fields() -> None:
    draft = ANALYSIS_OUTPUT_SCHEMA["properties"]
    assert isinstance(draft, dict)
    serialized = json.dumps(draft["draft"], sort_keys=True)

    for forbidden in ('"id"', '"slug"', '"markdown"', '"filename"', '"hash"'):
        assert forbidden not in serialized


def test_content_rejects_out_of_range_relationship_numbers() -> None:
    payload = _content_payload()
    issues = payload["issues"]
    assert isinstance(issues, list)
    issue = issues[0]
    assert isinstance(issue, dict)
    issue["dependency_numbers"] = [3]

    with pytest.raises(AnalysisContentError, match="dependency"):
        parse_analysis_content(payload)


def test_published_bytes_are_identical_for_the_same_content(
    tmp_path: Path,
) -> None:
    from devloop.analysis.package import publish_analysis_package

    draft = render_analysis_content(parse_analysis_content(_content_payload()), RUN_ID)
    first = publish_analysis_package(tmp_path / "one", draft)
    second = publish_analysis_package(tmp_path / "two", draft)

    first_root = Path(first.root)
    second_root = Path(second.root)
    assert (first_root / f"{draft.feature_slug.value}.md").read_bytes() == (
        second_root / f"{draft.feature_slug.value}.md"
    ).read_bytes()
    assert (first_root / "issues" / "index.json").read_bytes() == (
        second_root / "issues" / "index.json"
    ).read_bytes()
