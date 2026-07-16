from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .terminal_text import compact_terminal_text


MAX_ACTIVITY_TEXT_LENGTH = 240


class CodexTurnOutcome(Enum):
    COMPLETED = "turn.completed"
    FAILED = "turn.failed"


class CodexItemType(Enum):
    AGENT_MESSAGE = "agent_message"
    MESSAGE = "message"
    REASONING = "reasoning"
    COMMAND_EXECUTION = "command_execution"
    FILE_CHANGE = "file_change"
    MCP_TOOL_CALL = "mcp_tool_call"
    WEB_SEARCH = "web_search"
    PLAN_UPDATE = "todo_list"


class RunWideBlockerKind(str, Enum):
    USAGE_LIMIT = "USAGE_LIMIT"
    AUTHENTICATION = "AUTHENTICATION"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


@dataclass(frozen=True)
class RunWideBlocker:
    kind: RunWideBlockerKind
    summary: str


RUN_WIDE_BLOCKER_PATTERNS = (
    (
        RunWideBlockerKind.USAGE_LIMIT,
        re.compile(
            r"\b(usage limit|rate limit exceeded|insufficient_quota|"
            r"out of credits|credits? exhausted)\b",
            re.IGNORECASE,
        ),
        "Codex usage is exhausted. Restore usage availability, then rerun the same command.",
    ),
    (
        RunWideBlockerKind.AUTHENTICATION,
        re.compile(
            r"\b(invalid api key|authentication failed|unauthorized|"
            r"not authenticated|login required|http 401)\b",
            re.IGNORECASE,
        ),
        "Codex authentication is unavailable. Restore authentication, then rerun the same command.",
    ),
    (
        RunWideBlockerKind.SERVICE_UNAVAILABLE,
        re.compile(
            r"\b(service unavailable|temporarily unavailable|backend unavailable|"
            r"server overloaded|http 503)\b",
            re.IGNORECASE,
        ),
        "The Codex service is unavailable. Wait for recovery, then rerun the same command.",
    ),
)


def classify_run_wide_blocker(stdout: str, stderr: str) -> RunWideBlocker | None:
    terminal_errors: list[str] = []
    for line in stdout.splitlines():
        payload = parse_codex_event(line)
        if payload is None or payload.get("type") not in {
            "error",
            CodexTurnOutcome.FAILED.value,
        }:
            continue
        message = extract_text(payload.get("message")) or extract_text(
            payload.get("error")
        )
        if message:
            terminal_errors.append(message)
    if stderr:
        terminal_errors.append(stderr)
    error_text = "\n".join(terminal_errors)
    for kind, pattern, summary in RUN_WIDE_BLOCKER_PATTERNS:
        if pattern.search(error_text):
            return RunWideBlocker(kind=kind, summary=summary)
    return None


def parse_codex_event(line: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def codex_turn_outcome(payload: dict[str, Any] | None) -> CodexTurnOutcome | None:
    if payload is None:
        return None
    event_type = payload.get("type")
    if not isinstance(event_type, str):
        return None
    try:
        return CodexTurnOutcome(event_type)
    except ValueError:
        return None


def render_safe_codex_activity(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    event_type = payload.get("type")
    if event_type == "turn.started":
        return "Codex turn started."
    if event_type in {"error", CodexTurnOutcome.FAILED.value}:
        message = extract_text(payload.get("message")) or extract_text(
            payload.get("error")
        )
        if message:
            return f"Codex reported an error: {compact_activity_text(message)}"
        return "Codex reported an error."
    if event_type not in {"item.started", "item.updated", "item.completed"}:
        return None
    if event_type == "item.updated":
        return None

    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    completed = event_type == "item.completed"

    if item_type in {CodexItemType.AGENT_MESSAGE.value, CodexItemType.MESSAGE.value}:
        if not completed:
            return None
        if item_type == CodexItemType.MESSAGE.value and item.get("role") != "assistant":
            return None
        message = (
            extract_text(item.get("text"))
            or extract_text(item.get("message"))
            or extract_text(item.get("content"))
        )
        if not message:
            return None
        if looks_like_structured_result(message):
            return "Structured role result received."
        return f"Codex update: {compact_activity_text(message)}"

    if item_type == CodexItemType.REASONING.value:
        # Never print raw or hidden reasoning content. Codex's surfaced agent
        # messages provide useful progress without exposing chain-of-thought.
        return "Codex is reasoning about the task." if not completed else None
    if item_type == CodexItemType.COMMAND_EXECUTION.value:
        status = str(item.get("status", "")).lower()
        if status in {"failed", "error"}:
            return "A repository command failed."
        if completed:
            return "Repository command finished."
        return "Running a repository command."
    if item_type == CodexItemType.FILE_CHANGE.value:
        if completed:
            return "Repository file changes applied."
        return "Applying repository file changes."
    if item_type == CodexItemType.MCP_TOOL_CALL.value:
        return "External tool call finished." if completed else "Using an external tool."
    if item_type == CodexItemType.WEB_SEARCH.value:
        return "Web search finished." if completed else "Searching the web."
    if item_type == CodexItemType.PLAN_UPDATE.value:
        return "Execution plan updated." if completed else "Updating the execution plan."
    return None


def extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [extract_text(part) for part in value]
        return "".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "message", "content", "delta", "error"):
            text = extract_text(value.get(key))
            if text:
                return text
    return ""


def compact_activity_text(text: str) -> str:
    return compact_terminal_text(text, max_length=MAX_ACTIVITY_TEXT_LENGTH)


def looks_like_structured_result(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        stripped = stripped[7:-3].strip()
    if not stripped.startswith("{"):
        return False
    try:
        return isinstance(json.loads(stripped), dict)
    except json.JSONDecodeError:
        return False
