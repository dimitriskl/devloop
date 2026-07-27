from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable
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
    RESUME = "RESUME"
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
    APPROVAL = "APPROVAL"


@dataclass(frozen=True)
class PortableSessionInputRequest:
    kind: PortableSessionInputKind
    prompt: str = ""
    options: tuple[tuple[str, str], ...] = ()
    default_key: str = ""
    cancel_key: str | None = None


@dataclass(frozen=True)
class PortableSessionProgress:
    stage: str = ""
    completed_issues: int = 0
    total_issues: int = 0
    active_issue: str | None = None


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
    prd_path: Path | None = None
    progress: PortableSessionProgress = PortableSessionProgress()
    updated_at: float = 0.0


@dataclass(frozen=True)
class PortableSessionEvent:
    snapshot: PortableSessionSnapshot


@dataclass(frozen=True)
class PortableWorktreeLease:
    checkout: Path
    session_id: str
    owner_id: str
    process_id: int
    acquired_at: float
    heartbeat_at: float


class PortableWorktreeLeaseConflict(RuntimeError):
    def __init__(self, lease: PortableWorktreeLease) -> None:
        self.lease = lease
        super().__init__(
            "Portable worktree is already leased by session "
            f"{lease.session_id} in application {lease.owner_id}."
        )

    @property
    def session_id(self) -> str:
        return self.lease.session_id

    @property
    def owner_id(self) -> str:
        return self.lease.owner_id


class PortableSessionController(Protocol):
    def handle_intent(
        self,
        intent: PortableSessionIntent,
    ) -> PortableSessionSnapshot: ...

    def try_next_event(self) -> PortableSessionEvent | None: ...

    def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]: ...

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
_PROGRESS_REFRESH_INTERVAL_SECONDS = 1.0


class PortablePlanningSettingsRecord(Protocol):
    def to_dict(self) -> dict[str, object]: ...


class PortableCatalogSessionRecord(Protocol):
    session_id: str
    checkout: Path
    status: PortableSessionStatus
    planning_thread_id: str | None
    planning_settings: PortablePlanningSettingsRecord | None
    prd_path: Path | None
    activity_summary: str
    updated_at: float
    launch: PortableSessionLaunch


class PortableResumeCandidateRecord(Protocol):
    candidate_id: str
    checkout: Path
    prd_path: Path
    completed_issues: int
    total_issues: int
    active_issue: str | None
    active_status: str | None
    updated_at: float


class PortableSavedProjectRecord(Protocol):
    project_id: str
    checkout: Path


class PortableSessionCatalogController(Protocol):
    path: Path

    def create_session(
        self,
        launch: PortableSessionLaunch,
        planning_settings: PortablePlanningSettingsRecord | None = None,
    ) -> PortableCatalogSessionRecord: ...

    def get_session(self, session_id: str) -> PortableCatalogSessionRecord: ...

    def list_sessions(self) -> tuple[PortableCatalogSessionRecord, ...]: ...

    def list_saved_projects(self) -> tuple[PortableSavedProjectRecord, ...]: ...

    def update_session_status(
        self,
        session_id: str,
        status: PortableSessionStatus,
        *,
        activity_summary: str = "",
    ) -> None: ...

    def rollback_session_start(
        self,
        session_id: str,
        *,
        owner_id: str,
    ) -> None: ...


@dataclass
class _RunningSession:
    process: PortableWorkerProcess
    next_supervisor_sequence: int = 2
    next_worker_sequence: int = 1


class PortableSessionSupervisor:
    """Own isolated worker processes and project their protocol into session state."""

    def __init__(
        self,
        *,
        worker_launcher: WorkerLauncher | None = None,
        catalog: PortableSessionCatalogController | None = None,
        resume_candidates: Iterable[PortableResumeCandidateRecord] = (),
        resume_candidates_loader: (
            Callable[[], Iterable[PortableResumeCandidateRecord]] | None
        ) = None,
        owner_id: str | None = None,
    ) -> None:
        self._catalog = catalog
        self._owner_id = owner_id or str(uuid.uuid4())
        self._worker_launcher = worker_launcher or (
            lambda launch: _launch_portable_worker(
                launch,
                catalog_path=catalog.path if catalog is not None else None,
                owner_id=self._owner_id,
            )
        )
        self._snapshots: dict[str, PortableSessionSnapshot] = {}
        self._launches: dict[str, PortableSessionLaunch] = {}
        self._candidate_launches: dict[str, PortableSessionLaunch] = {}
        self._running: dict[str, _RunningSession] = {}
        self._last_progress_refresh: dict[str, float] = {}
        self._threads: list[Thread] = []
        self._events: Queue[PortableSessionEvent] = Queue()
        self._condition = Condition(RLock())
        self._resume_candidates_loader = resume_candidates_loader
        self._saved_projects = (
            tuple(catalog.list_saved_projects()) if catalog is not None else ()
        )
        if catalog is not None:
            for record in catalog.list_sessions():
                self._snapshots[record.session_id] = PortableSessionSnapshot(
                    session_id=record.session_id,
                    checkout=record.checkout,
                    status=record.status,
                    activity=(
                        (getattr(record, "activity_summary"),)
                        if getattr(record, "activity_summary", "")
                        else ()
                    ),
                    prd_path=record.prd_path,
                    updated_at=getattr(record, "updated_at", 0.0),
                )
                self._launches[record.session_id] = record.launch
        known_prd_paths = {
            snapshot.prd_path.resolve()
            for snapshot in self._snapshots.values()
            if snapshot.prd_path is not None
        }
        candidates = tuple(resume_candidates)
        self._unfinished_prd_paths = {
            candidate.prd_path.resolve() for candidate in candidates
        }
        if catalog is not None and resume_candidates_loader is not None:
            for snapshot in tuple(self._snapshots.values()):
                if (
                    snapshot.prd_path is not None
                    and snapshot.prd_path.resolve() not in self._unfinished_prd_paths
                    and snapshot.status
                    in {
                        PortableSessionStatus.READY,
                        PortableSessionStatus.FAILED,
                    }
                ):
                    completed = replace(
                        snapshot,
                        status=PortableSessionStatus.COMPLETED,
                        result=0,
                        input_request=None,
                    )
                    self._snapshots[completed.session_id] = completed
                    catalog.update_session_status(
                        completed.session_id,
                        PortableSessionStatus.COMPLETED,
                        activity_summary="Project workflow is no longer unfinished",
                    )
        for candidate in candidates:
            candidate_path = candidate.prd_path.resolve()
            matching_session = next(
                (
                    snapshot
                    for snapshot in self._snapshots.values()
                    if snapshot.prd_path is not None
                    and snapshot.prd_path.resolve() == candidate_path
                ),
                None,
            )
            if matching_session is not None:
                candidate_progress = _candidate_progress(candidate)
                matching_session = replace(
                    matching_session,
                    progress=candidate_progress,
                    updated_at=getattr(candidate, "updated_at", 0.0),
                )
                self._snapshots[matching_session.session_id] = matching_session
                if matching_session.status.terminal:
                    restored = replace(
                        matching_session,
                        status=PortableSessionStatus.READY,
                        result=None,
                    )
                    self._snapshots[restored.session_id] = restored
                    if self._catalog is not None:
                        self._catalog.update_session_status(
                            restored.session_id,
                            PortableSessionStatus.READY,
                            activity_summary="Unfinished project workflow",
                        )
                continue
            if (
                candidate.candidate_id in self._snapshots
                or candidate_path in known_prd_paths
            ):
                continue
            launch = PortableSessionLaunch(
                session_id=candidate.candidate_id,
                checkout=candidate.checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--prd", str(candidate.prd_path)),
            )
            self._snapshots[candidate.candidate_id] = PortableSessionSnapshot(
                session_id=candidate.candidate_id,
                checkout=candidate.checkout,
                status=PortableSessionStatus.READY,
                prd_path=candidate.prd_path,
                progress=_candidate_progress(candidate),
                updated_at=getattr(candidate, "updated_at", 0.0),
            )
            self._launches[candidate.candidate_id] = launch
            self._candidate_launches[candidate.candidate_id] = launch
            known_prd_paths.add(candidate_path)

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
            focused = self._claim_new_session(normalized_launch)
            if focused is not None:
                return focused
            return self._launch_session(
                normalized_launch,
                SupervisorMessageKind.START,
                {},
                rollback_new_session=True,
            )

    def resume_session(self, session_id: str) -> PortableSessionSnapshot:
        with self._condition:
            if session_id in self._running:
                raise ValueError(f"Portable session is already running: {session_id}")
            try:
                launch = self._launches[session_id]
            except KeyError as error:
                raise ValueError(f"Unknown portable session: {session_id}") from error
            if session_id in self._candidate_launches:
                focused = self._claim_new_session(launch)
                if focused is not None:
                    return focused
                del self._candidate_launches[session_id]
                return self._launch_session(
                    launch,
                    SupervisorMessageKind.START,
                    {},
                    rollback_new_session=True,
                )
            payload: dict[str, object] = {}
            command_kind = SupervisorMessageKind.RESUME
            if self._catalog is not None:
                record = self._catalog.get_session(session_id)
                launch = record.launch
                self._launches[session_id] = launch
                self._snapshots[session_id] = replace(
                    self._snapshots[session_id],
                    prd_path=record.prd_path,
                )
                payload["restore_catalog_session"] = True
                payload["planning_thread_id"] = record.planning_thread_id
                if record.planning_settings is not None:
                    payload["planning_settings"] = record.planning_settings.to_dict()
                if record.planning_thread_id is None:
                    command_kind = SupervisorMessageKind.START
                self._acquire_existing_session_lease(session_id)
            return self._launch_session(
                launch,
                command_kind,
                payload,
            )

    def _launch_session(
        self,
        launch: PortableSessionLaunch,
        command_kind: SupervisorMessageKind,
        command_payload: dict[str, object],
        *,
        rollback_new_session: bool = False,
    ) -> PortableSessionSnapshot:
        try:
            process = self._worker_launcher(launch)
        except BaseException:
            self._handle_launch_failure(
                launch.session_id,
                rollback_new_session=rollback_new_session,
            )
            raise
        try:
            if process.stdin is None or process.stdout is None or process.stderr is None:
                process.terminate()
                raise RuntimeError(
                    "Portable worker must redirect stdin, stdout, and stderr."
                )
            previous = self._snapshots.get(
                launch.session_id,
                PortableSessionSnapshot(
                    session_id=launch.session_id,
                    checkout=launch.checkout,
                    status=PortableSessionStatus.READY,
                ),
            )
            snapshot = replace(
                previous,
                checkout=launch.checkout,
                status=PortableSessionStatus.RUNNING,
                result=None,
                input_request=None,
                updated_at=time.time(),
            )
            self._snapshots[launch.session_id] = snapshot
            self._launches[launch.session_id] = launch
            running = _RunningSession(process)
            self._running[launch.session_id] = running
            if self._catalog is not None:
                try:
                    self._catalog.update_session_status(
                        launch.session_id,
                        PortableSessionStatus.RUNNING,
                    )
                except KeyError:
                    # Fresh sessions become durable at the worker's selected
                    # checkout boundary, not at the supervisor's launch cwd.
                    pass
            self._publish(snapshot)
            self._write_frame(
                launch.session_id,
                supervisor_frame(
                    launch.session_id,
                    1,
                    command_kind,
                    {
                        "operation": launch.operation.value,
                        "arguments": list(launch.arguments),
                        **command_payload,
                    },
                ),
            )
        except BaseException:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=1)
            self._handle_launch_failure(
                launch.session_id,
                rollback_new_session=rollback_new_session,
            )
            raise
        stdout_thread = Thread(
            target=self._read_worker_stdout,
            args=(launch.session_id, running),
            daemon=True,
            name=f"portable-session-{launch.session_id}-stdout",
        )
        stderr_thread = Thread(
            target=self._read_worker_stderr,
            args=(launch.session_id, running),
            daemon=True,
            name=f"portable-session-{launch.session_id}-stderr",
        )
        self._threads.extend((stdout_thread, stderr_thread))
        stdout_thread.start()
        stderr_thread.start()
        return snapshot

    def handle_intent(
        self,
        intent: PortableSessionIntent,
    ) -> PortableSessionSnapshot:
        if intent.kind is PortableSessionIntentKind.START:
            if intent.launch is None:
                raise ValueError("START intent requires a session launch.")
            return self.start_session(intent.launch)
        if intent.kind is PortableSessionIntentKind.RESUME:
            return self.resume_session(intent.session_id)
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
            if snapshot.status.terminal:
                raise ValueError(
                    "Portable session is terminal and cannot accept input: "
                    f"{session_id}"
                )
            running = self._running.get(session_id)
            if running is None:
                raise ValueError(
                    "Portable session is not running and cannot accept input: "
                    f"{session_id}"
                )
            if (
                snapshot.status is not PortableSessionStatus.WAITING_FOR_INPUT
                or snapshot.input_request is None
            ):
                raise ValueError(f"Portable session is not waiting for input: {session_id}")
            frame = supervisor_frame(
                session_id,
                running.next_supervisor_sequence,
                SupervisorMessageKind.USER_INPUT,
                {"value": value},
            )
            try:
                self._write_frame(session_id, frame)
            except (OSError, ValueError) as error:
                message = (
                    "Portable session worker input channel closed before input "
                    f"could be sent: {session_id}."
                )
                self._fail_session(session_id, message, running)
                raise ValueError(message) from error
            running.next_supervisor_sequence += 1
            updated = replace(
                snapshot,
                status=PortableSessionStatus.RUNNING,
                input_request=None,
                updated_at=time.time(),
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

    def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
        with self._condition:
            return tuple(self._snapshots.values())

    def list_saved_projects(self) -> tuple[PortableSavedProjectRecord, ...]:
        return self._saved_projects

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
        for thread in tuple(self._threads):
            thread.join(timeout=1)
        self._threads = [thread for thread in self._threads if thread.is_alive()]

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
            self._release_session_lease(session_id)
            snapshot = self._snapshots[session_id]
            if not snapshot.status.terminal:
                ready = replace(
                    snapshot,
                    status=PortableSessionStatus.READY,
                    input_request=None,
                    updated_at=time.time(),
                )
                self._snapshots[session_id] = ready
                self._persist_snapshot(ready)

    def _write_frame(self, session_id: str, frame: PortableProtocolFrame) -> None:
        process = self._running[session_id].process
        assert process.stdin is not None
        process.stdin.write(frame.to_json_line() + "\n")
        process.stdin.flush()

    def _read_worker_stdout(
        self,
        session_id: str,
        running: _RunningSession,
    ) -> None:
        assert running.process.stdout is not None
        try:
            for line in running.process.stdout:
                frame = PortableProtocolFrame.parse(
                    line,
                    expected_session_id=session_id,
                    expected_sequence=running.next_worker_sequence,
                )
                running.next_worker_sequence += 1
                self._apply_worker_frame(session_id, frame, running)
                if frame.kind in {
                    WorkerMessageKind.COMPLETION.value,
                    WorkerMessageKind.FAILURE.value,
                }:
                    return
            if not self.snapshot(session_id).status.terminal:
                self._fail_session(
                    session_id,
                    "Worker exited without a terminal result.",
                    running,
                )
        except (PortableProtocolError, OSError) as error:
            self._fail_session(session_id, str(error), running)
        finally:
            self._reap_worker(running)
            self._release_session_lease(session_id)

    def _read_worker_stderr(
        self,
        session_id: str,
        running: _RunningSession,
    ) -> None:
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
                    updated_at=time.time(),
                )
                self._snapshots[session_id] = updated
                self._publish(updated)

    def _apply_worker_frame(
        self,
        session_id: str,
        frame: PortableProtocolFrame,
        running: _RunningSession,
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
                context = PortableRunContext(
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
                )
                checkout = self._synchronize_catalog_checkout(
                    session_id,
                    snapshot,
                    context,
                )
                updated = replace(
                    snapshot,
                    checkout=checkout,
                    context=context,
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
                if status.terminal:
                    raise PortableProtocolError(
                        "Worker STATUS cannot claim a terminal session status."
                    )
                progress = snapshot.progress
                if any(
                    key in frame.payload
                    for key in (
                        "stage",
                        "completed_issues",
                        "total_issues",
                        "active_issue",
                    )
                ):
                    progress = PortableSessionProgress(
                        stage=_payload_text(frame, "stage"),
                        completed_issues=_payload_nonnegative_int(
                            frame,
                            "completed_issues",
                        ),
                        total_issues=_payload_nonnegative_int(
                            frame,
                            "total_issues",
                        ),
                        active_issue=_payload_optional_text(frame, "active_issue"),
                    )
                    if progress.completed_issues > progress.total_issues:
                        raise PortableProtocolError(
                            "Worker completed issue count cannot exceed its total."
                        )
                updated = replace(snapshot, status=status, progress=progress)
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
                if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                    raise PortableProtocolError(
                        "Worker completion exit_code must be an integer."
                    )
                planning_session_is_unfinished = False
                if (
                    exit_code == 0
                    and self._launches[session_id].operation
                    is PortableWorkflowOperation.PLANNING
                ):
                    planning_session_is_unfinished = (
                        self._reconcile_planning_publication(session_id)
                    )
                    snapshot = self._snapshots[session_id]
                completion_status = (
                    PortableSessionStatus.COMPLETED
                    if exit_code == 0
                    else PortableSessionStatus.FAILED
                )
                if planning_session_is_unfinished:
                    completion_status = PortableSessionStatus.READY
                updated = replace(
                    snapshot,
                    status=completion_status,
                    result=exit_code,
                    input_request=None,
                )
            elif kind is WorkerMessageKind.FAILURE:
                updated = replace(
                    snapshot,
                    status=PortableSessionStatus.FAILED,
                    result=1,
                    input_request=None,
                    diagnostics=(
                        *snapshot.diagnostics,
                        _payload_text(frame, "message"),
                    )[-100:],
                )
            if kind in {
                WorkerMessageKind.CONTEXT,
                WorkerMessageKind.ACTIVITY,
                WorkerMessageKind.SAFE_OUTPUT,
                WorkerMessageKind.STATUS,
                WorkerMessageKind.INPUT_REQUEST,
            }:
                updated = self._refresh_authoritative_progress(updated)
            updated = replace(updated, updated_at=time.time())
            self._snapshots[session_id] = updated
            self._persist_snapshot(updated)
            if kind in {
                WorkerMessageKind.COMPLETION,
                WorkerMessageKind.FAILURE,
            }:
                self._retire_running_session(session_id, running)
            self._publish(updated)
            self._condition.notify_all()

    def _synchronize_catalog_checkout(
        self,
        session_id: str,
        snapshot: PortableSessionSnapshot,
        context: PortableRunContext,
    ) -> Path:
        if self._catalog is None:
            return snapshot.checkout
        context_checkout = Path(context.implementation_worktree).expanduser().resolve()
        if context_checkout == snapshot.checkout.resolve():
            return snapshot.checkout
        from .worktree import find_git_checkout

        git_checkout = find_git_checkout(context_checkout)
        if (
            git_checkout is None
            or git_checkout.repo_root.resolve() != context_checkout
        ):
            raise PortableProtocolError(
                "Worker implementation worktree is not an exact Git checkout."
            )
        record = self._catalog.get_session(session_id)
        if record.checkout.resolve() != context_checkout:
            raise PortableProtocolError(
                "Worker implementation worktree does not match the catalog lease."
            )
        self._launches[session_id] = record.launch
        self._saved_projects = tuple(self._catalog.list_saved_projects())
        return record.checkout

    def _reconcile_planning_publication(self, session_id: str) -> bool:
        if self._catalog is None:
            return False
        try:
            record = self._catalog.get_session(session_id)
        except KeyError:
            return True
        if record.prd_path is None:
            return True
        prd_path = record.prd_path.resolve()
        if self._resume_candidates_loader is not None:
            candidates = tuple(self._resume_candidates_loader())
            self._unfinished_prd_paths = {
                candidate.prd_path.resolve() for candidate in candidates
            }
        self._snapshots[session_id] = replace(
            self._snapshots[session_id],
            prd_path=record.prd_path,
        )
        duplicate_candidate_ids = tuple(
            candidate_id
            for candidate_id in self._candidate_launches
            if candidate_id != session_id
            and self._snapshots[candidate_id].prd_path is not None
            and self._snapshots[candidate_id].prd_path.resolve() == prd_path
        )
        for candidate_id in duplicate_candidate_ids:
            self._candidate_launches.pop(candidate_id, None)
            self._launches.pop(candidate_id, None)
            self._snapshots.pop(candidate_id, None)
        return prd_path in self._unfinished_prd_paths

    def _refresh_authoritative_progress(
        self,
        snapshot: PortableSessionSnapshot,
    ) -> PortableSessionSnapshot:
        if self._resume_candidates_loader is None or snapshot.prd_path is None:
            return snapshot
        now = time.monotonic()
        last_refresh = self._last_progress_refresh.get(snapshot.session_id)
        if (
            last_refresh is not None
            and now - last_refresh < _PROGRESS_REFRESH_INTERVAL_SECONDS
        ):
            return snapshot
        self._last_progress_refresh[snapshot.session_id] = now
        prd_path = snapshot.prd_path.resolve()
        try:
            matching = next(
                (
                    candidate
                    for candidate in self._resume_candidates_loader()
                    if candidate.prd_path.resolve() == prd_path
                ),
                None,
            )
        except (OSError, RuntimeError, ValueError):
            return snapshot
        if matching is None:
            return snapshot
        return replace(
            snapshot,
            progress=_candidate_progress(matching),
            updated_at=getattr(matching, "updated_at", snapshot.updated_at),
        )

    def _fail_session(
        self,
        session_id: str,
        message: str,
        running: _RunningSession,
    ) -> None:
        with self._condition:
            snapshot = self._snapshots[session_id]
            if snapshot.status.terminal:
                return
            updated = replace(
                snapshot,
                status=PortableSessionStatus.FAILED,
                result=1,
                input_request=None,
                diagnostics=(*snapshot.diagnostics, message)[-100:],
                updated_at=time.time(),
            )
            self._snapshots[session_id] = updated
            self._persist_snapshot(updated)
            self._retire_running_session(session_id, running)
            self._publish(updated)
            self._condition.notify_all()
            if running.process.poll() is None:
                running.process.terminate()

    def _retire_running_session(
        self,
        session_id: str,
        running: _RunningSession,
    ) -> None:
        if self._running.get(session_id) is running:
            self._running.pop(session_id)

    @staticmethod
    def _reap_worker(running: _RunningSession) -> None:
        try:
            running.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            running.process.terminate()
            running.process.wait(timeout=1)
        finally:
            for stream in (
                running.process.stdin,
                running.process.stdout,
                running.process.stderr,
            ):
                if stream is not None:
                    stream.close()

    def _publish(self, snapshot: PortableSessionSnapshot) -> None:
        self._events.put(PortableSessionEvent(snapshot))

    def _persist_snapshot(self, snapshot: PortableSessionSnapshot) -> None:
        if self._catalog is None:
            return
        summary = snapshot.activity[-1] if snapshot.activity else ""
        try:
            self._catalog.update_session_status(
                snapshot.session_id,
                snapshot.status,
                activity_summary=summary,
            )
        except (KeyError, RuntimeError) as error:
            current = self._snapshots[snapshot.session_id]
            self._snapshots[snapshot.session_id] = replace(
                current,
                diagnostics=(
                    *current.diagnostics,
                    f"Portable Session Catalog update failed: {error}",
                )[-100:],
            )

    def _claim_new_session(
        self,
        launch: PortableSessionLaunch,
    ) -> PortableSessionSnapshot | None:
        if self._catalog is None:
            return None
        claim = getattr(self._catalog, "create_session_with_lease", None)
        if not callable(claim):
            return None
        try:
            claim(launch, owner_id=self._owner_id)
        except PortableWorktreeLeaseConflict as error:
            if error.owner_id == self._owner_id:
                focused = self._snapshots.get(error.session_id)
                if focused is not None:
                    return focused
            raise
        self._saved_projects = tuple(self._catalog.list_saved_projects())
        return None

    def _acquire_existing_session_lease(self, session_id: str) -> None:
        if self._catalog is None:
            return
        acquire = getattr(self._catalog, "acquire_session_lease", None)
        if callable(acquire):
            acquire(session_id, owner_id=self._owner_id)

    def _release_session_lease(self, session_id: str) -> None:
        if self._catalog is None:
            return
        release = getattr(self._catalog, "release_worktree_lease", None)
        if callable(release):
            release(session_id, owner_id=self._owner_id)

    def _mark_launch_failed(self, session_id: str) -> None:
        if self._catalog is None:
            return
        try:
            self._catalog.update_session_status(
                session_id,
                PortableSessionStatus.FAILED,
                activity_summary="Worker launch failed",
            )
        except KeyError:
            return

    def _handle_launch_failure(
        self,
        session_id: str,
        *,
        rollback_new_session: bool,
    ) -> None:
        if rollback_new_session and self._catalog is not None:
            rollback = getattr(self._catalog, "rollback_session_start", None)
            if callable(rollback):
                rollback(session_id, owner_id=self._owner_id)
                self._snapshots.pop(session_id, None)
                self._launches.pop(session_id, None)
                self._running.pop(session_id, None)
                self._saved_projects = tuple(self._catalog.list_saved_projects())
                return
        self._mark_launch_failed(session_id)
        self._release_session_lease(session_id)


def _payload_text(frame: PortableProtocolFrame, key: str) -> str:
    value = frame.payload.get(key, "")
    if not isinstance(value, str):
        raise PortableProtocolError(f"Worker payload {key!r} must be text.")
    return value


def _candidate_progress(
    candidate: PortableResumeCandidateRecord,
) -> PortableSessionProgress:
    return PortableSessionProgress(
        stage=getattr(candidate, "active_status", None) or "",
        completed_issues=getattr(candidate, "completed_issues", 0),
        total_issues=getattr(candidate, "total_issues", 0),
        active_issue=getattr(candidate, "active_issue", None),
    )


def _payload_optional_text(
    frame: PortableProtocolFrame,
    key: str,
) -> str | None:
    value = frame.payload.get(key)
    if value is not None and not isinstance(value, str):
        raise PortableProtocolError(f"Worker payload {key!r} must be text or null.")
    return value


def _payload_nonnegative_int(
    frame: PortableProtocolFrame,
    key: str,
) -> int:
    value = frame.payload.get(key)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise PortableProtocolError(
            f"Worker payload {key!r} must be a non-negative integer."
        )
    return value


def _launch_portable_worker(
    launch: PortableSessionLaunch,
    *,
    catalog_path: Path | None = None,
    owner_id: str | None = None,
) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment["DEVLOOP_UI_MODE"] = "application"
    environment["DEVLOOP_PORTABLE_SESSION_ID"] = launch.session_id
    if catalog_path is not None:
        environment["DEVLOOP_PORTABLE_SESSION_CATALOG"] = str(catalog_path)
    if owner_id is not None:
        environment["DEVLOOP_PORTABLE_SESSION_OWNER_ID"] = owner_id
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
