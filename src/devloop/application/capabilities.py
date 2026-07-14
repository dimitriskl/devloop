from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from devloop.domain.capabilities import (
    CapabilityCatalog,
    CapabilityDescriptor,
    CapabilityKind,
    ResolvedCapabilityProfile,
    StepCapabilityDefinition,
)
from devloop.domain.identifiers import CapabilityId, StepComponentId

CAPABILITY_PREFERENCES_SCHEMA = "devloop.capability-preferences/v1"
CAPABILITY_PREFERENCES_FILENAME = "capability-profiles.json"


class CapabilityProfileError(RuntimeError):
    pass


@dataclass(frozen=True)
class CapabilitySelection:
    component_id: StepComponentId
    required: tuple[CapabilityId, ...]
    defaults: tuple[CapabilityId, ...]
    selected: tuple[CapabilityId, ...]

    @property
    def resolved(self) -> tuple[CapabilityId, ...]:
        return tuple(dict.fromkeys((*self.required, *self.selected)))


@dataclass(frozen=True)
class CapabilityProfileSet:
    profiles: tuple[CapabilitySelection, ...]

    def profile(self, component_id: StepComponentId) -> CapabilitySelection:
        try:
            return next(item for item in self.profiles if item.component_id == component_id)
        except StopIteration as error:
            raise ValueError(f"Unknown Step Component: {component_id.value}.") from error

    def resolved(self) -> tuple[ResolvedCapabilityProfile, ...]:
        return tuple(
            ResolvedCapabilityProfile(item.component_id, item.resolved)
            for item in self.profiles
        )


class CapabilityProfileService:
    """Loads user-wide defaults and commits edits only when a session applies."""

    def __init__(self, user_config: Path, catalog: CapabilityCatalog) -> None:
        self._path = user_config / CAPABILITY_PREFERENCES_FILENAME
        self._catalog = catalog

    def begin(self) -> CapabilityOptionsSession:
        return CapabilityOptionsSession(self, self._catalog, self._load_selected())

    def resolved_profiles(self) -> tuple[ResolvedCapabilityProfile, ...]:
        session = self.begin()
        return session.current.resolved()

    def _load_selected(self) -> dict[StepComponentId, tuple[CapabilityId, ...]]:
        defaults = {
            item.component_id: item.defaults for item in self._catalog.definitions
        }
        if not self._path.exists():
            return defaults
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CapabilityProfileError("Capability preferences are unreadable.") from error
        if not isinstance(payload, dict) or payload.get("schema") != CAPABILITY_PREFERENCES_SCHEMA:
            raise CapabilityProfileError("Capability preferences use an unsupported schema.")
        values = payload.get("profiles")
        if not isinstance(values, list):
            raise CapabilityProfileError("Capability preferences profiles are invalid.")
        selected = defaults.copy()
        for value in values:
            if not isinstance(value, dict):
                raise CapabilityProfileError("A capability preference profile is invalid.")
            row = cast(dict[str, object], value)
            component_value = row.get("component_id")
            capabilities_value = row.get("selected")
            if not isinstance(component_value, str) or not isinstance(capabilities_value, list):
                raise CapabilityProfileError("A capability preference profile is invalid.")
            try:
                component_id = StepComponentId(component_value)
                self._catalog.definition(component_id)
                capabilities = tuple(CapabilityId(cast(str, item)) for item in capabilities_value)
                for capability in capabilities:
                    self._catalog.descriptor(capability)
            except (TypeError, ValueError) as error:
                raise CapabilityProfileError(
                    "A capability preference profile is invalid."
                ) from error
            if len(set(capabilities)) != len(capabilities):
                raise CapabilityProfileError("Selected capabilities must be unique.")
            definition = self._catalog.definition(component_id)
            if set(capabilities) & set(definition.required):
                raise CapabilityProfileError(
                    "Required capabilities cannot be stored as replaceable selections."
                )
            selected[component_id] = capabilities
        return selected

    def _save(self, selected: dict[StepComponentId, tuple[CapabilityId, ...]]) -> None:
        payload = {
            "schema": CAPABILITY_PREFERENCES_SCHEMA,
            "profiles": [
                {
                    "component_id": definition.component_id.value,
                    "selected": [
                        item.value for item in selected[definition.component_id]
                    ],
                }
                for definition in self._catalog.definitions
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(
            f".{self._path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise CapabilityProfileError("Unable to save capability preferences.") from error


class CapabilityOptionsSession:
    def __init__(
        self,
        service: CapabilityProfileService,
        catalog: CapabilityCatalog,
        selected: dict[StepComponentId, tuple[CapabilityId, ...]],
    ) -> None:
        self._service = service
        self._catalog = catalog
        self._selected = selected.copy()
        self._closed = False

    @property
    def current(self) -> CapabilityProfileSet:
        self._ensure_open()
        return CapabilityProfileSet(
            tuple(
                self._selection(definition)
                for definition in self._catalog.definitions
            )
        )

    def profile(self, component_id: StepComponentId) -> CapabilitySelection:
        self._ensure_open()
        return self._selection(self._catalog.definition(component_id))

    def search(self, query: str) -> tuple[CapabilityDescriptor, ...]:
        self._ensure_open()
        return self._catalog.search(query)

    def toggle(self, component_id: StepComponentId, capability_id: CapabilityId) -> None:
        self._ensure_open()
        definition = self._catalog.definition(component_id)
        self._catalog.descriptor(capability_id)
        if capability_id in definition.required:
            raise ValueError("A required capability is locked and cannot be changed.")
        selected = list(self._selected[component_id])
        if capability_id in selected:
            selected.remove(capability_id)
        else:
            selected.append(capability_id)
        self._selected[component_id] = tuple(selected)

    def reset(self, component_id: StepComponentId | None = None) -> None:
        self._ensure_open()
        definitions = (
            self._catalog.definitions
            if component_id is None
            else (self._catalog.definition(component_id),)
        )
        for definition in definitions:
            self._selected[definition.component_id] = definition.defaults

    def apply(self) -> CapabilityProfileSet:
        self._ensure_open()
        result = self.current
        self._service._save(self._selected)
        self._closed = True
        return result

    def cancel(self) -> None:
        self._ensure_open()
        self._closed = True

    def _selection(self, definition: StepCapabilityDefinition) -> CapabilitySelection:
        return CapabilitySelection(
            definition.component_id,
            definition.required,
            definition.defaults,
            self._selected[definition.component_id],
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("The capability options transaction is closed.")


def standard_capability_catalog() -> CapabilityCatalog:
    skill = CapabilityKind.SKILL
    agent = CapabilityKind.AGENT_REFERENCE

    def capability(
        value: str,
        kind: CapabilityKind,
        title: str,
        description: str,
    ) -> CapabilityDescriptor:
        return CapabilityDescriptor(CapabilityId(value), kind, title, description)

    descriptors = (
        capability(
            "to-prd",
            skill,
            "PRD synthesis",
            "Create a structured product requirements document.",
        ),
        capability(
            "to-issues",
            skill,
            "Issue planning",
            "Create agent-ready vertical Issue slices.",
        ),
        capability(
            "implement",
            skill,
            "Implementation",
            "Implement an accepted Issue in the repository.",
        ),
        capability(
            "tdd",
            skill,
            "Test-driven development",
            "Develop behavior through red-green-refactor cycles.",
        ),
        capability(
            "senior-code-reviewer",
            skill,
            "Senior code review",
            "Review changes independently and read-only.",
        ),
        capability(
            "qa-automation-engineer",
            skill,
            "QA automation",
            "Verify acceptance criteria and regression gates.",
        ),
        capability(
            "handoff",
            skill,
            "Handoff",
            "Produce a concise final implementation handoff.",
        ),
        capability(
            "csharp-expert-developer",
            agent,
            "C# expert",
            "Apply repository-aligned C# and .NET guidance.",
        ),
        capability(
            "angular-typescript-developer",
            agent,
            "Angular expert",
            "Apply repository-aligned Angular and TypeScript guidance.",
        ),
    )
    definitions = (
        StepCapabilityDefinition(
            StepComponentId("analysis"),
            (CapabilityId("to-prd"),),
            (CapabilityId("to-issues"),),
        ),
        StepCapabilityDefinition(StepComponentId("workspace-preparation"), (), ()),
        StepCapabilityDefinition(
            StepComponentId("development"),
            (CapabilityId("implement"),),
            (CapabilityId("tdd"),),
        ),
        StepCapabilityDefinition(
            StepComponentId("code-review"),
            (CapabilityId("senior-code-reviewer"),),
            (),
        ),
        StepCapabilityDefinition(
            StepComponentId("qa"),
            (CapabilityId("qa-automation-engineer"),),
            (),
        ),
        StepCapabilityDefinition(
            StepComponentId("workspace-finalization"),
            (CapabilityId("handoff"),),
            (),
        ),
    )
    return CapabilityCatalog(descriptors, definitions)
