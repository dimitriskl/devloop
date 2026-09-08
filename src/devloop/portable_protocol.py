from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .execution_backend_id import ExecutionBackendId

PORTABLE_PROTOCOL_VERSION = 1
MAX_PORTABLE_PROTOCOL_FRAME_BYTES = 65_536
MAX_PORTABLE_PROTOCOL_TEXT_CHARACTERS = 8_192
MAX_PORTABLE_PROTOCOL_COLLECTION_ITEMS = 200
MAX_PORTABLE_PROTOCOL_CONTEXT_ITEMS = 20
MAX_PORTABLE_PROTOCOL_CONTEXT_ITEM_CHARACTERS = 2_000
MAX_PORTABLE_EXECUTION_BUDGET_SECONDS = 3_600.0
_FRAME_FIELDS = frozenset({"version", "session_id", "sequence", "kind", "payload"})
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
_FAST_PREFERENCES = frozenset({"ON", "OFF"})
_RECOVERY_NEXT_ROLES = frozenset({"scheduler", "coder", "reviewer", "qa", "complete"})
_NULLABLE_TEXT_FIELDS = frozenset(
    {
        "active_issue",
        "cancel_key",
        "issue_id",
        "last_checkpoint",
        "next_role",
        "planning_thread_id",
        "prd_path",
        "issues_index_path",
    }
)


class SupervisorMessageKind(str, Enum):
    START = "START"
    RESUME = "RESUME"
    USER_INPUT = "USER_INPUT"
    APPROVAL_DECISION = "APPROVAL_DECISION"
    PAUSE = "PAUSE"
    FORCE_STOP = "FORCE_STOP"
    CANCEL = "CANCEL"
    SHUTDOWN = "SHUTDOWN"


class PortableApprovalDecision(str, Enum):
    APPROVE_ONCE = "APPROVE_ONCE"
    APPROVE_FOR_SESSION = "APPROVE_FOR_SESSION"
    DENY = "DENY"
    ABORT_RUN = "ABORT_RUN"


def validated_portable_approval_decisions(
    supported_decisions: Sequence[str],
    *,
    default_decision: str,
    cancel_decision: str,
) -> tuple[str, ...]:
    decisions = tuple(supported_decisions)
    closed_decisions = {decision.value for decision in PortableApprovalDecision}
    if (
        not decisions
        or not all(isinstance(decision, str) for decision in decisions)
        or len(decisions) != len(set(decisions))
        or not set(decisions).issubset(closed_decisions)
        or not isinstance(default_decision, str)
        or not isinstance(cancel_decision, str)
        or default_decision not in decisions
        or cancel_decision not in decisions
    ):
        raise PortableProtocolError("Portable approval decisions are invalid.")
    return decisions


class WorkerMessageKind(str, Enum):
    HELLO = "HELLO"
    CONTEXT = "CONTEXT"
    STATUS = "STATUS"
    ACTIVITY = "ACTIVITY"
    SAFE_OUTPUT = "SAFE_OUTPUT"
    SCREEN = "SCREEN"
    INPUT_REQUEST = "INPUT_REQUEST"
    CHECKPOINT = "CHECKPOINT"
    CHECKPOINT_FAILURE = "CHECKPOINT_FAILURE"
    TERMINATION = "TERMINATION"
    COMPLETION = "COMPLETION"
    FAILURE = "FAILURE"
    HEARTBEAT = "HEARTBEAT"


class PortableProtocolError(ValueError):
    """Raised when a supervisor/worker frame violates the protocol contract."""


class PortableProtocolDirection(str, Enum):
    SUPERVISOR_COMMAND = "SUPERVISOR_COMMAND"
    WORKER_EVENT = "WORKER_EVENT"


@dataclass(frozen=True)
class PortableProtocolFrame:
    version: int
    session_id: str
    sequence: int
    kind: str
    payload: Mapping[str, Any]

    def to_json_line(self) -> str:
        _validate_payload(self.kind, self.payload)
        line = json.dumps(
            {
                "version": self.version,
                "session_id": self.session_id,
                "sequence": self.sequence,
                "kind": self.kind,
                "payload": dict(self.payload),
            },
            separators=(",", ":"),
        )
        if len(line.encode("utf-8")) > MAX_PORTABLE_PROTOCOL_FRAME_BYTES:
            raise PortableProtocolError(
                f"Portable protocol frame exceeds {MAX_PORTABLE_PROTOCOL_FRAME_BYTES} bytes."
            )
        return line

    @classmethod
    def parse(
        cls,
        line: str,
        *,
        expected_session_id: str,
        expected_sequence: int | None = None,
    ) -> PortableProtocolFrame:
        try:
            value = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_fields,
                parse_constant=_reject_non_json_number,
            )
        except PortableProtocolError:
            raise
        except (ValueError, RecursionError) as error:
            raise PortableProtocolError("Worker sent malformed JSON.") from error
        if not isinstance(value, dict):
            raise PortableProtocolError("Worker frame must be a JSON object.")
        if set(value) != _FRAME_FIELDS:
            raise PortableProtocolError(
                "Portable protocol frame must contain exactly version, session_id, "
                "sequence, kind, and payload."
            )
        version = value.get("version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise PortableProtocolError(
                "Worker protocol version must be an integer."
            )
        if version != PORTABLE_PROTOCOL_VERSION:
            raise PortableProtocolError(
                f"Unsupported worker protocol version: {version!r}."
            )
        session_id = value.get("session_id")
        if session_id != expected_session_id:
            raise PortableProtocolError(
                f"Worker frame identified session {session_id!r}; "
                f"expected {expected_session_id!r}."
            )
        sequence = value.get("sequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
        ):
            raise PortableProtocolError("Worker frame sequence must be a positive integer.")
        if expected_sequence is not None and sequence != expected_sequence:
            raise PortableProtocolError(
                f"Worker frame sequence {sequence} arrived; "
                f"expected {expected_sequence}."
            )
        kind = value.get("kind")
        if not isinstance(kind, str) or not kind:
            raise PortableProtocolError("Worker frame kind must be a non-empty string.")
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise PortableProtocolError("Worker frame payload must be a JSON object.")
        return cls(
            version=version,
            session_id=session_id,
            sequence=sequence,
            kind=kind,
            payload=payload,
        )


class PortableProtocolStreamDecoder:
    """Incrementally frame and validate one session's bounded JSON Lines stream."""

    def __init__(
        self,
        *,
        session_id: str,
        direction: PortableProtocolDirection,
        expected_sequence: int = 1,
    ) -> None:
        self._session_id = session_id
        if _SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise PortableProtocolError("Portable protocol session identity is invalid.")
        if expected_sequence < 1:
            raise PortableProtocolError("Portable protocol expected sequence is invalid.")
        self._direction = direction
        self._expected_sequence = expected_sequence
        self._buffer = bytearray()

    @classmethod
    def for_worker_events(
        cls,
        session_id: str,
        *,
        expected_sequence: int = 1,
    ) -> PortableProtocolStreamDecoder:
        return cls(
            session_id=session_id,
            direction=PortableProtocolDirection.WORKER_EVENT,
            expected_sequence=expected_sequence,
        )

    @classmethod
    def for_supervisor_commands(
        cls,
        session_id: str,
        *,
        expected_sequence: int = 1,
    ) -> PortableProtocolStreamDecoder:
        return cls(
            session_id=session_id,
            direction=PortableProtocolDirection.SUPERVISOR_COMMAND,
            expected_sequence=expected_sequence,
        )

    def feed(self, content: bytes) -> tuple[PortableProtocolFrame, ...]:
        if not isinstance(content, bytes):
            raise TypeError("Portable protocol input must be bytes.")
        self._buffer.extend(content)
        if b"\n" not in self._buffer and _unterminated_frame_is_oversized(
            self._buffer
        ):
            raise PortableProtocolError(
                f"Portable protocol frame exceeds {MAX_PORTABLE_PROTOCOL_FRAME_BYTES} bytes."
            )
        frames: list[PortableProtocolFrame] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            encoded = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if encoded.endswith(b"\r"):
                encoded = encoded[:-1]
            if len(encoded) > MAX_PORTABLE_PROTOCOL_FRAME_BYTES:
                raise PortableProtocolError(
                    f"Portable protocol frame exceeds {MAX_PORTABLE_PROTOCOL_FRAME_BYTES} bytes."
                )
            frames.append(self._decode(encoded))
        if _unterminated_frame_is_oversized(self._buffer):
            raise PortableProtocolError(
                f"Portable protocol frame exceeds {MAX_PORTABLE_PROTOCOL_FRAME_BYTES} bytes."
            )
        return tuple(frames)

    def finish(self) -> tuple[PortableProtocolFrame, ...]:
        if self._buffer:
            raise PortableProtocolError("Portable protocol stream ended with a partial frame.")
        return ()

    def _decode(self, encoded: bytes) -> PortableProtocolFrame:
        try:
            line = encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise PortableProtocolError("Portable protocol frame is not valid UTF-8.") from error
        frame = PortableProtocolFrame.parse(
            line,
            expected_session_id=self._session_id,
            expected_sequence=self._expected_sequence,
        )
        allowed_kinds = (
            frozenset(kind.value for kind in WorkerMessageKind)
            if self._direction is PortableProtocolDirection.WORKER_EVENT
            else frozenset(kind.value for kind in SupervisorMessageKind)
        )
        if frame.kind not in allowed_kinds:
            peer = (
                "worker"
                if self._direction is PortableProtocolDirection.WORKER_EVENT
                else "supervisor"
            )
            raise PortableProtocolError(
                f"Unsupported {peer} message kind: {frame.kind!r}."
            )
        _validate_payload(frame.kind, frame.payload)
        self._expected_sequence += 1
        return frame


def _unterminated_frame_is_oversized(buffer: bytearray) -> bool:
    size = len(buffer)
    return size > MAX_PORTABLE_PROTOCOL_FRAME_BYTES and not (
        size == MAX_PORTABLE_PROTOCOL_FRAME_BYTES + 1 and buffer.endswith(b"\r")
    )


_WORKER_PAYLOAD_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    WorkerMessageKind.HELLO.value: (frozenset(), frozenset()),
    WorkerMessageKind.CONTEXT.value: (
        frozenset(
            {
                "project_root",
                "implementation_branch",
                "implementation_worktree",
                "prd_path",
            }
        ),
        frozenset(),
    ),
    WorkerMessageKind.STATUS.value: (
        frozenset({"status"}),
        frozenset({"stage", "completed_issues", "total_issues", "active_issue"}),
    ),
    WorkerMessageKind.ACTIVITY.value: (frozenset({"message"}), frozenset()),
    WorkerMessageKind.SAFE_OUTPUT.value: (
        frozenset({"content"}),
        frozenset({"is_error"}),
    ),
    WorkerMessageKind.SCREEN.value: (frozenset({"content"}), frozenset()),
    WorkerMessageKind.INPUT_REQUEST.value: (
        frozenset({"request_kind"}),
        frozenset(
            {
                "request_id",
                "request_generation",
                "prompt",
                "options",
                "default_key",
                "cancel_key",
                "shortcuts",
                "history",
            }
        ),
    ),
    WorkerMessageKind.CHECKPOINT.value: (
        frozenset(
            {
                "summary",
                "action",
                "worker_generation",
                "request_id",
            }
        ),
        frozenset(
            {
                "checkpoint_kind",
                "planning_thread_id",
                "planning_settings",
                "prd_path",
                "issues_index_path",
                "issue_id",
                "next_role",
                "pass_number",
            }
        ),
    ),
    WorkerMessageKind.CHECKPOINT_FAILURE.value: (
        frozenset({"message", "action", "worker_generation", "request_id"}),
        frozenset(),
    ),
    WorkerMessageKind.TERMINATION.value: (
        frozenset(
            {
                "action",
                "worker_generation",
                "request_id",
                "descendants_confirmed",
                "detail",
            }
        ),
        frozenset(),
    ),
    WorkerMessageKind.COMPLETION.value: (frozenset({"exit_code"}), frozenset()),
    WorkerMessageKind.FAILURE.value: (frozenset({"message"}), frozenset()),
    WorkerMessageKind.HEARTBEAT.value: (
        frozenset({"owner_id", "worker_generation"}),
        frozenset(),
    ),
}

_SUPERVISOR_PAYLOAD_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    SupervisorMessageKind.START.value: (
        frozenset({"operation", "arguments", "owner_id", "worker_generation"}),
        frozenset(
            {
                "last_checkpoint",
                "partial_work_context",
                "restore_catalog_session",
                "planning_thread_id",
                "planning_settings",
                "recovery",
            }
        ),
    ),
    SupervisorMessageKind.RESUME.value: (
        frozenset({"operation", "arguments", "owner_id", "worker_generation"}),
        frozenset(
            {
                "last_checkpoint",
                "partial_work_context",
                "restore_catalog_session",
                "planning_thread_id",
                "planning_settings",
                "recovery",
            }
        ),
    ),
    SupervisorMessageKind.USER_INPUT.value: (
        frozenset({"value", "request_id", "request_generation"}),
        frozenset(),
    ),
    SupervisorMessageKind.APPROVAL_DECISION.value: (
        frozenset({"decision", "request_id", "request_generation"}),
        frozenset(),
    ),
    SupervisorMessageKind.PAUSE.value: (
        frozenset(),
        frozenset({"action", "worker_generation", "request_id"}),
    ),
    SupervisorMessageKind.FORCE_STOP.value: (
        frozenset(),
        frozenset({"action", "worker_generation", "request_id"}),
    ),
    SupervisorMessageKind.CANCEL.value: (
        frozenset(),
        frozenset({"action", "worker_generation", "request_id"}),
    ),
    SupervisorMessageKind.SHUTDOWN.value: (
        frozenset(),
        frozenset({"action", "worker_generation", "request_id"}),
    ),
}


def _validate_payload(kind: str, payload: Mapping[str, Any]) -> None:
    schema = _WORKER_PAYLOAD_FIELDS.get(kind) or _SUPERVISOR_PAYLOAD_FIELDS.get(kind)
    if schema is None:
        raise PortableProtocolError(f"Unsupported portable protocol message kind: {kind!r}.")
    required, optional = schema
    fields = set(payload)
    if not required.issubset(fields) or not fields.issubset(required | optional):
        raise PortableProtocolError(f"Portable protocol {kind} payload has invalid fields.")
    lifecycle_fields = {"action", "worker_generation", "request_id"}
    if kind in {
        SupervisorMessageKind.PAUSE.value,
        SupervisorMessageKind.FORCE_STOP.value,
        SupervisorMessageKind.CANCEL.value,
        SupervisorMessageKind.SHUTDOWN.value,
    } and fields not in (set(), lifecycle_fields):
        raise PortableProtocolError(f"Portable protocol {kind} payload has invalid fields.")
    _validate_json_value(payload, depth=0)
    for key in _text_fields_for(kind):
        if key not in payload:
            continue
        if payload[key] is None and key in _NULLABLE_TEXT_FIELDS:
            continue
        if not isinstance(payload[key], str):
            raise PortableProtocolError(
                f"Portable protocol {kind} payload {key!r} must be text."
            )
    for key in _integer_fields_for(kind):
        if key in payload and (
            not isinstance(payload[key], int) or isinstance(payload[key], bool)
        ):
            if kind == WorkerMessageKind.COMPLETION.value and key == "exit_code":
                raise PortableProtocolError(
                    "Worker completion exit_code must be an integer."
                )
            raise PortableProtocolError(
                f"Portable protocol {kind} payload {key!r} must be an integer."
            )
    if "is_error" in payload and not isinstance(payload["is_error"], bool):
        raise PortableProtocolError("Portable protocol SAFE_OUTPUT is_error must be boolean.")
    if "descendants_confirmed" in payload and not isinstance(
        payload["descendants_confirmed"], bool
    ):
        raise PortableProtocolError(
            "Portable protocol TERMINATION descendants_confirmed must be boolean."
        )
    if kind == WorkerMessageKind.INPUT_REQUEST.value:
        _validate_input_request(payload)
    if kind == SupervisorMessageKind.APPROVAL_DECISION.value:
        if payload.get("decision") not in {
            decision.value for decision in PortableApprovalDecision
        }:
            raise PortableProtocolError("Supervisor approval decision is unsupported.")
    if kind in {SupervisorMessageKind.START.value, SupervisorMessageKind.RESUME.value}:
        _validate_launch_payload(payload)


def _text_fields_for(kind: str) -> frozenset[str]:
    common = {
        "message",
        "content",
        "status",
        "stage",
        "active_issue",
        "request_kind",
        "request_id",
        "prompt",
        "default_key",
        "cancel_key",
        "checkpoint_kind",
        "summary",
        "action",
        "detail",
        "owner_id",
        "operation",
        "planning_thread_id",
        "prd_path",
        "issues_index_path",
        "issue_id",
        "next_role",
        "last_checkpoint",
        "value",
        "decision",
        "project_root",
        "implementation_branch",
        "implementation_worktree",
    }
    return frozenset(common)


def _integer_fields_for(kind: str) -> frozenset[str]:
    return frozenset(
        {
            "sequence",
            "request_generation",
            "worker_generation",
            "completed_issues",
            "total_issues",
            "pass_number",
            "exit_code",
        }
    )


def _validate_json_value(value: Any, *, depth: int) -> None:
    if depth > 12:
        raise PortableProtocolError("Portable protocol payload nesting is too deep.")
    if isinstance(value, str):
        if len(value) > MAX_PORTABLE_PROTOCOL_TEXT_CHARACTERS:
            raise PortableProtocolError("Portable protocol payload text is oversized.")
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PortableProtocolError("Portable protocol payload number is invalid.")
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_PORTABLE_PROTOCOL_COLLECTION_ITEMS:
            raise PortableProtocolError("Portable protocol payload object is oversized.")
        if not all(isinstance(key, str) for key in value):
            raise PortableProtocolError("Portable protocol payload keys must be text.")
        for member in value.values():
            _validate_json_value(member, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_PORTABLE_PROTOCOL_COLLECTION_ITEMS:
            raise PortableProtocolError("Portable protocol payload list is oversized.")
        for member in value:
            _validate_json_value(member, depth=depth + 1)
        return
    raise PortableProtocolError("Portable protocol payload contains an unsupported value.")


def _validate_input_request(payload: Mapping[str, Any]) -> None:
    request_kind = payload.get("request_kind")
    if request_kind not in {"CHOICE", "TEXT", "APPROVAL"}:
        raise PortableProtocolError("Worker input request kind is unsupported.")
    request_id = payload.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not request_id):
        raise PortableProtocolError("Worker input request identity must be non-empty text.")
    generation = payload.get("request_generation")
    if generation is not None and (
        not isinstance(generation, int) or isinstance(generation, bool) or generation < 1
    ):
        raise PortableProtocolError("Worker input request generation must be positive.")
    if request_kind == "TEXT" and not isinstance(payload.get("prompt"), str):
        raise PortableProtocolError("Worker text input request requires a prompt.")
    if request_kind in {"CHOICE", "APPROVAL"}:
        if not isinstance(payload.get("options"), list) or not isinstance(
            payload.get("default_key"), str
        ):
            raise PortableProtocolError(
                "Worker choice input request requires options and a default key."
            )
    options = payload.get("options", [])
    if not isinstance(options, list) or not all(
        isinstance(option, list)
        and len(option) == 2
        and all(isinstance(item, str) for item in option)
        for option in options
    ):
        raise PortableProtocolError("Worker input request options must contain text pairs.")
    shortcuts = payload.get("shortcuts", {})
    if not isinstance(shortcuts, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in shortcuts.items()
    ):
        raise PortableProtocolError("Worker input request shortcuts must map text to text.")
    history = payload.get("history", [])
    if not isinstance(history, list) or not all(isinstance(item, str) for item in history):
        raise PortableProtocolError("Worker input history must contain text values.")
    if request_kind == "APPROVAL":
        required = {
            "request_id",
            "request_generation",
            "prompt",
            "options",
            "default_key",
            "cancel_key",
        }
        if not required.issubset(payload):
            raise PortableProtocolError("Worker approval request is incomplete.")
        validated_portable_approval_decisions(
            [option[0] for option in options],
            default_decision=payload["default_key"],
            cancel_decision=payload["cancel_key"],
        )


def _validate_launch_payload(payload: Mapping[str, Any]) -> None:
    if payload.get("operation") not in {"PLANNING", "DELIVERY"}:
        raise PortableProtocolError("Supervisor launch operation is unsupported.")
    arguments = payload.get("arguments")
    if not isinstance(arguments, list) or not all(
        isinstance(argument, str) for argument in arguments
    ):
        raise PortableProtocolError("Supervisor launch arguments must contain text values.")
    owner_id = payload.get("owner_id")
    if not isinstance(owner_id, str) or not owner_id:
        raise PortableProtocolError("Supervisor launch owner must be non-empty text.")
    generation = payload.get("worker_generation")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise PortableProtocolError("Supervisor worker generation must be positive.")
    partial_context = payload.get("partial_work_context")
    if partial_context is not None:
        _require_exact_mapping(
            partial_context,
            frozenset({"activity", "diagnostics"}),
            "Supervisor partial-work context",
        )
        for key in ("activity", "diagnostics"):
            _validate_context_text_list(
                partial_context[key],
                f"Supervisor partial-work context {key}",
            )
    restore = payload.get("restore_catalog_session")
    if restore is not None and not isinstance(restore, bool):
        raise PortableProtocolError("Supervisor catalog restore marker must be boolean.")
    settings = payload.get("planning_settings")
    if settings is not None:
        _validate_planning_settings(settings)
    recovery = payload.get("recovery")
    if recovery is not None:
        _validate_recovery(recovery)


_PLANNING_SETTINGS_FIELDS = frozenset(
    {
        "backend",
        "model",
        "reasoning_effort",
        "fast",
        "timeout_seconds",
        "checkpoint_seconds",
    }
)
_RECOVERY_FIELDS = frozenset(
    {
        "checkpoint_kind",
        "checkout",
        "activity",
        "diagnostics",
        "planning_thread_id",
        "planning_settings",
        "prd_path",
        "issues_index_path",
        "issue_id",
        "next_role",
        "pass_number",
    }
)


def _validate_planning_settings(value: object) -> None:
    settings = _require_exact_mapping(
        value,
        _PLANNING_SETTINGS_FIELDS,
        "Supervisor planning settings",
    )
    backend = _bounded_nonempty_text(settings["backend"], "planning backend")
    supported_backends = frozenset(item.value for item in ExecutionBackendId)
    if backend not in supported_backends:
        raise PortableProtocolError("Supervisor planning backend is unsupported.")
    model = _bounded_nonempty_text(settings["model"], "planning model")
    reasoning = _bounded_nonempty_text(
        settings["reasoning_effort"],
        "planning reasoning effort",
    )
    if model != model.strip() or reasoning != reasoning.strip():
        raise PortableProtocolError("Supervisor planning text must be normalized.")
    fast = _bounded_nonempty_text(settings["fast"], "planning Fast preference")
    if fast not in _FAST_PREFERENCES:
        raise PortableProtocolError("Supervisor planning Fast preference is unsupported.")
    if backend == ExecutionBackendId.CLAUDE_CODE.value and fast == "ON":
        raise PortableProtocolError("Supervisor planning Fast preference is unsupported.")
    timeout = _positive_finite_number(settings["timeout_seconds"], "planning timeout")
    checkpoint = _positive_finite_number(
        settings["checkpoint_seconds"],
        "planning checkpoint deadline",
    )
    if timeout > MAX_PORTABLE_EXECUTION_BUDGET_SECONDS or checkpoint > timeout:
        raise PortableProtocolError("Supervisor planning execution budget is invalid.")


def _validate_recovery(value: object) -> None:
    recovery = _require_exact_mapping(
        value,
        _RECOVERY_FIELDS,
        "Supervisor recovery data",
    )
    checkout = _absolute_path_text(recovery["checkout"], "recovery checkout")
    if not checkout:
        raise PortableProtocolError("Supervisor recovery checkout is invalid.")
    _validate_context_text_list(recovery["activity"], "Recovery activity")
    _validate_context_text_list(recovery["diagnostics"], "Recovery diagnostics")
    checkpoint_kind = _bounded_nonempty_text(
        recovery["checkpoint_kind"],
        "recovery checkpoint kind",
    )
    if checkpoint_kind == "PLANNING":
        _canonical_uuid(recovery["planning_thread_id"], "planning thread")
        _validate_planning_settings(recovery["planning_settings"])
        if any(
            recovery[field] is not None
            for field in (
                "prd_path",
                "issues_index_path",
                "issue_id",
                "next_role",
                "pass_number",
            )
        ):
            raise PortableProtocolError("Planning recovery fields are inconsistent.")
        return
    if checkpoint_kind != "PRD":
        raise PortableProtocolError("Supervisor recovery checkpoint kind is unsupported.")
    if (
        recovery["planning_thread_id"] is not None
        or recovery["planning_settings"] is not None
    ):
        raise PortableProtocolError("PRD recovery fields are inconsistent.")
    _absolute_path_text(recovery["prd_path"], "recovery PRD path")
    _absolute_path_text(recovery["issues_index_path"], "recovery Issue Index path")
    issue_id = recovery["issue_id"]
    if issue_id is not None:
        _bounded_nonempty_text(issue_id, "recovery issue identity", maximum_length=128)
    next_role = _bounded_nonempty_text(
        recovery["next_role"],
        "recovery role",
    )
    if next_role not in _RECOVERY_NEXT_ROLES:
        raise PortableProtocolError("Supervisor recovery role is unsupported.")
    if (issue_id is None) != (next_role == "scheduler"):
        raise PortableProtocolError("PRD recovery cursor is inconsistent.")
    pass_number = recovery["pass_number"]
    if (
        isinstance(pass_number, bool)
        or not isinstance(pass_number, int)
        or pass_number < 1
    ):
        raise PortableProtocolError("Supervisor recovery pass must be positive.")


def _validate_context_text_list(value: object, description: str) -> None:
    if (
        not isinstance(value, list)
        or len(value) > MAX_PORTABLE_PROTOCOL_CONTEXT_ITEMS
        or not all(
            isinstance(item, str)
            and len(item) <= MAX_PORTABLE_PROTOCOL_CONTEXT_ITEM_CHARACTERS
            for item in value
        )
    ):
        raise PortableProtocolError(f"{description} must contain bounded text values.")


def _bounded_nonempty_text(
    value: object,
    description: str,
    *,
    maximum_length: int = 200,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum_length
        or "\x00" in value
    ):
        raise PortableProtocolError(f"Supervisor {description} must be bounded text.")
    return value


def _positive_finite_number(value: object, description: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise PortableProtocolError(f"Supervisor {description} must be a positive number.")
    return float(value)


def _absolute_path_text(value: object, description: str) -> str:
    path_text = _bounded_nonempty_text(value, description, maximum_length=4_096)
    if not Path(path_text).is_absolute():
        raise PortableProtocolError(f"Supervisor {description} must be absolute.")
    return path_text


def _canonical_uuid(value: object, description: str) -> str:
    text = _bounded_nonempty_text(value, description, maximum_length=64)
    try:
        parsed = uuid.UUID(text)
    except ValueError as error:
        raise PortableProtocolError(f"Supervisor {description} must be a UUID.") from error
    if str(parsed) != text:
        raise PortableProtocolError(f"Supervisor {description} must be a UUID.")
    return text


def _require_exact_mapping(
    value: object,
    fields: frozenset[str],
    description: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise PortableProtocolError(f"{description} has invalid fields.")
    return value


def _reject_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, member in pairs:
        if key in value:
            raise PortableProtocolError(f"Portable protocol field {key!r} is duplicated.")
        value[key] = member
    return value


def _reject_non_json_number(value: str) -> None:
    raise PortableProtocolError(f"Portable protocol number {value!r} is invalid.")


def supervisor_frame(
    session_id: str,
    sequence: int,
    kind: SupervisorMessageKind,
    payload: Mapping[str, Any] | None = None,
) -> PortableProtocolFrame:
    return PortableProtocolFrame(
        version=PORTABLE_PROTOCOL_VERSION,
        session_id=session_id,
        sequence=sequence,
        kind=kind.value,
        payload=payload or {},
    )
