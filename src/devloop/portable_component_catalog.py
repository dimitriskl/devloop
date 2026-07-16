from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from . import catalog as capability_catalog
from .portable_workflow import (
    DEVELOPMENT_COMPONENT_ID,
    QA_COMPONENT_ID,
    REVIEWER_COMPONENT_ID,
    PortableRoleAdapter,
    PortableStepComponent,
    PortableStepComponentCatalog,
    StepComponentId,
    default_portable_component_catalog,
)
from .portable_text import normalize_single_line_display_name
from .step_configuration import (
    CapabilityKind,
    CapabilityReference,
    RequiredCapability,
)
from .templates import load_preset


DEFAULT_PORTABLE_PRESET = Path("presets") / "generic-minimal.json"
STEP_ADAPTER_KEY = "step_adapter"
COMPONENT_ID_KEY = "component_id"
DISPLAY_NAME_KEY = "display_name"

_STEP_ADAPTER_COMPONENT_IDS = {
    "coder": DEVELOPMENT_COMPONENT_ID,
    "reviewer": REVIEWER_COMPONENT_ID,
    "qa": QA_COMPONENT_ID,
}


def build_portable_component_catalog(
    bundle_root: Path,
    roles: Mapping[str, Mapping[str, Any]] | None = None,
) -> PortableStepComponentCatalog:
    """Build the portable catalog from installed roles and step adapters."""
    builtin_catalog = default_portable_component_catalog()
    role_definitions = roles if roles is not None else _load_installed_roles(bundle_root)
    installed_capability_paths = _installed_capability_paths(bundle_root)
    custom_components = _custom_components(
        role_definitions,
        builtin_catalog,
        bundle_root,
        installed_capability_paths,
    )
    return PortableStepComponentCatalog(
        (*builtin_catalog.components, *custom_components)
    )


def _load_installed_roles(bundle_root: Path) -> Mapping[str, Mapping[str, Any]]:
    preset_path = bundle_root / DEFAULT_PORTABLE_PRESET
    if not preset_path.is_file():
        return {}
    return load_preset(preset_path).roles


def _custom_components(
    role_definitions: Mapping[str, Mapping[str, Any]],
    builtin_catalog: PortableStepComponentCatalog,
    bundle_root: Path,
    installed_capability_paths: frozenset[Path],
) -> tuple[PortableStepComponent, ...]:
    components: list[PortableStepComponent] = []
    for role_name, role_configuration in role_definitions.items():
        if not isinstance(role_name, str) or not isinstance(role_configuration, Mapping):
            raise ValueError("Portable preset roles must map names to objects.")
        if STEP_ADAPTER_KEY not in role_configuration:
            continue
        components.append(
            _component_from_role(
                role_name,
                role_configuration,
                builtin_catalog,
                bundle_root,
                installed_capability_paths,
            )
        )
    return tuple(components)


def _installed_capability_paths(bundle_root: Path) -> frozenset[Path]:
    installed = capability_catalog.discover(bundle_root)
    return frozenset(
        entry.path.resolve()
        for entry in (*installed.skills, *installed.agents)
    )


def _component_from_role(
    role_name: str,
    configuration: Mapping[str, Any],
    builtin_catalog: PortableStepComponentCatalog,
    bundle_root: Path,
    installed_capability_paths: frozenset[Path],
) -> PortableStepComponent:
    if not role_name.strip():
        raise ValueError("Portable component roles require a non-empty name.")
    step_adapter = configuration.get(STEP_ADAPTER_KEY)
    if (
        not isinstance(step_adapter, str)
        or step_adapter not in _STEP_ADAPTER_COMPONENT_IDS
    ):
        raise ValueError(
            f"Portable role {role_name!r} selects an unknown step adapter: "
            f"{step_adapter!r}."
        )
    _validate_capability_paths(
        role_name,
        configuration,
        bundle_root,
        installed_capability_paths,
    )
    raw_component_id = configuration.get(COMPONENT_ID_KEY)
    if not isinstance(raw_component_id, str):
        raise ValueError(
            f"Portable role {role_name!r} requires a {COMPONENT_ID_KEY!r}."
        )
    raw_display_name = configuration.get(DISPLAY_NAME_KEY)
    display_name = normalize_single_line_display_name(
        raw_display_name,
        field_name=f"Portable role {role_name!r} {DISPLAY_NAME_KEY!r}",
    )
    adapter_component = builtin_catalog.resolve(
        _STEP_ADAPTER_COMPONENT_IDS[step_adapter]
    )
    return replace(
        adapter_component,
        component_id=StepComponentId(raw_component_id),
        default_display_name=display_name,
        adapter=PortableRoleAdapter(role_name, step_adapter),
        required_capabilities=_configured_required_capabilities(
            role_name,
            configuration,
            bundle_root,
        ),
        default_capabilities=_configured_default_capabilities(
            configuration,
            bundle_root,
        ),
    )


def _validate_capability_paths(
    role_name: str,
    configuration: Mapping[str, Any],
    bundle_root: Path,
    installed_capability_paths: frozenset[Path],
) -> None:
    for capability_kind in (
        "skills",
        "agents",
        "required_skills",
        "required_agents",
    ):
        configured_paths = configuration.get(capability_kind, [])
        if not isinstance(configured_paths, list):
            raise ValueError(
                f"Portable role {role_name!r} {capability_kind} must be a list."
            )
        for raw_path in configured_paths:
            if not isinstance(raw_path, str):
                raise ValueError(
                    f"Portable role {role_name!r} has a non-string "
                    f"{capability_kind} path."
                )
            configured_path = Path(raw_path)
            resolved_path = (
                configured_path
                if configured_path.is_absolute()
                else bundle_root / configured_path
            ).resolve()
            if resolved_path not in installed_capability_paths:
                raise ValueError(
                    f"Portable role {role_name!r} references an uninstalled "
                    f"capability: {raw_path!r}."
                )


def _configured_default_capabilities(
    configuration: Mapping[str, Any],
    bundle_root: Path,
) -> tuple[CapabilityReference, ...]:
    return _configured_capabilities(configuration, bundle_root, "skills", "agents")


def _configured_required_capabilities(
    role_name: str,
    configuration: Mapping[str, Any],
    bundle_root: Path,
) -> tuple[RequiredCapability, ...]:
    return tuple(
        RequiredCapability(
            reference,
            f"Portable role {role_name!r} declares this capability as required "
            "by its component contract.",
        )
        for reference in _configured_capabilities(
            configuration,
            bundle_root,
            "required_skills",
            "required_agents",
        )
    )


def _configured_capabilities(
    configuration: Mapping[str, Any],
    bundle_root: Path,
    skills_key: str,
    agents_key: str,
) -> tuple[CapabilityReference, ...]:
    references: list[CapabilityReference] = []
    for key, kind in (
        (skills_key, CapabilityKind.SKILL),
        (agents_key, CapabilityKind.AGENT_REFERENCE),
    ):
        for raw_path in configuration.get(key, []):
            configured_path = Path(raw_path)
            resolved_path = (
                configured_path
                if configured_path.is_absolute()
                else bundle_root / configured_path
            ).resolve()
            relative_path = resolved_path.relative_to(bundle_root.resolve()).as_posix()
            references.append(CapabilityReference(kind, relative_path))
    return tuple(references)
