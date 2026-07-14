from __future__ import annotations

import pytest

from devloop.domain.doctor import (
    DoctorCheckId,
    DoctorCheckResult,
    DoctorCheckStatus,
    DoctorExitCode,
    DoctorReport,
    redact_diagnostic,
    render_doctor_report,
)


def test_report_aggregates_all_failures_and_returns_nonzero_when_not_ready() -> None:
    report = DoctorReport.from_checks(
        (
            DoctorCheckResult.passed(DoctorCheckId.PYTHON, "Python", "Python 3.12.4 is supported."),
            DoctorCheckResult.failed(
                DoctorCheckId.GIT,
                "Git",
                "Git is unavailable.",
                "Install Git and ensure it is on PATH.",
            ),
            DoctorCheckResult.failed(
                DoctorCheckId.AUTHENTICATION,
                "Authentication",
                "Codex authentication is required.",
                "Run 'codex login', then run the doctor again.",
            ),
        )
    )

    assert report.ready is False
    assert report.exit_code is DoctorExitCode.NOT_READY
    assert tuple(failure.check_id for failure in report.failures) == (
        DoctorCheckId.GIT,
        DoctorCheckId.AUTHENTICATION,
    )


def test_warnings_do_not_block_readiness() -> None:
    report = DoctorReport.from_checks(
        (
            DoctorCheckResult.warning(
                DoctorCheckId.TERMINAL,
                "Terminal",
                "Terminal capabilities are limited.",
                "Use an interactive terminal for the full-screen runner.",
            ),
        )
    )

    assert report.ready is True
    assert report.exit_code is DoctorExitCode.READY


def test_failed_check_requires_an_action() -> None:
    with pytest.raises(ValueError, match="actionable"):
        DoctorCheckResult(
            DoctorCheckId.GIT,
            "Git",
            DoctorCheckStatus.FAIL,
            "Git is unavailable.",
        )


def test_duplicate_check_identity_is_rejected() -> None:
    check = DoctorCheckResult.passed(DoctorCheckId.PYTHON, "Python", "Supported.")

    with pytest.raises(ValueError, match="Duplicate"):
        DoctorReport.from_checks((check, check))


def test_rendering_is_actionable_and_redacts_sensitive_diagnostics() -> None:
    report = DoctorReport.from_checks(
        (
            DoctorCheckResult.failed(
                DoctorCheckId.AUTHENTICATION,
                "Authentication secret=do-not-print",
                (
                    "Account secret@example.com at C:\\Users\\alice\\.codex "
                    "used Bearer private-token."
                ),
                "Run codex login with api_key=sk-abcdefghijklmnop.",
            ),
        )
    )

    rendered = render_doctor_report(report)

    assert "CodexCLI doctor: NOT READY" in rendered
    assert "[FAIL] Authentication" in rendered
    assert "Action:" in rendered
    assert "alice" not in rendered
    assert "secret@example.com" not in rendered
    assert "private-token" not in rendered
    assert "sk-abcdefghijklmnop" not in rendered
    assert "do-not-print" not in rendered


def test_redaction_produces_one_bounded_line() -> None:
    assert redact_diagnostic("first\nsecond", limit=20) == "first second"
    assert redact_diagnostic("x" * 40, limit=20) == "x" * 17 + "..."
