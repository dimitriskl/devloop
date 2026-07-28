from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


PORTABLE_PROTOCOL_VERSION = 1


class SupervisorMessageKind(str, Enum):
    START = "START"
    RESUME = "RESUME"
    USER_INPUT = "USER_INPUT"
    PAUSE = "PAUSE"
    FORCE_STOP = "FORCE_STOP"
    CANCEL = "CANCEL"
    SHUTDOWN = "SHUTDOWN"


class WorkerMessageKind(str, Enum):
    HELLO = "HELLO"
    CONTEXT = "CONTEXT"
    STATUS = "STATUS"
    ACTIVITY = "ACTIVITY"
    SAFE_OUTPUT = "SAFE_OUTPUT"
    INPUT_REQUEST = "INPUT_REQUEST"
    CHECKPOINT = "CHECKPOINT"
    CHECKPOINT_FAILURE = "CHECKPOINT_FAILURE"
    TERMINATION = "TERMINATION"
    COMPLETION = "COMPLETION"
    FAILURE = "FAILURE"


class PortableProtocolError(ValueError):
    """Raised when a supervisor/worker frame violates the protocol contract."""


@dataclass(frozen=True)
class PortableProtocolFrame:
    version: int
    session_id: str
    sequence: int
    kind: str
    payload: Mapping[str, Any]

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "session_id": self.session_id,
                "sequence": self.sequence,
                "kind": self.kind,
                "payload": dict(self.payload),
            },
            separators=(",", ":"),
        )

    @classmethod
    def parse(
        cls,
        line: str,
        *,
        expected_session_id: str,
        expected_sequence: int | None = None,
    ) -> PortableProtocolFrame:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise PortableProtocolError("Worker sent malformed JSON.") from error
        if not isinstance(value, dict):
            raise PortableProtocolError("Worker frame must be a JSON object.")
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
