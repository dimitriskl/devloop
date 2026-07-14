from __future__ import annotations

from importlib.metadata import entry_points
from typing import cast

from devloop.components.analysis import analysis_component
from devloop.components.contracts import (
    ComponentManifest,
    ComponentRegistry,
    ComponentRegistryError,
    StepComponentRunner,
)
from devloop.components.development import development_component
from devloop.components.finalization import finalization_component
from devloop.components.qa import qa_component
from devloop.components.review import review_component
from devloop.components.workspace import workspace_component

STEP_COMPONENT_ENTRY_POINT_GROUP = "devloop.step_components"


def builtin_component_registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    for manifest, runner in (
        analysis_component(),
        workspace_component(),
        development_component(),
        review_component(),
        qa_component(),
        finalization_component(),
    ):
        registry.register(manifest, runner)
    return registry


def installed_component_registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    discovered = tuple(
        sorted(
            entry_points().select(group=STEP_COMPONENT_ENTRY_POINT_GROUP),
            key=lambda item: item.name,
        )
    )
    if not discovered:
        raise ComponentRegistryError("No installed Workflow Step Components were discovered.")
    for entry_point in discovered:
        factory = entry_point.load()
        if not callable(factory):
            raise ComponentRegistryError(
                f"Component entry point is not callable: {entry_point.name}."
            )
        loaded = factory()
        if not isinstance(loaded, tuple) or len(loaded) != 2:
            raise ComponentRegistryError(
                f"Component entry point returned an invalid registration: {entry_point.name}."
            )
        manifest = loaded[0]
        runner_object = loaded[1]
        if not isinstance(manifest, ComponentManifest):
            raise ComponentRegistryError(
                f"Component entry point returned an invalid manifest: {entry_point.name}."
            )
        if entry_point.name != manifest.component_id.value:
            raise ComponentRegistryError(
                f"Component entry point name differs from its manifest: {entry_point.name}."
            )
        runner = cast(StepComponentRunner, runner_object)
        registry.register(manifest, runner)
    return registry
