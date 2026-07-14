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
from devloop.components.structured_output import final_object, required_string
from devloop.domain.identifiers import (
    DataContractId,
    ExecutionThreadId,
    ExecutionTurnId,
    ReviewFindingId,
    StepComponentId,
)
from devloop.domain.review_qa import (
    FindingDisposition,
    FindingSeverity,
    ReviewFinding,
)
from devloop.execution.app_server import (
    AppServerApprovalPolicy,
    AppServerApprovalRequest,
    AppServerClient,
    AppServerReasoningEffort,
    AppServerSandboxMode,
    AppServerTurnResult,
    AppServerTurnStatus,
)
from devloop.infrastructure.codex import resolve_codex_executable

CODE_REVIEW_COMPONENT_ID = StepComponentId("code-review")
CODE_REVIEW_COMPONENT_VERSION = "1.0.0"
CODE_REVIEW_COMPONENT_SCHEMA = "devloop.step-component/v1"
CODE_REVIEW_DISTRIBUTION = "devloop-codexcli"
ISSUE_CONTRACT = DataContractId("devloop.issue/v1")
WORKSPACE_CONTRACT = DataContractId("devloop.workspace-ref/v1")
IMPLEMENTATION_CONTRACT = DataContractId("devloop.implementation-result/v1")
REVIEW_CONTRACT = DataContractId("devloop.review-result/v1")
REWORK_REQUEST_CONTRACT = DataContractId("devloop.rework-request/v1")
REVIEW_MODEL = "gpt-5.6-sol"
REVIEW_TURN_TIMEOUT_SECONDS = 1800.0

_REVIEW_INSTRUCTIONS = """Review only the supplied Issue implementation. Treat the structured
Review Input as the complete context. Do not load development transcripts, unrelated Issues, run
events, model reasoning, or broad memory. Inspect repository evidence read-only. Never edit files,
run formatters that write, commit, push, merge, or invoke development or QA roles. Return only the
requested structured Review Result. Every finding must cite concrete repository evidence."""


class ReviewComponentError(RuntimeError):
    pass


class ReviewTurnPaused(ReviewComponentError):
    pass


class ReviewTurnInterrupted(ReviewComponentError):
    pass


@dataclass(frozen=True)
class ReviewAgentOutput:
    thread_id: ExecutionThreadId
    turn_id: ExecutionTurnId
    completed_item_ids: tuple[str, ...]
    findings: tuple[ReviewFinding, ...]
    summary: str
    blocked_reason: str | None


class ReviewComponentRunner:
    @property
    def component_id(self) -> StepComponentId:
        return CODE_REVIEW_COMPONENT_ID

    def run_turn(
        self,
        *,
        workspace: Path,
        review_input: Mapping[str, object],
        thread_id: ExecutionThreadId | None = None,
        on_thread_bound: Callable[[ExecutionThreadId], None] | None = None,
        on_turn_started: Callable[[ExecutionTurnId], None] | None = None,
        on_item_started: Callable[[str], None] | None = None,
        on_item_completed: Callable[[str], None] | None = None,
        on_activity: Callable[[str], None] | None = None,
        pause_requested: Callable[[], bool] | None = None,
        interrupt_requested: Callable[[], bool] | None = None,
        on_approval: Callable[[AppServerApprovalRequest], str | None] | None = None,
    ) -> ReviewAgentOutput:
        with AppServerClient(
            str(resolve_codex_executable()),
            process_cwd=workspace,
            approval_handler=on_approval,
        ) as client:
            client.initialize()
            if thread_id is None:
                thread = client.start_thread(
                    workspace,
                    model=REVIEW_MODEL,
                    reasoning_effort=AppServerReasoningEffort.XHIGH,
                    developer_instructions=_REVIEW_INSTRUCTIONS,
                    sandbox=AppServerSandboxMode.READ_ONLY,
                    approval_policy=AppServerApprovalPolicy.NEVER,
                )
            else:
                thread = client.resume_thread(thread_id.value, workspace)
            bound_thread = ExecutionThreadId(thread.thread_id)
            if on_thread_bound is not None:
                on_thread_bound(bound_thread)
            turn = client.start_turn(
                thread.thread_id,
                "Review this structured input:\n\n"
                + json.dumps(review_input, ensure_ascii=False, separators=(",", ":")),
                output_schema=_review_output_schema(),
            )
            bound_turn = ExecutionTurnId(turn.turn_id)
            if on_turn_started is not None:
                on_turn_started(bound_turn)
            result = client.wait_for_turn(
                thread.thread_id,
                turn.turn_id,
                timeout_seconds=REVIEW_TURN_TIMEOUT_SECONDS,
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
            raise ReviewTurnPaused("Code-review turn paused after interruption.")
        if (
            result.status is AppServerTurnStatus.INTERRUPTED
            and interrupt_requested is not None
            and interrupt_requested()
        ):
            raise ReviewTurnInterrupted("Code-review turn explicitly interrupted.")
        return _output_from_result(result)

    def recover_completed_turn(
        self,
        *,
        workspace: Path,
        thread_id: ExecutionThreadId,
        turn_id: ExecutionTurnId,
        on_item_started: Callable[[str], None] | None = None,
        on_item_completed: Callable[[str], None] | None = None,
    ) -> ReviewAgentOutput:
        with AppServerClient(str(resolve_codex_executable()), process_cwd=workspace) as client:
            client.initialize()
            _, result = client.resume_thread_with_turn(thread_id.value, workspace, turn_id.value)
            if result is None:
                raise ReviewComponentError("Checkpointed review turn is missing.")
            if result.status is AppServerTurnStatus.IN_PROGRESS:
                result = client.wait_for_turn(
                    thread_id.value,
                    turn_id.value,
                    timeout_seconds=REVIEW_TURN_TIMEOUT_SECONDS,
                    on_item_started=on_item_started,
                    on_item_completed=on_item_completed,
                )
        return _output_from_result(result)


def review_component() -> tuple[ComponentManifest, ReviewComponentRunner]:
    return (
        ComponentManifest(
            CODE_REVIEW_COMPONENT_SCHEMA,
            CODE_REVIEW_COMPONENT_ID,
            CODE_REVIEW_COMPONENT_VERSION,
            CODE_REVIEW_DISTRIBUTION,
            package_source_hash(Path(__file__).resolve().parents[1]),
            StepExecutionPolicy.READ_ONLY,
            (
                ComponentPort("issue", ISSUE_CONTRACT, PortDirection.INPUT),
                ComponentPort("workspace", WORKSPACE_CONTRACT, PortDirection.INPUT),
                ComponentPort("implementation", IMPLEMENTATION_CONTRACT, PortDirection.INPUT),
                ComponentPort("review", REVIEW_CONTRACT, PortDirection.OUTPUT),
                ComponentPort(
                    "rework_request",
                    REWORK_REQUEST_CONTRACT,
                    PortDirection.OUTPUT,
                    required=False,
                ),
            ),
        ),
        ReviewComponentRunner(),
    )


def _output_from_result(result: AppServerTurnResult) -> ReviewAgentOutput:
    if result.status is not AppServerTurnStatus.COMPLETED:
        raise ReviewComponentError(f"Review turn ended with status {result.status.value}.")
    payload = final_object(result.message, "Review")
    values = payload.get("findings")
    if not isinstance(values, list):
        raise ReviewComponentError("Review findings are missing.")
    findings: list[ReviewFinding] = []
    for value in values:
        if not isinstance(value, dict):
            raise ReviewComponentError("Review Finding is invalid.")
        row = cast(dict[str, object], value)
        line = row.get("line")
        if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
            raise ReviewComponentError("Review Finding line is invalid.")
        findings.append(
            ReviewFinding(
                ReviewFindingId(required_string(row, "id", "Review")),
                FindingSeverity(required_string(row, "severity", "Review")),
                FindingDisposition(required_string(row, "disposition", "Review")),
                required_string(row, "title", "Review"),
                required_string(row, "rationale", "Review"),
                required_string(row, "evidence", "Review"),
                required_string(row, "file_path", "Review"),
                line,
                required_string(row, "expected_behavior", "Review"),
                required_string(row, "acceptance_condition", "Review"),
            )
        )
    return ReviewAgentOutput(
        ExecutionThreadId(result.thread_id),
        ExecutionTurnId(result.turn_id),
        result.completed_item_ids,
        tuple(findings),
        required_string(payload, "summary", "Review"),
        _optional_blocked_reason(payload),
    )


def _review_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["findings", "summary", "blocked_reason"],
        "properties": {
            "findings": {
                "type": "array",
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id",
                        "severity",
                        "disposition",
                        "title",
                        "rationale",
                        "evidence",
                        "file_path",
                        "line",
                        "expected_behavior",
                        "acceptance_condition",
                    ],
                    "properties": {
                        "id": {"type": "string", "pattern": "^RF-[0-9]{3,}$"},
                        "severity": {
                            "type": "string",
                            "enum": [item.value for item in FindingSeverity],
                        },
                        "disposition": {
                            "type": "string",
                            "enum": [item.value for item in FindingDisposition],
                        },
                        "title": {"type": "string", "minLength": 1, "maxLength": 500},
                        "rationale": {"type": "string", "minLength": 1, "maxLength": 4000},
                        "evidence": {"type": "string", "minLength": 1, "maxLength": 4000},
                        "file_path": {"type": "string", "minLength": 1, "maxLength": 1000},
                        "line": {"type": ["integer", "null"], "minimum": 1},
                        "expected_behavior": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 4000,
                        },
                        "acceptance_condition": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 4000,
                        },
                    },
                },
            },
            "summary": {"type": "string", "minLength": 1, "maxLength": 8000},
            "blocked_reason": {"type": ["string", "null"], "maxLength": 4000},
        },
    }


def _optional_blocked_reason(payload: Mapping[str, object]) -> str | None:
    value = payload.get("blocked_reason")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReviewComponentError("Review blocked reason is invalid.")
    return value
