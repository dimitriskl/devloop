from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from devloop.domain.identifiers import CapabilityId, StepComponentId


class CapabilityKind(str, Enum):
    SKILL = "SKILL"
    AGENT_REFERENCE = "AGENT_REFERENCE"


@dataclass(frozen=True)
class CapabilityDescriptor:
    capability_id: CapabilityId
    kind: CapabilityKind
    title: str
    description: str

    def __post_init__(self) -> None:
        if not self.title.strip() or not self.description.strip():
            raise ValueError("Capability display content is required.")


@dataclass(frozen=True)
class StepCapabilityDefinition:
    component_id: StepComponentId
    required: tuple[CapabilityId, ...]
    defaults: tuple[CapabilityId, ...]

    def __post_init__(self) -> None:
        if len(set(self.required)) != len(self.required):
            raise ValueError("Required capabilities must be unique.")
        if len(set(self.defaults)) != len(self.defaults):
            raise ValueError("Default capabilities must be unique.")
        if set(self.required) & set(self.defaults):
            raise ValueError("Required and default capabilities must be distinct.")


@dataclass(frozen=True)
class ResolvedCapabilityProfile:
    component_id: StepComponentId
    capabilities: tuple[CapabilityId, ...]

    def __post_init__(self) -> None:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("Resolved capabilities must be unique.")


class CapabilityCatalog:
    """Installed capabilities and per-component contract defaults."""

    def __init__(
        self,
        descriptors: tuple[CapabilityDescriptor, ...],
        definitions: tuple[StepCapabilityDefinition, ...],
    ) -> None:
        self._descriptors = {item.capability_id: item for item in descriptors}
        self._definitions = {item.component_id: item for item in definitions}
        if len(self._descriptors) != len(descriptors):
            raise ValueError("Capability IDs must be unique.")
        if len(self._definitions) != len(definitions):
            raise ValueError("Step capability definitions must be unique.")
        declared = {
            capability
            for definition in definitions
            for capability in (*definition.required, *definition.defaults)
        }
        if not declared <= self._descriptors.keys():
            raise ValueError("Step profiles reference an uninstalled capability.")

    @property
    def definitions(self) -> tuple[StepCapabilityDefinition, ...]:
        return tuple(sorted(self._definitions.values(), key=lambda item: item.component_id))

    @property
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(sorted(self._descriptors.values(), key=lambda item: item.capability_id))

    def definition(self, component_id: StepComponentId) -> StepCapabilityDefinition:
        try:
            return self._definitions[component_id]
        except KeyError as error:
            raise ValueError(f"Unknown Step Component: {component_id.value}.") from error

    def descriptor(self, capability_id: CapabilityId) -> CapabilityDescriptor:
        try:
            return self._descriptors[capability_id]
        except KeyError as error:
            raise ValueError(f"Capability is not installed: {capability_id.value}.") from error

    def search(self, query: str) -> tuple[CapabilityDescriptor, ...]:
        needle = query.strip().casefold()
        return tuple(
            item
            for item in self.descriptors
            if not needle
            or needle in item.capability_id.value.casefold()
            or needle in item.title.casefold()
            or needle in item.description.casefold()
        )


def capabilities_for(
    profiles: tuple[ResolvedCapabilityProfile, ...],
    component_id: StepComponentId,
    *,
    fallback: tuple[CapabilityId, ...] = (),
) -> tuple[CapabilityId, ...]:
    """Resolve a run-locked profile, with a compatibility fallback for old snapshots."""
    return next(
        (
            profile.capabilities
            for profile in profiles
            if profile.component_id == component_id
        ),
        fallback,
    )
