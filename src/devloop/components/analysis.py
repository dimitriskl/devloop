from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from devloop.analysis.rendering import (
    ANALYSIS_CONTENT_SCHEMA,
    parse_analysis_content,
    render_analysis_content,
)
from devloop.components.contracts import (
    ComponentManifest,
    ComponentPort,
    ComponentRegistry,
    PortDirection,
    StepExecutionPolicy,
    package_source_hash,
)
from devloop.domain.approval import ApprovalPolicy
from devloop.domain.execution import ExecutionProfile
from devloop.domain.identifiers import (
    DataContractId,
    ExecutionThreadId,
    ExecutionTurnId,
    StepComponentId,
    WorkflowRunId,
)
from devloop.domain.planning import (
    ANALYSIS_ACCEPTANCE_TEXT_MAX_LENGTH,
    ANALYSIS_CLARIFICATION_MAX_LENGTH,
    ANALYSIS_FEATURE_TITLE_MAX_LENGTH,
    ANALYSIS_ISSUE_TITLE_MAX_LENGTH,
    AnalysisDraft,
)
from devloop.domain.run import AnalysisResponseKind
from devloop.execution.app_server import (
    AppServerClient,
    AppServerReasoningEffort,
    AppServerTurnResult,
    AppServerTurnStatus,
)
from devloop.infrastructure.codex import resolve_codex_executable

ANALYSIS_COMPONENT_ID = StepComponentId("analysis")
ANALYSIS_COMPONENT_SCHEMA = "devloop.step-component/v1"
ANALYSIS_COMPONENT_VERSION = "1.0.0"
ANALYSIS_DISTRIBUTION = "devloop-codexcli"
FEATURE_REQUEST_CONTRACT = DataContractId("devloop.feature-request/v1")
PRD_PACKAGE_CONTRACT = DataContractId("devloop.prd-package/v1")
ANALYSIS_MODEL = "gpt-5.6-sol"
ANALYSIS_TURN_TIMEOUT_SECONDS = 900.0
ANALYSIS_CHECKPOINT_SECONDS = 180.0
ANALYSIS_EXECUTION_PROFILES = (
    ExecutionProfile.full(
        ANALYSIS_COMPONENT_ID.value,
        ANALYSIS_MODEL,
        AppServerReasoningEffort.XHIGH.value,
        ANALYSIS_TURN_TIMEOUT_SECONDS,
        ANALYSIS_CHECKPOINT_SECONDS,
    ),
)

_ANALYSIS_INSTRUCTIONS = """You are the analysis component of Dev Loop. Work only on planning.
Do not edit repository files or run implementation commands. Ask one concise clarification when
material product intent is missing; otherwise return a complete PRD and issue package. Preserve the
user's content language. Return only human-authored planning content and relationship numbers;
Dev Loop assigns every machine identifier and renders Markdown after the turn.
Return only data matching the supplied output schema. Never include secrets, transcripts, hidden
reasoning, environment dumps, or raw tool output."""

ANALYSIS_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "question", "draft"],
    "properties": {
        "kind": {"type": "string", "enum": ["CLARIFICATION", "DRAFT"]},
        "question": {
            "type": ["string", "null"],
            "maxLength": ANALYSIS_CLARIFICATION_MAX_LENGTH,
        },
        "draft": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": [
                "schema",
                "feature_title",
                "labels",
                "problem",
                "solution",
                "requirements",
                "issues",
                "revision",
            ],
            "properties": {
                "schema": {"type": "string", "const": ANALYSIS_CONTENT_SCHEMA},
                "feature_title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": ANALYSIS_FEATURE_TITLE_MAX_LENGTH,
                },
                "labels": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "problem",
                        "solution",
                        "requirements",
                        "description",
                        "acceptance",
                    ],
                    "properties": {
                        "problem": {"type": "string", "minLength": 1, "maxLength": 200},
                        "solution": {"type": "string", "minLength": 1, "maxLength": 200},
                        "requirements": {"type": "string", "minLength": 1, "maxLength": 200},
                        "description": {"type": "string", "minLength": 1, "maxLength": 200},
                        "acceptance": {"type": "string", "minLength": 1, "maxLength": 200},
                    },
                },
                "problem": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 250_000,
                },
                "solution": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 250_000,
                },
                "requirements": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 500,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["text"],
                        "properties": {
                            "text": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": ANALYSIS_ACCEPTANCE_TEXT_MAX_LENGTH,
                            },
                        },
                    },
                },
                "issues": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 200,
                    "items": {"$ref": "#/$defs/issue"},
                },
                "revision": {"type": "integer", "minimum": 1},
            },
        },
    },
    "$defs": {
        "issue": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "title",
                "description",
                "requirement_numbers",
                "dependency_numbers",
                "acceptance_criteria",
            ],
            "properties": {
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": ANALYSIS_ISSUE_TITLE_MAX_LENGTH,
                },
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 250_000,
                },
                "requirement_numbers": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 500,
                    "items": {"type": "integer", "minimum": 1},
                },
                "dependency_numbers": {
                    "type": "array",
                    "maxItems": 200,
                    "items": {"type": "integer", "minimum": 1},
                },
                "acceptance_criteria": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 200,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["text"],
                        "properties": {
                            "text": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": ANALYSIS_ACCEPTANCE_TEXT_MAX_LENGTH,
                            },
                        },
                    },
                },
            },
        }
    },
}


class AnalysisComponentError(RuntimeError):
    pass


@dataclass(frozen=True)
class AnalysisTurnOutput:
    kind: AnalysisResponseKind
    thread_id: ExecutionThreadId
    turn_id: ExecutionTurnId
    clarification: str | None
    draft: AnalysisDraft | None
    completed_item_ids: tuple[str, ...]


class AnalysisComponentRunner:
    @property
    def component_id(self) -> StepComponentId:
        return ANALYSIS_COMPONENT_ID

    def run_turn(
        self,
        *,
        repository: Path,
        run_id: WorkflowRunId,
        message: str,
        thread_id: ExecutionThreadId | None = None,
        on_thread_bound: Callable[[ExecutionThreadId], None] | None = None,
        on_turn_started: Callable[[ExecutionTurnId], None] | None = None,
        on_item_started: Callable[[str], None] | None = None,
        on_item_completed: Callable[[str], None] | None = None,
        on_activity: Callable[[str], None] | None = None,
        execution_profile: ExecutionProfile | None = None,
    ) -> AnalysisTurnOutput:
        profile = _execution_profile(execution_profile)
        executable = resolve_codex_executable()
        with AppServerClient(
            str(executable),
            experimental_api=True,
            process_cwd=repository,
        ) as client:
            client.initialize()
            if thread_id is None:
                thread = client.start_thread(
                    repository,
                    model=profile.model,
                    reasoning_effort=AppServerReasoningEffort(profile.reasoning_effort),
                    developer_instructions=_ANALYSIS_INSTRUCTIONS,
                    runtime_workspace_roots=(repository,),
                )
            else:
                thread = client.resume_thread(
                    thread_id.value,
                    repository,
                    runtime_workspace_roots=(repository,),
                )
            bound_thread_id = ExecutionThreadId(thread.thread_id)
            if on_thread_bound is not None:
                on_thread_bound(bound_thread_id)
            turn = client.start_turn(
                thread.thread_id,
                message,
                output_schema=ANALYSIS_OUTPUT_SCHEMA,
            )
            bound_turn_id = ExecutionTurnId(turn.turn_id)
            if on_turn_started is not None:
                on_turn_started(bound_turn_id)
            result = client.wait_for_turn(
                thread.thread_id,
                turn.turn_id,
                timeout_seconds=profile.budget.timeout_seconds,
                on_agent_delta=on_activity,
                on_item_started=on_item_started,
                on_item_completed=on_item_completed,
            )
        return _analysis_output_from_result(result, run_id)

    def recover_turn(
        self,
        *,
        repository: Path,
        run_id: WorkflowRunId,
        thread_id: ExecutionThreadId,
        turn_id: ExecutionTurnId,
        on_item_started: Callable[[str], None] | None = None,
        on_item_completed: Callable[[str], None] | None = None,
        on_activity: Callable[[str], None] | None = None,
    ) -> AnalysisTurnOutput:
        executable = resolve_codex_executable()
        with AppServerClient(
            str(executable),
            experimental_api=True,
            process_cwd=repository,
        ) as client:
            client.initialize()
            _, result = client.resume_thread_with_turn(
                thread_id.value,
                repository,
                turn_id.value,
                runtime_workspace_roots=(repository,),
            )
            if result is None:
                raise AnalysisComponentError("Checkpointed analysis turn is missing.")
            if result.status is AppServerTurnStatus.IN_PROGRESS:
                result = client.wait_for_turn(
                    thread_id.value,
                    turn_id.value,
                    timeout_seconds=ANALYSIS_TURN_TIMEOUT_SECONDS,
                    on_agent_delta=on_activity,
                    on_item_started=on_item_started,
                    on_item_completed=on_item_completed,
                )
        return _analysis_output_from_result(result, run_id)

    def validate_resume(self, repository: Path, thread_id: ExecutionThreadId) -> None:
        executable = resolve_codex_executable()
        with AppServerClient(str(executable), experimental_api=True) as client:
            client.initialize()
            client.resume_thread(
                thread_id.value,
                repository,
                runtime_workspace_roots=(repository,),
            )


def analysis_component() -> tuple[ComponentManifest, AnalysisComponentRunner]:
    runner = AnalysisComponentRunner()
    return (
        ComponentManifest(
            schema=ANALYSIS_COMPONENT_SCHEMA,
            component_id=ANALYSIS_COMPONENT_ID,
            version=ANALYSIS_COMPONENT_VERSION,
            distribution=ANALYSIS_DISTRIBUTION,
            package_hash=package_source_hash(Path(__file__).resolve().parents[1]),
            execution_policy=StepExecutionPolicy.ANALYSIS_DRAFT_ONLY,
            ports=(
                ComponentPort(
                    "feature_request",
                    FEATURE_REQUEST_CONTRACT,
                    PortDirection.INPUT,
                ),
                ComponentPort(
                    "prd_package",
                    PRD_PACKAGE_CONTRACT,
                    PortDirection.OUTPUT,
                ),
            ),
            approval_policy=ApprovalPolicy.read_only(ANALYSIS_COMPONENT_ID.value),
            execution_profiles=ANALYSIS_EXECUTION_PROFILES,
            default_execution_profile="FULL",
        ),
        runner,
    )


def _execution_profile(profile: ExecutionProfile | None) -> ExecutionProfile:
    selected = ANALYSIS_EXECUTION_PROFILES[0] if profile is None else profile
    if selected.component_id != ANALYSIS_COMPONENT_ID.value:
        raise AnalysisComponentError("Execution profile belongs to another component.")
    if selected not in ANALYSIS_EXECUTION_PROFILES:
        raise AnalysisComponentError("Execution profile is not supported by analysis.")
    return selected


def builtin_component_registry() -> ComponentRegistry:
    """Compatibility entry point retained for Issue 0001/0002 callers."""

    registry = ComponentRegistry()
    manifest, runner = analysis_component()
    registry.register(manifest, runner)
    return registry


def analysis_prompt(feature_request: str) -> str:
    return f"""Create the planning package for this feature request:\n\n{feature_request}\n\n
Provide translated section labels plus human planning content only. Use one-based requirement and
Issue positions for coverage and dependencies. Do not invent IDs, slugs, filenames, Markdown,
markers, or hashes; Dev Loop owns and renders those deterministically. Every Requirement must be
covered by at least one Issue. Dependencies must reference existing Issue positions and be
acyclic."""


def _analysis_output_from_result(
    result: AppServerTurnResult,
    run_id: WorkflowRunId,
) -> AnalysisTurnOutput:
    if result.status is not AppServerTurnStatus.COMPLETED:
        detail = f" Code: {result.failure_code}." if result.failure_code else ""
        raise AnalysisComponentError(
            f"Analysis turn ended with status {result.status.value}.{detail}"
        )
    response = _parse_response(result.message)
    kind = AnalysisResponseKind(_required_string(response, "kind"))
    clarification: str | None = None
    draft: AnalysisDraft | None = None
    if kind is AnalysisResponseKind.CLARIFICATION:
        clarification = _required_string(response, "question")
    else:
        draft_value = response.get("draft")
        if not isinstance(draft_value, dict):
            raise AnalysisComponentError("Analysis returned DRAFT without draft data.")
        content = parse_analysis_content(cast(dict[str, object], draft_value))
        draft = render_analysis_content(content, run_id)
    return AnalysisTurnOutput(
        kind=kind,
        thread_id=ExecutionThreadId(result.thread_id),
        turn_id=ExecutionTurnId(result.turn_id),
        clarification=clarification,
        draft=draft,
        completed_item_ids=result.completed_item_ids,
    )


def _parse_response(message: str) -> Mapping[str, object]:
    try:
        value = json.loads(message)
    except json.JSONDecodeError as error:
        raise AnalysisComponentError("Analysis returned invalid structured output.") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise AnalysisComponentError("Analysis structured output must be an object.")
    return cast(dict[str, object], value)


def _required_string(data: Mapping[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AnalysisComponentError(f"Analysis output is missing {name}.")
    return value
