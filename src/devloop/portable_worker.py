from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from itertools import count
from threading import RLock
from typing import Any, TextIO

from .portable_protocol import (
    PORTABLE_PROTOCOL_VERSION,
    PortableProtocolError,
    PortableProtocolFrame,
    SupervisorMessageKind,
    WorkerMessageKind,
)
from .portable_runtime import PortableRunContext, portable_runtime_session
from .portable_sessions import PortableWorkflowOperation


class _ProtocolOutputStream:
    def __init__(self, bridge: PortableWorkerRuntimeBridge, *, is_error: bool) -> None:
        self._bridge = bridge
        self._is_error = is_error

    def write(self, content: str) -> int:
        self._bridge.write_output(content, is_error=self._is_error)
        return len(content)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


class PortableWorkerRuntimeBridge:
    """Project the existing portable runtime interface through worker JSON Lines."""

    def __init__(
        self,
        session_id: str,
        *,
        command_stream: TextIO,
        event_stream: TextIO,
    ) -> None:
        self._session_id = session_id
        self._command_stream = command_stream
        self._event_stream = event_stream
        self._event_sequences = count(1)
        self._expected_command_sequence = 2
        self._write_lock = RLock()
        self._content_size: tuple[int, int] | None = None

    def choose(
        self,
        options: Sequence[tuple[str, str]],
        *,
        default_key: str,
        cancel_key: str | None,
        render: Callable[[str], None],
        shortcuts: Mapping[str, str] | None = None,
    ) -> str:
        render(default_key)
        self._send(
            WorkerMessageKind.INPUT_REQUEST,
            {
                "request_kind": "CHOICE",
                "options": [list(option) for option in options],
                "default_key": default_key,
                "cancel_key": cancel_key,
                "shortcuts": dict(shortcuts or {}),
            },
        )
        selected = self._read_user_input()
        render(selected)
        return selected

    def read_line(self, prompt: str, *, history: Sequence[str] = ()) -> str:
        self._send(
            WorkerMessageKind.INPUT_REQUEST,
            {
                "request_kind": "TEXT",
                "prompt": prompt,
                "history": list(history),
            },
        )
        return self._read_user_input()

    def request_stop(self) -> None:
        return None

    def show_screen(self, content: str) -> None:
        self._send(WorkerMessageKind.SAFE_OUTPUT, {"content": content})

    def update_run_context(self, context: PortableRunContext) -> None:
        self._send(WorkerMessageKind.CONTEXT, asdict(context))

    def write_output(self, content: str, *, is_error: bool) -> None:
        if content:
            self._send(
                WorkerMessageKind.SAFE_OUTPUT,
                {"content": content, "is_error": is_error},
            )

    def set_content_size(self, columns: int, rows: int) -> None:
        self._content_size = (max(1, columns), max(1, rows))

    def content_size(self, *, fallback: tuple[int, int]) -> tuple[int, int]:
        return self._content_size or fallback

    def send_completion(self, exit_code: int) -> None:
        self._send(WorkerMessageKind.COMPLETION, {"exit_code": exit_code})

    def send_hello(self) -> None:
        self._send(WorkerMessageKind.HELLO, {})

    def send_failure(self, error: BaseException) -> None:
        self._send(
            WorkerMessageKind.FAILURE,
            {"message": f"{type(error).__name__}: {error}"},
        )

    def _send(self, kind: WorkerMessageKind, payload: Mapping[str, Any]) -> None:
        with self._write_lock:
            frame = PortableProtocolFrame(
                version=PORTABLE_PROTOCOL_VERSION,
                session_id=self._session_id,
                sequence=next(self._event_sequences),
                kind=kind.value,
                payload=payload,
            )
            self._event_stream.write(frame.to_json_line() + "\n")
            self._event_stream.flush()

    def _read_user_input(self) -> str:
        line = self._command_stream.readline()
        if not line:
            raise PortableProtocolError("Supervisor closed worker input.")
        frame = PortableProtocolFrame.parse(
            line,
            expected_session_id=self._session_id,
            expected_sequence=self._expected_command_sequence,
        )
        self._expected_command_sequence += 1
        if frame.kind != SupervisorMessageKind.USER_INPUT.value:
            raise PortableProtocolError(
                f"Expected USER_INPUT; received {frame.kind!r}."
            )
        value = frame.payload.get("value")
        if not isinstance(value, str):
            raise PortableProtocolError("Supervisor USER_INPUT value must be text.")
        return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--session-id", required=True)
    options = parser.parse_args(argv)
    protocol_stdout = sys.stdout
    start = _read_launch_frame(options.session_id, sys.stdin)
    bridge = PortableWorkerRuntimeBridge(
        options.session_id,
        command_stream=sys.stdin,
        event_stream=protocol_stdout,
    )
    sys.stdout = _ProtocolOutputStream(bridge, is_error=False)
    try:
        bridge.send_hello()
        if start.payload.get("restore_catalog_session") is True:
            os.environ["DEVLOOP_PORTABLE_SESSION_RESTORE"] = "1"
        if start.kind == SupervisorMessageKind.RESUME.value:
            os.environ["DEVLOOP_PORTABLE_SESSION_RESUME"] = "1"
        operation = PortableWorkflowOperation(start.payload.get("operation"))
        arguments = start.payload.get("arguments")
        if not isinstance(arguments, list) or not all(
            isinstance(argument, str) for argument in arguments
        ):
            raise PortableProtocolError("START arguments must be a list of strings.")
        with portable_runtime_session(bridge):
            result = _run_operation(operation, arguments)
    except SystemExit as error:
        result = error.code if isinstance(error.code, int) else 1
    except BaseException as error:
        traceback.print_exc(file=sys.stderr)
        bridge.send_failure(error)
        return 1
    finally:
        sys.stdout = protocol_stdout
    bridge.send_completion(result)
    return result


def _read_launch_frame(
    session_id: str,
    command_stream: TextIO,
) -> PortableProtocolFrame:
    line = command_stream.readline()
    if not line:
        raise PortableProtocolError("Supervisor did not send START or RESUME.")
    frame = PortableProtocolFrame.parse(
        line,
        expected_session_id=session_id,
        expected_sequence=1,
    )
    launch_kinds = {
        SupervisorMessageKind.START.value,
        SupervisorMessageKind.RESUME.value,
    }
    if frame.kind not in launch_kinds:
        raise PortableProtocolError(
            f"Expected START or RESUME; received {frame.kind!r}."
        )
    return frame


def _run_operation(
    operation: PortableWorkflowOperation,
    arguments: list[str],
) -> int:
    if operation is PortableWorkflowOperation.PLANNING:
        from .interactive_runner import main as run_planning

        return run_planning(arguments)
    from .cli import main as run_delivery

    return run_delivery(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
