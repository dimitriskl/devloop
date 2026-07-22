from __future__ import annotations

import math
import shutil
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from .cli_ui import (
    editor_prompt,
    fit_text_to_screen,
    format_selected_step_line,
    render_action_bar,
    render_context_path,
    render_grouped_commands,
    render_screen_frame,
    render_split_panes,
)
from .lineeditor import display_width
from .terminal_menu import MenuAction, clear_terminal_screen
from .model_catalog import (
    CatalogDiscoveryError,
    CodexModel,
    CodexModelCatalog,
    CodexModelCatalogCache,
    model_catalog_cache_path,
)
from .portable_workflow import (
    CodexExecutionSettings,
    ExecutionBudget,
    FastPreference,
    PortableStepComponent,
    PortableStepComponentCatalog,
    PortBinding,
    StepComponentId,
    StepInstanceId,
    StepOutcome,
    StepScope,
    WorkflowDefinition,
    WorkflowStep,
    canonical_workflow_hash,
    compatible_port_bindings,
    default_portable_component_catalog,
    default_portable_workflow,
    load_portable_workflow,
    validate_port_binding,
)
from .workflow_defaults import WorkflowDefaultStore
from .step_configuration import (
    CapabilityReference,
    GuidanceReviewState,
    STEP_GUIDANCE_PRECEDENCE,
    StepGuidance,
)
from .terminal_text import sanitize_terminal_text

ReadLine = Callable[[str], str]
ReadCommand = Callable[[str], str]
WriteLine = Callable[[str], None]
OpenCapabilities = Callable[["WorkflowDraft", StepInstanceId], None]
ConfigurationUpdates = Callable[[], Mapping[str, object]]
ModelCatalogLoader = Callable[[], CodexModelCatalog]


@dataclass(frozen=True)
class SelectionMenu:
    title: str
    options: tuple[tuple[str, str], ...]
    default_key: str
    cancel_key: str
    description: tuple[str, ...] = ()


SelectOption = Callable[[SelectionMenu], str]

_APPLICATION_SELECTION_COMMAND = "__application_selection_updated__"
_APPLICATION_STEP_PREFIX = "__application_step__:"

WIDE_EDITOR_MINIMUM_WIDTH = 96
EDITOR_COMMAND_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("View", ("current", "future", "graph", "advanced")),
    ("Select step", ("step number", "select")),
    (
        "Edit step",
        (
            "rename",
            "type",
            "model",
            "reasoning",
            "fast",
            "budget",
            "guidance",
            "capabilities",
        ),
    ),
    (
        "Workflow structure",
        (
            "add",
            "insert",
            "duplicate",
            "delete",
            "move-up",
            "move-down",
            "position",
            "route",
            "bind",
        ),
    ),
    ("Catalog", ("retry-catalog",)),
    ("Draft", ("undo", "reset-step", "reset-workflow")),
    ("Finish", ("apply", "cancel")),
)

WORKFLOW_ACTION_BAR: tuple[tuple[str, str], ...] = (
    ("Up/Down", "Select step"),
    ("Enter", "Actions"),
    ("F1", "Help"),
    ("F2", "Apply"),
    ("F3", "Route map"),
    ("F4", "Details"),
    ("F5", "Add"),
    ("F7", "Capabilities"),
    ("F9", "Actions"),
    ("Esc", "Cancel"),
)

WORKFLOW_ACTIONS: tuple[MenuAction, ...] = (
    MenuAction("Help", "Keyboard and screen guide", "help"),
    MenuAction("View", "Inspect current run", "current"),
    MenuAction("View", "Edit future runs", "future"),
    MenuAction("View", "Show or hide route map", "graph"),
    MenuAction("View", "Show or hide technical details", "advanced"),
    MenuAction("Step", "Select any workflow step", "select"),
    MenuAction("Step", "Rename selected step", "rename"),
    MenuAction("Step", "Change component type", "type"),
    MenuAction("Step", "Choose model", "model"),
    MenuAction("Step", "Choose reasoning effort", "reasoning"),
    MenuAction("Step", "Toggle Fast mode", "fast"),
    MenuAction("Step", "Edit execution budget", "budget"),
    MenuAction("Step", "Edit guidance", "guidance"),
    MenuAction("Step", "Manage capabilities", "capabilities"),
    MenuAction("Structure", "Add step to the end", "add"),
    MenuAction("Structure", "Insert step at a position", "insert"),
    MenuAction("Structure", "Duplicate selected step", "duplicate"),
    MenuAction("Structure", "Delete selected step", "delete"),
    MenuAction("Structure", "Move selected step earlier", "move-up"),
    MenuAction("Structure", "Move selected step later", "move-down"),
    MenuAction("Structure", "Move selected step to position", "position"),
    MenuAction("Structure", "Edit outcome route", "route"),
    MenuAction("Structure", "Edit input binding", "bind"),
    MenuAction("Draft", "Undo last edit", "undo"),
    MenuAction("Draft", "Reset selected step", "reset-step"),
    MenuAction("Draft", "Reset entire workflow", "reset-workflow"),
    MenuAction("Catalog", "Retry model catalog", "retry-catalog"),
    MenuAction("Finish", "Apply future-run default", "apply"),
    MenuAction("Finish", "Cancel without saving", "cancel"),
)

_PREVIOUS_STEP_COMMAND = "__previous_step__"
_NEXT_STEP_COMMAND = "__next_step__"


def _parse_one_based_integer(value: str) -> int | None:
    if not value.isascii() or not value.isdecimal():
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 1 else None


def _parse_positive_seconds(value: str) -> float | None:
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


class EditorResult(str, Enum):
    APPLIED = "APPLIED"
    CANCELLED = "CANCELLED"


class EditorScope(str, Enum):
    CURRENT_RUN = "CURRENT_RUN"
    FUTURE_RUNS = "FUTURE_RUNS"


class WorkflowDefaultRecoveryState(str, Enum):
    NORMAL = "NORMAL"
    RESET_REQUIRED = "RESET_REQUIRED"
    APPLY_READY = "APPLY_READY"


@dataclass(frozen=True)
class DuplicateResult:
    step_instance_id: StepInstanceId
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class TransitionImpact:
    source_step_id: StepInstanceId
    source_display_name: str
    outcome: StepOutcome
    target_step_id: StepInstanceId | None


@dataclass(frozen=True)
class BindingImpact:
    consumer_step_id: StepInstanceId
    consumer_display_name: str
    input_port: str
    producer_step_id: StepInstanceId
    output_port: str


@dataclass(frozen=True)
class PrimaryPathRepair:
    predecessor_step_id: StepInstanceId | None
    successor_step_id: StepInstanceId | None


@dataclass(frozen=True)
class DeletePreview:
    step_instance_id: StepInstanceId
    step_display_name: str
    transition_impacts: tuple[TransitionImpact, ...]
    binding_impacts: tuple[BindingImpact, ...]
    primary_path_repair: PrimaryPathRepair | None
    workflow_hash: str


class WorkflowDraft:
    """Owns isolated workflow edits until the caller explicitly applies them."""

    def __init__(
        self,
        workflow: WorkflowDefinition,
        catalog: PortableStepComponentCatalog,
    ) -> None:
        self._workflow = workflow
        self._catalog = catalog
        self._history: list[WorkflowDefinition] = []

    @property
    def workflow(self) -> WorkflowDefinition:
        return self._workflow

    def rename(self, step_id: StepInstanceId, display_name: str) -> None:
        document = self._workflow.to_dict()
        for step in document["steps"]:
            if step["instance_id"] == step_id:
                step["display_name"] = display_name
                break
        edited = load_portable_workflow(document, self._catalog)
        self._history.append(self._workflow)
        self._workflow = edited

    def set_codex_settings(
        self,
        step_id: StepInstanceId,
        settings: CodexExecutionSettings,
    ) -> None:
        step = self._workflow.step(step_id)
        component = self._catalog.resolve(step.component_id)
        if not component.is_codex_backed:
            raise ValueError(
                f"Local deterministic step {step.display_name!r} has no Codex settings."
            )
        replacement = replace(step, codex_settings=settings)
        self._history.append(self._workflow)
        self._workflow = replace(
            self._workflow,
            steps=tuple(
                replacement if candidate.instance_id == step_id else candidate
                for candidate in self._workflow.steps
            ),
        )

    def set_execution_budget(
        self,
        step_id: StepInstanceId,
        budget: ExecutionBudget,
    ) -> None:
        step = self._workflow.step(step_id)
        replacement = replace(step, execution_budget=budget)
        self._history.append(self._workflow)
        self._workflow = replace(
            self._workflow,
            steps=tuple(
                replacement if candidate.instance_id == step_id else candidate
                for candidate in self._workflow.steps
            ),
        )

    def toggle_capability(
        self,
        step_id: StepInstanceId,
        capability: CapabilityReference,
    ) -> None:
        step = self._workflow.step(step_id)
        component = self._catalog.resolve(step.component_id)
        reason = component.required_capability_reason(capability)
        if reason is not None:
            raise ValueError(
                "This capability is locked by the component contract: " + reason
            )
        replacement = replace(
            step,
            capability_profile=step.capability_profile.toggled(capability),
        )
        self._replace_step(replacement)

    def reset_capabilities(self, step_id: StepInstanceId) -> None:
        step = self._workflow.step(step_id)
        component = self._catalog.resolve(step.component_id)
        self._replace_step(
            replace(
                step,
                capability_profile=component.default_capability_profile(),
            )
        )

    def set_guidance(self, step_id: StepInstanceId, text: str) -> None:
        step = self._workflow.step(step_id)
        component = self._catalog.resolve(step.component_id)
        if not component.is_codex_backed:
            raise ValueError("Local deterministic steps do not accept Step Guidance.")
        self._replace_step(replace(step, guidance=StepGuidance(text)))

    def keep_guidance(self, step_id: StepInstanceId) -> None:
        step = self._workflow.step(step_id)
        if step.guidance is None:
            raise ValueError("The selected step has no Step Guidance to keep.")
        self._replace_step(
            replace(
                step,
                guidance=replace(
                    step.guidance,
                    review_state=GuidanceReviewState.READY,
                ),
            )
        )

    def clear_guidance(self, step_id: StepInstanceId) -> None:
        step = self._workflow.step(step_id)
        if step.guidance is None:
            return
        self._replace_step(replace(step, guidance=None))

    def _replace_step(self, replacement: WorkflowStep) -> None:
        self._history.append(self._workflow)
        self._workflow = replace(
            self._workflow,
            steps=tuple(
                replacement
                if candidate.instance_id == replacement.instance_id
                else candidate
                for candidate in self._workflow.steps
            ),
        )

    def add(self, component_id: StepComponentId) -> StepInstanceId:
        return self.insert(component_id, len(self._workflow.primary_path()) + 1)

    def duplicate(self, step_id: StepInstanceId) -> DuplicateResult:
        source = self._workflow.step(step_id)
        component = self._catalog.resolve(source.component_id)
        if StepOutcome.SUCCEEDED not in component.supported_outcomes:
            raise ValueError(
                f"Step {source.display_name!r} cannot be duplicated safely because "
                "its component does not support SUCCEEDED."
            )
        instance_id = self._new_instance_id()
        duplicated = replace(
            source,
            instance_id=instance_id,
            display_name=self._unique_display_name(source.display_name),
            transitions={
                **source.transitions,
                StepOutcome.SUCCEEDED: source.transitions.get(StepOutcome.SUCCEEDED),
            },
            guidance=(
                source.guidance.marked_for_review()
                if source.guidance is not None
                else None
            ),
        )
        source_replacement = replace(
            source,
            transitions={
                **source.transitions,
                StepOutcome.SUCCEEDED: instance_id,
            },
        )
        steps: list[WorkflowStep] = []
        for step in self._workflow.steps:
            steps.append(source_replacement if step.instance_id == step_id else step)
            if step.instance_id == step_id:
                steps.append(duplicated)
        self._history.append(self._workflow)
        self._workflow = replace(self._workflow, steps=tuple(steps))
        return DuplicateResult(
            step_instance_id=instance_id,
            warnings=tuple(
                f"{duplicated.display_name} output {output_port!r} has no consumer "
                "and requires a deliberate consumer"
                for output_port in component.output_ports
            ),
        )

    def preview_delete(self, step_id: StepInstanceId) -> DeletePreview:
        if len(self._workflow.steps) == 1:
            raise ValueError("A workflow must keep at least one Workflow Step.")
        target = self._workflow.step(step_id)
        primary_path = self._workflow.primary_path()
        primary_position = next(
            (
                index
                for index, step in enumerate(primary_path)
                if step.instance_id == step_id
            ),
            None,
        )
        primary_path_repair: PrimaryPathRepair | None = None
        if primary_position is not None:
            predecessor_id = (
                primary_path[primary_position - 1].instance_id
                if primary_position > 0
                else None
            )
            successor_id = target.transitions.get(StepOutcome.SUCCEEDED)
            if predecessor_id is None and successor_id is None:
                raise ValueError(
                    "The Primary Path start has no unambiguous SUCCEEDED successor."
                )
            primary_path_repair = PrimaryPathRepair(
                predecessor_step_id=predecessor_id,
                successor_step_id=successor_id,
            )
        transition_impacts = tuple(
            TransitionImpact(
                source_step_id=source.instance_id,
                source_display_name=source.display_name,
                outcome=outcome,
                target_step_id=destination_id,
            )
            for source in self._workflow.steps
            for outcome, destination_id in source.transitions.items()
            if source.instance_id == step_id or destination_id == step_id
        )
        binding_impacts = tuple(
            BindingImpact(
                consumer_step_id=consumer.instance_id,
                consumer_display_name=consumer.display_name,
                input_port=input_port,
                producer_step_id=binding.producer_step_id,
                output_port=binding.output_port,
            )
            for consumer in self._workflow.steps
            for input_port, binding in consumer.input_bindings.items()
            if consumer.instance_id == step_id or binding.producer_step_id == step_id
        )
        return DeletePreview(
            step_instance_id=step_id,
            step_display_name=target.display_name,
            transition_impacts=transition_impacts,
            binding_impacts=binding_impacts,
            primary_path_repair=primary_path_repair,
            workflow_hash=canonical_workflow_hash(self._workflow),
        )

    def delete(self, preview: DeletePreview) -> None:
        if preview.workflow_hash != canonical_workflow_hash(self._workflow):
            raise ValueError(
                "The workflow changed after the deletion preview; preview it again."
            )
        self._workflow.step(preview.step_instance_id)
        repair = preview.primary_path_repair
        remaining_steps: list[WorkflowStep] = []
        for step in self._workflow.steps:
            if step.instance_id == preview.step_instance_id:
                continue
            transitions = dict(step.transitions)
            if (
                repair is not None
                and repair.predecessor_step_id == step.instance_id
                and transitions.get(StepOutcome.SUCCEEDED)
                == preview.step_instance_id
            ):
                transitions[StepOutcome.SUCCEEDED] = repair.successor_step_id
            remaining_steps.append(
                replace(
                    step,
                    transitions=transitions,
                )
            )
        start_step_id = self._workflow.start_step_id
        if start_step_id == preview.step_instance_id:
            assert repair is not None and repair.successor_step_id is not None
            start_step_id = repair.successor_step_id
        self._history.append(self._workflow)
        self._workflow = replace(
            self._workflow,
            start_step_id=start_step_id,
            steps=tuple(remaining_steps),
        )

    def change_type(
        self,
        step_id: StepInstanceId,
        component_id: StepComponentId,
    ) -> None:
        source = self._workflow.step(step_id)
        if source.component_id == component_id:
            raise ValueError(
                f"Step {source.display_name!r} already uses Type {component_id}."
            )
        component = self._catalog.resolve(component_id)
        is_primary_path_step = any(
            step.instance_id == step_id for step in self._workflow.primary_path()
        )
        if (
            is_primary_path_step
            and StepOutcome.SUCCEEDED not in component.supported_outcomes
        ):
            raise ValueError(
                f"Type {component_id!r} cannot preserve the step's Primary Path "
                "position because it does not support SUCCEEDED."
            )
        transitions = {
            outcome: None for outcome in component.supported_outcomes
        }
        if StepOutcome.SUCCEEDED in transitions:
            transitions[StepOutcome.SUCCEEDED] = source.transitions.get(
                StepOutcome.SUCCEEDED
            )
        self._replace_step(
            replace(
                source,
                component_id=component_id,
                transitions=transitions,
                input_bindings={},
                codex_settings=component.codex_execution_defaults,
                execution_budget=component.execution_budget_defaults,
                capability_profile=component.default_capability_profile(),
                guidance=(
                    source.guidance.marked_for_review()
                    if source.guidance is not None
                    else None
                ),
            )
        )

    def insert(
        self,
        component_id: StepComponentId,
        position: int,
    ) -> StepInstanceId:
        component = self._catalog.resolve(component_id)
        if StepOutcome.SUCCEEDED not in component.supported_outcomes:
            raise ValueError(
                f"Component {component.component_id!r} cannot join the Primary Path "
                "because it does not support SUCCEEDED."
            )
        primary_path = self._workflow.primary_path()
        if not 1 <= position <= len(primary_path) + 1:
            raise ValueError(
                f"Primary Path Position must be between 1 and {len(primary_path) + 1}."
            )
        insertion_index = position - 1
        instance_id = self._new_instance_id()
        new_step = WorkflowStep(
            instance_id=instance_id,
            display_name=self._unique_display_name(component.default_display_name),
            component_id=component.component_id,
            transitions={outcome: None for outcome in component.supported_outcomes},
            codex_settings=component.codex_execution_defaults,
            execution_budget=component.execution_budget_defaults,
            capability_profile=component.default_capability_profile(),
        )
        edited = self._rewire_primary_path(
            (
                *primary_path[:insertion_index],
                new_step,
                *primary_path[insertion_index:],
            ),
            additional_steps=(new_step,),
        )
        edited = self._with_automatic_bindings(edited, instance_id)
        self._history.append(self._workflow)
        self._workflow = edited
        return instance_id

    def move(self, step_id: StepInstanceId, position: int) -> None:
        primary_path = list(self._workflow.primary_path())
        if not 1 <= position <= len(primary_path):
            raise ValueError(
                f"Primary Path Position must be between 1 and {len(primary_path)}."
            )
        try:
            current_index = next(
                index
                for index, step in enumerate(primary_path)
                if step.instance_id == step_id
            )
        except StopIteration as error:
            raise ValueError("Only Primary Path steps have an editable Position.") from error
        destination_index = position - 1
        if current_index == destination_index:
            return
        moved_step = primary_path.pop(current_index)
        primary_path.insert(destination_index, moved_step)
        edited = self._rewire_primary_path(tuple(primary_path))
        self._history.append(self._workflow)
        self._workflow = edited

    def route(
        self,
        source_step_id: StepInstanceId,
        outcome: StepOutcome,
        target_step_id: StepInstanceId | None,
    ) -> None:
        source = self._workflow.step(source_step_id)
        component = self._catalog.resolve(source.component_id)
        if outcome not in component.supported_outcomes:
            raise ValueError(
                f"Step {source.display_name!r} does not support {outcome.value}."
            )
        if target_step_id is not None:
            self._workflow.step(target_step_id)
        replacement = replace(
            source,
            transitions={**source.transitions, outcome: target_step_id},
        )
        edited = replace(
            self._workflow,
            steps=tuple(
                replacement if step.instance_id == source_step_id else step
                for step in self._workflow.steps
            ),
        )
        try:
            edited.primary_path()
        except ValueError as error:
            raise ValueError(
                "The SUCCEEDED Primary Path cannot contain a loop. "
                "Route a secondary outcome to create a loop."
            ) from error
        self._history.append(self._workflow)
        self._workflow = edited

    def set_binding(
        self,
        step_id: StepInstanceId,
        input_port: str,
        binding: PortBinding | None,
    ) -> None:
        step = self._workflow.step(step_id)
        component = self._catalog.resolve(step.component_id)
        if input_port not in component.all_input_ports:
            raise ValueError(
                f"Step {step.display_name!r} has no input port {input_port!r}."
            )
        bindings = dict(step.input_bindings)
        if binding is None:
            bindings.pop(input_port, None)
        else:
            producer = self._workflow.step(binding.producer_step_id)
            producer_component = self._catalog.resolve(producer.component_id)
            produced_contract = producer_component.output_ports.get(binding.output_port)
            expected_contract = component.all_input_ports[input_port]
            if produced_contract != expected_contract:
                raise ValueError(
                    f"Binding for {step.display_name!r}.{input_port} is incompatible."
                )
            bindings[input_port] = binding
        replacement = replace(step, input_bindings=bindings)
        self._history.append(self._workflow)
        self._workflow = replace(
            self._workflow,
            steps=tuple(
                replacement if candidate.instance_id == step_id else candidate
                for candidate in self._workflow.steps
            ),
        )

    def create_step_on_route(
        self,
        source_step_id: StepInstanceId,
        outcome: StepOutcome,
        component_id: StepComponentId,
    ) -> StepInstanceId:
        source = self._workflow.step(source_step_id)
        source_component = self._catalog.resolve(source.component_id)
        if outcome not in source_component.supported_outcomes:
            raise ValueError(
                f"Step {source.display_name!r} does not support {outcome.value}."
            )
        component = self._catalog.resolve(component_id)
        if StepOutcome.SUCCEEDED not in component.supported_outcomes:
            raise ValueError(
                f"Component {component.component_id!r} cannot start a branch "
                "because it does not support SUCCEEDED."
            )
        instance_id = self._new_instance_id()
        branch_step = WorkflowStep(
            instance_id=instance_id,
            display_name=self._unique_display_name(component.default_display_name),
            component_id=component.component_id,
            transitions={outcome: None for outcome in component.supported_outcomes},
            codex_settings=component.codex_execution_defaults,
            execution_budget=component.execution_budget_defaults,
            capability_profile=component.default_capability_profile(),
        )
        source_replacement = replace(
            source,
            transitions={**source.transitions, outcome: instance_id},
        )
        edited = replace(
            self._workflow,
            steps=tuple(
                source_replacement if step.instance_id == source_step_id else step
                for step in self._workflow.steps
            )
            + (branch_step,),
        )
        edited = self._with_automatic_bindings(edited, instance_id)
        self._history.append(self._workflow)
        self._workflow = edited
        return instance_id

    def insert_step_on_route(
        self,
        source_step_id: StepInstanceId,
        outcome: StepOutcome,
        component_id: StepComponentId,
    ) -> StepInstanceId:
        source = self._workflow.step(source_step_id)
        destination = source.transitions.get(outcome)
        instance_id = self.create_step_on_route(
            source_step_id,
            outcome,
            component_id,
        )
        inserted = self._workflow.step(instance_id)
        replacement = replace(
            inserted,
            transitions={
                **inserted.transitions,
                StepOutcome.SUCCEEDED: destination,
            },
        )
        self._workflow = replace(
            self._workflow,
            steps=tuple(
                replacement if step.instance_id == instance_id else step
                for step in self._workflow.steps
            ),
        )
        return instance_id

    def _unique_display_name(
        self,
        default_name: str,
        *,
        excluding_step_id: StepInstanceId | None = None,
    ) -> str:
        existing = {
            step.display_name.casefold()
            for step in self._workflow.steps
            if step.instance_id != excluding_step_id
        }
        if default_name.casefold() not in existing:
            return default_name
        suffix = 2
        while f"{default_name} {suffix}".casefold() in existing:
            suffix += 1
        return f"{default_name} {suffix}"

    def _new_instance_id(self) -> StepInstanceId:
        existing = {step.instance_id for step in self._workflow.steps}
        while True:
            candidate = StepInstanceId(str(uuid.uuid4()))
            if candidate not in existing:
                return candidate

    def _with_automatic_bindings(
        self,
        workflow: WorkflowDefinition,
        step_id: StepInstanceId,
    ) -> WorkflowDefinition:
        step = workflow.step(step_id)
        component = self._catalog.resolve(step.component_id)
        bindings: dict[str, PortBinding] = {}
        for input_port in component.all_input_ports:
            candidates = compatible_port_bindings(
                workflow,
                step,
                input_port,
                self._catalog,
            )
            if len(candidates) == 1:
                bindings[input_port] = candidates[0]
        replacement = replace(step, input_bindings=bindings)
        return replace(
            workflow,
            steps=tuple(
                replacement if candidate.instance_id == step_id else candidate
                for candidate in workflow.steps
            ),
        )

    def _rewire_primary_path(
        self,
        primary_path: tuple[WorkflowStep, ...],
        *,
        additional_steps: tuple[WorkflowStep, ...] = (),
    ) -> WorkflowDefinition:
        successors = {
            step.instance_id: (
                primary_path[index + 1].instance_id
                if index + 1 < len(primary_path)
                else None
            )
            for index, step in enumerate(primary_path)
        }
        replacements = {
            step.instance_id: replace(
                step,
                transitions={
                    **step.transitions,
                    StepOutcome.SUCCEEDED: successors[step.instance_id],
                },
                input_bindings=self._upstream_bindings(step, primary_path),
            )
            for step in primary_path
        }
        return WorkflowDefinition(
            schema=self._workflow.schema,
            start_step_id=primary_path[0].instance_id,
            steps=tuple(
                replacements.get(step.instance_id, step)
                for step in (*self._workflow.steps, *additional_steps)
            ),
        )

    @staticmethod
    def _upstream_bindings(
        step: WorkflowStep,
        primary_path: tuple[WorkflowStep, ...],
    ) -> dict[str, PortBinding]:
        positions = {
            primary_step.instance_id: index
            for index, primary_step in enumerate(primary_path)
        }
        consumer_position = positions[step.instance_id]
        return {
            input_port: binding
            for input_port, binding in step.input_bindings.items()
            if binding.producer_step_id not in positions
            or positions[binding.producer_step_id] < consumer_position
        }

    def undo(self) -> bool:
        if not self._history:
            return False
        self._workflow = self._history.pop()
        return True

    def reset_step(
        self,
        step_id: StepInstanceId,
    ) -> None:
        step = self._workflow.step(step_id)
        component = self._catalog.resolve(step.component_id)
        self._replace_step(
            replace(
                step,
                display_name=self._unique_display_name(
                    component.default_display_name,
                    excluding_step_id=step_id,
                ),
                codex_settings=component.codex_execution_defaults,
                execution_budget=component.execution_budget_defaults,
                capability_profile=component.default_capability_profile(),
                guidance=None,
            )
        )

    def reset_workflow(self, builtin_workflow: WorkflowDefinition) -> None:
        validated = load_portable_workflow(builtin_workflow.to_dict(), self._catalog)
        self._history.append(self._workflow)
        self._workflow = validated


def run_workflow_editor(
    configuration_path: Path,
    *,
    read_line: ReadLine,
    read_command: ReadCommand | None = None,
    write: WriteLine,
    terminal_width: int,
    terminal_height: int | None = None,
    current_workflow: WorkflowDefinition | None = None,
    catalog: PortableStepComponentCatalog | None = None,
    open_capabilities: OpenCapabilities | None = None,
    configuration_updates: ConfigurationUpdates | None = None,
    model_catalog_loader: ModelCatalogLoader | None = None,
    select_option: SelectOption | None = None,
) -> EditorResult:
    component_catalog = catalog or default_portable_component_catalog()
    height = terminal_height or max(10, shutil.get_terminal_size(fallback=(100, 24)).lines)
    return _WorkflowEditorSession(
        store=WorkflowDefaultStore(configuration_path, component_catalog),
        catalog=component_catalog,
        read_line=read_line,
        read_command=read_command or read_line,
        write=write,
        terminal_width=terminal_width,
        terminal_height=height,
        current_workflow=current_workflow,
        open_capabilities=open_capabilities,
        configuration_updates=configuration_updates,
        model_catalog_loader=model_catalog_loader,
        select_option=select_option,
        model_catalog_cache=CodexModelCatalogCache(
            model_catalog_cache_path(configuration_path)
        ),
    ).run()


class _WorkflowEditorSession:
    def __init__(
        self,
        *,
        store: WorkflowDefaultStore,
        catalog: PortableStepComponentCatalog,
        read_line: ReadLine,
        read_command: ReadCommand,
        write: WriteLine,
        terminal_width: int,
        terminal_height: int,
        current_workflow: WorkflowDefinition | None,
        open_capabilities: OpenCapabilities | None,
        configuration_updates: ConfigurationUpdates | None,
        model_catalog_loader: ModelCatalogLoader | None,
        select_option: SelectOption | None,
        model_catalog_cache: CodexModelCatalogCache,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._read_line = read_line
        self._read_command = read_command
        self._write = write
        self._terminal_width = terminal_width
        self._terminal_height = terminal_height
        self._current_workflow = current_workflow
        self._open_capabilities = open_capabilities
        self._configuration_updates = configuration_updates
        self._model_catalog_loader = model_catalog_loader
        self._select_option = select_option
        self._model_catalog_cache = model_catalog_cache
        self._model_catalog: CodexModelCatalog | None = None
        self._model_catalog_error: str | None = None
        self._default_recovery_state = WorkflowDefaultRecoveryState.NORMAL
        self._default_recovery_error: str | None = None
        try:
            stored_workflow = store.load()
        except ValueError as error:
            stored_workflow = default_portable_workflow()
            self._default_recovery_state = (
                WorkflowDefaultRecoveryState.RESET_REQUIRED
            )
            self._default_recovery_error = sanitize_terminal_text(
                error,
                preserve_newlines=False,
            )
        if self._default_recovery_state is WorkflowDefaultRecoveryState.NORMAL:
            self._load_initial_model_catalog()
        self._draft = WorkflowDraft(stored_workflow, catalog)
        self._future_selected_step_id = next(
            (
                step.instance_id
                for step in self._draft.workflow.primary_path()
                if catalog.resolve(step.component_id).scope is StepScope.ISSUE
            ),
            self._draft.workflow.start_step_id,
        )
        self._current_selected_step_id = (
            current_workflow.primary_path()[0].instance_id
            if current_workflow is not None
            else None
        )
        self._scope = EditorScope.FUTURE_RUNS
        self._show_advanced = False
        self._show_graph = False
        self._notice: str | None = None

    def run(self) -> EditorResult:
        while True:
            from .portable_runtime import active_portable_runtime

            if active_portable_runtime() is None:
                self._render()
                command = self._read_command(self._prompt()).strip()
            else:
                command = self._read_application_command()
            if command == _APPLICATION_SELECTION_COMMAND:
                continue
            result = self._dispatch(command)
            if result is not None:
                return result

    def _read_application_command(self) -> str:
        from .portable_runtime import active_portable_runtime

        portable_runtime = active_portable_runtime()
        assert portable_runtime is not None
        originally_selected_step_id = self._selected_step_id()
        workflow_steps = self._viewed_workflow().steps
        selected_position = next(
            index
            for index, step in enumerate(workflow_steps, start=1)
            if step.instance_id == self._selected_step_id()
        )
        options = (
            *(
                (
                    f"{_APPLICATION_STEP_PREFIX}{index}",
                    f"{index}. {step.display_name}",
                )
                for index, step in enumerate(workflow_steps, start=1)
            ),
            ("actions", "Actions…"),
            ("cancel", "Cancel workflow draft"),
        )
        command = portable_runtime.choose(
            options,
            default_key=f"{_APPLICATION_STEP_PREFIX}{selected_position}",
            cancel_key="cancel",
            render=self._preview_application_step,
            shortcuts={
                "f2": "apply",
                "f3": "graph",
                "f5": "add",
                "f9": "actions",
            },
        )
        if command == "actions":
            return self._read_command(self._prompt()).strip()
        if command.startswith(_APPLICATION_STEP_PREFIX):
            if self._selected_step_id() == originally_selected_step_id:
                return self._read_command(self._prompt()).strip()
            return _APPLICATION_SELECTION_COMMAND
        return command

    def _preview_application_step(self, command: str) -> None:
        raw_position = command.removeprefix(_APPLICATION_STEP_PREFIX)
        position = _parse_one_based_integer(raw_position)
        workflow_steps = self._viewed_workflow().steps
        if position is not None and position <= len(workflow_steps):
            selected = workflow_steps[position - 1].instance_id
            if self._scope is EditorScope.CURRENT_RUN:
                self._current_selected_step_id = selected
            else:
                self._future_selected_step_id = selected
        self._render_application_detail()

    def _render_application_detail(self) -> None:
        from .portable_runtime import active_portable_runtime

        portable_runtime = active_portable_runtime()
        if portable_runtime is None:
            return
        workflow = self._viewed_workflow()
        selected = workflow.step(self._selected_step_id())
        component = self._catalog.resolve(selected.component_id)
        primary_path = workflow.primary_path()
        selected_position = next(
            (
                index
                for index, step in enumerate(primary_path, start=1)
                if step.instance_id == selected.instance_id
            ),
            None,
        )
        if self._show_graph:
            detail_lines = render_graph_preview(
                workflow,
                self._catalog,
                terminal_width=max(40, self._terminal_width - 12),
                max_lines=max(6, self._terminal_height - 8),
            ).splitlines()
        elif self._show_advanced:
            detail_lines = [f"Instance: {selected.instance_id}"]
            detail_lines.extend(
                _port_binding_lines(workflow, selected, component, self._catalog)
            )
        else:
            detail_lines = _compact_detail_lines(
                workflow,
                selected,
                component,
                self._catalog,
                selected_position=selected_position,
                primary_path_length=len(primary_path),
                model_catalog=self._model_catalog,
                model_catalog_error=self._model_catalog_error,
            )
        scope_label = (
            "Current Run (read-only)"
            if self._scope is EditorScope.CURRENT_RUN
            else "Future Runs (editable)"
        )
        lines = [
            f"Dev Loop > Workflow Editor > {scope_label} > {selected.display_name}",
            "",
            *detail_lines,
        ]
        if self._notice:
            lines.extend(("", f"Status: {self._notice}"))
        portable_runtime.show_screen("\n".join(lines))

    def _prompt(self) -> str:
        step = self._viewed_workflow().step(self._selected_step_id())
        return editor_prompt(step.display_name)

    def _render(self) -> None:
        clear_terminal_screen()
        if self._default_recovery_state is not WorkflowDefaultRecoveryState.NORMAL:
            self._write(
                render_workflow_default_recovery(
                    self._default_recovery_error or "The stored default is invalid.",
                    reset_applied=(
                        self._default_recovery_state
                        is WorkflowDefaultRecoveryState.APPLY_READY
                    ),
                    terminal_width=self._terminal_width,
                )
            )
            return
        self._write(
            render_workflow_editor(
                self._viewed_workflow(),
                self._selected_step_id(),
                self._catalog,
                terminal_width=self._terminal_width,
                terminal_height=self._terminal_height,
                current_workflow=self._current_workflow,
                show_advanced=self._show_advanced,
                show_graph=self._show_graph,
                scope=self._scope,
                model_catalog=self._model_catalog,
                model_catalog_error=self._model_catalog_error,
                notice=self._notice,
            )
        )

    def _dispatch(self, command: str) -> EditorResult | None:
        normalized = command.casefold()
        if normalized:
            self._notice = None
        if normalized == _PREVIOUS_STEP_COMMAND:
            self._select_relative_step(-1)
            return None
        if normalized == _NEXT_STEP_COMMAND:
            self._select_relative_step(1)
            return None
        if normalized == "help":
            self._show_help()
            return None
        if self._default_recovery_state is not WorkflowDefaultRecoveryState.NORMAL:
            return self._dispatch_default_recovery(normalized)
        handlers: dict[str, Callable[[], EditorResult | None]] = {
            "current": self._show_current_run,
            "future": self._show_future_runs,
            "select": self._select_any_step,
            "rename": self._rename,
            "add": self._add,
            "insert": self._insert,
            "duplicate": self._duplicate,
            "delete": self._delete,
            "type": self._change_type,
            "move-up": self._move_up,
            "move-down": self._move_down,
            "position": self._set_position,
            "model": self._set_model,
            "reasoning": self._set_reasoning,
            "fast": self._set_fast,
            "budget": self._set_execution_budget,
            "guidance": self._edit_guidance,
            "retry-catalog": self._retry_catalog,
            "route": self._route_outcome,
            "bind": self._bind_input,
            "advanced": self._toggle_advanced,
            "graph": self._toggle_graph,
            "capabilities": self._open_capability_options,
            "undo": self._undo,
            "reset-step": self._reset_step,
            "reset-workflow": self._reset_workflow,
            "apply": self._apply,
            "cancel": self._cancel,
        }
        if self._select_step(command):
            return None
        if self._reject_current_run_mutation(normalized):
            return None
        handler = handlers.get(normalized)
        if handler is not None:
            return handler()
        self._write(
            "\n".join(
                _render_command_lines(
                    self._terminal_width,
                    max_height=self._terminal_height,
                )
            )
        )
        return None

    def _show_help(self) -> None:
        help_text = render_workflow_help(
            terminal_width=self._terminal_width,
            terminal_height=self._terminal_height,
        )
        if self._select_option is not None:
            self._choose_menu(
                SelectionMenu(
                    title="Help",
                    options=(("back", "Back to Workflow Editor"),),
                    default_key="back",
                    cancel_key="back",
                    description=tuple(help_text.splitlines()),
                ),
                fallback_prompt="Press Enter to return to the Workflow Editor: ",
            )
            return
        clear_terminal_screen()
        self._write(help_text)
        self._read_line("Press Enter to return to the Workflow Editor: ")

    def _choose_menu(
        self,
        menu: SelectionMenu,
        *,
        fallback_prompt: str,
        fallback_content: str | None = None,
    ) -> str:
        if self._select_option is not None:
            return self._select_option(menu)
        if fallback_content is not None:
            self._write(fallback_content)
        return self._read_line(fallback_prompt).strip()

    def _select_relative_step(self, offset: int) -> None:
        primary_path = self._viewed_workflow().primary_path()
        selected_id = self._selected_step_id()
        current = next(
            (
                index
                for index, step in enumerate(primary_path)
                if step.instance_id == selected_id
            ),
            0,
        )
        selected = primary_path[(current + offset) % len(primary_path)].instance_id
        if self._scope is EditorScope.CURRENT_RUN:
            self._current_selected_step_id = selected
        else:
            self._future_selected_step_id = selected

    def _dispatch_default_recovery(self, command: str) -> EditorResult | None:
        if command == "cancel":
            return self._cancel()
        if command == "reset-workflow":
            self._draft.reset_workflow(default_portable_workflow())
            self._default_recovery_state = WorkflowDefaultRecoveryState.APPLY_READY
            self._message(
                "Built-in schema-v2 workflow prepared. Choose Apply to atomically "
                "replace the invalid default, or Cancel to leave it unchanged."
            )
            return None
        if command == "apply":
            if (
                self._default_recovery_state
                is WorkflowDefaultRecoveryState.RESET_REQUIRED
            ):
                self._message(
                    "The invalid User Workflow Default must be reset before Apply. "
                    "Choose reset-workflow or Cancel."
                )
                return None
            return self._apply()
        self._message(
            "Recovery mode permits only reset-workflow, Apply, or Cancel so invalid "
            "content cannot be accepted as a draft."
        )
        return None

    def _viewed_workflow(self) -> WorkflowDefinition:
        if self._scope is EditorScope.CURRENT_RUN:
            assert self._current_workflow is not None
            return self._current_workflow
        return self._draft.workflow

    def _selected_step_id(self) -> StepInstanceId:
        if self._scope is EditorScope.CURRENT_RUN:
            return self._current_selected_step_id or self._viewed_workflow().start_step_id
        return self._future_selected_step_id

    def _select_step(self, command: str) -> bool:
        primary_path = self._viewed_workflow().primary_path()
        position = _parse_one_based_integer(command)
        if position is None or position > len(primary_path):
            return False
        selected = primary_path[position - 1].instance_id
        if self._scope is EditorScope.CURRENT_RUN:
            self._current_selected_step_id = selected
        else:
            self._future_selected_step_id = selected
        return True

    def _select_any_step(self) -> None:
        workflow = self._viewed_workflow()
        picker = render_step_picker(
            workflow,
            terminal_width=self._terminal_width,
        )
        raw_position = self._choose_menu(
            SelectionMenu(
                title="Select workflow step",
                options=(
                    *(
                        (str(index), step.display_name)
                        for index, step in enumerate(workflow.steps, start=1)
                    ),
                    ("cancel", "Back to Workflow Editor"),
                ),
                default_key=str(
                    next(
                        (
                            index
                            for index, step in enumerate(workflow.steps, start=1)
                            if step.instance_id == self._selected_step_id()
                        ),
                        1,
                    )
                ),
                cancel_key="cancel",
            ),
            fallback_prompt="Step number (or cancel): ",
            fallback_content=picker,
        )
        if raw_position.casefold() == "cancel":
            return
        position = _parse_one_based_integer(raw_position)
        if position is None or position > len(workflow.steps):
            self._message("Choose a Workflow Step by number, or cancel.")
            return
        selected = workflow.steps[position - 1].instance_id
        if self._scope is EditorScope.CURRENT_RUN:
            self._current_selected_step_id = selected
        else:
            self._future_selected_step_id = selected

    def _reject_current_run_mutation(self, command: str) -> bool:
        if self._scope is not EditorScope.CURRENT_RUN or command not in {
            "rename",
            "add",
            "insert",
            "duplicate",
            "delete",
            "type",
            "move-up",
            "move-down",
            "position",
            "model",
            "reasoning",
            "fast",
            "budget",
            "guidance",
            "capabilities",
            "route",
            "bind",
            "undo",
            "reset-step",
            "reset-workflow",
        }:
            return False
        self._message(
            "Current Run cannot be edited. Switch to Future Runs with 'future'."
        )
        return True

    def _show_current_run(self) -> None:
        if self._current_workflow is None:
            self._message("There is no active Current Run to inspect.")
        else:
            self._scope = EditorScope.CURRENT_RUN

    def _show_future_runs(self) -> None:
        self._scope = EditorScope.FUTURE_RUNS

    def _rename(self) -> None:
        display_name = self._read_line("New display name: ").strip()
        try:
            self._draft.rename(self._future_selected_step_id, display_name)
        except ValueError as error:
            self._message(f"Cannot rename step: {error}")

    def _add(self) -> None:
        component = self._choose_component()
        if component is None:
            return
        try:
            self._future_selected_step_id = self._draft.add(component.component_id)
        except ValueError as error:
            self._message(f"Cannot add step: {error}")

    def _insert(self) -> None:
        component = self._choose_component()
        if component is None:
            return
        primary_path = self._draft.workflow.primary_path()
        raw_position = self._choose_menu(
            SelectionMenu(
                title="Insert position",
                options=(
                    *(
                        (
                            str(position),
                            (
                                f"Before {primary_path[position - 1].display_name}"
                                if position <= len(primary_path)
                                else "At the end of the Primary Path"
                            ),
                        )
                        for position in range(1, len(primary_path) + 2)
                    ),
                    ("cancel", "Back without inserting a step"),
                ),
                default_key=str(len(primary_path) + 1),
                cancel_key="cancel",
                description=(f"Step Type: {component.default_display_name}",),
            ),
            fallback_prompt="Primary Path Position: ",
        )
        if raw_position.casefold() == "cancel":
            return
        position = _parse_one_based_integer(raw_position)
        if position is None:
            self._message("Primary Path Position must be a one-based number.")
            return
        try:
            self._future_selected_step_id = self._draft.insert(
                component.component_id,
                position,
            )
        except ValueError as error:
            self._message(f"Cannot insert step: {error}")

    def _duplicate(self) -> None:
        try:
            result = self._draft.duplicate(self._future_selected_step_id)
        except ValueError as error:
            self._message(f"Cannot duplicate step: {error}")
            return
        self._future_selected_step_id = result.step_instance_id
        for warning in result.warnings:
            self._message(f"Warning: {warning}")

    def _delete(self) -> None:
        try:
            preview = self._draft.preview_delete(self._future_selected_step_id)
        except ValueError as error:
            self._message(f"Cannot delete step: {error}")
            return
        preview_text = render_delete_preview(
            preview,
            self._draft.workflow,
            terminal_width=self._terminal_width,
        )
        confirmation = self._choose_menu(
            SelectionMenu(
                title=f"Delete · {preview.step_display_name}",
                options=(
                    ("yes", "Delete this workflow step"),
                    ("no", "Keep this workflow step"),
                ),
                default_key="no",
                cancel_key="no",
                description=tuple(preview_text.splitlines()),
            ),
            fallback_prompt=f"Type yes to delete {preview.step_display_name!r}: ",
            fallback_content=preview_text,
        ).casefold()
        if confirmation != "yes":
            self._message("Deletion cancelled; the workflow draft was not changed.")
            return
        try:
            self._draft.delete(preview)
        except ValueError as error:
            self._message(f"Cannot delete step: {error}")
            return
        self._future_selected_step_id = self._draft.workflow.start_step_id

    def _change_type(self) -> None:
        source = self._draft.workflow.step(self._future_selected_step_id)
        component = self._choose_component()
        if component is None:
            return
        preview = render_type_change_preview(
            self._draft.workflow,
            source,
            component,
            terminal_width=self._terminal_width,
        )
        if self._select_option is not None:
            confirmation = self._choose_menu(
                SelectionMenu(
                    title=f"Change Step Type · {source.display_name}",
                    options=(
                        ("apply", "Apply this Step Type change"),
                        ("cancel", "Back without changing the Step Type"),
                    ),
                    default_key="cancel",
                    cancel_key="cancel",
                    description=tuple(preview.splitlines()),
                ),
                fallback_prompt="Apply type change [apply/cancel]: ",
            )
            if confirmation != "apply":
                return
        else:
            self._write(preview)
        try:
            self._draft.change_type(source.instance_id, component.component_id)
        except ValueError as error:
            self._message(f"Cannot change step Type: {error}")

    def _move_up(self) -> None:
        self._move_selected(-1)

    def _move_down(self) -> None:
        self._move_selected(1)

    def _move_selected(self, offset: int) -> None:
        primary_path = self._draft.workflow.primary_path()
        current_position = next(
            (
                index
                for index, step in enumerate(primary_path, start=1)
                if step.instance_id == self._future_selected_step_id
            ),
            None,
        )
        if current_position is None:
            self._message("Only Primary Path steps have an editable Position.")
            return
        try:
            self._draft.move(
                self._future_selected_step_id,
                current_position + offset,
            )
        except ValueError as error:
            self._message(f"Cannot move step: {error}")

    def _set_position(self) -> None:
        primary_path = self._draft.workflow.primary_path()
        current_position = next(
            (
                index
                for index, step in enumerate(primary_path, start=1)
                if step.instance_id == self._future_selected_step_id
            ),
            1,
        )
        raw_position = self._choose_menu(
            SelectionMenu(
                title="Move workflow step",
                options=(
                    *(
                        (str(index), f"Position {index} · {step.display_name}")
                        for index, step in enumerate(primary_path, start=1)
                    ),
                    ("cancel", "Back without moving the step"),
                ),
                default_key=str(current_position),
                cancel_key="cancel",
            ),
            fallback_prompt="Primary Path Position: ",
        )
        if raw_position.casefold() == "cancel":
            return
        position = _parse_one_based_integer(raw_position)
        if position is None:
            self._message("Primary Path Position must be a one-based number.")
            return
        try:
            self._draft.move(
                self._future_selected_step_id,
                position,
            )
        except ValueError as error:
            self._message(f"Cannot move step: {error}")

    def _set_model(self) -> None:
        selection = self._selected_codex_context()
        if selection is None:
            return
        step, settings, model_catalog = selection
        current_position = next(
            (
                index
                for index, model in enumerate(model_catalog.models, start=1)
                if model.model_id == settings.model
            ),
            1,
        )
        raw_position = self._choose_menu(
            SelectionMenu(
                title=f"Model · {step.display_name}",
                options=(
                    *(
                        (str(index), f"{model.display_name} — {model.model_id}")
                        for index, model in enumerate(model_catalog.models, start=1)
                    ),
                    ("cancel", "Back without changing the model"),
                ),
                default_key=str(current_position),
                cancel_key="cancel",
                description=(
                    "Choose the Codex model for this workflow step.",
                    "Catalog source: live",
                ),
            ),
            fallback_prompt="Model number (or cancel): ",
            fallback_content=render_model_picker(
                model_catalog,
                terminal_width=self._terminal_width,
            ),
        )
        if raw_position.casefold() == "cancel":
            return
        position = _parse_one_based_integer(raw_position)
        if position is None or position > len(model_catalog.models):
            self._message("Choose a Codex model by number, or cancel.")
            return
        model = model_catalog.models[position - 1]
        reasoning_effort = settings.reasoning_effort
        if reasoning_effort not in model.reasoning_efforts:
            selected_effort = self._choose_reasoning_effort(model)
            if selected_effort is None:
                return
            reasoning_effort = selected_effort
        fast = settings.fast
        if fast is FastPreference.ON and not model.supports_fast:
            fast = FastPreference.OFF
            self._message(
                f"{model.display_name} does not advertise Fast; Fast was set to Off."
            )
        self._draft.set_codex_settings(
            step.instance_id,
            CodexExecutionSettings(model.model_id, reasoning_effort, fast),
        )

    def _set_reasoning(self) -> None:
        selection = self._selected_codex_context()
        if selection is None:
            return
        step, settings, model_catalog = selection
        try:
            model = model_catalog.model(settings.model)
        except ValueError:
            self._message(
                f"Selected model {settings.model!r} is not in the displayed catalog; "
                "choose Model first or Retry Catalog."
            )
            return
        reasoning_effort = self._choose_reasoning_effort(model)
        if reasoning_effort is None:
            return
        self._draft.set_codex_settings(
            step.instance_id,
            replace(settings, reasoning_effort=reasoning_effort),
        )

    def _choose_reasoning_effort(self, model: CodexModel) -> str | None:
        lines = [f"Reasoning Efforts — {model.display_name}"]
        lines.extend(
            f"{index}. {effort}"
            for index, effort in enumerate(model.reasoning_efforts, start=1)
        )
        rendered = "\n".join(
            _fit_to_width(line, max(1, self._terminal_width))
            for line in lines
        )
        raw_position = self._choose_menu(
            SelectionMenu(
                title=f"Reasoning effort · {model.display_name}",
                options=(
                    *(
                        (str(index), effort)
                        for index, effort in enumerate(
                            model.reasoning_efforts,
                            start=1,
                        )
                    ),
                    ("cancel", "Back without changing reasoning"),
                ),
                default_key="1",
                cancel_key="cancel",
            ),
            fallback_prompt="Reasoning number (or cancel): ",
            fallback_content=rendered,
        )
        if raw_position.casefold() == "cancel":
            return None
        position = _parse_one_based_integer(raw_position)
        if position is None or position > len(model.reasoning_efforts):
            self._message("Choose an advertised reasoning effort by number, or cancel.")
            return None
        return model.reasoning_efforts[position - 1]

    def _set_fast(self) -> None:
        selection = self._selected_codex_context()
        if selection is None:
            return
        step, settings, model_catalog = selection
        try:
            model = model_catalog.model(settings.model)
        except ValueError:
            self._message(
                f"Selected model {settings.model!r} is not in the displayed catalog; "
                "choose Model first or Retry Catalog."
            )
            return
        if not model.supports_fast:
            self._message(
                f"Model {settings.model!r} does not advertise Fast; only Off is available."
            )
            return
        choice = self._choose_menu(
            SelectionMenu(
                title=f"Fast mode · {step.display_name}",
                options=(
                    ("on", "On"),
                    ("off", "Off"),
                    ("cancel", "Back without changing Fast mode"),
                ),
                default_key=settings.fast.value.casefold(),
                cancel_key="cancel",
                description=(f"Model: {model.display_name}",),
            ),
            fallback_prompt="Fast [on/off/cancel]: ",
        ).casefold()
        if choice == "cancel":
            return
        if choice not in {"on", "off"}:
            self._message("Choose on, off, or cancel for Fast.")
            return
        self._draft.set_codex_settings(
            step.instance_id,
            replace(
                settings,
                fast=(FastPreference.ON if choice == "on" else FastPreference.OFF),
            ),
        )

    def _set_execution_budget(self) -> None:
        step = self._draft.workflow.step(self._future_selected_step_id)
        raw_timeout = self._read_line("Execution timeout seconds: ").strip()
        timeout_seconds = _parse_positive_seconds(raw_timeout)
        if timeout_seconds is None:
            self._message("Execution timeout must be a positive number of seconds.")
            return
        raw_checkpoint = self._read_line(
            "Checkpoint deadline seconds: "
        ).strip()
        checkpoint_seconds = _parse_positive_seconds(raw_checkpoint)
        if checkpoint_seconds is None:
            self._message(
                "Checkpoint deadline must be a positive number of seconds."
            )
            return
        try:
            budget = ExecutionBudget(timeout_seconds, checkpoint_seconds)
        except ValueError as error:
            self._message(f"Cannot set Execution Budget: {error}")
            return
        self._draft.set_execution_budget(step.instance_id, budget)

    def _edit_guidance(self) -> None:
        step = self._draft.workflow.step(self._future_selected_step_id)
        component = self._catalog.resolve(step.component_id)
        if not component.is_codex_backed:
            if step.guidance is None:
                self._message("Local deterministic steps do not accept Step Guidance.")
                return
            action = self._choose_menu(
                SelectionMenu(
                    title=f"Guidance · {step.display_name}",
                    options=(
                        ("clear", "Clear existing guidance"),
                        ("cancel", "Back without changing guidance"),
                    ),
                    default_key="cancel",
                    cancel_key="cancel",
                ),
                fallback_prompt="Guidance action [clear/cancel]: ",
            ).casefold()
            if action == "clear":
                self._draft.clear_guidance(step.instance_id)
            elif action != "cancel":
                self._message("Choose clear or cancel.")
            return
        actions = "keep/edit/clear/cancel" if step.guidance is not None else "edit/cancel"
        options: tuple[tuple[str, str], ...]
        if step.guidance is None:
            options = (
                ("edit", "Add guidance"),
                ("cancel", "Back without changing guidance"),
            )
        else:
            options = (
                ("keep", "Keep guidance and mark it reviewed"),
                ("edit", "Replace guidance"),
                ("clear", "Remove guidance"),
                ("cancel", "Back without changing guidance"),
            )
        action = self._choose_menu(
            SelectionMenu(
                title=f"Guidance · {step.display_name}",
                options=options,
                default_key="keep" if step.guidance is not None else "edit",
                cancel_key="cancel",
            ),
            fallback_prompt=f"Guidance action [{actions}]: ",
        ).casefold()
        if action == "cancel":
            return
        if action == "keep" and step.guidance is not None:
            self._draft.keep_guidance(step.instance_id)
            return
        if action == "clear" and step.guidance is not None:
            self._draft.clear_guidance(step.instance_id)
            return
        if action != "edit":
            self._message(f"Choose {actions.replace('/', ', ')}.")
            return
        self._write(
            "Enter Step Guidance one line at a time. Enter a single '.' to finish."
        )
        lines: list[str] = []
        while True:
            line = self._read_line("guidance> ")
            if line == ".":
                break
            lines.append(line)
        try:
            self._draft.set_guidance(step.instance_id, "\n".join(lines))
        except ValueError as error:
            self._message(f"Cannot set Step Guidance: {error}")

    def _selected_codex_context(
        self,
    ) -> tuple[WorkflowStep, CodexExecutionSettings, CodexModelCatalog] | None:
        step = self._draft.workflow.step(self._future_selected_step_id)
        component = self._catalog.resolve(step.component_id)
        if not component.is_codex_backed:
            self._message(
                f"{step.display_name!r} is local deterministic; Codex settings do not apply."
            )
            return None
        if step.codex_settings is None:
            self._message(f"{step.display_name!r} has no Codex Execution Settings.")
            return None
        if self._model_catalog is None:
            self._message(
                "No Codex Model Catalog is available. Use Retry Catalog after "
                "checking the Codex installation and authentication."
            )
            return None
        if not self._model_catalog.is_fresh:
            self._message(
                "A fresh live Codex Model Catalog is required to change Model, "
                "Reasoning, or Fast. The stale cache is display-only; use Retry "
                "Catalog."
            )
            return None
        return step, step.codex_settings, self._model_catalog

    def _load_initial_model_catalog(self) -> None:
        if self._model_catalog_loader is not None:
            self._refresh_model_catalog()
            return
        try:
            self._model_catalog = self._model_catalog_cache.load()
        except ValueError as error:
            self._model_catalog_error = sanitize_terminal_text(
                error,
                preserve_newlines=False,
            )

    def _retry_catalog(self) -> None:
        if self._model_catalog_loader is None:
            self._message("Live Codex Model Catalog discovery is unavailable here.")
            return
        self._refresh_model_catalog()
        if self._model_catalog is not None and self._model_catalog.is_fresh:
            self._message("Codex Model Catalog refreshed from the live backend.")
        else:
            self._message(
                self._model_catalog_error
                or "Codex Model Catalog refresh failed; no cache is available."
            )

    def _refresh_model_catalog(self) -> None:
        assert self._model_catalog_loader is not None
        try:
            live_catalog = self._model_catalog_loader()
            if not live_catalog.is_fresh:
                raise ValueError("Catalog discovery did not return fresh live data.")
            self._model_catalog = live_catalog
            self._model_catalog_error = None
            try:
                self._model_catalog_cache.replace(live_catalog)
            except OSError as error:
                self._model_catalog_error = (
                    "Live catalog loaded, but its display cache could not be updated: "
                    f"{sanitize_terminal_text(error, preserve_newlines=False)}"
                )
            return
        except (CatalogDiscoveryError, OSError, ValueError) as error:
            safe_error = sanitize_terminal_text(error, preserve_newlines=False)
            self._model_catalog_error = (
                f"Live Codex Model Catalog unavailable: {safe_error}. "
                "Check Codex installation/authentication and use Retry Catalog."
            )
        try:
            self._model_catalog = self._model_catalog_cache.load()
        except ValueError as cache_error:
            safe_cache_error = sanitize_terminal_text(
                cache_error,
                preserve_newlines=False,
            )
            self._model_catalog = None
            self._model_catalog_error = (
                f"{self._model_catalog_error} Cached display data is invalid: "
                f"{safe_cache_error}"
            )

    def _route_outcome(self) -> None:
        step = self._draft.workflow.step(self._future_selected_step_id)
        component = self._catalog.resolve(step.component_id)
        outcomes = tuple(
            outcome
            for outcome in StepOutcome
            if outcome in component.supported_outcomes
        )
        outcome_picker = render_outcome_picker(
            step,
            outcomes,
            self._draft.workflow,
            terminal_width=self._terminal_width,
        )
        raw_outcome = self._choose_menu(
            SelectionMenu(
                title=f"Outcome route · {step.display_name}",
                options=(
                    *(
                        (
                            str(index),
                            (
                                f"{outcome.value} → "
                                f"{_outcome_destination_label(self._draft.workflow, step, outcome)}"
                            ),
                        )
                        for index, outcome in enumerate(outcomes, start=1)
                    ),
                    ("cancel", "Back without changing a route"),
                ),
                default_key="1",
                cancel_key="cancel",
            ),
            fallback_prompt="Outcome number (or cancel): ",
            fallback_content=outcome_picker,
        )
        if raw_outcome.casefold() == "cancel":
            return
        outcome_position = _parse_one_based_integer(raw_outcome)
        if outcome_position is None or outcome_position > len(outcomes):
            self._message("Choose a supported Step Outcome by number, or cancel.")
            return
        action = self._choose_menu(
            SelectionMenu(
                title=f"Route action · {outcomes[outcome_position - 1].value}",
                options=(
                    ("existing", "Route to an existing workflow step"),
                    ("new", "Create a new branch step"),
                    ("insert", "Insert a new step into this route"),
                    ("terminal", "End the workflow on this outcome"),
                    ("cancel", "Back without changing the route"),
                ),
                default_key="existing",
                cancel_key="cancel",
                description=(f"Source: {step.display_name}",),
            ),
            fallback_prompt="Route action [existing/new/insert/terminal/cancel]: ",
        ).casefold()
        if action == "cancel":
            return
        if action == "terminal":
            self._draft.route(
                step.instance_id,
                outcomes[outcome_position - 1],
                None,
            )
            return
        if action == "new":
            component = self._choose_component()
            if component is None:
                return
            try:
                self._future_selected_step_id = self._draft.create_step_on_route(
                    step.instance_id,
                    outcomes[outcome_position - 1],
                    component.component_id,
                )
            except ValueError as error:
                self._message(f"Cannot create branch step: {error}")
            return
        if action == "insert":
            component = self._choose_component()
            if component is None:
                return
            try:
                self._future_selected_step_id = self._draft.insert_step_on_route(
                    step.instance_id,
                    outcomes[outcome_position - 1],
                    component.component_id,
                )
            except ValueError as error:
                self._message(f"Cannot insert route step: {error}")
            return
        if action != "existing":
            self._message("Choose existing, new, insert, terminal, or cancel.")
            return
        target_picker = render_step_picker(
            self._draft.workflow,
            terminal_width=self._terminal_width,
        )
        raw_target = self._choose_menu(
            SelectionMenu(
                title="Route target",
                options=(
                    *(
                        (str(index), target.display_name)
                        for index, target in enumerate(
                            self._draft.workflow.steps,
                            start=1,
                        )
                    ),
                    ("cancel", "Back without changing the route"),
                ),
                default_key="1",
                cancel_key="cancel",
                description=(
                    f"{step.display_name}.{outcomes[outcome_position - 1].value}",
                ),
            ),
            fallback_prompt="Target step number (or cancel): ",
            fallback_content=target_picker,
        )
        if raw_target.casefold() == "cancel":
            return
        target_position = _parse_one_based_integer(raw_target)
        if (
            target_position is None
            or target_position > len(self._draft.workflow.steps)
        ):
            self._message("Choose an existing Workflow Step by number, or cancel.")
            return
        target = self._draft.workflow.steps[target_position - 1]
        try:
            self._draft.route(
                step.instance_id,
                outcomes[outcome_position - 1],
                target.instance_id,
            )
        except ValueError as error:
            self._message(f"Cannot route outcome: {error}")

    def _bind_input(self) -> None:
        workflow = self._draft.workflow
        step = workflow.step(self._future_selected_step_id)
        component = self._catalog.resolve(step.component_id)
        ports = tuple(component.all_input_ports.items())
        if not ports:
            self._message(f"Step {step.display_name!r} has no Input Ports.")
            return
        lines = [f"Input Ports — {step.display_name}"]
        for index, (input_port, contract_id) in enumerate(ports, start=1):
            requirement = (
                "required" if input_port in component.input_ports else "optional"
            )
            lines.append(f"{index}. {input_port} [{requirement}] {contract_id}")
        port_picker = "\n".join(
            _fit_to_width(line, max(1, self._terminal_width))
            for line in lines
        )
        raw_port = self._choose_menu(
            SelectionMenu(
                title=f"Input binding · {step.display_name}",
                options=(
                    *(
                        (
                            str(index),
                            f"{input_port} · "
                            f"{'required' if input_port in component.input_ports else 'optional'} · "
                            f"{contract_id}",
                        )
                        for index, (input_port, contract_id) in enumerate(
                            ports,
                            start=1,
                        )
                    ),
                    ("cancel", "Back without changing a binding"),
                ),
                default_key="1",
                cancel_key="cancel",
            ),
            fallback_prompt="Input port number (or cancel): ",
            fallback_content=port_picker,
        )
        if raw_port.casefold() == "cancel":
            return
        port_position = _parse_one_based_integer(raw_port)
        if port_position is None or port_position > len(ports):
            self._message("Choose an Input Port by number, or cancel.")
            return
        input_port = ports[port_position - 1][0]
        candidates = compatible_port_bindings(
            workflow,
            step,
            input_port,
            self._catalog,
        )
        candidate_lines = [f"Compatible Producers — {step.display_name}.{input_port}"]
        candidate_lines.extend(
            f"{index}. {workflow.step(binding.producer_step_id).display_name}."
            f"{binding.output_port}"
            for index, binding in enumerate(candidates, start=1)
        )
        candidate_lines.append("Enter clear to remove the current binding.")
        candidate_picker = "\n".join(
            _fit_to_width(line, max(1, self._terminal_width))
            for line in candidate_lines
        )
        raw_candidate = self._choose_menu(
            SelectionMenu(
                title=f"Producer · {step.display_name}.{input_port}",
                options=(
                    *(
                        (
                            str(index),
                            f"{workflow.step(binding.producer_step_id).display_name}."
                            f"{binding.output_port}",
                        )
                        for index, binding in enumerate(candidates, start=1)
                    ),
                    ("clear", "Remove the current binding"),
                    ("cancel", "Back without changing the binding"),
                ),
                default_key="1" if candidates else "clear",
                cancel_key="cancel",
            ),
            fallback_prompt="Producer number (clear or cancel): ",
            fallback_content=candidate_picker,
        )
        if raw_candidate.casefold() == "cancel":
            return
        if raw_candidate.casefold() == "clear":
            self._draft.set_binding(step.instance_id, input_port, None)
            return
        candidate_position = _parse_one_based_integer(raw_candidate)
        if candidate_position is None or candidate_position > len(candidates):
            self._message("Choose a compatible producer by number, clear, or cancel.")
            return
        self._draft.set_binding(
            step.instance_id,
            input_port,
            candidates[candidate_position - 1],
        )

    def _choose_component(self) -> PortableStepComponent | None:
        picker = render_component_type_picker(
            self._catalog,
            terminal_width=self._terminal_width,
        )
        choice = self._choose_menu(
            SelectionMenu(
                title="Workflow Step Type",
                options=(
                    *(
                        (
                            str(index),
                            f"{component.default_display_name} ({component.scope.value})",
                        )
                        for index, component in enumerate(
                            self._catalog.components,
                            start=1,
                        )
                    ),
                    ("cancel", "Back without choosing a Step Type"),
                ),
                default_key="1",
                cancel_key="cancel",
            ),
            fallback_prompt="Type number (or cancel): ",
            fallback_content=picker,
        )
        if choice.casefold() == "cancel":
            return None
        position = _parse_one_based_integer(choice)
        if position is not None and position <= len(self._catalog.components):
            return self._catalog.components[position - 1]
        self._message("Choose an installed Workflow Step Type by number, or cancel.")
        return None

    def _toggle_advanced(self) -> None:
        self._show_advanced = not self._show_advanced

    def _toggle_graph(self) -> None:
        self._show_graph = not self._show_graph
        state = "shown" if self._show_graph else "hidden"
        self._message(f"Route map {state}. Press F3 to toggle it.")

    def _open_capability_options(self) -> None:
        if self._open_capabilities is None:
            self._message("Capability options are unavailable in this editor context.")
        else:
            self._open_capabilities(
                self._draft,
                self._future_selected_step_id,
            )

    def _undo(self) -> None:
        if not self._draft.undo():
            self._message("Nothing to undo.")
            return
        if self._future_selected_step_id not in {
            step.instance_id for step in self._draft.workflow.steps
        }:
            self._future_selected_step_id = self._draft.workflow.start_step_id

    def _reset_step(self) -> None:
        try:
            self._draft.reset_step(self._future_selected_step_id)
        except ValueError as error:
            self._message(f"Cannot reset step: {error}")

    def _reset_workflow(self) -> None:
        selected_step_id = self._future_selected_step_id
        self._draft.reset_workflow(default_portable_workflow())
        if selected_step_id not in {
            step.instance_id for step in self._draft.workflow.steps
        }:
            self._future_selected_step_id = self._draft.workflow.start_step_id

    def _apply(self) -> EditorResult | None:
        updates = (
            self._configuration_updates()
            if self._configuration_updates is not None
            else None
        )
        try:
            self._store.replace(
                self._draft.workflow,
                configuration_updates=updates,
            )
        except ValueError as error:
            self._message(f"Cannot apply workflow: {error}")
            return None
        self._message("Future Runs workflow default applied.")
        return EditorResult.APPLIED

    def _cancel(self) -> EditorResult:
        self._message("Workflow draft cancelled; no changes were saved.")
        return EditorResult.CANCELLED

    def _message(self, message: str) -> None:
        self._notice = sanitize_terminal_text(message, preserve_newlines=False)
        _write_message(self._write, message, self._terminal_width)


def render_workflow_default_recovery(
    load_error: str,
    *,
    reset_applied: bool,
    terminal_width: int,
) -> str:
    """Render the fail-closed editor for a rejected User Workflow Default."""
    width = max(1, terminal_width)
    safe_error = sanitize_terminal_text(load_error, preserve_newlines=False)
    status = (
        "Reset prepared; Apply may now replace the invalid default atomically."
        if reset_applied
        else "The invalid default must be reset before Apply is available."
    )
    lines = (
        "Workflow Editor — User Workflow Default recovery mode",
        "The stored default failed validation and was not loaded as an editable draft.",
        f"Validation error: {safe_error}",
        status,
        "Cancel leaves the stored configuration unchanged.",
        "Commands: reset-workflow | apply | cancel",
    )
    return "\n".join(
        wrapped_line
        for line in lines
        for wrapped_line in _wrap_to_width(line, width)
    )


def render_workflow_help(
    *,
    terminal_width: int,
    terminal_height: int,
) -> str:
    return render_screen_frame(
        path=render_context_path("Workflow Editor", "Help"),
        body=(
            "Navigate",
            "  Up / Down      Select a workflow step",
            "  Enter or F9   Open the complete action list",
            "",
            "Common actions",
            "  F2 Apply      Save the Future Runs workflow default",
            "  F3 Route map Show or hide workflow routes",
            "  F4 Details   Show summary or technical settings",
            "  F5 Add       Add a workflow step",
            "  F7 Capabilities  Manage skills and agent references",
            "  Esc Cancel   Leave without saving draft changes",
            "",
            "Compatibility",
            "  Number keys select steps. Terminals without raw-key support retain the",
            "  complete-word command prompt and the same workflow behavior.",
        ),
        action_bar=(("Enter", "Back"),),
        width=terminal_width,
        height=terminal_height,
    )


def _compact_detail_lines(
    workflow: WorkflowDefinition,
    selected: WorkflowStep,
    component: PortableStepComponent,
    catalog: PortableStepComponentCatalog,
    *,
    selected_position: int | None,
    primary_path_length: int,
    model_catalog: CodexModelCatalog | None,
    model_catalog_error: str | None,
) -> list[str]:
    lines = [
        f"Component: {selected.component_id}",
        f"Scope: {component.scope.value.title()}",
    ]
    if component.is_codex_backed:
        if selected.codex_settings is None:
            lines.append("Codex settings: missing")
        else:
            lines.append(
                "Model: "
                f"{selected.codex_settings.model} | "
                f"Effort: {selected.codex_settings.reasoning_effort} | "
                f"Fast: {selected.codex_settings.fast.value.title()} | "
                f"Timeout: {selected.execution_budget.timeout_seconds:g}s"
            )
        if model_catalog is None:
            lines.append("Codex Model Catalog: unavailable")
        elif not model_catalog.is_fresh:
            lines.append("Codex Model Catalog: STALE — retry-catalog before apply")
        if model_catalog_error:
            lines.append(
                "Catalog action: "
                + sanitize_terminal_text(model_catalog_error, preserve_newlines=False)
            )
    else:
        lines.append("Local deterministic execution")
    capability_count = len(selected.capability_profile.capabilities)
    required_count = sum(
        1
        for capability in selected.capability_profile.capabilities
        if component.required_capability_reason(capability) is not None
    )
    lines.append(f"Capabilities: {capability_count} ({required_count} required)")
    if selected.guidance is None:
        lines.append("Guidance: none")
    else:
        preview = selected.guidance.text.splitlines()[0]
        lines.append(
            f"Guidance: {selected.guidance.review_state.value} — {preview}"
        )
    if selected_position is None:
        lines.append("Position: branch-only")
    else:
        lines.append(f"Position: {selected_position}/{primary_path_length}")
    unresolved = _unresolved_input_lines(workflow, selected, catalog)
    if unresolved:
        lines.extend(unresolved[:2])
        if len(unresolved) > 2:
            lines.append(f"… {len(unresolved) - 2} more unresolved inputs")
    return lines


def render_workflow_editor(
    workflow: WorkflowDefinition,
    selected_step_id: StepInstanceId,
    catalog: PortableStepComponentCatalog,
    *,
    terminal_width: int,
    terminal_height: int = 24,
    current_workflow: WorkflowDefinition | None = None,
    show_advanced: bool = False,
    show_graph: bool = False,
    scope: EditorScope = EditorScope.FUTURE_RUNS,
    model_catalog: CodexModelCatalog | None = None,
    model_catalog_error: str | None = None,
    notice: str | None = None,
) -> str:
    width = max(1, terminal_width)
    height = max(10, terminal_height)
    selected = workflow.step(selected_step_id)
    component = catalog.resolve(selected.component_id)
    primary_path = workflow.primary_path()
    selected_position = next(
        (
            index
            for index, step in enumerate(primary_path, start=1)
            if step.instance_id == selected_step_id
        ),
        None,
    )
    scope_label = (
        "Current Run (read-only)"
        if scope is EditorScope.CURRENT_RUN
        else "Future Runs (editable)"
    )
    position_label = (
        "branch-only"
        if selected_position is None
        else f"{selected_position}/{len(primary_path)}"
    )
    context_path = render_context_path(
        "Workflow Editor",
        scope_label,
        selected.display_name,
        position_label,
    )
    primary_lines = list(
        format_selected_step_line(
            index,
            step.display_name,
            selected=step.instance_id == selected_step_id,
        )
        for index, step in enumerate(primary_path, start=1)
    )
    route_status = "Route map shown" if show_graph else "Route map hidden"
    header = [
        f"Mode: {scope_label}  |  {route_status} (F3)  |  "
        f"Details: {'technical' if show_advanced else 'summary'}"
    ]
    if current_workflow is not None:
        header.append(
            f"Current Run hash: {canonical_workflow_hash(current_workflow)[:12]}…"
        )
    if notice:
        header.append(f"Status: {notice}")
    if show_graph:
        detail_lines = render_graph_preview(
            workflow,
            catalog,
            terminal_width=max(20, width - 4),
            max_lines=max(6, height - 10),
        ).splitlines()
        right_title = f"Route map — {selected.display_name}"
    elif show_advanced:
        detail_lines = [
            f"Instance: {selected.instance_id}",
        ]
        detail_lines.extend(
            _port_binding_lines(
                workflow,
                selected,
                component,
                catalog,
            )
        )
        right_title = f"Settings — {selected.display_name} · Technical details"
    else:
        detail_lines = _compact_detail_lines(
            workflow,
            selected,
            component,
            catalog,
            selected_position=selected_position,
            primary_path_length=len(primary_path),
            model_catalog=model_catalog,
            model_catalog_error=model_catalog_error,
        )
        right_title = f"Settings — {selected.display_name}"

    action_line_count = len(
        render_action_bar(WORKFLOW_ACTION_BAR, width=max(1, width - 2))
    )
    available_body_height = max(4, height - 4 - action_line_count)
    if show_graph:
        body_lines = [*header, "", right_title, *detail_lines]
    elif width >= WIDE_EDITOR_MINIMUM_WIDTH:
        pane_height = max(4, available_body_height - len(header))
        panes = render_split_panes(
            left_title="Workflow Steps",
            left_lines=primary_lines,
            right_title=right_title,
            right_lines=detail_lines,
            width=max(20, width - 2),
            height=pane_height,
        )
        body_lines = [*header, *panes]
    else:
        body_lines = [
            *header,
            "",
            "Workflow Steps",
            *primary_lines,
            "",
            right_title,
            *detail_lines,
        ]
    return render_screen_frame(
        path=context_path,
        body=body_lines,
        action_bar=WORKFLOW_ACTION_BAR,
        width=width,
        height=height,
    )


def render_component_type_picker(
    catalog: PortableStepComponentCatalog,
    *,
    terminal_width: int,
) -> str:
    width = max(1, terminal_width)
    lines = ["Workflow Step Types"]
    lines.extend(
        (
            f"{index}. {component.default_display_name} "
            f"({component.scope.value}) — {component.component_id}"
        )
        for index, component in enumerate(catalog.components, start=1)
    )
    lines.append("Step Scope is component-owned and read-only.")
    return "\n".join(_fit_to_width(line, width) for line in lines)


def render_delete_preview(
    preview: DeletePreview,
    workflow: WorkflowDefinition,
    *,
    terminal_width: int,
) -> str:
    width = max(1, terminal_width)
    lines = [f"Delete Preview — {preview.step_display_name}", "Transitions affected:"]
    lines.extend(
        (
            f"{impact.source_display_name}.{impact.outcome.value} -> "
            f"{_step_destination_label(workflow, impact.target_step_id)}"
        )
        for impact in preview.transition_impacts
    )
    lines.append("Bindings affected:")
    lines.extend(
        (
            f"{impact.consumer_display_name}.{impact.input_port} <- "
            f"{workflow.step(impact.producer_step_id).display_name}."
            f"{impact.output_port}"
        )
        for impact in preview.binding_impacts
    )
    repair = preview.primary_path_repair
    if repair is not None:
        successor = _step_destination_label(workflow, repair.successor_step_id)
        if repair.predecessor_step_id is None:
            lines.append(f"Primary Path repair: Start -> {successor}")
        else:
            predecessor = workflow.step(repair.predecessor_step_id).display_name
            lines.append(
                f"Primary Path repair: {predecessor}.SUCCEEDED -> {successor}"
            )
    else:
        lines.append("Primary Path repair: None (branch references remain explicit).")
    lines.extend(
        (
            "Bindings sourced from the deleted step will remain unresolved until "
            "they are explicitly rebound or cleared.",
            "Other references to the deleted step remain visible for deliberate repair.",
            "No downstream Workflow Steps will be deleted.",
        )
    )
    return "\n".join(
        line
        for source_line in lines
        for line in _wrap_to_width(source_line, width)
    )


def render_type_change_preview(
    workflow: WorkflowDefinition,
    source: WorkflowStep,
    component: PortableStepComponent,
    *,
    terminal_width: int,
) -> str:
    width = max(1, terminal_width)
    position = next(
        (
            index
            for index, step in enumerate(workflow.primary_path(), start=1)
            if step.instance_id == source.instance_id
        ),
        None,
    )
    location = (
        f"Primary Path Position {position}"
        if position is not None
        else "branch location"
    )
    lines = [
        f"Type Change Preview — {source.display_name}",
        f"Type: {source.component_id} -> {component.component_id}",
        f"Preserved: Step Instance ID, display name, and {location}",
        (
            "Reset: Codex settings, Execution Budget, capabilities, ports, "
            "bindings, and outcomes"
        ),
    ]
    if source.guidance is not None:
        lines.append("Guidance: preserved as NEEDS_REVIEW before Apply")
    return "\n".join(
        line
        for source_line in lines
        for line in _wrap_to_width(source_line, width)
    )


def _step_destination_label(
    workflow: WorkflowDefinition,
    step_id: StepInstanceId | None,
) -> str:
    if step_id is None:
        return "Terminal"
    try:
        return workflow.step(step_id).display_name
    except KeyError:
        return f"[deleted Step Instance {step_id}]"


def _outcome_destination_label(
    workflow: WorkflowDefinition,
    step: WorkflowStep,
    outcome: StepOutcome,
) -> str:
    if outcome not in step.transitions:
        return "[not configured]"
    return _step_destination_label(workflow, step.transitions[outcome])


def render_model_picker(
    catalog: CodexModelCatalog,
    *,
    terminal_width: int,
) -> str:
    width = max(1, terminal_width)
    source = "live" if catalog.is_fresh else "STALE DISPLAY CACHE"
    lines = [f"Codex Models — {source}"]
    lines.extend(
        f"{index}. {model.display_name} — {model.model_id}"
        for index, model in enumerate(catalog.models, start=1)
    )
    return "\n".join(_fit_to_width(line, width) for line in lines)


def render_outcome_picker(
    step: WorkflowStep,
    outcomes: tuple[StepOutcome, ...],
    workflow: WorkflowDefinition,
    *,
    terminal_width: int,
) -> str:
    width = max(1, terminal_width)
    lines = [f"Outcome Routes — {step.display_name}"]
    for index, outcome in enumerate(outcomes, start=1):
        if outcome not in step.transitions:
            target = "[not configured]"
        else:
            target_id = step.transitions[outcome]
            target = _step_destination_label(workflow, target_id)
        lines.append(f"{index}. {outcome.value} -> {target}")
    return "\n".join(_fit_to_width(line, width) for line in lines)


def render_step_picker(
    workflow: WorkflowDefinition,
    *,
    terminal_width: int,
) -> str:
    width = max(1, terminal_width)
    primary_ids = {step.instance_id for step in workflow.primary_path()}
    lines = ["Workflow Steps"]
    for index, step in enumerate(workflow.steps, start=1):
        location = "Primary Path" if step.instance_id in primary_ids else "Branch"
        lines.append(f"{index}. {step.display_name} ({location})")
    return "\n".join(_fit_to_width(line, width) for line in lines)


def render_graph_preview(
    workflow: WorkflowDefinition,
    catalog: PortableStepComponentCatalog,
    *,
    terminal_width: int,
    max_lines: int | None = None,
) -> str:
    width = max(1, terminal_width)
    lines = ["Route Map"]
    for step in workflow.steps:
        component = catalog.resolve(step.component_id)
        for outcome in StepOutcome:
            if outcome not in component.supported_outcomes:
                continue
            if outcome not in step.transitions:
                target = "[not configured]"
            else:
                target_id = step.transitions[outcome]
                target = _step_destination_label(workflow, target_id)
            lines.append(f"{step.display_name} --{outcome.value}--> {target}")
    if max_lines is not None and len(lines) > max_lines:
        hidden = len(lines) - max_lines + 1
        lines = [*lines[: max_lines - 1], f"… {hidden} routes hidden — widen terminal or hide graph"]
    return "\n".join(_fit_to_width(line, width) for line in lines)


def _unresolved_input_lines(
    workflow: WorkflowDefinition,
    step: WorkflowStep,
    catalog: PortableStepComponentCatalog,
) -> list[str]:
    component = catalog.resolve(step.component_id)
    missing_inputs = set(component.input_ports) - set(step.input_bindings)
    if not missing_inputs:
        return []
    lines: list[str] = []
    for input_port in sorted(missing_inputs):
        candidate_count = len(
            compatible_port_bindings(
                workflow,
                step,
                input_port,
                catalog,
            )
        )
        if candidate_count == 0:
            detail = "MISSING (no source)"
        elif candidate_count == 1:
            detail = "UNRESOLVED (1 compatible source)"
        else:
            detail = f"AMBIGUOUS ({candidate_count} sources)"
        lines.append(f"Input {input_port}: {detail}")
    return lines


def _port_binding_lines(
    workflow: WorkflowDefinition,
    step: WorkflowStep,
    component: PortableStepComponent,
    catalog: PortableStepComponentCatalog,
) -> list[str]:
    lines = ["Advanced Port Bindings"]
    port_groups = (
        ("required", component.input_ports),
        ("optional", component.optional_input_ports),
    )
    for requirement, ports in port_groups:
        for input_port, contract_id in sorted(ports.items()):
            lines.append(f"{input_port} [{requirement}] {contract_id}")
            binding = step.input_bindings.get(input_port)
            if binding is None:
                lines.append("Current: Unbound")
                binding_error = None
            else:
                producer_label = _step_destination_label(
                    workflow,
                    binding.producer_step_id,
                )
                lines.append(f"Current: {producer_label}.{binding.output_port}")
                lines.append(
                    "Allowed outcomes: "
                    + ", ".join(
                        sorted(outcome.value for outcome in binding.allowed_outcomes)
                    )
                )
                try:
                    validate_port_binding(
                        workflow,
                        step,
                        input_port,
                        binding,
                        catalog,
                    )
                except ValueError as error:
                    binding_error = str(error)
                else:
                    binding_error = None
            candidates = compatible_port_bindings(
                workflow,
                step,
                input_port,
                catalog,
            )
            candidate_labels = [
                f"{workflow.step(candidate.producer_step_id).display_name}."
                f"{candidate.output_port}"
                for candidate in candidates
            ]
            lines.append(
                "Compatible: "
                + (", ".join(candidate_labels) if candidate_labels else "None")
            )
            if requirement == "required" and binding is None:
                lines.append(
                    f"Error: {step.display_name} ({step.instance_id}) port "
                    f"{input_port} requires a binding."
                )
            elif binding_error is not None:
                if binding is not None and "binds unknown producer" in binding_error:
                    lines.append(
                        f"Error: {step.display_name}.{input_port} binds unknown producer "
                        f"{binding.producer_step_id}."
                    )
                else:
                    lines.append(f"Error: {binding_error}")
    return lines


def _render_command_lines(width: int, *, max_height: int | None = None) -> list[str]:
    command_budget = None
    if max_height is not None:
        command_budget = max(4, min(8, max_height // 3))
    return render_grouped_commands(
        EDITOR_COMMAND_GROUPS,
        width=width,
        heading="Available commands",
        max_lines=command_budget,
        inner=True,
    )


def _write_message(write: WriteLine, message: str, width: int) -> None:
    write("\n".join(_wrap_to_width(message, max(1, width))))


def _wrap_to_width(text: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = word if not current else f"{current} {word}"
        if display_width(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        while display_width(word) > width:
            prefix, word = _split_display_prefix(word, width)
            lines.append(prefix)
        current = word
    if current or not lines:
        lines.append(current)
    return lines


def _split_display_prefix(text: str, width: int) -> tuple[str, str]:
    used = 0
    split_at = 0
    for index, character in enumerate(text):
        character_width = display_width(character)
        if used + character_width > width:
            break
        used += character_width
        split_at = index + 1
    if split_at == 0:
        split_at = 1
    return text[:split_at], text[split_at:]


def _fit_to_width(text: str, width: int) -> str:
    if width < 1:
        return ""
    if display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    kept: list[str] = []
    available = width - 1
    used = 0
    for character in text:
        character_width = display_width(character)
        if used + character_width > available:
            break
        kept.append(character)
        used += character_width
    return "".join(kept) + "…"
