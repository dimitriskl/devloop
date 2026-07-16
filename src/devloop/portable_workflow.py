from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Protocol

from .codex_runner import RoleResult, result_to_dict
from .issue_pack import Issue
from .model_catalog import CodexModelCatalog
from .portable_text import normalize_single_line_display_name
from .step_configuration import (
    CapabilityKind,
    CapabilityReference,
    GuidanceReviewState,
    RequiredCapability,
    StepAttemptContext,
    StepCapabilityProfile,
    StepGuidance,
    capability_profile_from_defaults,
)
from .terminal_text import has_unsafe_terminal_controls


PORTABLE_WORKFLOW_SCHEMA = "devloop.portable-workflow/v2"
PORTABLE_WORKFLOW_FIELDS = frozenset({"schema", "start_step_id", "steps"})
REWORK_INPUT_KEY = "__rework__"
CODEX_EXECUTION_SETTINGS_FIELDS = frozenset(
    {"model", "reasoning_effort", "fast"}
)
EXECUTION_BUDGET_FIELDS = frozenset(
    {"timeout_seconds", "checkpoint_seconds"}
)
MAX_EXECUTION_BUDGET_SECONDS = 3600.0


class StepScope(str, Enum):
    WORKFLOW = "WORKFLOW"
    ISSUE = "ISSUE"


class StepOutcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class IssueStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_ON_DEPENDENCY = "WAITING_ON_DEPENDENCY"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


LEGACY_ISSUE_STATUS_ALIASES = {
    "Pending": IssueStatus.PENDING,
    "Ready": IssueStatus.READY,
    "Waiting for Input": IssueStatus.WAITING_FOR_INPUT,
    "Changes Requested": IssueStatus.CHANGES_REQUESTED,
    "Completed": IssueStatus.COMPLETED,
    "Blocked": IssueStatus.BLOCKED,
    "Failed": IssueStatus.FAILED,
    "Cancelled": IssueStatus.CANCELLED,
    "Skipped": IssueStatus.SKIPPED,
    "Dry Run": IssueStatus.SKIPPED,
}


def parse_issue_status(value: Any) -> IssueStatus | None:
    if isinstance(value, IssueStatus):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    try:
        return IssueStatus(normalized)
    except ValueError:
        if normalized.startswith("In Progress"):
            return IssueStatus.IN_PROGRESS
        return LEGACY_ISSUE_STATUS_ALIASES.get(normalized)


class StepRuntimeStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"


class FastPreference(str, Enum):
    ON = "ON"
    OFF = "OFF"


@dataclass(frozen=True)
class CodexExecutionSettings:
    model: str
    reasoning_effort: str
    fast: FastPreference = FastPreference.OFF

    def __post_init__(self) -> None:
        for field_name, value in (
            ("model", self.model),
            ("reasoning effort", self.reasoning_effort),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
                or has_unsafe_terminal_controls(value)
            ):
                raise ValueError(
                    f"Codex Execution Settings {field_name} must be a non-empty "
                    "single-line value."
                )

    def as_tuple(self) -> tuple[str, str, FastPreference]:
        return self.model, self.reasoning_effort, self.fast

    def to_dict(self) -> dict[str, str]:
        return {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "fast": self.fast.value,
        }


@dataclass(frozen=True)
class ExecutionBudget:
    timeout_seconds: float = 1800.0
    checkpoint_seconds: float = 300.0

    def __post_init__(self) -> None:
        for field_name, value in (
            ("timeout", self.timeout_seconds),
            ("checkpoint deadline", self.checkpoint_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(
                    f"Execution Budget {field_name} must be a positive number of seconds."
                )
        if self.timeout_seconds > MAX_EXECUTION_BUDGET_SECONDS:
            raise ValueError(
                "Execution Budget timeout cannot exceed 3600 seconds."
            )
        if self.checkpoint_seconds > self.timeout_seconds:
            raise ValueError(
                "Execution Budget checkpoint deadline must fit inside the timeout."
            )

    def to_dict(self) -> dict[str, float]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "checkpoint_seconds": self.checkpoint_seconds,
        }


def default_codex_execution_settings(role: str) -> CodexExecutionSettings:
    defaults = {
        "analysis": ("gpt-5.6-sol", "xhigh"),
        "coder": ("gpt-5.6-luna", "high"),
        "reviewer": ("gpt-5.6-sol", "xhigh"),
        "qa": ("gpt-5.6-terra", "high"),
    }
    try:
        model, reasoning_effort = defaults[role]
    except KeyError as error:
        raise ValueError(
            f"No built-in Codex Execution Settings exist for role {role!r}."
        ) from error
    return CodexExecutionSettings(model, reasoning_effort, FastPreference.OFF)


def default_execution_budget(role: str) -> ExecutionBudget:
    defaults = {
        "analysis": (900.0, 180.0),
        "coder": (1800.0, 300.0),
        "reviewer": (1800.0, 240.0),
        "qa": (1800.0, 240.0),
    }
    try:
        timeout_seconds, checkpoint_seconds = defaults[role]
    except KeyError as error:
        raise ValueError(
            f"No built-in Execution Budget exists for role {role!r}."
        ) from error
    return ExecutionBudget(timeout_seconds, checkpoint_seconds)


class StepInstanceId(str):
    def __new__(cls, value: str) -> StepInstanceId:
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError(f"Step Instance ID must be a canonical UUIDv4: {value!r}") from error
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError(f"Step Instance ID must be a canonical UUIDv4: {value!r}")
        return str.__new__(cls, value)


class StepComponentId(str):
    _PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")

    def __new__(cls, value: str) -> StepComponentId:
        if not isinstance(value, str) or not cls._PATTERN.fullmatch(value):
            raise ValueError(f"Invalid Step Component ID: {value!r}")
        return str.__new__(cls, value)


class DataContractId(str):
    _PATTERN = re.compile(r"^[a-z][a-z0-9.-]*@[1-9][0-9]*$")

    def __new__(cls, value: str) -> DataContractId:
        if not isinstance(value, str) or not cls._PATTERN.fullmatch(value):
            raise ValueError(f"Invalid Data Contract ID: {value!r}")
        return str.__new__(cls, value)


ANALYSIS_COMPONENT_ID = StepComponentId("devloop.analysis")
DEVELOPMENT_COMPONENT_ID = StepComponentId("devloop.development")
REVIEWER_COMPONENT_ID = StepComponentId("devloop.reviewer")
QA_COMPONENT_ID = StepComponentId("devloop.qa")

IMPLEMENTATION_RESULT_CONTRACT = DataContractId("devloop.implementation-result@1")
REVIEW_RESULT_CONTRACT = DataContractId("devloop.review-result@1")
QA_RESULT_CONTRACT = DataContractId("devloop.qa-result@1")

ANALYSIS_STEP_ID = StepInstanceId("1f2e3d4c-5b6a-4789-8abc-0def12345670")
DEVELOPMENT_STEP_ID = StepInstanceId("9c30a1c0-57b4-4cf6-8b6d-a568dac11e01")
SECURITY_REVIEW_STEP_ID = StepInstanceId("e7f9d3a2-1b64-48c5-9d20-6a7b8c9d0e02")
FINAL_REVIEW_STEP_ID = StepInstanceId("3a8c1e5f-92d7-4b60-a134-5e6f7a8b9c03")
QA_STEP_ID = StepInstanceId("b4d6f8a0-23c5-47e9-8a12-3c4d5e6f7a04")


def _skill(path: str) -> CapabilityReference:
    return CapabilityReference(CapabilityKind.SKILL, path)


def _agent_reference(path: str) -> CapabilityReference:
    return CapabilityReference(CapabilityKind.AGENT_REFERENCE, path)


ANALYSIS_DEFAULT_CAPABILITIES = (
    _skill("skills/codex/grill-with-docs/SKILL.md"),
    _skill("skills/codex/domain-modeling/SKILL.md"),
    _skill("skills/codex/to-prd/SKILL.md"),
    _skill("skills/codex/to-issues/SKILL.md"),
)
DEVELOPMENT_REQUIRED_CAPABILITIES = (
    RequiredCapability(
        _skill("skills/codex/tdd/SKILL.md"),
        "The Development component contract requires test-first implementation guidance.",
    ),
)
DEVELOPMENT_DEFAULT_CAPABILITIES = (
    _skill("skills/codex/csharp-expert-developer/SKILL.md"),
    _skill("skills/codex/angular-typescript-developer/SKILL.md"),
    _agent_reference("agents/codex/csharp-expert-developer.md"),
    _agent_reference("agents/codex/angular-typescript-developer.md"),
)
REVIEW_REQUIRED_CAPABILITIES = (
    RequiredCapability(
        _skill("skills/codex/senior-code-reviewer/SKILL.md"),
        "The Review component contract requires the senior code-review gate.",
    ),
)
REVIEW_DEFAULT_CAPABILITIES = (
    _agent_reference("agents/codex/senior-code-reviewer.md"),
)
QA_REQUIRED_CAPABILITIES = (
    RequiredCapability(
        _skill("skills/codex/qa-automation-engineer/SKILL.md"),
        "The QA component contract requires acceptance-focused automation guidance.",
    ),
)
QA_DEFAULT_CAPABILITIES = (
    _agent_reference("agents/codex/qa-automation-engineer.md"),
)


class RoleRunner(Protocol):
    def run_role(self, **arguments: Any) -> RoleResult: ...


@dataclass(frozen=True)
class PortableRoleAdapter:
    role: str
    step_adapter: str | None = None

    @property
    def execution_role(self) -> str:
        return self.step_adapter or self.role

    def execute(
        self,
        runner: RoleRunner,
        *,
        step: WorkflowStep,
        issue: Issue,
        pass_number: int,
        step_attempt_id: str,
        prompt_session_id: str,
        inputs: Mapping[str, RoleResult],
        rework_attempt: StepAttemptRecord | None = None,
    ) -> RoleResult:
        execution_role = self.execution_role
        arguments: dict[str, Any] = {
            "role": self.role,
            "issue": issue,
            "pass_number": pass_number,
            "step_instance_id": str(step.instance_id),
            "step_display_name": step.display_name,
            "step_attempt_id": step_attempt_id,
            "prompt_session_id": prompt_session_id,
        }
        if execution_role != self.role:
            arguments["role_adapter"] = execution_role
        if execution_role in {"reviewer", "qa"}:
            arguments["coder_result"] = inputs["implementation"]
        if execution_role == "qa":
            arguments["review_result"] = inputs["review_result"]
        if execution_role == "coder" and REWORK_INPUT_KEY in inputs:
            rework = inputs[REWORK_INPUT_KEY]
            arguments["fix_list"] = rework.fix_list or rework.findings
            if rework_attempt is None:
                raise ValueError("Rework input requires its triggering Step Attempt Record.")
            arguments["rework_attempt_record"] = step_attempt_record_to_dict(
                rework_attempt
            )
        arguments["codex_settings"] = step.codex_settings
        arguments["execution_budget"] = step.execution_budget
        arguments["skill_paths"] = step.capability_profile.skills
        arguments["agent_paths"] = step.capability_profile.agent_references
        arguments["step_guidance"] = (
            step.guidance.text if step.guidance is not None else None
        )
        return runner.run_role(**arguments)


@dataclass(frozen=True)
class PortableStepComponent:
    component_id: StepComponentId
    default_display_name: str
    scope: StepScope
    supported_outcomes: frozenset[StepOutcome]
    adapter: PortableRoleAdapter | None
    input_ports: Mapping[str, DataContractId] = field(default_factory=dict)
    optional_input_ports: Mapping[str, DataContractId] = field(default_factory=dict)
    output_ports: Mapping[str, DataContractId] = field(default_factory=dict)
    codex_execution_defaults: CodexExecutionSettings | None = None
    execution_budget_defaults: ExecutionBudget = field(
        default_factory=ExecutionBudget
    )
    required_capabilities: tuple[RequiredCapability, ...] = ()
    default_capabilities: tuple[CapabilityReference, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "default_display_name",
            normalize_single_line_display_name(
                self.default_display_name,
                field_name="Component default display name",
            ),
        )
        overlapping_inputs = set(self.input_ports) & set(self.optional_input_ports)
        if overlapping_inputs:
            raise ValueError(
                "Step Component input ports cannot be both required and optional: "
                f"{sorted(overlapping_inputs)}"
            )
        if not isinstance(self.execution_budget_defaults, ExecutionBudget):
            raise ValueError(
                "Step Components require an Execution Budget default."
            )
        if self.adapter is None:
            if self.codex_execution_defaults is not None:
                raise ValueError(
                    "Local deterministic Step Components cannot declare Codex defaults."
                )
        elif self.codex_execution_defaults is None:
            object.__setattr__(
                self,
                "codex_execution_defaults",
                default_codex_execution_settings(self.adapter.execution_role),
            )
        required_references = tuple(
            item.reference for item in self.required_capabilities
        )
        all_references = (*required_references, *self.default_capabilities)
        if len(set(all_references)) != len(all_references):
            raise ValueError(
                "Required and default component capabilities must be distinct."
            )

    @property
    def all_input_ports(self) -> dict[str, DataContractId]:
        return {**self.input_ports, **self.optional_input_ports}

    @property
    def is_codex_backed(self) -> bool:
        return self.adapter is not None

    def default_capability_profile(self) -> StepCapabilityProfile:
        return capability_profile_from_defaults(
            self.required_capabilities,
            self.default_capabilities,
        )

    def required_capability_reason(
        self,
        reference: CapabilityReference,
    ) -> str | None:
        return next(
            (
                item.reason
                for item in self.required_capabilities
                if item.reference == reference
            ),
            None,
        )


class PortableStepComponentCatalog:
    def __init__(self, components: Iterable[PortableStepComponent]) -> None:
        self._components: dict[StepComponentId, PortableStepComponent] = {}
        for component in components:
            if component.component_id in self._components:
                raise ValueError(
                    f"Duplicate installed Step Component ID: {component.component_id}"
                )
            self._components[component.component_id] = component

    @property
    def components(self) -> tuple[PortableStepComponent, ...]:
        return tuple(self._components.values())

    def resolve(self, component_id: StepComponentId) -> PortableStepComponent:
        try:
            return self._components[component_id]
        except KeyError as error:
            raise ValueError(
                f"Step Component {component_id!r} is not installed in the portable catalog."
            ) from error


def default_portable_component_catalog() -> PortableStepComponentCatalog:
    common_outcomes = frozenset(
        {
            StepOutcome.SUCCEEDED,
            StepOutcome.BLOCKED,
            StepOutcome.FAILED,
            StepOutcome.CANCELLED,
        }
    )
    analysis_adapter = PortableRoleAdapter("analysis")
    development_adapter = PortableRoleAdapter("coder")
    reviewer_adapter = PortableRoleAdapter("reviewer")
    qa_adapter = PortableRoleAdapter("qa")
    return PortableStepComponentCatalog(
        (
            PortableStepComponent(
                component_id=DEVELOPMENT_COMPONENT_ID,
                default_display_name="Development",
                scope=StepScope.ISSUE,
                supported_outcomes=common_outcomes,
                adapter=development_adapter,
                output_ports={"implementation": IMPLEMENTATION_RESULT_CONTRACT},
                execution_budget_defaults=default_execution_budget("coder"),
                required_capabilities=DEVELOPMENT_REQUIRED_CAPABILITIES,
                default_capabilities=DEVELOPMENT_DEFAULT_CAPABILITIES,
            ),
            PortableStepComponent(
                component_id=REVIEWER_COMPONENT_ID,
                default_display_name="Code Review",
                scope=StepScope.ISSUE,
                supported_outcomes=common_outcomes | {StepOutcome.CHANGES_REQUESTED},
                adapter=reviewer_adapter,
                input_ports={"implementation": IMPLEMENTATION_RESULT_CONTRACT},
                output_ports={"review": REVIEW_RESULT_CONTRACT},
                execution_budget_defaults=default_execution_budget("reviewer"),
                required_capabilities=REVIEW_REQUIRED_CAPABILITIES,
                default_capabilities=REVIEW_DEFAULT_CAPABILITIES,
            ),
            PortableStepComponent(
                component_id=QA_COMPONENT_ID,
                default_display_name="QA",
                scope=StepScope.ISSUE,
                supported_outcomes=common_outcomes | {StepOutcome.CHANGES_REQUESTED},
                adapter=qa_adapter,
                input_ports={
                    "implementation": IMPLEMENTATION_RESULT_CONTRACT,
                    "review_result": REVIEW_RESULT_CONTRACT,
                },
                output_ports={"qa_result": QA_RESULT_CONTRACT},
                execution_budget_defaults=default_execution_budget("qa"),
                required_capabilities=QA_REQUIRED_CAPABILITIES,
                default_capabilities=QA_DEFAULT_CAPABILITIES,
            ),
            PortableStepComponent(
                component_id=ANALYSIS_COMPONENT_ID,
                default_display_name="Analysis",
                scope=StepScope.WORKFLOW,
                supported_outcomes=common_outcomes,
                adapter=analysis_adapter,
                execution_budget_defaults=default_execution_budget("analysis"),
                default_capabilities=ANALYSIS_DEFAULT_CAPABILITIES,
            ),
        )
    )


@dataclass(frozen=True)
class PortBinding:
    producer_step_id: StepInstanceId
    output_port: str
    allowed_outcomes: frozenset[StepOutcome] = field(
        default_factory=lambda: frozenset({StepOutcome.SUCCEEDED})
    )


@dataclass(frozen=True)
class WorkflowStep:
    instance_id: StepInstanceId
    display_name: str
    component_id: StepComponentId
    transitions: Mapping[StepOutcome, StepInstanceId | None] = field(default_factory=dict)
    input_bindings: Mapping[str, PortBinding] = field(default_factory=dict)
    codex_settings: CodexExecutionSettings | None = None
    execution_budget: ExecutionBudget = field(default_factory=ExecutionBudget)
    capability_profile: StepCapabilityProfile = field(
        default_factory=StepCapabilityProfile
    )
    guidance: StepGuidance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "display_name",
            normalize_single_line_display_name(
                self.display_name,
                field_name="Step display name",
            ),
        )
        if not isinstance(self.execution_budget, ExecutionBudget):
            raise ValueError("Workflow Steps require an Execution Budget.")
        if not isinstance(self.capability_profile, StepCapabilityProfile):
            raise ValueError("Workflow Steps require a Step Capability Profile.")
        if self.guidance is not None and not isinstance(self.guidance, StepGuidance):
            raise ValueError("Workflow Step guidance is invalid.")


@dataclass(frozen=True)
class WorkflowDefinition:
    schema: str
    start_step_id: StepInstanceId
    steps: tuple[WorkflowStep, ...]

    def step(self, instance_id: StepInstanceId) -> WorkflowStep:
        for step in self.steps:
            if step.instance_id == instance_id:
                return step
        raise KeyError(f"Unknown Step Instance ID: {instance_id}")

    def primary_path(self) -> tuple[WorkflowStep, ...]:
        path: list[WorkflowStep] = []
        visited: set[StepInstanceId] = set()
        current_id: StepInstanceId | None = self.start_step_id
        while current_id is not None:
            if current_id in visited:
                raise ValueError("The SUCCEEDED primary path contains a cycle.")
            visited.add(current_id)
            current = self.step(current_id)
            path.append(current)
            current_id = current.transitions.get(StepOutcome.SUCCEEDED)
        return tuple(path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "start_step_id": str(self.start_step_id),
            "steps": [
                {
                    "instance_id": str(step.instance_id),
                    "display_name": step.display_name,
                    "component_id": str(step.component_id),
                    "transitions": {
                        outcome.value: str(target) if target is not None else None
                        for outcome, target in sorted(
                            step.transitions.items(),
                            key=lambda item: item[0].value,
                        )
                    },
                    "input_bindings": {
                        input_port: {
                            "producer_step_id": str(binding.producer_step_id),
                            "output_port": binding.output_port,
                            **(
                                {
                                    "allowed_outcomes": sorted(
                                        outcome.value
                                        for outcome in binding.allowed_outcomes
                                    )
                                }
                                if binding.allowed_outcomes
                                != frozenset({StepOutcome.SUCCEEDED})
                                else {}
                            ),
                        }
                        for input_port, binding in sorted(step.input_bindings.items())
                    },
                    **(
                        {"codex_settings": step.codex_settings.to_dict()}
                        if step.codex_settings is not None
                        else {}
                    ),
                    "execution_budget": step.execution_budget.to_dict(),
                    "capability_profile": step.capability_profile.to_dict(),
                    **(
                        {"guidance": step.guidance.to_dict()}
                        if step.guidance is not None
                        else {}
                    ),
                }
                for step in self.steps
            ],
        }


@dataclass(frozen=True)
class TypedStepOutput:
    contract_id: DataContractId
    value: RoleResult


@dataclass(frozen=True)
class StepAttemptRecord:
    attempt_id: str
    step_instance_id: StepInstanceId
    issue_id: str | None
    pass_number: int
    prompt_session_id: str
    outcome: StepOutcome
    result: RoleResult
    outputs: Mapping[str, TypedStepOutput]
    started_at: str
    finished_at: str
    elapsed_seconds: float
    backend_thread_id: str | None = None
    backend_turn_id: str | None = None
    blocked_reason: str | None = None
    blocker_details: tuple[str, ...] = ()
    failure_reason: str | None = None
    rework_attempt_id: str | None = None
    attempt_context: StepAttemptContext | None = None


def step_attempt_record_to_dict(attempt: StepAttemptRecord) -> dict[str, Any]:
    return {
        "attempt_id": attempt.attempt_id,
        "step_instance_id": str(attempt.step_instance_id),
        "issue_id": attempt.issue_id,
        "pass": attempt.pass_number,
        "prompt_session_id": attempt.prompt_session_id,
        "outcome": attempt.outcome.value,
        "result": result_to_dict(attempt.result),
        "outputs": {
            port_name: {
                "contract_id": str(output.contract_id),
                "result": result_to_dict(output.value),
            }
            for port_name, output in attempt.outputs.items()
        },
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
        "elapsed_seconds": attempt.elapsed_seconds,
        "backend_thread_id": attempt.backend_thread_id,
        "backend_turn_id": attempt.backend_turn_id,
        "blocked_reason": attempt.blocked_reason,
        "blocker_details": list(attempt.blocker_details),
        "failure_reason": attempt.failure_reason,
        "rework_attempt_id": attempt.rework_attempt_id,
        "attempt_context": (
            attempt.attempt_context.to_dict()
            if attempt.attempt_context is not None
            else None
        ),
    }


@dataclass(frozen=True)
class StepRuntimeState:
    step_instance_id: StepInstanceId
    issue_id: str | None
    status: StepRuntimeStatus
    pass_number: int
    prompt_session_id: str | None = None
    attempt_id: str | None = None
    started_at: str | None = None
    outcome: StepOutcome | None = None
    backend_thread_id: str | None = None
    backend_turn_id: str | None = None
    checkpoint: str | None = None
    component_state: Mapping[str, Any] = field(default_factory=dict)
    attempt_context: StepAttemptContext | None = None


@dataclass(frozen=True)
class InterruptedStepAttemptRecord:
    attempt_id: str
    step_instance_id: StepInstanceId
    issue_id: str | None
    pass_number: int
    prompt_session_id: str
    started_at: str
    interrupted_at: str
    backend_thread_id: str | None = None
    backend_turn_id: str | None = None
    checkpoint: str | None = None
    attempt_context: StepAttemptContext | None = None


@dataclass(frozen=True)
class PortableWorkflowRunResult:
    issue_status: IssueStatus
    current_step_instance_id: StepInstanceId | None
    runtime_states: tuple[StepRuntimeState, ...]
    attempts: tuple[StepAttemptRecord, ...]
    role_result: RoleResult


@dataclass(frozen=True)
class PortableWorkflowCheckpoint:
    issue_id: str
    issue_status: IssueStatus
    current_step_instance_id: StepInstanceId | None
    pass_number: int
    runtime_states: tuple[StepRuntimeState, ...]
    attempts: tuple[StepAttemptRecord, ...]
    pending_rework_attempt_id: str | None = None
    cycle_path_step_instance_ids: tuple[StepInstanceId, ...] = ()


def resolve_portable_inputs(
    step: WorkflowStep,
    attempts: Iterable[StepAttemptRecord],
    *,
    issue_id: str | None,
    catalog: PortableStepComponentCatalog,
) -> dict[str, RoleResult]:
    attempt_list = list(attempts)
    resolved: dict[str, RoleResult] = {}
    for input_port, binding in step.input_bindings.items():
        expected_contract = catalog.resolve(step.component_id).all_input_ports[
            input_port
        ]
        for attempt in reversed(attempt_list):
            output = attempt.outputs.get(binding.output_port)
            if (
                attempt.step_instance_id == binding.producer_step_id
                and attempt.issue_id == issue_id
                and attempt.outcome in binding.allowed_outcomes
                and output is not None
                and output.contract_id == expected_contract
            ):
                resolved[input_port] = output.value
                break
        else:
            outcome_kind = (
                "successful"
                if binding.allowed_outcomes == frozenset({StepOutcome.SUCCEEDED})
                else "permitted"
            )
            raise RuntimeError(
                f"No compatible {outcome_kind} output resolves binding for "
                f"{step.display_name!r}.{input_port}."
            )
    return resolved


class PortableWorkflowExecutor:
    def __init__(
        self,
        workflow: WorkflowDefinition,
        catalog: PortableStepComponentCatalog,
        role_runner: RoleRunner,
    ) -> None:
        load_portable_workflow(workflow.to_dict(), catalog)
        self._workflow = workflow
        self._catalog = catalog
        self._role_runner = role_runner
        self._issue_start_step_id = self._find_issue_start_step_id()

    def _find_issue_start_step_id(self) -> StepInstanceId:
        for step in self._workflow.primary_path():
            if self._catalog.resolve(step.component_id).scope is StepScope.ISSUE:
                return step.instance_id
        raise ValueError("Portable issue execution requires an issue-scoped Workflow Step.")

    def run(
        self,
        issue: Issue,
        *,
        pass_number: int,
        max_passes: int | None = None,
        recovery: PortableWorkflowCheckpoint | None = None,
        checkpoint: Callable[[PortableWorkflowCheckpoint], None] | None = None,
    ) -> PortableWorkflowRunResult:
        final_pass = pass_number if max_passes is None else max_passes
        if final_pass < pass_number:
            raise ValueError("Portable workflow max passes cannot precede its starting pass.")
        (
            attempts,
            runtimes,
            current_step_id,
            current_pass,
            cycle_path,
        ) = self._restore_execution(issue, pass_number, recovery)
        if current_pass > final_pass:
            raise ValueError("Portable workflow recovery pass exceeds max passes.")
        pending_rework_attempt = self._pending_rework_attempt(recovery, attempts)
        last_result = (
            attempts[-1].result
            if attempts
            else RoleResult(status="BLOCKED", summary="Workflow did not execute a step.")
        )

        while current_step_id is not None:
            step = self._workflow.step(current_step_id)
            component = self._catalog.resolve(step.component_id)
            if component.adapter is None:
                raise RuntimeError(
                    f"Local deterministic step {step.display_name!r} has no portable "
                    "execution adapter."
                )
            step_attempt_id = str(uuid.uuid4())
            prompt_session_id = str(uuid.uuid4())
            started_at = _timestamp()
            attempt_context = StepAttemptContext(
                capability_profile=step.capability_profile,
                guidance=(
                    step.guidance.text
                    if step.guidance is not None
                    else None
                ),
            )
            runtimes[current_step_id] = StepRuntimeState(
                step_instance_id=current_step_id,
                issue_id=issue.number,
                status=StepRuntimeStatus.RUNNING,
                pass_number=current_pass,
                prompt_session_id=prompt_session_id,
                attempt_id=step_attempt_id,
                started_at=started_at,
                attempt_context=attempt_context,
            )
            self._emit_checkpoint(
                checkpoint,
                issue,
                IssueStatus.IN_PROGRESS,
                current_step_id,
                current_pass,
                runtimes,
                attempts,
                pending_rework_attempt,
                cycle_path,
            )
            inputs = resolve_portable_inputs(
                step,
                attempts,
                issue_id=issue.number,
                catalog=self._catalog,
            )
            if (
                component.adapter.execution_role == "coder"
                and pending_rework_attempt is not None
            ):
                inputs[REWORK_INPUT_KEY] = pending_rework_attempt.result
            consumed_rework_attempt_id = (
                pending_rework_attempt.attempt_id
                if component.adapter.execution_role == "coder"
                and pending_rework_attempt is not None
                else None
            )
            started_clock = time.monotonic()
            last_result = component.adapter.execute(
                self._role_runner,
                step=step,
                issue=issue,
                pass_number=current_pass,
                step_attempt_id=step_attempt_id,
                prompt_session_id=prompt_session_id,
                inputs=inputs,
                rework_attempt=(
                    pending_rework_attempt
                    if component.adapter.execution_role == "coder"
                    else None
                ),
            )
            elapsed_seconds = max(0.0, time.monotonic() - started_clock)
            outcome = _outcome_for_role_result(last_result, component)
            if (
                component.adapter.execution_role == "coder"
                and outcome is StepOutcome.SUCCEEDED
            ):
                pending_rework_attempt = None
            outputs = {
                port_name: TypedStepOutput(contract_id, last_result)
                for port_name, contract_id in component.output_ports.items()
            }
            attempts.append(
                StepAttemptRecord(
                    attempt_id=step_attempt_id,
                    step_instance_id=current_step_id,
                    issue_id=issue.number,
                    pass_number=current_pass,
                    prompt_session_id=prompt_session_id,
                    outcome=outcome,
                    result=last_result,
                    outputs=outputs,
                    started_at=started_at,
                    finished_at=_timestamp(),
                    elapsed_seconds=elapsed_seconds,
                    blocked_reason=(
                        last_result.summary
                        if outcome is StepOutcome.BLOCKED
                        else None
                    ),
                    blocker_details=(
                        tuple(
                            last_result.fix_list
                            or last_result.findings
                            or last_result.residual_risks
                        )
                        if outcome is StepOutcome.BLOCKED
                        else ()
                    ),
                    failure_reason=(
                        last_result.summary
                        if outcome is StepOutcome.FAILED
                        else None
                    ),
                    rework_attempt_id=consumed_rework_attempt_id,
                    attempt_context=attempt_context,
                )
            )
            runtimes[current_step_id] = StepRuntimeState(
                step_instance_id=current_step_id,
                issue_id=issue.number,
                status=StepRuntimeStatus.COMPLETED,
                pass_number=current_pass,
                prompt_session_id=prompt_session_id,
                attempt_id=step_attempt_id,
                started_at=started_at,
                outcome=outcome,
                attempt_context=attempt_context,
            )
            next_step_id = step.transitions.get(outcome)
            if outcome is StepOutcome.CHANGES_REQUESTED:
                pending_rework_attempt = attempts[-1]
            exhausted_cycle_target: WorkflowStep | None = None
            if next_step_id is not None:
                starts_new_pass = (
                    outcome is StepOutcome.CHANGES_REQUESTED
                    or next_step_id in cycle_path
                )
                if starts_new_pass and current_pass >= final_pass:
                    exhausted_cycle_target = self._workflow.step(next_step_id)
                    next_step_id = None
                elif starts_new_pass:
                    current_pass += 1
                    cycle_path = [next_step_id]
                else:
                    cycle_path.append(next_step_id)
            current_step_id = next_step_id
            if (
                exhausted_cycle_target is not None
                and outcome is StepOutcome.SUCCEEDED
            ):
                last_result = RoleResult(
                    status="BLOCKED",
                    summary=(
                        f"Workflow cycle budget exhausted at pass {current_pass} of "
                        f"{final_pass}: {step.display_name!r} produced SUCCEEDED, but "
                        "its transition still targets "
                        f"{exhausted_cycle_target.display_name!r}."
                    ),
                    fix_list=[
                        "Increase the configured maximum passes or repair the cycle "
                        f"between {step.display_name!r} and "
                        f"{exhausted_cycle_target.display_name!r} in the workflow editor."
                    ],
                )
                issue_status = IssueStatus.BLOCKED
            else:
                issue_status = (
                    _issue_status_for_outcome(outcome)
                    if current_step_id is None
                    else IssueStatus.IN_PROGRESS
                )
            self._emit_checkpoint(
                checkpoint,
                issue,
                issue_status,
                current_step_id,
                current_pass,
                runtimes,
                attempts,
                pending_rework_attempt,
                cycle_path,
            )
            if current_step_id is None:
                return PortableWorkflowRunResult(
                    issue_status=issue_status,
                    current_step_instance_id=None,
                    runtime_states=tuple(runtimes.values()),
                    attempts=tuple(attempts),
                    role_result=last_result,
                )

        raise RuntimeError("Portable workflow ended without a terminal result.")

    def _restore_execution(
        self,
        issue: Issue,
        pass_number: int,
        recovery: PortableWorkflowCheckpoint | None,
    ) -> tuple[
        list[StepAttemptRecord],
        dict[StepInstanceId, StepRuntimeState],
        StepInstanceId,
        int,
        list[StepInstanceId],
    ]:
        if recovery is None:
            return (
                [],
                {},
                self._issue_start_step_id,
                pass_number,
                [self._issue_start_step_id],
            )
        if recovery.issue_id != issue.number:
            raise ValueError("Portable workflow recovery belongs to another Issue.")
        if recovery.issue_status is not IssueStatus.IN_PROGRESS:
            raise ValueError("Portable workflow recovery must be in progress.")
        if recovery.pass_number < 1:
            raise ValueError("Portable workflow recovery pass must be positive.")
        current_step_id = recovery.current_step_instance_id
        if current_step_id is None:
            raise ValueError("A terminal portable workflow checkpoint cannot be resumed.")
        self._workflow.step(current_step_id)
        for runtime in recovery.runtime_states:
            if runtime.issue_id != issue.number:
                raise ValueError("Portable workflow runtime belongs to another Issue.")
            self._workflow.step(runtime.step_instance_id)
        for attempt in recovery.attempts:
            if attempt.issue_id != issue.number:
                raise ValueError("Portable workflow attempt belongs to another Issue.")
            self._workflow.step(attempt.step_instance_id)
        cycle_path = list(recovery.cycle_path_step_instance_ids)
        if cycle_path:
            if len(set(cycle_path)) != len(cycle_path):
                raise ValueError("Portable workflow recovery cycle path must be unique.")
            for step_id in cycle_path:
                self._workflow.step(step_id)
            if cycle_path[-1] != current_step_id:
                raise ValueError(
                    "Portable workflow recovery cycle path must end at its current step."
                )
        else:
            cycle_path = [current_step_id]
        return (
            list(recovery.attempts),
            {runtime.step_instance_id: runtime for runtime in recovery.runtime_states},
            current_step_id,
            recovery.pass_number,
            cycle_path,
        )

    @staticmethod
    def _pending_rework_attempt(
        recovery: PortableWorkflowCheckpoint | None,
        attempts: Iterable[StepAttemptRecord],
    ) -> StepAttemptRecord | None:
        if recovery is None or recovery.pending_rework_attempt_id is None:
            return None
        for attempt in attempts:
            if attempt.attempt_id == recovery.pending_rework_attempt_id:
                if attempt.outcome is not StepOutcome.CHANGES_REQUESTED:
                    raise ValueError("Pending rework must reference a changes-requested attempt.")
                return attempt
        raise ValueError("Pending rework references an unknown Step Attempt Record.")

    @staticmethod
    def _emit_checkpoint(
        callback: Callable[[PortableWorkflowCheckpoint], None] | None,
        issue: Issue,
        issue_status: IssueStatus,
        current_step_instance_id: StepInstanceId | None,
        pass_number: int,
        runtimes: Mapping[StepInstanceId, StepRuntimeState],
        attempts: Iterable[StepAttemptRecord],
        pending_rework_attempt: StepAttemptRecord | None,
        cycle_path: Iterable[StepInstanceId],
    ) -> None:
        if callback is None:
            return
        callback(
            PortableWorkflowCheckpoint(
                issue_id=issue.number,
                issue_status=issue_status,
                current_step_instance_id=current_step_instance_id,
                pass_number=pass_number,
                runtime_states=tuple(runtimes.values()),
                attempts=tuple(attempts),
                pending_rework_attempt_id=(
                    pending_rework_attempt.attempt_id
                    if pending_rework_attempt is not None
                    else None
                ),
                cycle_path_step_instance_ids=tuple(cycle_path),
            )
        )


def _outcome_for_role_result(
    result: RoleResult,
    component: PortableStepComponent,
) -> StepOutcome:
    if result.status == "PASS":
        return StepOutcome.SUCCEEDED
    if result.status == "BLOCKED":
        return StepOutcome.BLOCKED
    if StepOutcome.CHANGES_REQUESTED in component.supported_outcomes:
        return StepOutcome.CHANGES_REQUESTED
    return StepOutcome.FAILED


def _issue_status_for_outcome(outcome: StepOutcome) -> IssueStatus:
    return {
        StepOutcome.SUCCEEDED: IssueStatus.COMPLETED,
        StepOutcome.CHANGES_REQUESTED: IssueStatus.CHANGES_REQUESTED,
        StepOutcome.BLOCKED: IssueStatus.BLOCKED,
        StepOutcome.FAILED: IssueStatus.FAILED,
        StepOutcome.CANCELLED: IssueStatus.CANCELLED,
    }[outcome]


def _timestamp() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def canonical_workflow_hash(workflow: WorkflowDefinition) -> str:
    return canonical_workflow_document_hash(workflow.to_dict())


def canonical_workflow_document_hash(document: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_portable_workflow(
    document: Mapping[str, Any],
    catalog: PortableStepComponentCatalog,
) -> WorkflowDefinition:
    unknown_fields = set(document) - PORTABLE_WORKFLOW_FIELDS
    if unknown_fields:
        raise ValueError(
            f"Unsupported portable workflow fields: {sorted(unknown_fields)}"
        )
    if document.get("schema") != PORTABLE_WORKFLOW_SCHEMA:
        raise ValueError(f"Expected workflow schema {PORTABLE_WORKFLOW_SCHEMA!r}.")

    raw_steps = document.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("Portable workflow steps must be a non-empty list.")

    steps: list[WorkflowStep] = []
    names: set[str] = set()
    for raw_step in raw_steps:
        if not isinstance(raw_step, Mapping):
            raise ValueError("Each portable workflow step must be an object.")
        if "scope" in raw_step:
            raise ValueError("Step scope is component-owned and cannot be configured.")
        unknown_fields = set(raw_step) - {
            "instance_id",
            "display_name",
            "component_id",
            "transitions",
            "input_bindings",
            "codex_settings",
            "execution_budget",
            "capability_profile",
            "guidance",
        }
        if unknown_fields:
            raise ValueError(
                f"Unsupported portable workflow step fields: {sorted(unknown_fields)}"
            )

        instance_id = StepInstanceId(raw_step.get("instance_id"))
        display_name = normalize_single_line_display_name(
            raw_step.get("display_name"),
            field_name=f"Step {instance_id} display name",
        )
        normalized_name = display_name.casefold()
        if normalized_name in names:
            raise ValueError("Portable workflow requires unique display names.")
        names.add(normalized_name)

        component_id = StepComponentId(raw_step.get("component_id"))
        component = catalog.resolve(component_id)
        codex_settings = _load_codex_execution_settings(
            raw_step.get("codex_settings"),
            component,
        )
        execution_budget = _load_execution_budget(
            raw_step.get("execution_budget"),
            component,
        )
        capability_profile = _load_capability_profile(
            raw_step.get("capability_profile"),
            component,
        )
        guidance = _load_guidance(raw_step.get("guidance"), component)
        try:
            transitions = _load_transitions(
                raw_step.get("transitions", {}),
                component,
            )
        except ValueError as error:
            raise ValueError(
                f"Step {display_name!r} ({instance_id}) Outcome Transition error: "
                f"{error}"
            ) from error
        try:
            input_bindings = _load_input_bindings(
                raw_step.get("input_bindings", {})
            )
        except ValueError as error:
            raise ValueError(
                f"Step {display_name!r} ({instance_id}) Port Binding error: {error}"
            ) from error
        steps.append(
            WorkflowStep(
                instance_id=instance_id,
                display_name=display_name,
                component_id=component_id,
                transitions=transitions,
                input_bindings=input_bindings,
                codex_settings=codex_settings,
                execution_budget=execution_budget,
                capability_profile=capability_profile,
                guidance=guidance,
            )
        )

    workflow = WorkflowDefinition(
        schema=PORTABLE_WORKFLOW_SCHEMA,
        start_step_id=StepInstanceId(document.get("start_step_id")),
        steps=tuple(steps),
    )
    step_ids = {step.instance_id for step in workflow.steps}
    if len(step_ids) != len(workflow.steps):
        raise ValueError("Portable workflow Step Instance IDs must be unique.")
    if workflow.start_step_id not in step_ids:
        raise ValueError("Portable workflow start Step Instance ID is unknown.")
    for step in workflow.steps:
        for target in step.transitions.values():
            if target is not None and target not in step_ids:
                raise ValueError(
                    f"Step {step.display_name!r} targets unknown Step Instance ID {target}."
                )
    reachable_step_ids = _reachable_step_ids(workflow)
    for step in workflow.steps:
        if step.instance_id not in reachable_step_ids:
            raise ValueError(
                f"Step {step.display_name!r} ({step.instance_id}) is unreachable; "
                "repair an Outcome Transition that leads to this Step Instance."
            )
    primary_path = workflow.primary_path()
    successful_terminal = primary_path[-1]
    if (
        StepOutcome.SUCCEEDED not in successful_terminal.transitions
        or successful_terminal.transitions[StepOutcome.SUCCEEDED] is not None
    ):
        raise ValueError(
            f"Step {successful_terminal.display_name!r} "
            f"({successful_terminal.instance_id}) requires an explicit SUCCEEDED "
            "transition to a successful terminal."
        )
    for step in workflow.steps:
        _validate_input_bindings(workflow, step, catalog)
    _validate_scope_ordering(workflow, catalog)
    return workflow


def validate_portable_workflow_for_apply(
    workflow: WorkflowDefinition,
    catalog: PortableStepComponentCatalog,
) -> WorkflowDefinition:
    """Validate stricter editor invariants for a newly applied workflow."""
    for step in workflow.steps:
        if (
            step.guidance is not None
            and step.guidance.review_state is GuidanceReviewState.NEEDS_REVIEW
        ):
            raise ValueError(
                f"Step {step.display_name!r} ({step.instance_id}) has Step Guidance "
                "in NEEDS_REVIEW; explicitly keep, edit, or clear it before Apply."
            )
    validated = load_portable_workflow(workflow.to_dict(), catalog)
    planning_workflow_step(validated, catalog)
    for step in validated.steps:
        component = catalog.resolve(step.component_id)
        missing_outcomes = component.supported_outcomes - set(step.transitions)
        if missing_outcomes:
            raise ValueError(
                f"Step {step.display_name!r} ({step.instance_id}) has no Outcome "
                "Transition for "
                f"{sorted(outcome.value for outcome in missing_outcomes)}; route each "
                "declared outcome to a Step Instance or an explicit terminal."
            )
    return validated


def planning_workflow_step(
    workflow: WorkflowDefinition,
    catalog: PortableStepComponentCatalog,
) -> WorkflowStep:
    """Return the one workflow-scoped step supported by portable planning."""
    workflow_steps = tuple(
        step
        for step in workflow.steps
        if catalog.resolve(step.component_id).scope is StepScope.WORKFLOW
    )
    if len(workflow_steps) != 1:
        step_names = ", ".join(repr(step.display_name) for step in workflow_steps)
        found_steps = f": {step_names}." if step_names else "."
        raise ValueError(
            "Portable planning requires exactly one WORKFLOW-scoped Workflow "
            f"Step; found {len(workflow_steps)}"
            f"{found_steps} Keep one planning step before Apply."
        )
    return workflow_steps[0]


def preflight_codex_execution_settings(
    workflow: WorkflowDefinition,
    component_catalog: PortableStepComponentCatalog,
    model_catalog: CodexModelCatalog,
) -> None:
    """Authorize exact snapshotted settings against a fresh live catalog."""
    if not model_catalog.is_fresh:
        raise ValueError(
            "Run preflight requires a fresh live Codex Model Catalog; cached data "
            "is display-only. Use Retry Catalog in /options and start again."
        )
    for step in workflow.steps:
        component = component_catalog.resolve(step.component_id)
        if not component.is_codex_backed:
            continue
        settings = step.codex_settings
        if settings is None:
            raise ValueError(
                f"Step {step.display_name!r} has no Codex Execution Settings. "
                "Repair it in /options."
            )
        try:
            model = model_catalog.model(settings.model)
        except ValueError as error:
            raise ValueError(
                f"Step {step.display_name!r} selects unavailable model "
                f"{settings.model!r}. Use Retry Catalog in /options."
            ) from error
        if settings.reasoning_effort not in model.reasoning_efforts:
            raise ValueError(
                f"Step {step.display_name!r} selects unsupported reasoning effort "
                f"{settings.reasoning_effort!r} for model {settings.model!r}. "
                "Use Retry Catalog in /options."
            )
        if settings.fast is FastPreference.ON and not model.supports_fast:
            raise ValueError(
                f"Step {step.display_name!r} selects Fast ON, but model "
                f"{settings.model!r} does not advertise Fast. Use Retry Catalog "
                "in /options."
            )


def _reachable_step_ids(workflow: WorkflowDefinition) -> frozenset[StepInstanceId]:
    reachable: set[StepInstanceId] = set()
    pending = [workflow.start_step_id]
    while pending:
        current_id = pending.pop()
        if current_id in reachable:
            continue
        reachable.add(current_id)
        current = workflow.step(current_id)
        pending.extend(
            target for target in current.transitions.values() if target is not None
        )
    return frozenset(reachable)


def _load_transitions(
    raw_transitions: Any,
    component: PortableStepComponent,
) -> dict[StepOutcome, StepInstanceId | None]:
    if not isinstance(raw_transitions, Mapping):
        raise ValueError("Portable workflow transitions must be an object.")
    transitions: dict[StepOutcome, StepInstanceId | None] = {}
    for raw_outcome, raw_target in raw_transitions.items():
        try:
            outcome = StepOutcome(raw_outcome)
        except ValueError as error:
            raise ValueError(f"Unsupported Step Outcome: {raw_outcome!r}") from error
        if outcome not in component.supported_outcomes:
            raise ValueError(
                f"Component {component.component_id!r} does not support {outcome.value}."
            )
        transitions[outcome] = (
            None if raw_target is None else StepInstanceId(raw_target)
        )
    return transitions


def _load_codex_execution_settings(
    raw_settings: Any,
    component: PortableStepComponent,
) -> CodexExecutionSettings | None:
    if raw_settings is None:
        return component.codex_execution_defaults
    if component.codex_execution_defaults is None:
        raise ValueError(
            f"Local component {component.component_id!r} does not accept Codex settings."
        )
    if not isinstance(raw_settings, Mapping):
        raise ValueError("Codex Execution Settings must be an object.")
    unknown_fields = set(raw_settings) - CODEX_EXECUTION_SETTINGS_FIELDS
    if unknown_fields:
        raise ValueError(
            f"Unsupported Codex Execution Settings fields: {sorted(unknown_fields)}"
        )
    model = raw_settings.get("model")
    reasoning_effort = raw_settings.get("reasoning_effort")
    raw_fast = raw_settings.get("fast")
    if not isinstance(model, str) or not isinstance(reasoning_effort, str):
        raise ValueError("Codex model and reasoning effort must be strings.")
    try:
        fast = FastPreference(raw_fast)
    except (TypeError, ValueError) as error:
        raise ValueError("Codex Fast preference must be ON or OFF.") from error
    return CodexExecutionSettings(model, reasoning_effort, fast)


def _load_capability_profile(
    raw_profile: Any,
    component: PortableStepComponent,
) -> StepCapabilityProfile:
    profile = (
        component.default_capability_profile()
        if raw_profile is None
        else StepCapabilityProfile.from_dict(raw_profile)
    )
    missing_required = tuple(
        item
        for item in component.required_capabilities
        if not profile.contains(item.reference)
    )
    if missing_required:
        missing = missing_required[0]
        raise ValueError(
            f"Required capability {missing.reference.path!r} is locked by the "
            f"component contract: {missing.reason}"
        )
    return profile


def _load_guidance(
    raw_guidance: Any,
    component: PortableStepComponent,
) -> StepGuidance | None:
    if raw_guidance is None:
        return None
    if not component.is_codex_backed:
        raise ValueError(
            f"Local component {component.component_id!r} does not accept Step Guidance."
        )
    return StepGuidance.from_dict(raw_guidance)


def _load_execution_budget(
    raw_budget: Any,
    component: PortableStepComponent,
) -> ExecutionBudget:
    if raw_budget is None:
        return component.execution_budget_defaults
    if not isinstance(raw_budget, Mapping):
        raise ValueError("Execution Budget must be an object.")
    unknown_fields = set(raw_budget) - EXECUTION_BUDGET_FIELDS
    if unknown_fields:
        raise ValueError(
            f"Unsupported Execution Budget fields: {sorted(unknown_fields)}"
        )
    if set(raw_budget) != EXECUTION_BUDGET_FIELDS:
        raise ValueError(
            "Execution Budget requires timeout_seconds and checkpoint_seconds."
        )
    return ExecutionBudget(
        timeout_seconds=raw_budget["timeout_seconds"],
        checkpoint_seconds=raw_budget["checkpoint_seconds"],
    )


def _load_input_bindings(raw_bindings: Any) -> dict[str, PortBinding]:
    if not isinstance(raw_bindings, Mapping):
        raise ValueError("Portable workflow input bindings must be an object.")
    bindings: dict[str, PortBinding] = {}
    for input_port, raw_binding in raw_bindings.items():
        if not isinstance(input_port, str) or not input_port:
            raise ValueError("Portable workflow input port names must be non-empty strings.")
        if not isinstance(raw_binding, Mapping):
            raise ValueError(f"Input binding {input_port!r} must be an object.")
        if not set(raw_binding).issubset(
            {"producer_step_id", "output_port", "allowed_outcomes"}
        ) or not {"producer_step_id", "output_port"}.issubset(raw_binding):
            raise ValueError(f"Input binding {input_port!r} has unsupported fields.")
        output_port = raw_binding.get("output_port")
        if not isinstance(output_port, str) or not output_port:
            raise ValueError(f"Input binding {input_port!r} requires an output port.")
        raw_allowed_outcomes = raw_binding.get(
            "allowed_outcomes",
            [StepOutcome.SUCCEEDED.value],
        )
        if not isinstance(raw_allowed_outcomes, list) or not raw_allowed_outcomes:
            raise ValueError(
                f"Input binding {input_port!r} allowed outcomes must be a non-empty list."
            )
        try:
            allowed_outcomes = frozenset(
                StepOutcome(raw_outcome) for raw_outcome in raw_allowed_outcomes
            )
        except ValueError as error:
            raise ValueError(
                f"Input binding {input_port!r} has an unsupported allowed outcome."
            ) from error
        bindings[input_port] = PortBinding(
            producer_step_id=StepInstanceId(raw_binding.get("producer_step_id")),
            output_port=output_port,
            allowed_outcomes=allowed_outcomes,
        )
    return bindings


def _validate_input_bindings(
    workflow: WorkflowDefinition,
    step: WorkflowStep,
    catalog: PortableStepComponentCatalog,
) -> None:
    component = catalog.resolve(step.component_id)
    unknown_inputs = set(step.input_bindings) - set(component.all_input_ports)
    if unknown_inputs:
        raise ValueError(
            f"Step {step.display_name!r} ({step.instance_id}) binds unknown input "
            f"ports: {sorted(unknown_inputs)}"
        )
    missing_inputs = set(component.input_ports) - set(step.input_bindings)
    if missing_inputs:
        raise ValueError(
            "missing required input bindings for Step "
            f"{step.display_name!r} ({step.instance_id}), ports: {sorted(missing_inputs)}"
        )

    for input_port, binding in step.input_bindings.items():
        validate_port_binding(
            workflow,
            step,
            input_port,
            binding,
            catalog,
        )


def _validate_scope_ordering(
    workflow: WorkflowDefinition,
    catalog: PortableStepComponentCatalog,
) -> None:
    for source in workflow.steps:
        source_scope = catalog.resolve(source.component_id).scope
        if source_scope is not StepScope.ISSUE:
            continue
        for target_id in source.transitions.values():
            if target_id is None:
                continue
            target = workflow.step(target_id)
            target_scope = catalog.resolve(target.component_id).scope
            if target_scope is StepScope.WORKFLOW:
                raise ValueError(
                    f"Step {source.display_name!r} has ISSUE scope and transitions "
                    f"to Step {target.display_name!r} with WORKFLOW scope; portable "
                    "issue execution would run it once per Issue."
                )


def _is_binding_definitely_available(
    workflow: WorkflowDefinition,
    binding: PortBinding,
    consumer_step_id: StepInstanceId,
) -> bool:
    if binding.producer_step_id == consumer_step_id:
        return False

    pending = [(workflow.start_step_id, False)]
    visited: set[tuple[StepInstanceId, bool]] = set()
    reached_consumer = False
    while pending:
        current_id, output_available = pending.pop()
        state = (current_id, output_available)
        if state in visited:
            continue
        visited.add(state)
        if current_id == consumer_step_id:
            reached_consumer = True
            if not output_available:
                return False
            continue
        try:
            current = workflow.step(current_id)
        except KeyError:
            continue
        for outcome, target in current.transitions.items():
            if target is None:
                continue
            pending.append(
                (
                    target,
                    output_available
                    or (
                        current_id == binding.producer_step_id
                        and outcome in binding.allowed_outcomes
                    ),
                )
            )
    return reached_consumer


def validate_port_binding(
    workflow: WorkflowDefinition,
    step: WorkflowStep,
    input_port: str,
    binding: PortBinding,
    catalog: PortableStepComponentCatalog,
) -> None:
    """Validate one typed binding using its explicitly permitted outcomes."""
    component = catalog.resolve(step.component_id)
    if input_port not in component.all_input_ports:
        raise ValueError(
            f"Step {step.display_name!r} ({step.instance_id}) binds unknown input "
            f"port {input_port!r}."
        )
    try:
        producer = workflow.step(binding.producer_step_id)
    except KeyError as error:
        raise ValueError(
            f"Step {step.display_name!r} ({step.instance_id}) input port "
            f"{input_port!r} binds unknown producer {binding.producer_step_id}."
        ) from error
    producer_component = catalog.resolve(producer.component_id)
    if (
        producer_component.scope is StepScope.ISSUE
        and component.scope is StepScope.WORKFLOW
    ):
        raise ValueError(
            f"Step {step.display_name!r} ({step.instance_id}) input port "
            f"{input_port!r} has an invalid scope relationship: an ISSUE "
            "output cannot bind directly into a WORKFLOW-scoped step."
        )
    unsupported_outcomes = (
        binding.allowed_outcomes - producer_component.supported_outcomes
    )
    if unsupported_outcomes:
        raise ValueError(
            f"Binding for {step.display_name!r} ({step.instance_id}) input port "
            f"{input_port!r} permits outcomes the producer does not support: "
            f"{sorted(outcome.value for outcome in unsupported_outcomes)}."
        )
    produced_contract = producer_component.output_ports.get(binding.output_port)
    if produced_contract is None:
        raise ValueError(
            f"Binding for {step.display_name!r} ({step.instance_id}) input port "
            f"{input_port!r} targets {producer.display_name!r}, which has no "
            f"output port {binding.output_port!r}."
        )
    expected_contract = component.all_input_ports[input_port]
    if produced_contract != expected_contract:
        raise ValueError(
            f"Binding for {step.display_name!r} ({step.instance_id}) input port "
            f"{input_port!r} is incompatible: "
            f"expected {expected_contract}, got {produced_contract}."
        )
    if not _is_binding_definitely_available(
        workflow,
        binding,
        step.instance_id,
    ):
        raise ValueError(
            f"Binding for {step.display_name!r} ({step.instance_id}) input port "
            f"{input_port!r} does not have a permitted producer output on "
            "every executable path entering the consumer."
        )


def compatible_port_bindings(
    workflow: WorkflowDefinition,
    step: WorkflowStep,
    input_port: str,
    catalog: PortableStepComponentCatalog,
) -> tuple[PortBinding, ...]:
    """Return SUCCEEDED-only default bindings definite at the consumer."""
    component = catalog.resolve(step.component_id)
    if input_port not in component.all_input_ports:
        raise ValueError(
            f"Step {step.display_name!r} has no input port {input_port!r}."
        )
    bindings: list[PortBinding] = []
    for producer in workflow.steps:
        producer_component = catalog.resolve(producer.component_id)
        if StepOutcome.SUCCEEDED not in producer_component.supported_outcomes:
            continue
        for output_port in producer_component.output_ports:
            binding = PortBinding(producer.instance_id, output_port)
            try:
                validate_port_binding(
                    workflow,
                    step,
                    input_port,
                    binding,
                    catalog,
                )
            except ValueError:
                continue
            bindings.append(binding)
    return tuple(bindings)


def default_portable_workflow() -> WorkflowDefinition:
    component_catalog = default_portable_component_catalog()
    return WorkflowDefinition(
        schema=PORTABLE_WORKFLOW_SCHEMA,
        start_step_id=ANALYSIS_STEP_ID,
        steps=(
            WorkflowStep(
                instance_id=ANALYSIS_STEP_ID,
                display_name="Analysis",
                component_id=ANALYSIS_COMPONENT_ID,
                codex_settings=default_codex_execution_settings("analysis"),
                execution_budget=default_execution_budget("analysis"),
                capability_profile=component_catalog.resolve(
                    ANALYSIS_COMPONENT_ID
                ).default_capability_profile(),
                transitions={
                    StepOutcome.SUCCEEDED: DEVELOPMENT_STEP_ID,
                    StepOutcome.BLOCKED: None,
                    StepOutcome.FAILED: None,
                    StepOutcome.CANCELLED: None,
                },
            ),
            WorkflowStep(
                instance_id=DEVELOPMENT_STEP_ID,
                display_name="Development",
                component_id=DEVELOPMENT_COMPONENT_ID,
                codex_settings=default_codex_execution_settings("coder"),
                execution_budget=default_execution_budget("coder"),
                capability_profile=component_catalog.resolve(
                    DEVELOPMENT_COMPONENT_ID
                ).default_capability_profile(),
                transitions={
                    StepOutcome.SUCCEEDED: SECURITY_REVIEW_STEP_ID,
                    StepOutcome.BLOCKED: None,
                    StepOutcome.FAILED: None,
                    StepOutcome.CANCELLED: None,
                },
            ),
            WorkflowStep(
                instance_id=SECURITY_REVIEW_STEP_ID,
                display_name="Security Review",
                component_id=REVIEWER_COMPONENT_ID,
                codex_settings=default_codex_execution_settings("reviewer"),
                execution_budget=default_execution_budget("reviewer"),
                capability_profile=component_catalog.resolve(
                    REVIEWER_COMPONENT_ID
                ).default_capability_profile(),
                transitions={
                    StepOutcome.SUCCEEDED: FINAL_REVIEW_STEP_ID,
                    StepOutcome.CHANGES_REQUESTED: DEVELOPMENT_STEP_ID,
                    StepOutcome.BLOCKED: None,
                    StepOutcome.FAILED: None,
                    StepOutcome.CANCELLED: None,
                },
                input_bindings={
                    "implementation": PortBinding(
                        DEVELOPMENT_STEP_ID,
                        "implementation",
                    )
                },
            ),
            WorkflowStep(
                instance_id=FINAL_REVIEW_STEP_ID,
                display_name="Final Review",
                component_id=REVIEWER_COMPONENT_ID,
                codex_settings=default_codex_execution_settings("reviewer"),
                execution_budget=default_execution_budget("reviewer"),
                capability_profile=component_catalog.resolve(
                    REVIEWER_COMPONENT_ID
                ).default_capability_profile(),
                transitions={
                    StepOutcome.SUCCEEDED: QA_STEP_ID,
                    StepOutcome.CHANGES_REQUESTED: DEVELOPMENT_STEP_ID,
                    StepOutcome.BLOCKED: None,
                    StepOutcome.FAILED: None,
                    StepOutcome.CANCELLED: None,
                },
                input_bindings={
                    "implementation": PortBinding(
                        DEVELOPMENT_STEP_ID,
                        "implementation",
                    )
                },
            ),
            WorkflowStep(
                instance_id=QA_STEP_ID,
                display_name="QA",
                component_id=QA_COMPONENT_ID,
                codex_settings=default_codex_execution_settings("qa"),
                execution_budget=default_execution_budget("qa"),
                capability_profile=component_catalog.resolve(
                    QA_COMPONENT_ID
                ).default_capability_profile(),
                transitions={
                    StepOutcome.SUCCEEDED: None,
                    StepOutcome.CHANGES_REQUESTED: DEVELOPMENT_STEP_ID,
                    StepOutcome.BLOCKED: None,
                    StepOutcome.FAILED: None,
                    StepOutcome.CANCELLED: None,
                },
                input_bindings={
                    "implementation": PortBinding(
                        DEVELOPMENT_STEP_ID,
                        "implementation",
                    ),
                    "review_result": PortBinding(
                        FINAL_REVIEW_STEP_ID,
                        "review",
                    ),
                },
            ),
        ),
    )
