from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from devloop.analysis.package import parse_analysis_draft
from devloop.components.contracts import (
    ComponentManifest,
    ComponentPort,
    ComponentRegistry,
    PortDirection,
    StepExecutionPolicy,
    package_source_hash,
)
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
    ANALYSIS_DRAFT_SCHEMA,
    ANALYSIS_FEATURE_TITLE_MAX_LENGTH,
    ANALYSIS_ISSUE_MARKDOWN_MAX_LENGTH,
    ANALYSIS_ISSUE_TITLE_MAX_LENGTH,
    ANALYSIS_PRD_MARKDOWN_MAX_LENGTH,
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

_ANALYSIS_INSTRUCTIONS = """You are the analysis component of Dev Loop. Work only on planning.
Do not edit repository files or run implementation commands. Ask one concise clarification when
material product intent is missing; otherwise return a complete PRD and issue package. Preserve the
user's content language. Machine identifiers and hidden markers must use the exact schema tokens.
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
                "feature_slug",
                "prd_markdown",
                "requirements",
                "issues",
                "revision",
            ],
            "properties": {
                "schema": {"type": "string", "const": ANALYSIS_DRAFT_SCHEMA},
                "feature_title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": ANALYSIS_FEATURE_TITLE_MAX_LENGTH,
                },
                "feature_slug": {
                    "type": "string",
                    "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
                    "maxLength": 100,
                },
                "prd_markdown": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": ANALYSIS_PRD_MARKDOWN_MAX_LENGTH,
                },
                "requirements": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 500,
                    "items": {"type": "string", "pattern": "^REQ-[0-9]{3,}$"},
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
                "id",
                "slug",
                "title",
                "requirements",
                "dependencies",
                "acceptance_criteria",
                "markdown",
            ],
            "properties": {
                "id": {"type": "string", "pattern": "^ISSUE-[0-9]{3,}$"},
                "slug": {
                    "type": "string",
                    "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
                    "maxLength": 100,
                },
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": ANALYSIS_ISSUE_TITLE_MAX_LENGTH,
                },
                "requirements": {
                    "type": "array",
                    "maxItems": 500,
                    "items": {"type": "string", "pattern": "^REQ-[0-9]{3,}$"},
                },
                "dependencies": {
                    "type": "array",
                    "maxItems": 200,
                    "items": {"type": "string", "pattern": "^ISSUE-[0-9]{3,}$"},
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
                "markdown": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": ANALYSIS_ISSUE_MARKDOWN_MAX_LENGTH,
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
    ) -> AnalysisTurnOutput:
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
                    model=ANALYSIS_MODEL,
                    reasoning_effort=AppServerReasoningEffort.XHIGH,
                    developer_instructions=_ANALYSIS_INSTRUCTIONS,
                )
            else:
                thread = client.resume_thread(thread_id.value, repository)
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
                timeout_seconds=ANALYSIS_TURN_TIMEOUT_SECONDS,
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
        with AppServerClient(str(executable)) as client:
            client.initialize()
            client.resume_thread(thread_id.value, repository)


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
        ),
        runner,
    )


def builtin_component_registry() -> ComponentRegistry:
    """Compatibility entry point retained for Issue 0001/0002 callers."""

    registry = ComponentRegistry()
    manifest, runner = analysis_component()
    registry.register(manifest, runner)
    return registry


def analysis_prompt(feature_request: str) -> str:
    return f"""Create the planning package for this feature request:\n\n{feature_request}\n\n
The PRD Markdown must include these exact markers: <!-- devloop:prd:v1 -->,
<!-- devloop:section:problem -->, <!-- devloop:section:solution -->, and
<!-- devloop:section:requirements -->. Each Issue Markdown must include
<!-- devloop:issue:v1 -->, <!-- devloop:section:description -->, and
<!-- devloop:section:acceptance -->. Put every stable Requirement ID in the PRD and every stable
Acceptance criterion text after the acceptance marker. Dev Loop assigns stable Acceptance Criterion
IDs deterministically. Every Requirement must be covered by at least one Issue. Dependencies must
reference existing Issues and be acyclic."""


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
        normalized = _normalize_draft_payload(cast(dict[str, object], draft_value))
        draft = parse_analysis_draft(normalized, run_id)
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


def _normalize_draft_payload(draft: Mapping[str, object]) -> dict[str, object]:
    issues_value = draft.get("issues")
    if not isinstance(issues_value, list):
        raise AnalysisComponentError("Analysis Draft is missing Issues.")
    issue_rows: list[dict[str, object]] = []
    id_mapping: dict[str, str] = {}
    for position, value in enumerate(issues_value, start=1):
        if not isinstance(value, dict):
            raise AnalysisComponentError("Analysis Draft contains an invalid Issue.")
        row = cast(dict[str, object], value)
        original_id = _required_string(row, "id")
        canonical_id = f"ISSUE-{position:03d}"
        if original_id in id_mapping:
            raise AnalysisComponentError("Analysis Draft contains duplicate Issue IDs.")
        id_mapping[original_id] = canonical_id
        issue_rows.append(row)

    normalized_issues: list[dict[str, object]] = []
    for position, row in enumerate(issue_rows, start=1):
        canonical_id = f"ISSUE-{position:03d}"
        dependencies_value = row.get("dependencies")
        criteria_value = row.get("acceptance_criteria")
        if not isinstance(dependencies_value, list) or not all(
            isinstance(item, str) for item in dependencies_value
        ):
            raise AnalysisComponentError("Analysis Issue dependencies are invalid.")
        if not isinstance(criteria_value, list):
            raise AnalysisComponentError("Analysis acceptance criteria are invalid.")
        criteria: list[dict[str, object]] = []
        for criterion_position, criterion_value in enumerate(criteria_value, start=1):
            if not isinstance(criterion_value, dict):
                raise AnalysisComponentError("Analysis acceptance criterion is invalid.")
            criterion = cast(dict[str, object], criterion_value)
            criteria.append(
                {
                    "id": f"AC-{canonical_id}-{criterion_position:03d}",
                    "text": _required_string(criterion, "text"),
                }
            )
        normalized = dict(row)
        normalized["id"] = canonical_id
        normalized["dependencies"] = [
            id_mapping.get(cast(str, item), cast(str, item)) for item in dependencies_value
        ]
        normalized["acceptance_criteria"] = criteria
        normalized["markdown"] = _canonical_issue_markdown(
            _required_string(row, "markdown"),
            criteria,
        )
        normalized_issues.append(normalized)
    normalized_draft = dict(draft)
    normalized_draft["issues"] = normalized_issues
    return normalized_draft


def _canonical_issue_markdown(
    markdown: str,
    criteria: list[dict[str, object]],
) -> str:
    marker = "<!-- devloop:section:acceptance -->"
    prefix = markdown.split(marker, maxsplit=1)[0].rstrip()
    lines = [prefix, "", marker, "## Acceptance Criteria", ""]
    for criterion in criteria:
        lines.append(f"- **{criterion['id']}**: {criterion['text']}")
    return "\n".join(lines)
