from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping


MAX_STEP_GUIDANCE_CHARACTERS = 4_000
STEP_GUIDANCE_PRECEDENCE = (
    "Component instructions, the Step Contract, Step Execution Policy, output "
    "requirements, required capabilities, permissions, and safety boundaries "
    "outrank Step Guidance. Guidance cannot change workflow structure or Codex "
    "Execution Settings."
)

_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[^\s]+")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_GITHUB_TOKEN = re.compile(
    r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----.*?"
    r"(?:-----END \1-----|\Z)",
    re.DOTALL,
)
_SECRET_KEY_PARTS = frozenset(
    {"secret", "token", "password", "passwd", "credential", "credentials"}
)
_KEY_CHARACTER_SET = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


class CapabilityKind(str, Enum):
    SKILL = "SKILL"
    AGENT_REFERENCE = "AGENT_REFERENCE"


class GuidanceReviewState(str, Enum):
    READY = "READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass(frozen=True, order=True)
class CapabilityReference:
    kind: CapabilityKind
    path: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("Capability paths must be non-empty text.")
        if self.path != self.path.strip() or "\\" in self.path:
            raise ValueError("Capability paths must use normalized bundle-relative paths.")
        if any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            for character in self.path
        ):
            raise ValueError("Capability paths must not contain control characters.")
        parsed = PurePosixPath(self.path)
        if parsed.is_absolute() or ".." in parsed.parts or "." in parsed.parts:
            raise ValueError("Capability paths must stay inside the Dev Loop bundle.")
        if self.kind is CapabilityKind.SKILL and not (
            self.path.startswith("skills/codex/")
            and self.path.endswith("/SKILL.md")
        ):
            raise ValueError("Skill capabilities must reference a bundled SKILL.md.")
        if self.kind is CapabilityKind.AGENT_REFERENCE and not (
            self.path.startswith("agents/codex/") and self.path.endswith(".md")
        ):
            raise ValueError(
                "Agent Reference capabilities must reference a bundled Markdown file."
            )


@dataclass(frozen=True)
class RequiredCapability:
    reference: CapabilityReference
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("Required capabilities need a component-contract reason.")


@dataclass(frozen=True)
class StepCapabilityProfile:
    capabilities: tuple[CapabilityReference, ...] = ()

    def __post_init__(self) -> None:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("A Step Capability Profile cannot contain duplicates.")

    @property
    def skills(self) -> tuple[str, ...]:
        return tuple(
            item.path
            for item in self.capabilities
            if item.kind is CapabilityKind.SKILL
        )

    @property
    def agent_references(self) -> tuple[str, ...]:
        return tuple(
            item.path
            for item in self.capabilities
            if item.kind is CapabilityKind.AGENT_REFERENCE
        )

    def contains(self, reference: CapabilityReference) -> bool:
        return reference in self.capabilities

    def toggled(self, reference: CapabilityReference) -> StepCapabilityProfile:
        selected = list(self.capabilities)
        if reference in selected:
            selected.remove(reference)
        else:
            selected.append(reference)
        return StepCapabilityProfile(tuple(selected))

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "skills": list(self.skills),
            "agent_references": list(self.agent_references),
        }

    @classmethod
    def from_dict(cls, value: Any) -> StepCapabilityProfile:
        if not isinstance(value, Mapping):
            raise ValueError("Step Capability Profile must be an object.")
        if set(value) != {"skills", "agent_references"}:
            raise ValueError(
                "Step Capability Profile requires skills and agent_references."
            )
        return cls(
            _capability_references(value.get("skills"), CapabilityKind.SKILL)
            + _capability_references(
                value.get("agent_references"),
                CapabilityKind.AGENT_REFERENCE,
            )
        )


@dataclass(frozen=True)
class StepGuidance:
    text: str
    review_state: GuidanceReviewState = GuidanceReviewState.READY

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("Step Guidance must be text.")
        if len(self.text) > MAX_STEP_GUIDANCE_CHARACTERS:
            raise ValueError(
                "Step Guidance cannot exceed "
                f"{MAX_STEP_GUIDANCE_CHARACTERS} characters before redaction."
            )
        normalized = self.text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            raise ValueError("Empty Step Guidance must be stored as no guidance.")
        if any(_unsafe_guidance_character(character) for character in normalized):
            raise ValueError("Step Guidance contains unsupported control characters.")
        sanitized = redact_step_guidance(normalized)
        if len(sanitized) > MAX_STEP_GUIDANCE_CHARACTERS:
            raise ValueError(
                "Step Guidance cannot exceed "
                f"{MAX_STEP_GUIDANCE_CHARACTERS} characters after redaction."
            )
        object.__setattr__(self, "text", sanitized)

    def to_dict(self) -> dict[str, str]:
        return {"text": self.text, "review_state": self.review_state.value}

    def marked_for_review(self) -> StepGuidance:
        return StepGuidance(self.text, GuidanceReviewState.NEEDS_REVIEW)

    @classmethod
    def from_dict(cls, value: Any) -> StepGuidance:
        if not isinstance(value, Mapping) or set(value) != {"text", "review_state"}:
            raise ValueError("Step Guidance requires text and review_state.")
        raw_text = value.get("text")
        raw_state = value.get("review_state")
        if not isinstance(raw_text, str):
            raise ValueError("Step Guidance text must be a string.")
        try:
            review_state = GuidanceReviewState(raw_state)
        except (TypeError, ValueError) as error:
            raise ValueError("Step Guidance review state is invalid.") from error
        return cls(raw_text, review_state)


@dataclass(frozen=True)
class StepAttemptContext:
    capability_profile: StepCapabilityProfile
    guidance: str | None
    guidance_precedence: str = STEP_GUIDANCE_PRECEDENCE

    def __post_init__(self) -> None:
        if self.guidance_precedence != STEP_GUIDANCE_PRECEDENCE:
            raise ValueError("Step attempt guidance precedence is not configurable.")
        if self.guidance is not None:
            object.__setattr__(
                self,
                "guidance",
                StepGuidance(self.guidance).text,
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "capability_profile": self.capability_profile.to_dict(),
            "guidance": self.guidance,
            "guidance_precedence": self.guidance_precedence,
        }


def capability_profile_from_defaults(
    required: Iterable[RequiredCapability],
    defaults: Iterable[CapabilityReference],
) -> StepCapabilityProfile:
    return StepCapabilityProfile(
        tuple(item.reference for item in required) + tuple(defaults)
    )


def redact_step_guidance(value: str) -> str:
    redacted = _PRIVATE_KEY.sub("[redacted-private-key]", value)
    redacted = _BEARER_SECRET.sub("Bearer [redacted]", redacted)
    redacted = _redact_assigned_secrets(redacted)
    redacted = _OPENAI_KEY.sub("[redacted-key]", redacted)
    return _GITHUB_TOKEN.sub("[redacted-github-token]", redacted)


def _redact_assigned_secrets(value: str) -> str:
    """Redact secret assignments with a monotonic, bounded scanner.

    Quoted values are parsed explicitly so malformed input is redacted through
    the end of the guidance instead of being left available to persistence.
    """
    output: list[str] = []
    cursor = 0
    while cursor < len(value):
        assignment = _find_secret_assignment(value, cursor)
        if assignment is None:
            output.append(value[cursor:])
            break

        token_start, separator = assignment
        value_start = separator + 1
        while value_start < len(value) and value[value_start] in " \t\r\n":
            value_start += 1
        output.append(value[cursor:value_start])
        if value_start >= len(value):
            break
        if value[value_start] in "|>":
            output.append("[redacted]")
            cursor = _skip_secret_block(value, value_start, token_start)
            continue
        if value[value_start] in "'\"":
            replacement, cursor = _redact_quoted_secret_value(value, value_start)
            output.append(replacement)
            continue

        output.append("[redacted]")
        line_end = value.find("\n", value_start)
        cursor = len(value) if line_end == -1 else line_end

    return "".join(output)


def _find_secret_assignment(value: str, start: int) -> tuple[int, int] | None:
    """Find the next recognized assignment without backtracking or rescans."""
    cursor = start
    while cursor < len(value):
        if value[cursor] not in _KEY_CHARACTER_SET or (
            cursor > 0 and value[cursor - 1] in _KEY_CHARACTER_SET
        ):
            cursor += 1
            continue

        token_start = cursor
        cursor += 1
        while cursor < len(value) and value[cursor] in _KEY_CHARACTER_SET:
            cursor += 1
        if not _is_secret_key_name(value[token_start:cursor]):
            continue

        separator = cursor
        while separator < len(value) and value[separator] in " \t'\"":
            separator += 1
        if separator < len(value) and value[separator] in ":=":
            return token_start, separator

    return None


def _is_secret_key_name(value: str) -> bool:
    parts = value.lower().replace("-", "_").split("_")
    if not parts or any(not part for part in parts):
        return False
    if any(part in _SECRET_KEY_PARTS for part in parts):
        return True
    if any(
        part == "connection" and index + 1 < len(parts)
        and parts[index + 1] == "string"
        for index, part in enumerate(parts)
    ):
        return True
    if "connectionstring" in parts:
        return True
    return any(
        part in {"api", "access", "private"} and index + 1 < len(parts)
        and parts[index + 1] == "key"
        or part in {"apikey", "accesskey", "privatekey"}
        for index, part in enumerate(parts)
    )


def _redact_quoted_secret_value(value: str, start: int) -> tuple[str, int]:
    """Redact a quoted assignment through its complete logical line.

    A closing quote is not a safe boundary for a secret assignment because the
    value may continue through concatenation or another expression.  Preserve
    the quote style for readable diagnostics, but resume copying only at the
    next line.
    """
    delimiter = value[start : start + 3]
    if delimiter not in {"'''", '\"\"\"'}:
        delimiter = value[start]
    content_start = start + len(delimiter)
    cursor = content_start
    while cursor < len(value):
        if value.startswith(delimiter, cursor):
            if len(delimiter) == 1 and value.startswith(delimiter * 2, cursor):
                cursor += 2
                continue
            following = cursor + len(delimiter)
            if len(delimiter) > 1 or following == len(value) or value[following] in (
                " \t\r\n,.;:)]}"
            ):
                line_end = value.find("\n", following)
                return (
                    f"{delimiter}[redacted]{delimiter}",
                    len(value) if line_end == -1 else line_end,
                )
        if value[cursor] == "\\" and cursor + 1 < len(value):
            cursor += 2
        else:
            cursor += 1
    return f"{delimiter}[redacted]", len(value)


def _skip_secret_block(value: str, value_start: int, assignment_start: int) -> int:
    line_start = value.rfind("\n", 0, assignment_start) + 1
    line_prefix = value[line_start:assignment_start]
    base_indent = len(line_prefix) if line_prefix.strip() == "" else 0
    cursor = value.find("\n", value_start)
    if cursor == -1:
        return len(value)

    while cursor < len(value):
        next_line_start = cursor + 1
        line_end = value.find("\n", next_line_start)
        if line_end == -1:
            line_end = len(value)
        line = value[next_line_start:line_end]
        indentation = len(line) - len(line.lstrip(" \t"))
        if line.strip() and indentation <= base_indent:
            return cursor
        cursor = line_end
    return cursor


def _capability_references(
    value: Any,
    kind: CapabilityKind,
) -> tuple[CapabilityReference, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Step Capability Profile entries must be string lists.")
    return tuple(CapabilityReference(kind, item) for item in value)


def _unsafe_guidance_character(character: str) -> bool:
    if character in {"\n", "\t"}:
        return False
    return unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
