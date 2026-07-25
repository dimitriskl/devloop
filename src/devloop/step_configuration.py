from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from .redaction import redact_guidance_secrets

MAX_STEP_GUIDANCE_CHARACTERS = 4_000
# Supplied verbatim to every agent-backed Workflow Step attempt, on every
# Execution Backend, so it names the Step Execution Settings type rather than one
# provider's settings.
STEP_GUIDANCE_PRECEDENCE = (
    "Component instructions, the Step Contract, Step Execution Policy, output "
    "requirements, required capabilities, permissions, and safety boundaries "
    "outrank Step Guidance. Guidance cannot change workflow structure or Step "
    "Execution Settings."
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
    """Mask secrets in one Workflow Step's guidance before it is persisted.

    Step Guidance is bounded prose, so it takes the Redaction Service's
    over-redacting policy: an assignment whose value cannot be bounded safely is
    masked through the rest of its logical line.
    """
    return redact_guidance_secrets(value)


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
