from __future__ import annotations

import argparse
import os
import sys
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from itertools import count
from queue import Queue
from threading import Event, RLock, Thread
from typing import Any, TextIO

from .portable_protocol import (
    PORTABLE_PROTOCOL_VERSION,
    PortableProtocolError,
    PortableProtocolFrame,
    SupervisorMessageKind,
    WorkerMessageKind,
)
from .portable_runtime import (
    PortableRunContext,
    PortableRuntimeStopped,
    portable_runtime_session,
)
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
        self._request_generations = count(1)
        self._write_lock = RLock()
        self._lifecycle_request: SupervisorMessageKind | None = None
        self._command_queue: Queue[PortableProtocolFrame | PortableProtocolError] = (
            Queue()
        )
        self._control_reader_started = False
        self._content_size: tuple[int, int] | None = None

    @property
    def lifecycle_request(self) -> str | None:
        request = self._lifecycle_request
        return None if request is None else request.value

    def start_control_reader(
        self,
        *,
        on_lifecycle: Callable[[SupervisorMessageKind], None] | None = None,
    ) -> None:
        if self._control_reader_started:
            return
        self._control_reader_started = True
        Thread(
            target=self._read_commands,
            args=(on_lifecycle,),
            daemon=True,
            name=f"portable-worker-{self._session_id}-control",
        ).start()

    def choose(
        self,
        options: Sequence[tuple[str, str]],
        *,
        default_key: str,
        cancel_key: str | None,
        render: Callable[[str], None],
        shortcuts: Mapping[str, str] | None = None,
    ) -> str:
        self._raise_if_stopping()
        render(default_key)
        request_id = str(uuid.uuid4())
        request_generation = next(self._request_generations)
        self._send(
            WorkerMessageKind.INPUT_REQUEST,
            {
                "request_id": request_id,
                "request_generation": request_generation,
                "request_kind": "CHOICE",
                "options": [list(option) for option in options],
                "default_key": default_key,
                "cancel_key": cancel_key,
                "shortcuts": dict(shortcuts or {}),
            },
        )
        selected = self._read_user_input(
            request_id=request_id,
            request_generation=request_generation,
        )
        render(selected)
        return selected

    def read_line(self, prompt: str, *, history: Sequence[str] = ()) -> str:
        self._raise_if_stopping()
        request_id = str(uuid.uuid4())
        request_generation = next(self._request_generations)
        self._send(
            WorkerMessageKind.INPUT_REQUEST,
            {
                "request_id": request_id,
                "request_generation": request_generation,
                "request_kind": "TEXT",
                "prompt": prompt,
                "history": list(history),
            },
        )
        return self._read_user_input(
            request_id=request_id,
            request_generation=request_generation,
        )

    def request_stop(self) -> None:
        self._lifecycle_request = SupervisorMessageKind.SHUTDOWN

    def show_screen(self, content: str) -> None:
        self._raise_if_stopping()
        self._send(WorkerMessageKind.SAFE_OUTPUT, {"content": content})

    def update_run_context(self, context: PortableRunContext) -> None:
        self._raise_if_stopping()
        self._send(WorkerMessageKind.CONTEXT, asdict(context))

    def update_session_status(
        self,
        *,
        stage: str,
        active_issue: str | None = None,
    ) -> None:
        self._raise_if_stopping()
        payload: dict[str, Any] = {
            "status": "RUNNING",
            "stage": stage,
        }
        if active_issue is not None:
            payload["active_issue"] = active_issue
        self._send(WorkerMessageKind.STATUS, payload)

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

    def send_checkpoint(self, evidence: Mapping[str, Any]) -> None:
        self._send(WorkerMessageKind.CHECKPOINT, evidence)

    def send_checkpoint_failure(self, message: str) -> None:
        self._send(WorkerMessageKind.CHECKPOINT_FAILURE, {"message": message})

    def send_termination(
        self,
        request: SupervisorMessageKind,
        *,
        descendants_confirmed: bool,
        detail: str,
    ) -> None:
        self._send(
            WorkerMessageKind.TERMINATION,
            {
                "request": request.value,
                "descendants_confirmed": descendants_confirmed,
                "detail": detail,
            },
        )

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

    def _read_user_input(
        self,
        *,
        request_id: str,
        request_generation: int,
    ) -> str:
        frame = self._next_command()
        try:
            lifecycle_kind = SupervisorMessageKind(frame.kind)
        except ValueError:
            lifecycle_kind = None
        if lifecycle_kind in {
            SupervisorMessageKind.PAUSE,
            SupervisorMessageKind.FORCE_STOP,
            SupervisorMessageKind.CANCEL,
            SupervisorMessageKind.SHUTDOWN,
        }:
            self._lifecycle_request = lifecycle_kind
            raise PortableRuntimeStopped(
                f"Portable worker received {lifecycle_kind.value}."
            )
        if frame.kind != SupervisorMessageKind.USER_INPUT.value:
            raise PortableProtocolError(
                f"Expected USER_INPUT; received {frame.kind!r}."
            )
        if (
            frame.payload.get("request_id") != request_id
            or frame.payload.get("request_generation") != request_generation
        ):
            raise PortableProtocolError(
                "Supervisor USER_INPUT does not match the current input request."
            )
        value = frame.payload.get("value")
        if not isinstance(value, str):
            raise PortableProtocolError("Supervisor USER_INPUT value must be text.")
        return value

    def _next_command(self) -> PortableProtocolFrame:
        if self._control_reader_started:
            queued = self._command_queue.get()
            if isinstance(queued, PortableProtocolError):
                raise queued
            return queued
        line = self._command_stream.readline()
        if not line:
            raise PortableProtocolError("Supervisor closed worker input.")
        frame = PortableProtocolFrame.parse(
            line,
            expected_session_id=self._session_id,
            expected_sequence=self._expected_command_sequence,
        )
        self._expected_command_sequence += 1
        return frame

    def _read_commands(
        self,
        on_lifecycle: Callable[[SupervisorMessageKind], None] | None,
    ) -> None:
        while True:
            line = self._command_stream.readline()
            if not line:
                self._command_queue.put(
                    PortableProtocolError("Supervisor closed worker input.")
                )
                return
            try:
                frame = PortableProtocolFrame.parse(
                    line,
                    expected_session_id=self._session_id,
                    expected_sequence=self._expected_command_sequence,
                )
            except PortableProtocolError as error:
                self._command_queue.put(error)
                return
            self._expected_command_sequence += 1
            try:
                kind = SupervisorMessageKind(frame.kind)
            except ValueError:
                kind = None
            if kind in {
                SupervisorMessageKind.PAUSE,
                SupervisorMessageKind.FORCE_STOP,
                SupervisorMessageKind.CANCEL,
                SupervisorMessageKind.SHUTDOWN,
            }:
                self._lifecycle_request = kind
                if on_lifecycle is not None:
                    on_lifecycle(kind)
                self._command_queue.put(frame)
                return
            self._command_queue.put(frame)

    def _raise_if_stopping(self) -> None:
        request = self._lifecycle_request
        if request is not None:
            raise PortableRuntimeStopped(
                f"Portable worker received {request.value}."
            )


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
        from .subprocess_utils import terminate_active_process_trees

        termination_handled = Event()

        def handle_lifecycle(request: SupervisorMessageKind) -> None:
            if request is SupervisorMessageKind.PAUSE:
                return
            try:
                results = terminate_active_process_trees()
                descendants_confirmed = all(
                    result.tree_terminated for result in results
                )
                detail = (
                    "All owned backend process trees stopped."
                    if descendants_confirmed
                    else "; ".join(result.detail for result in results)
                )
                bridge.send_termination(
                    request,
                    descendants_confirmed=descendants_confirmed,
                    detail=detail,
                )
            except BaseException as error:
                bridge.send_termination(
                    request,
                    descendants_confirmed=False,
                    detail=(
                        "Backend tree termination failed: "
                        f"{type(error).__name__}: {error}"
                    ),
                )
            finally:
                termination_handled.set()

        bridge.start_control_reader(on_lifecycle=handle_lifecycle)
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
            bridge.update_session_status(
                stage=(
                    "analysis"
                    if (
                        operation is PortableWorkflowOperation.PLANNING
                        and not any(
                            argument == "--prd" or argument.startswith("--prd=")
                            for argument in arguments
                        )
                    )
                    else "delivery"
                )
            )
            result = _run_operation(operation, arguments)
    except PortableRuntimeStopped:
        result = 0
    except SystemExit as error:
        result = error.code if isinstance(error.code, int) else 1
    except BaseException as error:
        traceback.print_exc(file=sys.stderr)
        bridge.send_failure(error)
        return 1
    finally:
        sys.stdout = protocol_stdout
    if bridge.lifecycle_request == SupervisorMessageKind.PAUSE.value:
        try:
            evidence = _capture_durable_checkpoint(operation, arguments)
        except (OSError, RuntimeError, ValueError) as error:
            bridge.send_checkpoint_failure(
                f"Cooperative checkpoint failed: {type(error).__name__}: {error}"
            )
            return 1
        bridge.send_checkpoint(evidence)
        return 0
    if bridge.lifecycle_request in {
        SupervisorMessageKind.FORCE_STOP.value,
        SupervisorMessageKind.CANCEL.value,
        SupervisorMessageKind.SHUTDOWN.value,
    }:
        termination_handled.wait(timeout=6.0)
        return 0
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


def _capture_durable_checkpoint(
    operation: PortableWorkflowOperation,
    arguments: Sequence[str],
) -> dict[str, Any]:
    from .portable_session_catalog import active_portable_catalog_session
    from .subprocess_utils import owned_process_trees_are_stopped

    if not owned_process_trees_are_stopped():
        raise RuntimeError("an owned backend process tree is still running")
    active_session = active_portable_catalog_session()
    if active_session is None:
        raise RuntimeError("the Portable Session Catalog is unavailable")
    _catalog, record, _restore_requested = active_session
    if record is None:
        raise RuntimeError("the Portable Session Catalog session is missing")
    if record.prd_path is None:
        if operation is not PortableWorkflowOperation.PLANNING:
            raise RuntimeError("delivery has no authoritative PRD checkpoint")
        if record.planning_thread_id is None:
            raise RuntimeError("the planning thread identity is not durable")
        if record.planning_settings is None:
            raise RuntimeError("the planning settings snapshot is not durable")
        return {
            "checkpoint_kind": "PLANNING",
            "planning_thread_id": record.planning_thread_id,
            "planning_settings": record.planning_settings.to_dict(),
            "summary": (
                "Planning checkpoint "
                f"{record.planning_thread_id} with exact settings persisted"
            ),
        }
    issues_index = record.issues_index_path
    if issues_index is None:
        raise RuntimeError("the published workflow has no Issue Index pointer")
    prd_path = record.prd_path.resolve()
    issues_index = issues_index.resolve()
    if not prd_path.is_file() or not issues_index.is_file():
        raise RuntimeError("the published PRD checkpoint files are unavailable")

    from .issue_pack import parse_issue_index
    from .state import LoopStateWriter

    issues = {issue.number: issue for issue in parse_issue_index(issues_index)}
    writer = LoopStateWriter(issues_index)
    active_attempt = writer.active_scheduling_attempt()
    if active_attempt is None:
        return {
            "checkpoint_kind": "PRD",
            "prd_path": str(prd_path),
            "issues_index_path": str(issues_index),
            "issue_id": None,
            "next_role": "scheduler",
            "pass_number": 1,
            "summary": "PRD scheduler checkpoint persisted",
        }
    issue_id = active_attempt["issue"]
    issue = issues.get(issue_id)
    if issue is None:
        raise RuntimeError(
            f"the scheduling cursor references unknown issue {issue_id!r}"
        )
    cursor = writer.resume_issue(issue)
    return {
        "checkpoint_kind": "PRD",
        "prd_path": str(prd_path),
        "issues_index_path": str(issues_index),
        "issue_id": issue_id,
        "next_role": cursor.next_role.value,
        "pass_number": cursor.pass_number,
        "summary": (
            f"PRD checkpoint issue {issue_id} · "
            f"{cursor.next_role.value} pass {cursor.pass_number} persisted"
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())
