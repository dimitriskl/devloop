from __future__ import annotations

import json
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
from devloop.components.review import (
    IMPLEMENTATION_CONTRACT,
    ISSUE_CONTRACT,
    REVIEW_CONTRACT,
    REWORK_REQUEST_CONTRACT,
    WORKSPACE_CONTRACT,
)
from devloop.components.structured_output import (
    final_object,
    optional_string,
    required_string,
    string_tuple,
)
from devloop.domain.approval import ApprovalPolicy
from devloop.domain.execution import ExecutionProfile
from devloop.domain.identifiers import (
    AcceptanceCriterionId,
    DataContractId,
    ExecutionThreadId,
    ExecutionTurnId,
    QaCheckId,
    StepComponentId,
)
from devloop.domain.review_qa import (
    CheckRequirement,
    QaCheck,
    QaCheckKind,
    QaCheckStatus,
)
from devloop.execution.app_server import (
    AppServerApprovalPolicy,
    AppServerApprovalRequest,
    AppServerClient,
    AppServerPermissionProfile,
    AppServerReasoningEffort,
    AppServerTurnResult,
    AppServerTurnStatus,
)
from devloop.execution.environment import VERIFICATION_ENVIRONMENT
from devloop.infrastructure.codex import resolve_codex_executable

QA_COMPONENT_ID = StepComponentId("qa")
QA_COMPONENT_VERSION = "1.0.0"
QA_COMPONENT_SCHEMA = "devloop.step-component/v1"
QA_DISTRIBUTION = "devloop-codexcli"
QA_RESULT_CONTRACT = DataContractId("devloop.qa-result/v1")
QA_MODEL = "gpt-5.6-sol"
QA_TURN_TIMEOUT_SECONDS = 1800.0
QA_CHECKPOINT_SECONDS = 240.0
QA_EXECUTION_PROFILES = (
    ExecutionProfile.full(
        QA_COMPONENT_ID.value,
        QA_MODEL,
        AppServerReasoningEffort.XHIGH.value,
        QA_TURN_TIMEOUT_SECONDS,
        QA_CHECKPOINT_SECONDS,
    ),
    ExecutionProfile.lightweight(
        QA_COMPONENT_ID.value,
        QA_MODEL,
        AppServerReasoningEffort.LOW.value,
        600.0,
        120.0,
    ),
)

_QA_INSTRUCTIONS = """Verify only the supplied Issue implementation. Treat the structured QA
Input as the complete context. Do not load development or review transcripts, unrelated Issues,
run events, model reasoning, or broad memory. You may run builds, tests, linters, type checks, and
security checks. You may write ignored build output, caches, or Run Artifacts, but never edit
source-controlled files, run source-writing formatters, commit, push, merge, or invoke development
or code-review roles. Return only the requested structured QA Result. Map every acceptance criterion
to at least one REQUIRED check and report commands and evidence exactly."""


class QaComponentError(RuntimeError):
    pass


class QaTurnPaused(QaComponentError):
    pass


class QaTurnInterrupted(QaComponentError):
    pass


@dataclass(frozen=True)
class QaAgentOutput:
    thread_id: ExecutionThreadId
    turn_id: ExecutionTurnId
    completed_item_ids: tuple[str, ...]
    checks: tuple[QaCheck, ...]
    residual_risks: tuple[str, ...]
    summary: str


class QaComponentRunner:
    @property
    def component_id(self) -> StepComponentId:
        return QA_COMPONENT_ID

    def run_turn(
        self,
        *,
        workspace: Path,
        qa_input: Mapping[str, object],
        criterion_ids: tuple[AcceptanceCriterionId, ...],
        thread_id: ExecutionThreadId | None = None,
        on_thread_bound: Callable[[ExecutionThreadId], None] | None = None,
        on_turn_started: Callable[[ExecutionTurnId], None] | None = None,
        on_item_started: Callable[[str], None] | None = None,
        on_item_completed: Callable[[str], None] | None = None,
        on_activity: Callable[[str], None] | None = None,
        pause_requested: Callable[[], bool] | None = None,
        interrupt_requested: Callable[[], bool] | None = None,
        on_approval: Callable[[AppServerApprovalRequest], str | None] | None = None,
        execution_profile: ExecutionProfile | None = None,
    ) -> QaAgentOutput:
        profile = _execution_profile(execution_profile)
        with AppServerClient(
            str(resolve_codex_executable()),
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
                    developer_instructions=_QA_INSTRUCTIONS,
                    approval_policy=AppServerApprovalPolicy.ON_REQUEST,
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
            turn = client.start_turn(
                thread.thread_id,
                "Verify this structured input:\n\n"
                + json.dumps(qa_input, ensure_ascii=False, separators=(",", ":")),
                output_schema=_qa_output_schema(criterion_ids),
            )
            bound_turn = ExecutionTurnId(turn.turn_id)
            if on_turn_started is not None:
                on_turn_started(bound_turn)
            result = client.wait_for_turn(
                thread.thread_id,
                turn.turn_id,
                timeout_seconds=profile.budget.timeout_seconds,
                on_agent_delta=on_activity,
                on_item_started=on_item_started,
                on_item_completed=on_item_completed,
                interrupt_requested=lambda: bool(
                    (pause_requested is not None and pause_requested())
                    or (interrupt_requested is not None and interrupt_requested())
                ),
            )
        if (
            result.status is AppServerTurnStatus.INTERRUPTED
            and pause_requested is not None
            and pause_requested()
        ):
            raise QaTurnPaused("QA turn paused after interruption.")
        if (
            result.status is AppServerTurnStatus.INTERRUPTED
            and interrupt_requested is not None
            and interrupt_requested()
        ):
            raise QaTurnInterrupted("QA turn explicitly interrupted.")
        return _output_from_result(result)

    def recover_completed_turn(
        self,
        *,
        workspace: Path,
        thread_id: ExecutionThreadId,
        turn_id: ExecutionTurnId,
        on_item_started: Callable[[str], None] | None = None,
        on_item_completed: Callable[[str], None] | None = None,
    ) -> QaAgentOutput:
        with AppServerClient(
            str(resolve_codex_executable()),
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
                raise QaComponentError("Checkpointed QA turn is missing.")
            if result.status is AppServerTurnStatus.IN_PROGRESS:
                result = client.wait_for_turn(
                    thread_id.value,
                    turn_id.value,
                    timeout_seconds=QA_TURN_TIMEOUT_SECONDS,
                    on_item_started=on_item_started,
                    on_item_completed=on_item_completed,
                )
        return _output_from_result(result)


def qa_component() -> tuple[ComponentManifest, QaComponentRunner]:
    return (
        ComponentManifest(
            QA_COMPONENT_SCHEMA,
            QA_COMPONENT_ID,
            QA_COMPONENT_VERSION,
            QA_DISTRIBUTION,
            package_source_hash(Path(__file__).resolve().parents[1]),
            StepExecutionPolicy.VERIFICATION_ONLY,
            (
                ComponentPort("issue", ISSUE_CONTRACT, PortDirection.INPUT),
                ComponentPort("workspace", WORKSPACE_CONTRACT, PortDirection.INPUT),
                ComponentPort("implementation", IMPLEMENTATION_CONTRACT, PortDirection.INPUT),
                ComponentPort("review", REVIEW_CONTRACT, PortDirection.INPUT),
                ComponentPort("qa_result", QA_RESULT_CONTRACT, PortDirection.OUTPUT),
                ComponentPort(
                    "rework_request",
                    REWORK_REQUEST_CONTRACT,
                    PortDirection.OUTPUT,
                    required=False,
                ),
            ),
            approval_policy=ApprovalPolicy.read_only(
                QA_COMPONENT_ID.value,
                focused_tests=True,
            ),
            execution_profiles=QA_EXECUTION_PROFILES,
            default_execution_profile="FULL",
        ),
        QaComponentRunner(),
    )


def _execution_profile(profile: ExecutionProfile | None) -> ExecutionProfile:
    selected = QA_EXECUTION_PROFILES[0] if profile is None else profile
    if selected.component_id != QA_COMPONENT_ID.value:
        raise QaComponentError("Execution profile belongs to another component.")
    if selected not in QA_EXECUTION_PROFILES:
        raise QaComponentError("Execution profile is not supported by QA.")
    return selected


def _output_from_result(result: AppServerTurnResult) -> QaAgentOutput:
    if result.status is not AppServerTurnStatus.COMPLETED:
        raise QaComponentError(f"QA turn ended with status {result.status.value}.")
    payload = final_object(result.message, "QA")
    values = payload.get("checks")
    if not isinstance(values, list):
        raise QaComponentError("QA checks are missing.")
    checks: list[QaCheck] = []
    for value in values:
        if not isinstance(value, dict):
            raise QaComponentError("QA Check is invalid.")
        row = cast(dict[str, object], value)
        exit_code = row.get("exit_code")
        duration_ms = row.get("duration_ms")
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise QaComponentError("QA Check exit code is invalid.")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
            raise QaComponentError("QA Check duration is invalid.")
        checks.append(
            QaCheck(
                QaCheckId(required_string(row, "id", "QA")),
                AcceptanceCriterionId(required_string(row, "criterion_id", "QA")),
                QaCheckKind(required_string(row, "kind", "QA")),
                CheckRequirement(required_string(row, "requirement", "QA")),
                QaCheckStatus(required_string(row, "status", "QA")),
                optional_string(row, "command", "QA"),
                exit_code,
                duration_ms,
                required_string(row, "evidence", "QA"),
                cast(str, row.get("reason", "")),
                required_string(row, "expected_behavior", "QA"),
                required_string(row, "acceptance_condition", "QA"),
            )
        )
    return QaAgentOutput(
        ExecutionThreadId(result.thread_id),
        ExecutionTurnId(result.turn_id),
        result.completed_item_ids,
        tuple(checks),
        string_tuple(payload.get("residual_risks"), "residual_risks", "QA"),
        required_string(payload, "summary", "QA"),
    )


def _qa_output_schema(
    criterion_ids: tuple[AcceptanceCriterionId, ...],
) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["checks", "residual_risks", "summary"],
        "properties": {
            "checks": {
                "type": "array",
                "minItems": len(criterion_ids),
                "maxItems": 200,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id",
                        "criterion_id",
                        "kind",
                        "requirement",
                        "status",
                        "command",
                        "exit_code",
                        "duration_ms",
                        "evidence",
                        "reason",
                        "expected_behavior",
                        "acceptance_condition",
                    ],
                    "properties": {
                        "id": {"type": "string", "pattern": "^QC-[0-9]{3,}$"},
                        "criterion_id": {
                            "type": "string",
                            "enum": [item.value for item in criterion_ids],
                        },
                        "kind": {"type": "string", "enum": [item.value for item in QaCheckKind]},
                        "requirement": {
                            "type": "string",
                            "enum": [item.value for item in CheckRequirement],
                        },
                        "status": {
                            "type": "string",
                            "enum": [item.value for item in QaCheckStatus],
                        },
                        "command": {"type": ["string", "null"], "maxLength": 2000},
                        "exit_code": {"type": ["integer", "null"]},
                        "duration_ms": {"type": "integer", "minimum": 0},
                        "evidence": {"type": "string", "minLength": 1, "maxLength": 4000},
                        "reason": {"type": "string", "maxLength": 4000},
                        "expected_behavior": {"type": "string", "minLength": 1, "maxLength": 4000},
                        "acceptance_condition": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 4000,
                        },
                    },
                },
            },
            "residual_risks": {
                "type": "array",
                "maxItems": 100,
                "items": {"type": "string", "maxLength": 4000},
            },
            "summary": {"type": "string", "minLength": 1, "maxLength": 8000},
        },
    }
