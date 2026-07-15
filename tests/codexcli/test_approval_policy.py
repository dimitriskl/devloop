from __future__ import annotations

import json
from pathlib import Path

import pytest

from devloop.domain.approval import (
    ApprovalClassification,
    ApprovalDecisionScope,
    ApprovalPolicy,
    CommandFamily,
    PathBoundary,
    classify_command,
    decision_evidence,
)


@pytest.fixture
def policy() -> ApprovalPolicy:
    return ApprovalPolicy.standard("development")


@pytest.mark.parametrize(
    ("command", "family"),
    [
        ("git status --short", CommandFamily.GIT_INSPECTION),
        ("git diff -- src/app.py", CommandFamily.GIT_INSPECTION),
        ("rg -n greeting src", CommandFamily.WORKSPACE_READ),
        ("python -m pytest tests/test_greeting.py -q", CommandFamily.FOCUSED_TEST),
        ("icacls src/app.py /grant:r *S-1-5-21:(F)", CommandFamily.WINDOWS_ACL_HANDOFF),
    ],
)
def test_policy_classifies_single_commands(
    tmp_path: Path,
    policy: ApprovalPolicy,
    command: str,
    family: CommandFamily,
) -> None:
    classified = classify_command(command, tmp_path, tmp_path, policy)

    assert classified.family is family
    assert classified.boundary is PathBoundary.WORKSPACE
    assert classified.classification is ApprovalClassification.USER_DECISION


@pytest.mark.parametrize(
    "command",
    [
        "git status; rm -rf .",
        "git status && git clean -fdx",
        "git status | tee state.txt",
        "python -m pytest > result.log",
        "rg $(python payload.py) src",
        "rg `python payload.py` src",
        "/tmp/tool --version",
    ],
)
def test_ambiguous_or_compound_command_fails_closed(
    tmp_path: Path,
    policy: ApprovalPolicy,
    command: str,
) -> None:
    classified = classify_command(command, tmp_path, tmp_path, policy)

    assert classified.family is CommandFamily.AMBIGUOUS
    assert classified.classification is ApprovalClassification.USER_DECISION
    assert classified.auto_decision is None
    assert "explicit" in classified.reason.lower()


def test_path_escape_is_visible_and_never_auto_approved(
    tmp_path: Path,
    policy: ApprovalPolicy,
) -> None:
    classified = classify_command("rg token ../sibling", tmp_path, tmp_path, policy)

    assert classified.boundary is PathBoundary.OUTSIDE_WORKSPACE
    assert classified.classification is ApprovalClassification.UNSUPPORTED
    assert classified.auto_decision is None


def test_family_excluded_by_locked_policy_is_unsupported(tmp_path: Path) -> None:
    policy = ApprovalPolicy(
        "devloop.approval-policy/v1",
        "1.0.0",
        "development",
        (CommandFamily.GIT_INSPECTION,),
        PathBoundary.WORKSPACE,
        ("accept", "decline", "cancel"),
    )

    classified = classify_command("rg token src", tmp_path, tmp_path, policy)

    assert classified.family is CommandFamily.WORKSPACE_READ
    assert classified.classification is ApprovalClassification.UNSUPPORTED


def test_windows_and_option_path_escapes_are_detected_on_every_platform(
    tmp_path: Path,
    policy: ApprovalPolicy,
) -> None:
    windows = classify_command(r"rg token C:\outside", tmp_path, tmp_path, policy)
    option = classify_command(
        "python -m pytest --basetemp=../outside tests",
        tmp_path,
        tmp_path,
        policy,
    )
    provider = classify_command("rg token Env:SECRET", tmp_path, tmp_path, policy)
    unc = classify_command(r"rg token \\server\share\secret", tmp_path, tmp_path, policy)
    windows_parent = classify_command(
        r"rg token C:\workspace\..\outside",
        tmp_path,
        tmp_path,
        policy,
    )

    assert windows.boundary is PathBoundary.OUTSIDE_WORKSPACE
    assert option.boundary is PathBoundary.OUTSIDE_WORKSPACE
    assert provider.boundary is PathBoundary.UNKNOWN
    assert unc.boundary is PathBoundary.OUTSIDE_WORKSPACE
    assert windows_parent.boundary is PathBoundary.UNKNOWN


def test_recursive_acl_grant_is_rejected_by_locked_policy(
    tmp_path: Path,
    policy: ApprovalPolicy,
) -> None:
    classified = classify_command(
        "icacls src /grant:r *S-1-5-21:(F) /T",
        tmp_path,
        tmp_path,
        policy,
    )

    assert classified.family is CommandFamily.WINDOWS_ACL_HANDOFF
    assert classified.classification is ApprovalClassification.UNSUPPORTED


def test_decision_evidence_is_typed_redacted_and_contains_no_command(
    tmp_path: Path,
    policy: ApprovalPolicy,
) -> None:
    classified = classify_command(
        "git status --short token=secret-value",
        tmp_path,
        tmp_path,
        policy,
    )

    payload = decision_evidence(
        component_id="development",
        issue_id="ISSUE-001",
        attempt_id="attempt-001",
        request_id="approval-1",
        request_kind="COMMAND",
        classification=classified,
        selected_decision="acceptForSession",
        supported_decisions=("accept", "acceptForSession", "decline"),
    )
    serialized = json.dumps(payload)

    assert payload["schema"] == "devloop.approval-decision/v1"
    assert payload["decision_scope"] == ApprovalDecisionScope.SESSION.value
    assert payload["policy_hash"] == policy.policy_hash
    assert "git status" not in serialized
    assert "secret-value" not in serialized
