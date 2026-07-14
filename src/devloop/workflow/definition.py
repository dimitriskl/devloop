from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from typing import cast

from devloop.components.contracts import ComponentManifest, PortDirection
from devloop.domain.identifiers import (
    DataContractId,
    StepComponentId,
    StepInstanceId,
    WorkflowId,
)
from devloop.domain.run import StepOutcome
from devloop.domain.scheduler import RetryPolicy

WORKFLOW_SCHEMA = "devloop.workflow-definition/v1"
STANDARD_WORKFLOW_RESOURCE = "standard.v1.json"


class WorkflowDefinitionError(ValueError):
    pass


@dataclass(frozen=True)
class PortBinding:
    port_name: str
    contract_id: DataContractId
    source: str


@dataclass(frozen=True)
class WorkflowStepDefinition:
    step_id: StepInstanceId
    component_id: StepComponentId
    inputs: tuple[PortBinding, ...]
    outputs: tuple[PortBinding, ...]
    transitions: tuple[tuple[StepOutcome, StepInstanceId | None], ...]


@dataclass(frozen=True)
class WorkflowDefinition:
    schema: str
    workflow_id: WorkflowId
    version: str
    definition_hash: str
    retry_policy: RetryPolicy
    steps: tuple[WorkflowStepDefinition, ...]

    def step(self, step_id: StepInstanceId) -> WorkflowStepDefinition:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise WorkflowDefinitionError(f"Unknown workflow step: {step_id}.")

    def step_for_component(self, component_id: StepComponentId) -> WorkflowStepDefinition:
        candidates = tuple(step for step in self.steps if step.component_id == component_id)
        if len(candidates) != 1:
            raise WorkflowDefinitionError(
                f"Workflow Definition must resolve one step for component {component_id}."
            )
        return candidates[0]

    def transition_target(
        self,
        step_id: StepInstanceId,
        outcome: StepOutcome,
    ) -> StepInstanceId | None:
        for candidate, target in self.step(step_id).transitions:
            if candidate is outcome:
                return target
        raise WorkflowDefinitionError(
            f"Workflow step {step_id} has no transition for {outcome.value}."
        )

    def required_transition_target(
        self,
        step_id: StepInstanceId,
        outcome: StepOutcome,
    ) -> StepInstanceId:
        target = self.transition_target(step_id, outcome)
        if target is None:
            raise WorkflowDefinitionError(
                f"Workflow step {step_id} has a terminal {outcome.value} transition."
            )
        return target

    def completion_step(self) -> StepInstanceId:
        candidates = tuple(
            step.step_id
            for step in self.steps
            if any(
                outcome is StepOutcome.SUCCEEDED and target is None
                for outcome, target in step.transitions
            )
        )
        if len(candidates) != 1:
            raise WorkflowDefinitionError(
                "Workflow Definition requires one successful completion step."
            )
        return candidates[0]


def load_standard_workflow() -> WorkflowDefinition:
    raw = files("devloop.workflows").joinpath(STANDARD_WORKFLOW_RESOURCE).read_bytes()
    return parse_workflow_definition(raw)


def parse_workflow_definition(raw: bytes) -> WorkflowDefinition:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowDefinitionError("Workflow Definition is not valid UTF-8 JSON.") from error
    if not isinstance(payload, dict):
        raise WorkflowDefinitionError("Workflow Definition must be a JSON object.")
    data = cast(dict[str, object], payload)
    if data.get("schema") != WORKFLOW_SCHEMA:
        raise WorkflowDefinitionError("Unsupported Workflow Definition schema.")
    steps_data = data.get("steps")
    if not isinstance(steps_data, list) or not steps_data:
        raise WorkflowDefinitionError("Workflow Definition requires at least one step.")
    steps = tuple(_parse_step(item) for item in steps_data)
    step_ids = {step.step_id for step in steps}
    if len(step_ids) != len(steps):
        raise WorkflowDefinitionError("Workflow Definition contains duplicate step IDs.")
    for step in steps:
        for _, target in step.transitions:
            if target is not None and target not in step_ids:
                raise WorkflowDefinitionError(f"Transition target does not exist: {target}.")
    return WorkflowDefinition(
        schema=WORKFLOW_SCHEMA,
        workflow_id=WorkflowId(_string(data, "id")),
        version=_string(data, "version"),
        definition_hash=hashlib.sha256(raw).hexdigest(),
        retry_policy=_retry_policy(data.get("retry_policy")),
        steps=steps,
    )


def validate_component_ports(
    definition: WorkflowStepDefinition,
    manifest: ComponentManifest,
) -> None:
    if definition.component_id != manifest.component_id:
        raise WorkflowDefinitionError("Workflow step resolved to a different component.")
    input_names = [binding.port_name for binding in definition.inputs]
    output_names = [binding.port_name for binding in definition.outputs]
    if len(input_names) != len(set(input_names)) or len(output_names) != len(set(output_names)):
        raise WorkflowDefinitionError("Workflow step contains duplicate port bindings.")
    known_inputs = {
        port.name: port.contract_id
        for port in manifest.ports
        if port.direction is PortDirection.INPUT
    }
    known_outputs = {
        port.name: port.contract_id
        for port in manifest.ports
        if port.direction is PortDirection.OUTPUT
    }
    required_inputs = {
        port.name
        for port in manifest.ports
        if port.direction is PortDirection.INPUT and port.required
    }
    required_outputs = {
        port.name
        for port in manifest.ports
        if port.direction is PortDirection.OUTPUT and port.required
    }
    actual_inputs = {binding.port_name: binding.contract_id for binding in definition.inputs}
    actual_outputs = {binding.port_name: binding.contract_id for binding in definition.outputs}
    if (
        not required_inputs.issubset(actual_inputs)
        or not required_outputs.issubset(actual_outputs)
        or any(known_inputs.get(name) != contract for name, contract in actual_inputs.items())
        or any(known_outputs.get(name) != contract for name, contract in actual_outputs.items())
    ):
        raise WorkflowDefinitionError("Workflow port bindings do not match the component manifest.")


def _parse_step(value: object) -> WorkflowStepDefinition:
    if not isinstance(value, dict):
        raise WorkflowDefinitionError("Workflow steps must be JSON objects.")
    data = cast(dict[str, object], value)
    return WorkflowStepDefinition(
        step_id=StepInstanceId(_string(data, "id")),
        component_id=StepComponentId(_string(data, "component")),
        inputs=_bindings(data.get("inputs")),
        outputs=_bindings(data.get("outputs")),
        transitions=_transitions(data.get("transitions")),
    )


def _retry_policy(value: object) -> RetryPolicy:
    if not isinstance(value, dict):
        raise WorkflowDefinitionError("Workflow Definition requires a retry policy.")
    data = cast(dict[str, object], value)
    try:
        return RetryPolicy(
            _integer(data, "max_rework_cycles_per_issue"),
            _integer(data, "max_transient_backend_retries"),
        )
    except (TypeError, ValueError) as error:
        raise WorkflowDefinitionError("Workflow retry policy is invalid.") from error


def _integer(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkflowDefinitionError(f"Workflow Definition integer is invalid: {key}.")
    return value


def _bindings(value: object) -> tuple[PortBinding, ...]:
    if not isinstance(value, list):
        raise WorkflowDefinitionError("Workflow port bindings must be a list.")
    bindings: list[PortBinding] = []
    for item in value:
        if not isinstance(item, dict):
            raise WorkflowDefinitionError("Workflow port bindings must be objects.")
        data = cast(dict[str, object], item)
        bindings.append(
            PortBinding(
                port_name=_string(data, "port"),
                contract_id=DataContractId(_string(data, "contract")),
                source=_string(data, "source"),
            )
        )
    return tuple(bindings)


def _transitions(value: object) -> tuple[tuple[StepOutcome, StepInstanceId | None], ...]:
    if not isinstance(value, dict):
        raise WorkflowDefinitionError("Workflow transitions must be an object.")
    data = cast(dict[str, object], value)
    transitions: list[tuple[StepOutcome, StepInstanceId | None]] = []
    for outcome_value, target_value in data.items():
        outcome = StepOutcome(outcome_value)
        target = None if target_value is None else StepInstanceId(_typed_string(target_value))
        transitions.append((outcome, target))
    return tuple(transitions)


def _string(data: dict[str, object], key: str) -> str:
    return _typed_string(data.get(key))


def _typed_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise WorkflowDefinitionError("Workflow Definition contains a missing string value.")
    return value
