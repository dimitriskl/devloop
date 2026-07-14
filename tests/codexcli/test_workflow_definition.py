from __future__ import annotations

from pathlib import Path

import pytest

import devloop.components.builtin as builtin_components
from devloop.components.analysis import ANALYSIS_COMPONENT_ID, builtin_component_registry
from devloop.components.contracts import (
    ComponentManifest,
    ComponentRegistry,
    ComponentRegistryError,
    StepExecutionPolicy,
    package_source_hash,
)
from devloop.domain.identifiers import StepInstanceId
from devloop.domain.outcomes import StepOutcome
from devloop.workflow.definition import (
    WorkflowDefinitionError,
    WorkflowStepDefinition,
    load_standard_workflow,
    validate_component_ports,
)


def test_standard_workflow_resolves_analysis_through_registered_typed_ports() -> None:
    definition = load_standard_workflow()
    manifest, runner = builtin_component_registry().resolve(ANALYSIS_COMPONENT_ID)
    analysis = definition.step(StepInstanceId("analysis"))

    validate_component_ports(analysis, manifest)

    assert runner.component_id == ANALYSIS_COMPONENT_ID
    assert definition.definition_hash
    assert analysis.transitions
    assert definition.retry_policy.max_rework_cycles_per_issue > 0
    assert definition.retry_policy.max_transient_backend_retries >= 0


def test_workflow_definition_resolves_declared_navigation_targets() -> None:
    definition = load_standard_workflow()

    assert definition.transition_target(
        StepInstanceId("development"), StepOutcome.SUCCEEDED
    ) == StepInstanceId("code-review")
    assert (
        definition.transition_target(StepInstanceId("development"), StepOutcome.BLOCKED)
        is None
    )
    assert definition.completion_step() == StepInstanceId("workspace-finalization")
    assert definition.step_for_component(ANALYSIS_COMPONENT_ID).step_id == StepInstanceId(
        "analysis"
    )


def test_component_port_mismatch_is_rejected_before_execution() -> None:
    definition = load_standard_workflow()
    manifest, _ = builtin_component_registry().resolve(ANALYSIS_COMPONENT_ID)
    invalid = ComponentManifest(
        manifest.schema,
        manifest.component_id,
        manifest.version,
        manifest.distribution,
        manifest.package_hash,
        StepExecutionPolicy.READ_ONLY,
        (),
    )

    with pytest.raises(WorkflowDefinitionError, match="port bindings"):
        validate_component_ports(definition.step(StepInstanceId("analysis")), invalid)


def test_component_registry_rejects_invalid_versioned_manifest_metadata() -> None:
    manifest, runner = builtin_component_registry().resolve(ANALYSIS_COMPONENT_ID)
    registry = ComponentRegistry()
    wrong_schema = ComponentManifest(
        "not-a-component-schema",
        manifest.component_id,
        manifest.version,
        manifest.distribution,
        manifest.package_hash,
        manifest.execution_policy,
        manifest.ports,
    )

    with pytest.raises(ComponentRegistryError, match="schema"):
        registry.register(wrong_schema, runner)

    wrong_hash = ComponentManifest(
        manifest.schema,
        manifest.component_id,
        manifest.version,
        manifest.distribution,
        "not-a-sha256",
        manifest.execution_policy,
        manifest.ports,
    )
    with pytest.raises(ComponentRegistryError, match="hash"):
        registry.register(wrong_hash, runner)


def test_duplicate_workflow_port_bindings_are_rejected_before_execution() -> None:
    definition = load_standard_workflow()
    manifest, _ = builtin_component_registry().resolve(ANALYSIS_COMPONENT_ID)
    analysis = definition.step(StepInstanceId("analysis"))
    duplicate = WorkflowStepDefinition(
        analysis.step_id,
        analysis.component_id,
        (*analysis.inputs, analysis.inputs[0]),
        analysis.outputs,
        analysis.transitions,
    )

    with pytest.raises(WorkflowDefinitionError, match="duplicate"):
        validate_component_ports(duplicate, manifest)


def test_installed_registry_discovers_analysis_through_its_declared_entry_point() -> None:
    registry = builtin_components.installed_component_registry()

    manifest, runner = registry.resolve(ANALYSIS_COMPONENT_ID)

    assert manifest.component_id == ANALYSIS_COMPONENT_ID
    assert runner.component_id == ANALYSIS_COMPONENT_ID


def test_component_package_hash_covers_execution_dependencies(tmp_path: Path) -> None:
    package_root = tmp_path / "devloop"
    package_root.mkdir()
    (package_root / "component.py").write_text("COMPONENT = 1\n", encoding="utf-8")
    dependency = package_root / "dependency.py"
    dependency.write_text("DEPENDENCY = 1\n", encoding="utf-8")
    original_hash = package_source_hash(package_root)

    dependency.write_text("DEPENDENCY = 2\n", encoding="utf-8")

    assert package_source_hash(package_root) != original_hash
