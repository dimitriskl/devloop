from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, IntEnum

_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\b")
_WINDOWS_USER = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s]+")
_ASSIGNMENT = re.compile(r"(?i)\b(secret|token|api[_-]?key|password)\s*=\s*[^\s]+")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


class DoctorCheckId(str, Enum):
    PYTHON = "python"
    GIT = "git"
    REPOSITORY = "repository"
    CODEX_CLI = "codex-cli"
    CODEX_EXECUTABLE = "codex-executable"
    CODEX_VERSION = "codex-version"
    APP_SERVER = "app-server"
    BACKEND_COMPATIBILITY = "backend-compatibility"
    AUTHENTICATION = "authentication"
    TERMINAL = "terminal"
    STORAGE = "storage"


class DoctorCheckStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class DoctorExitCode(IntEnum):
    READY = 0
    NOT_READY = 1


@dataclass(frozen=True)
class DoctorCheckResult:
    check_id: DoctorCheckId
    title: str
    status: DoctorCheckStatus
    summary: str
    action: str | None = None

    def __post_init__(self) -> None:
        if self.status is DoctorCheckStatus.FAIL and not self.action:
            raise ValueError("A failed doctor check must be actionable.")

    @classmethod
    def passed(
        cls,
        check_id: DoctorCheckId,
        title: str,
        summary: str,
    ) -> DoctorCheckResult:
        return cls(check_id, title, DoctorCheckStatus.PASS, summary)

    @classmethod
    def warning(
        cls,
        check_id: DoctorCheckId,
        title: str,
        summary: str,
        action: str,
    ) -> DoctorCheckResult:
        return cls(check_id, title, DoctorCheckStatus.WARN, summary, action)

    @classmethod
    def failed(
        cls,
        check_id: DoctorCheckId,
        title: str,
        summary: str,
        action: str,
    ) -> DoctorCheckResult:
        return cls(check_id, title, DoctorCheckStatus.FAIL, summary, action)


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheckResult, ...]

    @classmethod
    def from_checks(cls, checks: tuple[DoctorCheckResult, ...]) -> DoctorReport:
        identities = [check.check_id for check in checks]
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicate doctor check identity.")
        return cls(checks)

    @property
    def failures(self) -> tuple[DoctorCheckResult, ...]:
        return tuple(check for check in self.checks if check.status is DoctorCheckStatus.FAIL)

    @property
    def ready(self) -> bool:
        return not self.failures

    @property
    def exit_code(self) -> DoctorExitCode:
        return DoctorExitCode.READY if self.ready else DoctorExitCode.NOT_READY


def render_doctor_report(report: DoctorReport) -> str:
    readiness = "READY" if report.ready else "NOT READY"
    lines = [f"CodexCLI doctor: {readiness}"]
    for check in report.checks:
        title = redact_diagnostic(check.title)
        summary = redact_diagnostic(check.summary)
        lines.append(f"[{check.status.value}] {title}: {summary}")
        if check.action:
            lines.append(f"       Action: {redact_diagnostic(check.action)}")
    return "\n".join(lines)


def redact_diagnostic(value: str, *, limit: int = 300) -> str:
    flattened = " ".join(value.split())
    return redact_sensitive_text(flattened, limit=limit)


def redact_sensitive_text(value: str, *, limit: int) -> str:
    redacted = _EMAIL.sub("[redacted-email]", value)
    redacted = _WINDOWS_USER.sub(lambda _: r"C:\Users\[redacted]", redacted)
    redacted = _BEARER.sub("Bearer [redacted]", redacted)
    redacted = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[redacted]", redacted)
    redacted = _OPENAI_KEY.sub("[redacted-key]", redacted)
    if len(redacted) <= limit:
        return redacted
    if limit <= 3:
        return "." * max(0, limit)
    return redacted[: limit - 3] + "..."
