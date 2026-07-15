from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from devloop.components.contracts import (
    ComponentManifest,
    ComponentPort,
    PortDirection,
    StepExecutionPolicy,
    package_source_hash,
)
from devloop.domain.approval import ApprovalPolicy
from devloop.domain.development import (
    CriterionImplementation,
    CriterionImplementationStatus,
    ReworkResolution,
    ReworkResolutionStatus,
)
from devloop.domain.doctor import redact_diagnostic
from devloop.domain.execution import ExecutionProfile
from devloop.domain.identifiers import (
    AcceptanceCriterionId,
    DataContractId,
    ExecutionThreadId,
    ExecutionTurnId,
    StepComponentId,
)
from devloop.domain.outcomes import StepOutcome
from devloop.execution.app_server import (
    AppServerApprovalPolicy,
    AppServerApprovalRequest,
    AppServerApprovalsReviewer,
    AppServerCheckpointDeadline,
    AppServerClient,
    AppServerPermissionProfile,
    AppServerReasoningEffort,
    AppServerSandboxMode,
    AppServerTransientError,
    AppServerTurnResult,
    AppServerTurnStatus,
    is_transient_turn_failure,
)
from devloop.execution.environment import VERIFICATION_ENVIRONMENT
from devloop.infrastructure.codex import resolve_codex_executable
from devloop.infrastructure.windows_acl import current_windows_user_sid

DEVELOPMENT_COMPONENT_ID = StepComponentId("development")
DEVELOPMENT_COMPONENT_VERSION = "1.0.0"
DEVELOPMENT_COMPONENT_SCHEMA = "devloop.step-component/v1"
DEVELOPMENT_DISTRIBUTION = "devloop-codexcli"
CONTEXT_MANIFEST_CONTRACT = DataContractId("devloop.context-manifest/v1")
IMPLEMENTATION_RESULT_CONTRACT = DataContractId("devloop.implementation-result/v1")
DEVELOPMENT_MODEL = "gpt-5.6-sol"
DEVELOPMENT_TURN_TIMEOUT_SECONDS = 1800.0
DEVELOPMENT_CHECKPOINT_SECONDS = 300.0
DEVELOPMENT_EXECUTION_PROFILES = (
    ExecutionProfile.full(
        DEVELOPMENT_COMPONENT_ID.value,
        DEVELOPMENT_MODEL,
        AppServerReasoningEffort.XHIGH.value,
        DEVELOPMENT_TURN_TIMEOUT_SECONDS,
        DEVELOPMENT_CHECKPOINT_SECONDS,
    ),
    ExecutionProfile.lightweight(
        DEVELOPMENT_COMPONENT_ID.value,
        DEVELOPMENT_MODEL,
        AppServerReasoningEffort.LOW.value,
        600.0,
        120.0,
    ),
)
_DEVELOPMENT_INSTRUCTIONS = """Implement only the supplied Issue in the current workspace.
Use the Context Manifest as the complete task context. Do not load unrelated Issues, transcripts,
run events, model reasoning, or broad memory. Respect repository instructions and Codex sandbox and
approval policy. Make real source changes, run focused verification, and return only the requested
structured result. Do not commit, push, merge, create pull requests, delete branches, or remove
worktrees. Do not spawn subagents or run code-review or QA roles in this development turn. Keep
verification output out of source control by disabling tool caches or using already ignored paths.
Never run recursive cleanup or deletion commands; report an unexpected generated path instead."""
_DEVELOPMENT_INSTRUCTIONS += """ Issue one shell command per tool call. Never join commands with
semicolons, pipes, logical operators, redirections, or substitutions. Use individual read-only Git
or workspace inspection commands and individual focused test commands."""


class DevelopmentComponentError(RuntimeError):
    pass


class DevelopmentTurnPaused(DevelopmentComponentError):
    pass


class DevelopmentTurnInterrupted(DevelopmentComponentError):
    pass


class DevelopmentTurnStalled(DevelopmentComponentError):
    def __init__(
        self,
        message: str,
        thread_id: ExecutionThreadId,
        turn_id: ExecutionTurnId,
        completed_item_ids: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.completed_item_ids = completed_item_ids


@dataclass(frozen=True)
class DevelopmentAgentOutput:
    thread_id: ExecutionThreadId
    turn_id: ExecutionTurnId
    completed_item_ids: tuple[str, ...]
    criteria: tuple[CriterionImplementation, ...]
    commands: tuple[str, ...]
    rework_resolutions: tuple[ReworkResolution, ...]
    assumptions: tuple[str, ...]
    risks: tuple[str, ...]
    summary: str
    outcome: StepOutcome = StepOutcome.SUCCEEDED
    blocked_reason: str | None = None


class DevelopmentComponentRunner:
    @property
    def component_id(self) -> StepComponentId:
        return DEVELOPMENT_COMPONENT_ID

    def run_turn(
        self,
        *,
        workspace: Path,
        context_manifest: Mapping[str, object],
        criterion_ids: tuple[AcceptanceCriterionId, ...],
        thread_id: ExecutionThreadId | None = None,
        on_thread_bound: Callable[[ExecutionThreadId], None] | None = None,
        on_turn_started: Callable[[ExecutionTurnId], None] | None = None,
        on_item_started: Callable[[str], None] | None = None,
        on_item_completed: Callable[[str], None] | None = None,
        on_file_change: Callable[[str], None] | None = None,
        on_activity: Callable[[str], None] | None = None,
        pause_requested: Callable[[], bool] | None = None,
        interrupt_requested: Callable[[], bool] | None = None,
        on_approval: Callable[[AppServerApprovalRequest], str | None] | None = None,
        execution_profile: ExecutionProfile | None = None,
    ) -> DevelopmentAgentOutput:
        profile = _execution_profile(execution_profile)
        executable = resolve_codex_executable()
        instructions = _DEVELOPMENT_INSTRUCTIONS
        if sys.platform.startswith("win") and _requires_windows_acl_handoff(
            context_manifest
        ):
            sid = current_windows_user_sid()
            instructions += (
                "\nWindows handoff requirement: before the final response, grant the parent user "
                "access to every path you created. For every created directory, run "
                "`icacls <directory> /grant:r *"
                f"{sid}:(F)`. For every created or replaced file, run "
                "`icacls <file> /grant:r *"
                f"{sid}:(F)`. Do not use `/T` or modify ACLs outside this workspace."
            )
        with AppServerClient(
            str(executable),
            experimental_api=True,
            process_cwd=workspace,
            approval_handler=on_approval,
            environment_overrides=VERIFICATION_ENVIRONMENT,
        ) as client:
            client.initialize()
            if thread_id is None:
                thread = client.start_thread(
                    workspace,
                    model=profile.model,
                    reasoning_effort=AppServerReasoningEffort(profile.reasoning_effort),
                    developer_instructions=instructions,
                    sandbox=AppServerSandboxMode.WORKSPACE_WRITE,
                    approval_policy=AppServerApprovalPolicy.ON_REQUEST,
                    approvals_reviewer=AppServerApprovalsReviewer.USER,
                    permission_profile=AppServerPermissionProfile.WORKSPACE,
                    runtime_workspace_roots=(workspace,),
                )
            else:
                thread = client.resume_thread(
                    thread_id.value,
                    workspace,
                    runtime_workspace_roots=(workspace,),
                )
            bound_thread = ExecutionThreadId(thread.thread_id)
            if on_thread_bound is not None:
                on_thread_bound(bound_thread)
            prompt = (
                "Implement this Context Manifest. It is the complete allowed context:\n\n"
                + json.dumps(context_manifest, ensure_ascii=False, separators=(",", ":"))
            )
            turn = client.start_turn(
                thread.thread_id,
                prompt,
                output_schema=_implementation_output_schema(criterion_ids),
            )
            bound_turn = ExecutionTurnId(turn.turn_id)
            if on_turn_started is not None:
                on_turn_started(bound_turn)
            try:
                result = client.wait_for_turn(
                    thread.thread_id,
                    turn.turn_id,
                    timeout_seconds=profile.budget.timeout_seconds,
                    checkpoint_seconds=profile.budget.checkpoint_seconds,
                    on_agent_delta=on_activity,
                    on_item_started=on_item_started,
                    on_item_completed=on_item_completed,
                    on_file_change=on_file_change,
                    interrupt_requested=lambda: bool(
                        (pause_requested is not None and pause_requested())
                        or (interrupt_requested is not None and interrupt_requested())
                    ),
                )
            except AppServerCheckpointDeadline as error:
                raise DevelopmentTurnStalled(
                    str(error),
                    ExecutionThreadId(error.thread_id),
                    ExecutionTurnId(error.turn_id),
                    error.completed_item_ids,
                ) from error
        if (
            result.status is AppServerTurnStatus.INTERRUPTED
            and pause_requested is not None
            and pause_requested()
        ):
            raise DevelopmentTurnPaused("Development turn paused after interruption.")
        if (
            result.status is AppServerTurnStatus.INTERRUPTED
            and interrupt_requested is not None
            and interrupt_requested()
        ):
            raise DevelopmentTurnInterrupted("Development turn explicitly interrupted.")
        return _output_from_result(result, criterion_ids)

    def recover_completed_turn(
        self,
        *,
        workspace: Path,
        thread_id: ExecutionThreadId,
        turn_id: ExecutionTurnId,
        criterion_ids: tuple[AcceptanceCriterionId, ...],
        on_item_started: Callable[[str], None] | None = None,
        on_item_completed: Callable[[str], None] | None = None,
    ) -> DevelopmentAgentOutput:
        executable = resolve_codex_executable()
        with AppServerClient(
            str(executable),
            experimental_api=True,
            process_cwd=workspace,
        ) as client:
            client.initialize()
            _, result = client.resume_thread_with_turn(
                thread_id.value,
                workspace,
                turn_id.value,
                runtime_workspace_roots=(workspace,),
            )
            if result is None:
                raise DevelopmentComponentError("Checkpointed development turn is missing.")
            if result.status is AppServerTurnStatus.IN_PROGRESS:
                result = client.wait_for_turn(
                    thread_id.value,
                    turn_id.value,
                    timeout_seconds=DEVELOPMENT_TURN_TIMEOUT_SECONDS,
                    on_item_started=on_item_started,
                    on_item_completed=on_item_completed,
                )
        return _output_from_result(result, criterion_ids)

    def validate_resume(self, workspace: Path, thread_id: ExecutionThreadId) -> None:
        executable = resolve_codex_executable()
        with AppServerClient(
            str(executable),
            experimental_api=True,
            process_cwd=workspace,
        ) as client:
            client.initialize()
            client.resume_thread(
                thread_id.value,
                workspace,
                runtime_workspace_roots=(workspace,),
            )


def _requires_windows_acl_handoff(context_manifest: Mapping[str, object]) -> bool:
    workspace = context_manifest.get("workspace")
    if not isinstance(workspace, dict):
        raise DevelopmentComponentError("Context Manifest workspace is invalid.")
    required = workspace.get("requires_windows_acl_handoff")
    if not isinstance(required, bool):
        raise DevelopmentComponentError(
            "Context Manifest workspace permission profile is invalid."
        )
    return required


def _output_from_result(
    result: AppServerTurnResult,
    criterion_ids: tuple[AcceptanceCriterionId, ...],
) -> DevelopmentAgentOutput:
    if result.status is not AppServerTurnStatus.COMPLETED:
        code = f" Code: {result.failure_code}." if result.failure_code else ""
        detail = (
            ""
            if result.failure_message is None
            else f" Detail: {redact_diagnostic(result.failure_message, limit=2000)}"
        )
        message = f"Development turn ended with status {result.status.value}.{code}{detail}"
        if is_transient_turn_failure(result):
            raise AppServerTransientError(message)
        raise DevelopmentComponentError(message)
    payload = parse_development_output(result.message)
    criteria = _parse_criteria(payload.get("criteria"))
    expected = set(criterion_ids)
    if {item.criterion_id for item in criteria} != expected:
        raise DevelopmentComponentError("Implementation Result does not cover every criterion.")
    outcome = StepOutcome(_required_string(payload, "outcome"))
    if outcome not in {StepOutcome.SUCCEEDED, StepOutcome.BLOCKED}:
        raise DevelopmentComponentError("Development outcome is unsupported.")
    blocked_reason = _optional_blocked_reason(payload)
    if (outcome is StepOutcome.BLOCKED) != (blocked_reason is not None):
        raise DevelopmentComponentError(
            "Development BLOCKED outcome and blocked reason must be provided together."
        )
    return DevelopmentAgentOutput(
        ExecutionThreadId(result.thread_id),
        ExecutionTurnId(result.turn_id),
        result.completed_item_ids,
        criteria,
        _string_tuple(payload.get("commands"), "commands"),
        _parse_rework(payload.get("rework_resolutions")),
        _string_tuple(payload.get("assumptions"), "assumptions"),
        _string_tuple(payload.get("risks"), "risks"),
        _required_string(payload, "summary"),
        outcome,
        blocked_reason,
    )


def development_component() -> tuple[ComponentManifest, DevelopmentComponentRunner]:
    runner = DevelopmentComponentRunner()
    return (
        ComponentManifest(
            DEVELOPMENT_COMPONENT_SCHEMA,
            DEVELOPMENT_COMPONENT_ID,
            DEVELOPMENT_COMPONENT_VERSION,
            DEVELOPMENT_DISTRIBUTION,
            package_source_hash(Path(__file__).resolve().parents[1]),
            StepExecutionPolicy.WORKSPACE_WRITE,
            (
                ComponentPort("context_manifest", CONTEXT_MANIFEST_CONTRACT, PortDirection.INPUT),
                ComponentPort(
                    "implementation_result",
                    IMPLEMENTATION_RESULT_CONTRACT,
                    PortDirection.OUTPUT,
                ),
            ),
            approval_policy=ApprovalPolicy.standard(DEVELOPMENT_COMPONENT_ID.value),
            execution_profiles=DEVELOPMENT_EXECUTION_PROFILES,
            default_execution_profile="FULL",
        ),
        runner,
    )


def _execution_profile(profile: ExecutionProfile | None) -> ExecutionProfile:
    selected = DEVELOPMENT_EXECUTION_PROFILES[0] if profile is None else profile
    if selected.component_id != DEVELOPMENT_COMPONENT_ID.value:
        raise DevelopmentComponentError("Execution profile belongs to another component.")
    if selected not in DEVELOPMENT_EXECUTION_PROFILES:
        raise DevelopmentComponentError("Execution profile is not supported by development.")
    return selected


def _implementation_output_schema(
    criterion_ids: tuple[AcceptanceCriterionId, ...],
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "criteria",
            "commands",
            "rework_resolutions",
            "assumptions",
            "risks",
            "summary",
            "outcome",
            "blocked_reason",
        ],
        "properties": {
            "criteria": {
                "type": "array",
                "minItems": len(criterion_ids),
                "maxItems": len(criterion_ids),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "status", "evidence"],
                    "properties": {
                        "id": {"type": "string", "enum": [item.value for item in criterion_ids]},
                        "status": {
                            "type": "string",
                            "enum": [item.value for item in CriterionImplementationStatus],
                        },
                        "evidence": {"type": "string", "maxLength": 4000},
                    },
                },
            },
            "commands": _string_array_schema(100, 2000),
            "rework_resolutions": {
                "type": "array",
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "status", "evidence"],
                    "properties": {
                        "id": {"type": "string", "maxLength": 200},
                        "status": {
                            "type": "string",
                            "enum": [item.value for item in ReworkResolutionStatus],
                        },
                        "evidence": {"type": "string", "maxLength": 4000},
                    },
                },
            },
            "assumptions": _string_array_schema(100, 4000),
            "risks": _string_array_schema(100, 4000),
            "summary": {"type": "string", "minLength": 1, "maxLength": 8000},
            "outcome": {
                "type": "string",
                "enum": [StepOutcome.SUCCEEDED.value, StepOutcome.BLOCKED.value],
            },
            "blocked_reason": {"type": ["string", "null"], "maxLength": 4000},
        },
    }


def _string_array_schema(max_items: int, max_length: int) -> dict[str, object]:
    return {
        "type": "array",
        "maxItems": max_items,
        "items": {"type": "string", "maxLength": max_length},
    }


def parse_development_output(message: str) -> Mapping[str, object]:
    decoder = json.JSONDecoder()
    index = 0
    objects: list[dict[str, object]] = []
    try:
        while index < len(message):
            while index < len(message) and message[index].isspace():
                index += 1
            if index >= len(message):
                break
            value, index = decoder.raw_decode(message, index)
            if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
                raise DevelopmentComponentError(
                    "Development structured output must contain only objects."
                )
            objects.append(cast(dict[str, object], value))
    except json.JSONDecodeError as error:
        raise DevelopmentComponentError(
            "Development returned invalid structured output."
        ) from error
    if not objects:
        raise DevelopmentComponentError("Development structured output is empty.")
    return objects[-1]


def _parse_criteria(value: object) -> tuple[CriterionImplementation, ...]:
    if not isinstance(value, list):
        raise DevelopmentComponentError("Implementation criteria are missing.")
    result: list[CriterionImplementation] = []
    for row_value in value:
        if not isinstance(row_value, dict):
            raise DevelopmentComponentError("Implementation criterion is invalid.")
        row = cast(dict[str, object], row_value)
        result.append(
            CriterionImplementation(
                AcceptanceCriterionId(_required_string(row, "id")),
                CriterionImplementationStatus(_required_string(row, "status")),
                _required_string(row, "evidence"),
            )
        )
    return tuple(result)


def _parse_rework(value: object) -> tuple[ReworkResolution, ...]:
    if not isinstance(value, list):
        raise DevelopmentComponentError("Rework resolutions are invalid.")
    rows: list[ReworkResolution] = []
    for row_value in value:
        if not isinstance(row_value, dict):
            raise DevelopmentComponentError("Rework resolution is invalid.")
        row = cast(dict[str, object], row_value)
        rows.append(
            ReworkResolution(
                _required_string(row, "id"),
                ReworkResolutionStatus(_required_string(row, "status")),
                _required_string(row, "evidence"),
            )
        )
    return tuple(rows)


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DevelopmentComponentError(f"Implementation Result {name} are invalid.")
    return tuple(cast(list[str], value))


def _required_string(data: Mapping[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DevelopmentComponentError(f"Implementation Result is missing {name}.")
    return value


def _optional_blocked_reason(data: Mapping[str, object]) -> str | None:
    value = data.get("blocked_reason")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DevelopmentComponentError("Development blocked reason is invalid.")
    return value
