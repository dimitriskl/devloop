from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from queue import Empty, Queue
from threading import Condition, RLock, Thread
from typing import IO, Protocol

from .portable_protocol import (
    PortableProtocolError,
    PortableProtocolFrame,
    SupervisorMessageKind,
    WorkerMessageKind,
    supervisor_frame,
)
from .portable_runtime import PortableRunContext


class PortableSessionStatus(str, Enum):
    READY = "READY"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    INTERRUPTED = "INTERRUPTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNAVAILABLE = "UNAVAILABLE"

    @property
    def terminal(self) -> bool:
        return self in {
            PortableSessionStatus.COMPLETED,
            PortableSessionStatus.FAILED,
            PortableSessionStatus.CANCELLED,
        }


class PortableWorkflowOperation(str, Enum):
    PLANNING = "PLANNING"
    DELIVERY = "DELIVERY"


class PortableSessionIntentKind(str, Enum):
    START = "START"
    PROVIDE_INPUT = "PROVIDE_INPUT"


@dataclass(frozen=True)
class PortableSessionLaunch:
    session_id: str
    checkout: Path
    operation: PortableWorkflowOperation
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class PortableSessionIntent:
    kind: PortableSessionIntentKind
    launch: PortableSessionLaunch | None = None
    session_id: str = ""
    value: str = ""


class PortableSessionInputKind(str, Enum):
    CHOICE = "CHOICE"
    TEXT = "TEXT"


@dataclass(frozen=True)
class PortableSessionInputRequest:
    kind: PortableSessionInputKind
    prompt: str = ""
    options: tuple[tuple[str, str], ...] = ()
    default_key: str = ""
    cancel_key: str | None = None


@dataclass(frozen=True)
class PortableSessionSnapshot:
    session_id: str
    checkout: Path
    status: PortableSessionStatus
    context: PortableRunContext | None = None
    activity: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    result: int | None = None
    input_request: PortableSessionInputRequest | None = None


@dataclass(frozen=True)
class PortableSessionEvent:
    snapshot: PortableSessionSnapshot


class PortableSessionController(Protocol):
    def handle_intent(
        self,
        intent: PortableSessionIntent,
    ) -> PortableSessionSnapshot: ...

    def try_next_event(self) -> PortableSessionEvent | None: ...

    def shutdown(self) -> None: ...


class PortableWorkerProcess(Protocol):
    stdin: IO[str] | None
    stdout: IO[str] | None
    stderr: IO[str] | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


WorkerLauncher = Callable[[PortableSessionLaunch], PortableWorkerProcess]
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


@dataclass
class _RunningSession:
    process: PortableWorkerProcess
    next_supervisor_sequence: int = 2
    next_worker_sequence: int = 1


class PortableSessionSupervisor:
    """Own isolated worker processes and project their protocol into session state."""

    def __init__(self, *, worker_launcher: WorkerLauncher | None = None) -> None:
        self._worker_launcher = worker_launcher or _launch_portable_worker
        self._snapshots: dict[str, PortableSessionSnapshot] = {}
        self._running: dict[str, _RunningSession] = {}
        self._events: Queue[PortableSessionEvent] = Queue()
        self._condition = Condition(RLock())

    def start_session(
        self,
        launch: PortableSessionLaunch,
    ) -> PortableSessionSnapshot:
        if _SESSION_ID_PATTERN.fullmatch(launch.session_id) is None:
            raise ValueError(
                "Portable session identity must contain 1-128 letters, digits, "
                "periods, underscores, or hyphens."
            )
        checkout = launch.checkout.resolve()
        if not checkout.is_dir():
            raise ValueError(f"Portable session checkout does not exist: {checkout}")
        with self._condition:
            if launch.session_id in self._snapshots:
                raise ValueError(
                    f"Portable session already exists: {launch.session_id}"
                )
            normalized_launch = replace(launch, checkout=checkout)
            process = self._worker_launcher(normalized_launch)
            if process.stdin is None or process.stdout is None or process.stderr is None:
                process.terminate()
                raise RuntimeError(
                    "Portable worker must redirect stdin, stdout, and stderr."
                )
            snapshot = PortableSessionSnapshot(
                session_id=launch.session_id,
                checkout=checkout,
                status=PortableSessionStatus.RUNNING,
            )
            self._snapshots[launch.session_id] = snapshot
            self._running[launch.session_id] = _RunningSession(process)
            self._publish(snapshot)
            self._write_frame(
                launch.session_id,
                supervisor_frame(
                    launch.session_id,
                    1,
                    SupervisorMessageKind.START,
                    {
                        "operation": launch.operation.value,
                        "arguments": list(launch.arguments),
                    },
                ),
            )
        Thread(
            target=self._read_worker_stdout,
            args=(launch.session_id,),
            daemon=True,
            name=f"portable-session-{launch.session_id}-stdout",
        ).start()
        Thread(
            target=self._read_worker_stderr,
            args=(launch.session_id,),
            daemon=True,
            name=f"portable-session-{launch.session_id}-stderr",
        ).start()
        return snapshot

    def handle_intent(
        self,
        intent: PortableSessionIntent,
    ) -> PortableSessionSnapshot:
        if intent.kind is PortableSessionIntentKind.START:
            if intent.launch is None:
                raise ValueError("START intent requires a session launch.")
            return self.start_session(intent.launch)
        if intent.kind is PortableSessionIntentKind.PROVIDE_INPUT:
            return self.provide_input(intent.session_id, intent.value)
        raise ValueError(f"Unsupported portable session intent: {intent.kind}")

    def provide_input(
        self,
        session_id: str,
        value: str,
    ) -> PortableSessionSnapshot:
        with self._condition:
            snapshot = self.snapshot(session_id)
            if snapshot.input_request is None:
                raise ValueError(f"Portable session is not waiting for input: {session_id}")
            running = self._running[session_id]
            frame = supervisor_frame(
                session_id,
                running.next_supervisor_sequence,
                SupervisorMessageKind.USER_INPUT,
                {"value": value},
            )
            running.next_supervisor_sequence += 1
            self._write_frame(session_id, frame)
            updated = replace(
                snapshot,
                status=PortableSessionStatus.RUNNING,
                input_request=None,
            )
            self._snapshots[session_id] = updated
            self._publish(updated)
            return updated

    def snapshot(self, session_id: str) -> PortableSessionSnapshot:
        with self._condition:
            try:
                return self._snapshots[session_id]
            except KeyError as error:
                raise ValueError(f"Unknown portable session: {session_id}") from error

    def try_next_event(self) -> PortableSessionEvent | None:
        try:
            return self._events.get_nowait()
        except Empty:
            return None

    def wait_for_terminal(
        self,
        session_id: str,
        *,
        timeout: float | None = None,
    ) -> PortableSessionSnapshot:
        with self._condition:
            completed = self._condition.wait_for(
                lambda: self.snapshot(session_id).status.terminal,
                timeout=timeout,
            )
            if not completed:
                raise TimeoutError(f"Portable session did not finish: {session_id}")
            return self.snapshot(session_id)

    def shutdown(self) -> None:
        with self._condition:
            session_ids = tuple(self._running)
        for session_id in session_ids:
            self._shutdown_session(session_id)

    def _shutdown_session(self, session_id: str) -> None:
        with self._condition:
            running = self._running.get(session_id)
            if running is None:
                return
            if running.process.poll() is None:
                frame = supervisor_frame(
                    session_id,
                    running.next_supervisor_sequence,
                    SupervisorMessageKind.SHUTDOWN,
                )
                running.next_supervisor_sequence += 1
                try:
                    self._write_frame(session_id, frame)
                    running.process.wait(timeout=1)
                except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                    running.process.terminate()
            try:
                running.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                running.process.terminate()
                running.process.wait(timeout=1)
            for stream in (
                running.process.stdin,
                running.process.stdout,
                running.process.stderr,
            ):
                if stream is not None:
                    stream.close()
            self._running.pop(session_id, None)

    def _write_frame(self, session_id: str, frame: PortableProtocolFrame) -> None:
        process = self._running[session_id].process
        assert process.stdin is not None
        process.stdin.write(frame.to_json_line() + "\n")
        process.stdin.flush()

    def _read_worker_stdout(self, session_id: str) -> None:
        running = self._running[session_id]
        assert running.process.stdout is not None
        try:
            for line in running.process.stdout:
                frame = PortableProtocolFrame.parse(
                    line,
                    expected_session_id=session_id,
                    expected_sequence=running.next_worker_sequence,
                )
                running.next_worker_sequence += 1
                self._apply_worker_frame(session_id, frame)
                if self.snapshot(session_id).status.terminal:
                    return
            if not self.snapshot(session_id).status.terminal:
                self._fail_session(session_id, "Worker exited without a terminal result.")
        except (PortableProtocolError, OSError) as error:
            self._fail_session(session_id, str(error))

    def _read_worker_stderr(self, session_id: str) -> None:
        running = self._running[session_id]
        assert running.process.stderr is not None
        for line in running.process.stderr:
            diagnostic = line.rstrip("\r\n")
            if not diagnostic:
                continue
            with self._condition:
                snapshot = self._snapshots[session_id]
                updated = replace(
                    snapshot,
                    diagnostics=(*snapshot.diagnostics, diagnostic)[-100:],
                )
                self._snapshots[session_id] = updated
                self._publish(updated)

    def _apply_worker_frame(
        self,
        session_id: str,
        frame: PortableProtocolFrame,
    ) -> None:
        try:
            kind = WorkerMessageKind(frame.kind)
        except ValueError as error:
            raise PortableProtocolError(
                f"Unsupported worker message kind: {frame.kind!r}."
            ) from error
        with self._condition:
            snapshot = self._snapshots[session_id]
            if kind is WorkerMessageKind.HELLO:
                updated = snapshot
            elif kind is WorkerMessageKind.CONTEXT:
                updated = replace(
                    snapshot,
                    context=PortableRunContext(
                        project_root=_payload_text(frame, "project_root"),
                        implementation_branch=_payload_text(
                            frame,
                            "implementation_branch",
                        ),
                        implementation_worktree=_payload_text(
                            frame,
                            "implementation_worktree",
                        ),
                        prd_path=_payload_text(frame, "prd_path"),
                    ),
                )
            elif kind is WorkerMessageKind.ACTIVITY:
                updated = replace(
                    snapshot,
                    activity=(
                        *snapshot.activity,
                        _payload_text(frame, "message"),
                    )[-100:],
                )
            elif kind is WorkerMessageKind.SAFE_OUTPUT:
                updated = replace(
                    snapshot,
                    activity=(
                        *snapshot.activity,
                        _payload_text(frame, "content"),
                    )[-100:],
                )
            elif kind is WorkerMessageKind.STATUS:
                try:
                    status = PortableSessionStatus(_payload_text(frame, "status"))
                except ValueError as error:
                    raise PortableProtocolError(
                        "Worker sent an invalid session status."
                    ) from error
                updated = replace(snapshot, status=status)
            elif kind is WorkerMessageKind.INPUT_REQUEST:
                try:
                    request_kind = PortableSessionInputKind(
                        _payload_text(frame, "request_kind")
                    )
                except ValueError as error:
                    raise PortableProtocolError(
                        "Worker sent an invalid input request kind."
                    ) from error
                options_value = frame.payload.get("options", [])
                if not isinstance(options_value, list) or not all(
                    isinstance(option, list)
                    and len(option) == 2
                    and all(isinstance(value, str) for value in option)
                    for option in options_value
                ):
                    raise PortableProtocolError(
                        "Worker input request options must contain text pairs."
                    )
                cancel_key = frame.payload.get("cancel_key")
                if cancel_key is not None and not isinstance(cancel_key, str):
                    raise PortableProtocolError(
                        "Worker input request cancel_key must be text or null."
                    )
                updated = replace(
                    snapshot,
                    status=PortableSessionStatus.WAITING_FOR_INPUT,
                    input_request=PortableSessionInputRequest(
                        kind=request_kind,
                        prompt=_payload_text(frame, "prompt"),
                        options=tuple(
                            (option[0], option[1]) for option in options_value
                        ),
                        default_key=_payload_text(frame, "default_key"),
                        cancel_key=cancel_key,
                    ),
                )
            elif kind is WorkerMessageKind.COMPLETION:
                exit_code = frame.payload.get("exit_code")
                if not isinstance(exit_code, int):
                    raise PortableProtocolError(
                        "Worker completion exit_code must be an integer."
                    )
                updated = replace(
                    snapshot,
                    status=(
                        PortableSessionStatus.COMPLETED
                        if exit_code == 0
                        else PortableSessionStatus.FAILED
                    ),
                    result=exit_code,
                )
            elif kind is WorkerMessageKind.FAILURE:
                updated = replace(
                    snapshot,
                    status=PortableSessionStatus.FAILED,
                    result=1,
                    diagnostics=(
                        *snapshot.diagnostics,
                        _payload_text(frame, "message"),
                    )[-100:],
                )
            self._snapshots[session_id] = updated
            self._publish(updated)
            self._condition.notify_all()

    def _fail_session(self, session_id: str, message: str) -> None:
        with self._condition:
            snapshot = self._snapshots[session_id]
            if snapshot.status.terminal:
                return
            updated = replace(
                snapshot,
                status=PortableSessionStatus.FAILED,
                result=1,
                diagnostics=(*snapshot.diagnostics, message)[-100:],
            )
            self._snapshots[session_id] = updated
            self._publish(updated)
            self._condition.notify_all()
            running = self._running.get(session_id)
            if running is not None and running.process.poll() is None:
                running.process.terminate()

    def _publish(self, snapshot: PortableSessionSnapshot) -> None:
        self._events.put(PortableSessionEvent(snapshot))


def _payload_text(frame: PortableProtocolFrame, key: str) -> str:
    value = frame.payload.get(key, "")
    if not isinstance(value, str):
        raise PortableProtocolError(f"Worker payload {key!r} must be text.")
    return value


def _launch_portable_worker(
    launch: PortableSessionLaunch,
) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment["DEVLOOP_UI_MODE"] = "application"
    return subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "devloop.portable_worker",
            "--session-id",
            launch.session_id,
        ],
        cwd=launch.checkout,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
