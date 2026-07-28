from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from queue import Empty, Queue
from threading import Condition, Event as ThreadEvent, RLock, Thread
from typing import IO, Protocol

from .portable_protocol import (
    PortableProtocolError,
    PortableProtocolFrame,
    SupervisorMessageKind,
    WorkerMessageKind,
    supervisor_frame,
)
from .portable_runtime import PortableRunContext
from .subprocess_utils import (
    ProcessTreeState,
    ProcessTerminationResult,
    launch_process_tree,
    register_process_tree,
    terminate_process,
)


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
    PAUSE = "PAUSE"
    FORCE_STOP = "FORCE_STOP"
    CANCEL = "CANCEL"


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
    request_id: str = ""
    request_generation: int = 0


class PortableSessionInputKind(str, Enum):
    CHOICE = "CHOICE"
    TEXT = "TEXT"
    APPROVAL = "APPROVAL"


@dataclass(frozen=True)
class PortableSessionInputRequest:
    kind: PortableSessionInputKind
    request_id: str = ""
    generation: int = 0
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

    def pause_session(self, session_id: str) -> PortableSessionSnapshot: ...

    def force_stop_session(self, session_id: str) -> PortableSessionSnapshot: ...

    def cancel_session(self, session_id: str) -> PortableSessionSnapshot: ...

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
_CAPACITY_REFRESH_INTERVAL_SECONDS = 0.05
_COOPERATIVE_PAUSE_TIMEOUT_SECONDS = 2.0
_TERMINATION_ACK_TIMEOUT_SECONDS = 8.0
_CLEANUP_RETRY_SECONDS = 0.5
_CLEANUP_REAPER_MAX_ATTEMPTS = 3


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
    active_stage: str | None
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

    def get_concurrency_limit(self) -> int: ...

    def set_concurrency_limit(self, limit: int) -> None: ...

    def request_execution_capacity(
        self,
        session_id: str,
        *,
        owner_id: str,
        process_id: int | None = None,
    ) -> bool: ...

    def enqueue_execution_capacity(
        self,
        session_id: str,
        *,
        owner_id: str,
        process_id: int | None = None,
    ) -> None: ...

    def owns_execution_capacity(
        self,
        session_id: str,
        *,
        owner_id: str,
        process_id: int | None = None,
    ) -> bool: ...

    def release_execution_capacity(
        self,
        session_id: str,
        *,
        owner_id: str,
        status: PortableSessionStatus,
        activity_summary: str = "",
    ) -> bool: ...


@dataclass
class _CleanupOwnership:
    state: ProcessTreeState = ProcessTreeState.RUNNING
    retry_at: float = 0.0

    @property
    def confirmed(self) -> bool:
        return self.state is ProcessTreeState.STOPPED

    def record(self, result: ProcessTerminationResult) -> None:
        self.state = result.state
        self.retry_at = (
            0.0
            if self.confirmed
            else time.monotonic() + _CLEANUP_RETRY_SECONDS
        )


@dataclass
class _RunningSession:
    process: PortableWorkerProcess
    generation: int
    next_supervisor_sequence: int = 2
    next_worker_sequence: int = 1
    checkpoint_summary: str | None = None
    termination_ack: bool | None = None
    termination_detail: str = ""
    cleanup: _CleanupOwnership = field(default_factory=_CleanupOwnership)
    stop_requested: bool = False
    pending_lifecycle: _LifecycleCommandIdentity | None = None
    launch_failure_rollback_new_session: bool | None = None


@dataclass(frozen=True)
class _LifecycleCommandIdentity:
    action: SupervisorMessageKind
    worker_generation: int
    request_id: str

    @property
    def acknowledgement_kinds(self) -> frozenset[WorkerMessageKind]:
        if self.action is SupervisorMessageKind.PAUSE:
            return frozenset(
                {
                    WorkerMessageKind.CHECKPOINT,
                    WorkerMessageKind.CHECKPOINT_FAILURE,
                }
            )
        return frozenset({WorkerMessageKind.TERMINATION})

    def matches(self, frame: PortableProtocolFrame) -> bool:
        return (
            frame.payload.get("action") == self.action.value
            and frame.payload.get("worker_generation") == self.worker_generation
            and frame.payload.get("request_id") == self.request_id
        )


@dataclass(frozen=True)
class _QueuedSession:
    launch: PortableSessionLaunch
    command_kind: SupervisorMessageKind
    command_payload: dict[str, object]
    rollback_new_session: bool = False


@dataclass(frozen=True)
class _QueuedInput:
    value: str
    request_id: str
    request_generation: int


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
        self._queued: dict[str, _QueuedSession | _QueuedInput] = {}
        self._owned_session_ids: set[str] = set()
        self._next_worker_generation = 1
        self._last_progress_refresh: dict[str, float] = {}
        self._live_stage_session_ids: set[str] = set()
        self._threads: list[Thread] = []
        self._events: Queue[PortableSessionEvent] = Queue()
        self._condition = Condition(RLock())
        self._scheduler_stop = ThreadEvent()
        self._scheduler_thread: Thread | None = None
        self._cleanup_reaper_thread: Thread | None = None
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
        if self._catalog is not None and callable(
            getattr(self._catalog, "request_execution_capacity", None)
        ):
            self._scheduler_thread = Thread(
                target=self._run_capacity_scheduler,
                daemon=True,
                name=f"portable-session-capacity-{self._owner_id}",
            )
            self._scheduler_thread.start()

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
            return self._schedule_session(
                normalized_launch,
                SupervisorMessageKind.START,
                {},
                rollback_new_session=True,
            )

    def resume_session(self, session_id: str) -> PortableSessionSnapshot:
        with self._condition:
            if session_id in self._running:
                snapshot = self._snapshots.get(session_id)
                if (
                    snapshot is None
                    or snapshot.status
                    in {
                        PortableSessionStatus.QUEUED,
                        PortableSessionStatus.RUNNING,
                        PortableSessionStatus.WAITING_FOR_INPUT,
                        PortableSessionStatus.PAUSING,
                    }
                    or not self._condition.wait_for(
                        lambda: session_id not in self._running,
                        timeout=_COOPERATIVE_PAUSE_TIMEOUT_SECONDS,
                    )
                ):
                    raise ValueError(
                        f"Portable session is already running: {session_id}"
                    )
            try:
                launch = self._launches[session_id]
            except KeyError as error:
                raise ValueError(f"Unknown portable session: {session_id}") from error
            if session_id in self._candidate_launches:
                focused = self._claim_new_session(launch)
                if focused is not None:
                    return focused
                del self._candidate_launches[session_id]
                return self._schedule_session(
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
                if record.status.terminal:
                    self._catalog.update_session_status(
                        session_id,
                        PortableSessionStatus.READY,
                        activity_summary="Explicit resume requested",
                    )
            return self._schedule_session(
                launch,
                command_kind,
                payload,
            )

    def _schedule_session(
        self,
        launch: PortableSessionLaunch,
        command_kind: SupervisorMessageKind,
        command_payload: dict[str, object],
        *,
        rollback_new_session: bool = False,
    ) -> PortableSessionSnapshot:
        request_capacity = (
            getattr(self._catalog, "request_execution_capacity", None)
            if self._catalog is not None
            else None
        )
        if not callable(request_capacity):
            return self._launch_session(
                launch,
                command_kind,
                command_payload,
                rollback_new_session=rollback_new_session,
            )
        if request_capacity(launch.session_id, owner_id=self._owner_id):
            return self._launch_session(
                launch,
                command_kind,
                command_payload,
                rollback_new_session=rollback_new_session,
            )
        previous = self._snapshots.get(
            launch.session_id,
            PortableSessionSnapshot(
                session_id=launch.session_id,
                checkout=launch.checkout,
                status=PortableSessionStatus.READY,
            ),
        )
        queued = replace(
            previous,
            checkout=launch.checkout,
            status=PortableSessionStatus.QUEUED,
            result=None,
            input_request=None,
            updated_at=time.time(),
        )
        self._snapshots[launch.session_id] = queued
        self._launches[launch.session_id] = launch
        self._queued[launch.session_id] = _QueuedSession(
            launch=launch,
            command_kind=command_kind,
            command_payload=dict(command_payload),
            rollback_new_session=rollback_new_session,
        )
        self._publish(queued)
        self._condition.notify_all()
        return queued

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
        running = _RunningSession(
            process,
            generation=self._next_worker_generation,
        )
        self._next_worker_generation += 1
        self._running[launch.session_id] = running
        try:
            register_process_tree(process)
            if process.stdin is None or process.stdout is None or process.stderr is None:
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
        except BaseException as error:
            self._handle_post_launch_failure(
                launch,
                running,
                error,
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
            return self.provide_input(
                intent.session_id,
                intent.value,
                request_id=intent.request_id,
                request_generation=intent.request_generation,
            )
        if intent.kind is PortableSessionIntentKind.PAUSE:
            return self.pause_session(intent.session_id)
        if intent.kind is PortableSessionIntentKind.FORCE_STOP:
            return self.force_stop_session(intent.session_id)
        if intent.kind is PortableSessionIntentKind.CANCEL:
            return self.cancel_session(intent.session_id)
        raise ValueError(f"Unsupported portable session intent: {intent.kind}")

    def pause_session(self, session_id: str) -> PortableSessionSnapshot:
        with self._condition:
            snapshot = self.snapshot(session_id)
            if snapshot.status in {
                PortableSessionStatus.PAUSING,
                PortableSessionStatus.PAUSED,
            }:
                return snapshot
            if snapshot.status.terminal:
                raise ValueError(
                    f"Portable session is terminal and cannot be paused: {session_id}"
                )
            queued = self._queued.pop(session_id, None)
            running = self._running.get(session_id)
            if running is None:
                if queued is None:
                    raise ValueError(
                        f"Portable session is not active and cannot be paused: {session_id}"
                    )
                paused = replace(
                    snapshot,
                    status=PortableSessionStatus.PAUSED,
                    input_request=None,
                    updated_at=time.time(),
                )
                self._snapshots[session_id] = paused
                self._release_execution_capacity(paused)
                self._release_session_lease(session_id)
                self._publish(paused)
                self._condition.notify_all()
                return paused
            pausing = replace(
                snapshot,
                status=PortableSessionStatus.PAUSING,
                input_request=None,
                updated_at=time.time(),
            )
            self._snapshots[session_id] = pausing
            self._persist_snapshot(pausing)
            self._publish(pausing)
            command_identity = _LifecycleCommandIdentity(
                action=SupervisorMessageKind.PAUSE,
                worker_generation=running.generation,
                request_id=str(uuid.uuid4()),
            )
            running.pending_lifecycle = command_identity
            frame = supervisor_frame(
                session_id,
                running.next_supervisor_sequence,
                SupervisorMessageKind.PAUSE,
                {
                    "action": command_identity.action.value,
                    "worker_generation": command_identity.worker_generation,
                    "request_id": command_identity.request_id,
                },
            )
            try:
                self._write_frame(session_id, frame)
            except (BrokenPipeError, OSError) as error:
                self._fail_session(
                    session_id,
                    f"Portable session worker could not accept Pause: {error}",
                    running,
                )
                return self._snapshots[session_id]
            running.next_supervisor_sequence += 1
            self._condition.notify_all()
            return pausing

    def force_stop_session(self, session_id: str) -> PortableSessionSnapshot:
        return self._terminate_session(
            session_id,
            command_kind=SupervisorMessageKind.FORCE_STOP,
            status=PortableSessionStatus.INTERRUPTED,
            activity="Force Stop preserved partial work at the last durable checkpoint",
        )

    def cancel_session(self, session_id: str) -> PortableSessionSnapshot:
        return self._terminate_session(
            session_id,
            command_kind=SupervisorMessageKind.CANCEL,
            status=PortableSessionStatus.CANCELLED,
            activity="Session cancelled by explicit user action",
        )

    def _terminate_session(
        self,
        session_id: str,
        *,
        command_kind: SupervisorMessageKind,
        status: PortableSessionStatus,
        activity: str,
    ) -> PortableSessionSnapshot:
        with self._condition:
            snapshot = self.snapshot(session_id)
            if snapshot.status.terminal:
                return snapshot
            queued = self._queued.pop(session_id, None)
            running = self._running.get(session_id)
            if queued is None and running is None:
                if (
                    command_kind is SupervisorMessageKind.FORCE_STOP
                    and snapshot.status is PortableSessionStatus.INTERRUPTED
                ):
                    return snapshot
                if snapshot.status in {
                    PortableSessionStatus.PAUSED,
                    PortableSessionStatus.INTERRUPTED,
                }:
                    cancelled = replace(
                        snapshot,
                        status=PortableSessionStatus.CANCELLED,
                        result=130,
                        input_request=None,
                        activity=(*snapshot.activity, activity)[-100:],
                        updated_at=time.time(),
                    )
                    self._snapshots[session_id] = cancelled
                    self._persist_snapshot(cancelled)
                    self._release_session_lease(session_id)
                    self._publish(cancelled)
                    self._condition.notify_all()
                    return cancelled
                raise ValueError(
                    f"Portable session is not active and cannot be stopped: {session_id}"
                )
            if running is not None and running.process.poll() is None:
                running.stop_requested = True
                command_identity = _LifecycleCommandIdentity(
                    action=command_kind,
                    worker_generation=running.generation,
                    request_id=str(uuid.uuid4()),
                )
                running.pending_lifecycle = command_identity
                frame = supervisor_frame(
                    session_id,
                    running.next_supervisor_sequence,
                    command_kind,
                    {
                        "action": command_identity.action.value,
                        "worker_generation": command_identity.worker_generation,
                        "request_id": command_identity.request_id,
                    },
                )
                running.next_supervisor_sequence += 1
                try:
                    process_input = running.process.stdin
                    if process_input is not None:
                        process_input.write(frame.to_json_line() + "\n")
                        process_input.flush()
                except (BrokenPipeError, OSError):
                    pass
                self._condition.wait_for(
                    lambda: (
                        running.termination_ack is not None
                        or running.process.poll() is not None
                    ),
                    timeout=_TERMINATION_ACK_TIMEOUT_SECONDS,
                )
                if running.termination_ack is True:
                    try:
                        running.process.wait(
                            timeout=_COOPERATIVE_PAUSE_TIMEOUT_SECONDS
                        )
                    except subprocess.TimeoutExpired:
                        termination = terminate_process(
                            running.process  # type: ignore[arg-type]
                        )
                    else:
                        termination = terminate_process(
                            running.process  # type: ignore[arg-type]
                        )
                    running.cleanup.record(termination)
                else:
                    termination = terminate_process(
                        running.process  # type: ignore[arg-type]
                    )
                    running.cleanup.record(termination)
                if not running.cleanup.confirmed:
                    snapshot = self.snapshot(session_id)
                    interrupted = replace(
                        snapshot,
                        status=PortableSessionStatus.INTERRUPTED,
                        result=130,
                        input_request=None,
                        activity=(*snapshot.activity, activity)[-100:],
                        diagnostics=(
                            *snapshot.diagnostics,
                            running.termination_detail or termination.detail,
                        )[-100:],
                        updated_at=time.time(),
                    )
                    self._snapshots[session_id] = interrupted
                    self._persist_snapshot(interrupted)
                    self._publish(interrupted)
                    self._condition.notify_all()
                    return interrupted
            elif running is not None and not running.cleanup.confirmed:
                termination = terminate_process(
                    running.process  # type: ignore[arg-type]
                )
                running.cleanup.record(termination)
                if not running.cleanup.confirmed:
                    interrupted = replace(
                        snapshot,
                        status=PortableSessionStatus.INTERRUPTED,
                        result=130,
                        input_request=None,
                        activity=(*snapshot.activity, activity)[-100:],
                        diagnostics=(
                            *snapshot.diagnostics,
                            "Worker exited without confirmed descendant-tree cleanup; "
                            "ownership remains retained.",
                        )[-100:],
                        updated_at=time.time(),
                    )
                    self._snapshots[session_id] = interrupted
                    self._persist_snapshot(interrupted)
                    self._publish(interrupted)
                    self._condition.notify_all()
                    return interrupted
            if running is not None:
                self._running.pop(session_id, None)
            snapshot = self.snapshot(session_id)
            updated = replace(
                snapshot,
                status=status,
                result=130,
                input_request=None,
                activity=(*snapshot.activity, activity)[-100:],
                updated_at=time.time(),
            )
            self._snapshots[session_id] = updated
            self._persist_snapshot(updated)
            self._release_session_lease(session_id)
            self._publish(updated)
            self._condition.notify_all()
            return updated

    def provide_input(
        self,
        session_id: str,
        value: str,
        *,
        request_id: str,
        request_generation: int,
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
            request = snapshot.input_request
            if (
                request.request_id != request_id
                or request.generation != request_generation
            ):
                raise ValueError(
                    "Portable session input is no longer the current input "
                    f"request: {session_id}"
                )
            enqueue_capacity = (
                getattr(self._catalog, "enqueue_execution_capacity", None)
                if self._catalog is not None
                else None
            )
            if callable(enqueue_capacity):
                enqueue_capacity(
                    session_id,
                    owner_id=self._owner_id,
                )
                self._queued[session_id] = _QueuedInput(
                    value=value,
                    request_id=request.request_id,
                    request_generation=request.generation,
                )
                queued = replace(
                    snapshot,
                    status=PortableSessionStatus.QUEUED,
                    input_request=None,
                    updated_at=time.time(),
                )
                self._snapshots[session_id] = queued
                self._publish(queued)
                self._condition.notify_all()
                return queued
            request_capacity = (
                getattr(self._catalog, "request_execution_capacity", None)
                if self._catalog is not None
                else None
            )
            if callable(request_capacity) and not request_capacity(
                session_id,
                owner_id=self._owner_id,
            ):
                self._queued[session_id] = _QueuedInput(
                    value=value,
                    request_id=request.request_id,
                    request_generation=request.generation,
                )
                queued = replace(
                    snapshot,
                    status=PortableSessionStatus.QUEUED,
                    input_request=None,
                    updated_at=time.time(),
                )
                self._snapshots[session_id] = queued
                self._publish(queued)
                self._condition.notify_all()
                return queued
            return self._send_worker_input(
                session_id,
                value,
                request_id=request.request_id,
                request_generation=request.generation,
                running=running,
            )

    def _send_worker_input(
        self,
        session_id: str,
        value: str,
        *,
        request_id: str,
        request_generation: int,
        running: _RunningSession,
    ) -> PortableSessionSnapshot:
        snapshot = self.snapshot(session_id)
        frame = supervisor_frame(
            session_id,
            running.next_supervisor_sequence,
            SupervisorMessageKind.USER_INPUT,
            {
                "value": value,
                "request_id": request_id,
                "request_generation": request_generation,
            },
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
        self._persist_snapshot(updated)
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

    def get_concurrency_limit(self) -> int:
        if self._catalog is None:
            return 2
        return self._catalog.get_concurrency_limit()

    def set_concurrency_limit(self, limit: int) -> None:
        if self._catalog is None:
            raise RuntimeError(
                "Portable session concurrency requires a machine catalog."
            )
        self._catalog.set_concurrency_limit(limit)

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
        self._scheduler_stop.set()
        scheduler_thread = self._scheduler_thread
        if scheduler_thread is not None:
            scheduler_thread.join(timeout=6)
        with self._condition:
            live_session_ids = tuple(
                dict.fromkeys((*self._running, *self._queued))
            )
        for session_id in live_session_ids:
            with self._condition:
                running = self._running.get(session_id)
                if running is not None and running.stop_requested:
                    continue
            try:
                self.pause_session(session_id)
            except ValueError:
                continue
        deadline = time.monotonic() + _COOPERATIVE_PAUSE_TIMEOUT_SECONDS
        with self._condition:
            while any(
                session_id in self._running
                for session_id in live_session_ids
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)
            unresponsive_ids = tuple(
                session_id
                for session_id in live_session_ids
                if session_id in self._running
            )
        for session_id in unresponsive_ids:
            self._terminate_session(
                session_id,
                command_kind=SupervisorMessageKind.SHUTDOWN,
                status=PortableSessionStatus.INTERRUPTED,
                activity=(
                    "Application exit interrupted a worker that did not reach "
                    "a cooperative checkpoint"
                ),
            )
        for thread in tuple(self._threads):
            thread.join(timeout=1)
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        self._ensure_cleanup_reaper()
        cleanup_reaper = self._cleanup_reaper_thread
        if cleanup_reaper is not None:
            cleanup_reaper.join(timeout=_COOPERATIVE_PAUSE_TIMEOUT_SECONDS)

    def _run_capacity_scheduler(self) -> None:
        while not self._scheduler_stop.wait(_CAPACITY_REFRESH_INTERVAL_SECONDS):
            self._synchronize_catalog_sessions()
            self._retry_pending_worker_cleanup()
            with self._condition:
                queued_ids = tuple(self._queued)
            for session_id in queued_ids:
                with self._condition:
                    queued = self._queued.get(session_id)
                    if queued is None:
                        continue
                    assert self._catalog is not None
                    try:
                        granted = self._catalog.request_execution_capacity(
                            session_id,
                            owner_id=self._owner_id,
                        )
                    except (KeyError, RuntimeError, ValueError) as error:
                        self._queued.pop(session_id, None)
                        previous = self._snapshots[session_id]
                        failed = replace(
                            previous,
                            status=PortableSessionStatus.FAILED,
                            result=1,
                            diagnostics=(
                                *previous.diagnostics,
                                f"Execution capacity request failed: {error}",
                            )[-100:],
                            updated_at=time.time(),
                        )
                        self._snapshots[session_id] = failed
                        self._persist_snapshot(failed)
                        self._publish(failed)
                        self._condition.notify_all()
                        continue
                    if not granted:
                        continue
                    self._queued.pop(session_id, None)
                    if isinstance(queued, _QueuedInput):
                        running = self._running.get(session_id)
                        if running is None:
                            failed = replace(
                                self._snapshots[session_id],
                                status=PortableSessionStatus.FAILED,
                                result=1,
                                diagnostics=(
                                    *self._snapshots[session_id].diagnostics,
                                    "Queued input lost its worker process.",
                                )[-100:],
                                updated_at=time.time(),
                            )
                            self._snapshots[session_id] = failed
                            self._persist_snapshot(failed)
                            self._publish(failed)
                            self._condition.notify_all()
                            continue
                        try:
                            self._send_worker_input(
                                session_id,
                                queued.value,
                                request_id=queued.request_id,
                                request_generation=queued.request_generation,
                                running=running,
                            )
                        except ValueError:
                            pass
                        continue
                    try:
                        self._launch_session(
                            queued.launch,
                            queued.command_kind,
                            queued.command_payload,
                            rollback_new_session=queued.rollback_new_session,
                        )
                    except BaseException as error:
                        current = self._snapshots.get(session_id)
                        if current is not None:
                            if (
                                session_id in self._running
                                and current.status
                                is PortableSessionStatus.INTERRUPTED
                            ):
                                continue
                            failed = replace(
                                current,
                                status=PortableSessionStatus.FAILED,
                                result=1,
                                diagnostics=(
                                    *current.diagnostics,
                                    f"Worker launch failed: {error}",
                                )[-100:],
                                updated_at=time.time(),
                            )
                            self._snapshots[session_id] = failed
                            self._persist_snapshot(failed)
                            self._publish(failed)
                            self._condition.notify_all()

    def _retry_pending_worker_cleanup(self, *, force: bool = False) -> None:
        with self._condition:
            now = time.monotonic()
            pending = tuple(
                (session_id, running)
                for session_id, running in self._running.items()
                if not running.cleanup.confirmed
                and running.cleanup.retry_at
                and (force or running.cleanup.retry_at <= now)
                and (
                    self._snapshots[session_id].status.terminal
                    or self._snapshots[session_id].status
                    is PortableSessionStatus.INTERRUPTED
                )
            )
            for session_id, running in pending:
                cleanup = terminate_process(
                    running.process  # type: ignore[arg-type]
                )
                if self._running.get(session_id) is not running:
                    continue
                if not cleanup.tree_terminated:
                    running.cleanup.record(cleanup)
                    continue
                running.cleanup.record(cleanup)
                self._close_worker_streams(running)
                self._running.pop(session_id, None)
                if running.launch_failure_rollback_new_session is not None:
                    self._finalize_post_launch_failure(
                        session_id,
                        rollback_new_session=(
                            running.launch_failure_rollback_new_session
                        ),
                    )
                    self._condition.notify_all()
                    continue
                snapshot = self._snapshots[session_id]
                self._release_execution_capacity(snapshot)
                self._release_session_lease(session_id)
                self._condition.notify_all()

    def _ensure_cleanup_reaper(self) -> None:
        with self._condition:
            pending = any(
                not running.cleanup.confirmed
                and (
                    self._snapshots[session_id].status.terminal
                    or self._snapshots[session_id].status
                    is PortableSessionStatus.INTERRUPTED
                )
                for session_id, running in self._running.items()
            )
            if not pending:
                return
            if (
                self._cleanup_reaper_thread is not None
                and self._cleanup_reaper_thread.is_alive()
            ):
                self._condition.notify_all()
                return
            self._cleanup_reaper_thread = Thread(
                target=self._run_cleanup_reaper,
                name="portable-session-cleanup-reaper",
                daemon=False,
            )
            self._cleanup_reaper_thread.start()

    def _run_cleanup_reaper(self) -> None:
        for _attempt in range(_CLEANUP_REAPER_MAX_ATTEMPTS):
            self._retry_pending_worker_cleanup(force=True)
            with self._condition:
                pending = any(
                    not running.cleanup.confirmed
                    and (
                        self._snapshots[session_id].status.terminal
                        or self._snapshots[session_id].status
                        is PortableSessionStatus.INTERRUPTED
                    )
                    for session_id, running in self._running.items()
                )
                if not pending:
                    return
                self._condition.wait(timeout=_CLEANUP_RETRY_SECONDS)

    def _synchronize_catalog_sessions(self) -> None:
        if self._catalog is None:
            return
        try:
            records = self._catalog.list_sessions()
        except RuntimeError:
            return
        with self._condition:
            for record in records:
                if (
                    record.session_id in self._owned_session_ids
                    or record.session_id in self._running
                    or record.session_id in self._queued
                ):
                    continue
                previous = self._snapshots.get(record.session_id)
                activity = (
                    (record.activity_summary,)
                    if record.activity_summary
                    else (() if previous is None else previous.activity)
                )
                synchronized = PortableSessionSnapshot(
                    session_id=record.session_id,
                    checkout=record.checkout,
                    status=record.status,
                    context=None if previous is None else previous.context,
                    activity=activity,
                    diagnostics=() if previous is None else previous.diagnostics,
                    result=None if previous is None else previous.result,
                    input_request=None,
                    prd_path=record.prd_path,
                    progress=(
                        PortableSessionProgress()
                        if previous is None
                        else previous.progress
                    ),
                    updated_at=record.updated_at,
                )
                self._launches[record.session_id] = record.launch
                if synchronized != previous:
                    self._snapshots[record.session_id] = synchronized
                    self._publish(synchronized)

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
                if not self._apply_worker_frame(session_id, frame, running):
                    return
                if frame.kind in {
                    WorkerMessageKind.CHECKPOINT.value,
                    WorkerMessageKind.CHECKPOINT_FAILURE.value,
                    WorkerMessageKind.COMPLETION.value,
                    WorkerMessageKind.FAILURE.value,
                }:
                    return
            if (
                self._running.get(session_id) is running
                and not self.snapshot(session_id).status.terminal
                and self.snapshot(session_id).status
                not in {
                    PortableSessionStatus.PAUSING,
                    PortableSessionStatus.INTERRUPTED,
                }
                and not running.stop_requested
            ):
                self._fail_session(
                    session_id,
                    "Worker exited without a terminal result.",
                    running,
                )
        except (PortableProtocolError, OSError) as error:
            self._fail_session(session_id, str(error), running)
        finally:
            cleanup = self._reap_worker(running)
            running.cleanup.record(cleanup)
            with self._condition:
                current_running = self._running.get(session_id)
                snapshot = self._snapshots[session_id]
                if current_running is running and running.checkpoint_summary is not None:
                    if cleanup.tree_terminated:
                        running.cleanup.record(cleanup)
                        self._running.pop(session_id, None)
                        paused = replace(
                            snapshot,
                            status=PortableSessionStatus.PAUSED,
                            result=None,
                            input_request=None,
                            activity=(
                                *snapshot.activity,
                                running.checkpoint_summary,
                            )[-100:],
                            updated_at=time.time(),
                        )
                        self._snapshots[session_id] = paused
                        self._persist_snapshot(paused)
                        self._publish(paused)
                        current_running = None
                    else:
                        running.cleanup.record(cleanup)
                        interrupted = replace(
                            snapshot,
                            status=PortableSessionStatus.INTERRUPTED,
                            result=130,
                            diagnostics=(
                                *snapshot.diagnostics,
                                cleanup.detail,
                            )[-100:],
                            updated_at=time.time(),
                        )
                        self._snapshots[session_id] = interrupted
                        self._persist_snapshot(interrupted)
                        self._publish(interrupted)
                elif (
                    current_running is running
                    and (
                        snapshot.status.terminal
                        or snapshot.status is PortableSessionStatus.INTERRUPTED
                        or running.stop_requested
                    )
                ):
                    if cleanup.tree_terminated:
                        self._running.pop(session_id, None)
                        current_running = None
                    else:
                        running.cleanup.record(cleanup)
                        retained = replace(
                            snapshot,
                            diagnostics=(
                                *snapshot.diagnostics,
                                cleanup.detail,
                            )[-100:],
                            updated_at=time.time(),
                        )
                        self._snapshots[session_id] = retained
                        self._persist_snapshot(retained)
                        self._publish(retained)
                releasable = self._snapshots[session_id]
                if (
                    current_running is None
                    and running.cleanup.confirmed
                    and releasable.status
                    not in {
                        PortableSessionStatus.RUNNING,
                        PortableSessionStatus.PAUSING,
                        PortableSessionStatus.QUEUED,
                    }
                ):
                    self._release_execution_capacity(
                        releasable
                    )
                    self._release_session_lease(session_id)
                self._condition.notify_all()

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
                if (
                    self._running.get(session_id) is not running
                    or snapshot.status.terminal
                ):
                    return
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
    ) -> bool:
        try:
            kind = WorkerMessageKind(frame.kind)
        except ValueError as error:
            raise PortableProtocolError(
                f"Unsupported worker message kind: {frame.kind!r}."
            ) from error
        with self._condition:
            snapshot = self._snapshots[session_id]
            if (
                self._running.get(session_id) is not running
                or snapshot.status.terminal
            ):
                return False
            pending_lifecycle = running.pending_lifecycle
            if pending_lifecycle is not None:
                safe_diagnostics = {
                    WorkerMessageKind.ACTIVITY,
                    WorkerMessageKind.SAFE_OUTPUT,
                }
                if kind not in (
                    safe_diagnostics | pending_lifecycle.acknowledgement_kinds
                ):
                    return True
                if (
                    kind in pending_lifecycle.acknowledgement_kinds
                    and (
                        pending_lifecycle.worker_generation != running.generation
                        or not pending_lifecycle.matches(frame)
                    )
                ):
                    diagnostic = (
                        f"Worker {kind.value} does not match "
                        "the pending lifecycle command."
                    )
                    pause_rejected = (
                        pending_lifecycle.action is SupervisorMessageKind.PAUSE
                    )
                    updated = replace(
                        snapshot,
                        status=(
                            PortableSessionStatus.INTERRUPTED
                            if pause_rejected
                            else snapshot.status
                        ),
                        result=(
                            130 if pause_rejected else snapshot.result
                        ),
                        input_request=(
                            None if pause_rejected else snapshot.input_request
                        ),
                        diagnostics=(*snapshot.diagnostics, diagnostic)[-100:],
                        updated_at=time.time(),
                    )
                    if pause_rejected:
                        running.stop_requested = True
                    self._snapshots[session_id] = updated
                    self._persist_snapshot(updated)
                    self._publish(updated)
                    self._condition.notify_all()
                    return True
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
                if status is not PortableSessionStatus.RUNNING:
                    # Generic worker progress cannot authorize supervisor-owned
                    # lifecycle transitions or release machine capacity.
                    return True
                if (
                    self._catalog is not None
                    and not self._catalog.owns_execution_capacity(
                        session_id,
                        owner_id=self._owner_id,
                    )
                ):
                    ignored = replace(
                        snapshot,
                        diagnostics=(
                            *snapshot.diagnostics,
                            "Worker STATUS RUNNING without owned execution "
                            "capacity was ignored.",
                        )[-100:],
                        updated_at=time.time(),
                    )
                    self._snapshots[session_id] = ignored
                    self._publish(ignored)
                    self._condition.notify_all()
                    return True
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
                        stage=(
                            _payload_text(frame, "stage")
                            if "stage" in frame.payload
                            else progress.stage
                        ),
                        completed_issues=(
                            _payload_nonnegative_int(frame, "completed_issues")
                            if "completed_issues" in frame.payload
                            else progress.completed_issues
                        ),
                        total_issues=(
                            _payload_nonnegative_int(frame, "total_issues")
                            if "total_issues" in frame.payload
                            else progress.total_issues
                        ),
                        active_issue=(
                            _payload_optional_text(frame, "active_issue")
                            if "active_issue" in frame.payload
                            else progress.active_issue
                        ),
                    )
                    if progress.completed_issues > progress.total_issues:
                        raise PortableProtocolError(
                            "Worker completed issue count cannot exceed its total."
                        )
                if "stage" in frame.payload:
                    self._live_stage_session_ids.add(session_id)
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
                request_id = frame.payload.get("request_id")
                if request_id is None:
                    # Protocol-v1 workers did not identify requests. Scope the
                    # compatibility token to this exact worker generation so
                    # delayed input can never satisfy a replacement worker.
                    request_id = (
                        f"legacy-{running.generation}-{frame.sequence}"
                    )
                if not isinstance(request_id, str) or not request_id:
                    raise PortableProtocolError(
                        "Worker input request request_id must be non-empty text."
                    )
                request_generation = frame.payload.get(
                    "request_generation",
                    frame.sequence,
                )
                if (
                    not isinstance(request_generation, int)
                    or isinstance(request_generation, bool)
                    or request_generation < 1
                ):
                    raise PortableProtocolError(
                        "Worker input request generation must be a positive integer."
                    )
                updated = replace(
                    snapshot,
                    status=PortableSessionStatus.WAITING_FOR_INPUT,
                    input_request=PortableSessionInputRequest(
                        kind=request_kind,
                        request_id=request_id,
                        generation=request_generation,
                        prompt=_payload_text(frame, "prompt"),
                        options=tuple(
                            (option[0], option[1]) for option in options_value
                        ),
                        default_key=_payload_text(frame, "default_key"),
                        cancel_key=cancel_key,
                    ),
                )
            elif kind is WorkerMessageKind.CHECKPOINT:
                if snapshot.status is not PortableSessionStatus.PAUSING:
                    raise PortableProtocolError(
                        "Worker CHECKPOINT requires a pausing session."
                    )
                running.checkpoint_summary = self._validate_checkpoint_evidence(
                    session_id,
                    frame,
                )
                updated = snapshot
            elif kind is WorkerMessageKind.CHECKPOINT_FAILURE:
                if snapshot.status is not PortableSessionStatus.PAUSING:
                    raise PortableProtocolError(
                        "Worker CHECKPOINT_FAILURE requires a pausing session."
                    )
                updated = replace(
                    snapshot,
                    status=PortableSessionStatus.INTERRUPTED,
                    result=130,
                    input_request=None,
                    diagnostics=(
                        *snapshot.diagnostics,
                        _payload_text(frame, "message"),
                    )[-100:],
                )
            elif kind is WorkerMessageKind.TERMINATION:
                pending = running.pending_lifecycle
                if (
                    pending is None
                    or kind not in pending.acknowledgement_kinds
                    or not pending.matches(frame)
                    or pending.worker_generation != running.generation
                ):
                    raise PortableProtocolError(
                        "Worker TERMINATION does not match the pending lifecycle command."
                    )
                descendants_confirmed = frame.payload.get("descendants_confirmed")
                if not isinstance(descendants_confirmed, bool):
                    raise PortableProtocolError(
                        "Worker TERMINATION descendants_confirmed must be boolean."
                    )
                running.termination_ack = descendants_confirmed
                running.termination_detail = _payload_text(frame, "detail")
                updated = snapshot
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
                WorkerMessageKind.CHECKPOINT,
                WorkerMessageKind.CHECKPOINT_FAILURE,
            }:
                updated = self._refresh_authoritative_progress(updated)
            updated = replace(updated, updated_at=time.time())
            self._snapshots[session_id] = updated
            if kind is WorkerMessageKind.INPUT_REQUEST:
                self._release_execution_capacity(updated)
            else:
                self._persist_snapshot(updated)
            if kind in {
                WorkerMessageKind.COMPLETION,
                WorkerMessageKind.FAILURE,
            }:
                running.stop_requested = True
            self._publish(updated)
            self._condition.notify_all()
            return True

    def _validate_checkpoint_evidence(
        self,
        session_id: str,
        frame: PortableProtocolFrame,
    ) -> str:
        if self._catalog is None:
            raise PortableProtocolError(
                "Worker CHECKPOINT requires an authoritative session catalog."
            )
        record = self._catalog.get_session(session_id)
        checkpoint_kind = _payload_text(frame, "checkpoint_kind")
        summary = _payload_text(frame, "summary")
        if checkpoint_kind == "PLANNING":
            thread_id = _payload_text(frame, "planning_thread_id")
            settings = frame.payload.get("planning_settings")
            if (
                record.prd_path is not None
                or record.planning_thread_id != thread_id
                or record.planning_settings is None
                or not isinstance(settings, dict)
                or record.planning_settings.to_dict() != settings
            ):
                raise PortableProtocolError(
                    "Worker planning checkpoint does not match durable catalog state."
                )
            return summary
        if checkpoint_kind != "PRD":
            raise PortableProtocolError(
                "Worker CHECKPOINT has an unsupported evidence kind."
            )
        if record.prd_path is None or record.issues_index_path is None:
            raise PortableProtocolError(
                "Worker PRD checkpoint has no authoritative catalog pointers."
            )
        prd_path = Path(_payload_text(frame, "prd_path")).resolve()
        issues_index = Path(_payload_text(frame, "issues_index_path")).resolve()
        if (
            prd_path != record.prd_path.resolve()
            or issues_index != record.issues_index_path.resolve()
            or not prd_path.is_file()
            or not issues_index.is_file()
        ):
            raise PortableProtocolError(
                "Worker PRD checkpoint does not match durable workflow files."
            )
        issue_id = frame.payload.get("issue_id")
        next_role = _payload_text(frame, "next_role")
        pass_number = frame.payload.get("pass_number")
        if (
            issue_id is not None and not isinstance(issue_id, str)
        ) or (
            not isinstance(pass_number, int)
            or isinstance(pass_number, bool)
            or pass_number < 1
        ):
            raise PortableProtocolError(
                "Worker PRD checkpoint cursor is invalid."
            )
        from .issue_pack import parse_issue_index
        from .state import LoopStateWriter

        writer = LoopStateWriter(issues_index)
        active_attempt = writer.active_scheduling_attempt()
        if active_attempt is None:
            if issue_id is not None or next_role != "scheduler" or pass_number != 1:
                raise PortableProtocolError(
                    "Worker PRD scheduler checkpoint is not the durable cursor."
                )
            return summary
        issues = {
            issue.number: issue
            for issue in parse_issue_index(issues_index)
        }
        durable_issue_id = active_attempt["issue"]
        issue = issues.get(durable_issue_id)
        if issue is None:
            raise PortableProtocolError(
                "Durable PRD checkpoint references an unknown Issue."
            )
        cursor = writer.resume_issue(issue)
        if (
            issue_id != durable_issue_id
            or next_role != cursor.next_role.value
            or pass_number != cursor.pass_number
        ):
            raise PortableProtocolError(
                "Worker PRD checkpoint is not the exact durable role/pass cursor."
            )
        return summary

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
        candidate_progress = _candidate_progress(matching)
        if (
            snapshot.session_id in self._live_stage_session_ids
            and snapshot.progress.stage
        ):
            candidate_progress = replace(
                candidate_progress,
                stage=snapshot.progress.stage,
            )
        return replace(
            snapshot,
            progress=candidate_progress,
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
            if (
                self._running.get(session_id) is not running
                or snapshot.status.terminal
            ):
                return
            updated = replace(
                snapshot,
                status=(
                    PortableSessionStatus.INTERRUPTED
                    if snapshot.status is PortableSessionStatus.PAUSING
                    else PortableSessionStatus.FAILED
                ),
                result=(
                    130
                    if snapshot.status is PortableSessionStatus.PAUSING
                    else 1
                ),
                input_request=None,
                diagnostics=(*snapshot.diagnostics, message)[-100:],
                updated_at=time.time(),
            )
            self._snapshots[session_id] = updated
            self._persist_snapshot(updated)
            cleanup = terminate_process(
                running.process  # type: ignore[arg-type]
            )
            running.cleanup.record(cleanup)
            if cleanup.tree_terminated:
                self._close_worker_streams(running)
                self._running.pop(session_id, None)
            else:
                running.cleanup.record(cleanup)
            self._publish(updated)
            self._condition.notify_all()

    @staticmethod
    def _reap_worker(running: _RunningSession) -> ProcessTerminationResult:
        try:
            running.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        cleanup = terminate_process(running.process)  # type: ignore[arg-type]
        if cleanup.tree_terminated:
            PortableSessionSupervisor._close_worker_streams(running)
        return cleanup

    @staticmethod
    def _close_worker_streams(running: _RunningSession) -> None:
        for stream in (
            running.process.stdin,
            running.process.stdout,
            running.process.stderr,
        ):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _publish(self, snapshot: PortableSessionSnapshot) -> None:
        self._events.put(PortableSessionEvent(snapshot))

    def _persist_snapshot(self, snapshot: PortableSessionSnapshot) -> None:
        if self._catalog is None:
            return
        summary = snapshot.activity[-1] if snapshot.activity else ""
        try:
            if (
                snapshot.session_id not in self._running
                and snapshot.status not in {
                PortableSessionStatus.RUNNING,
                PortableSessionStatus.PAUSING,
                PortableSessionStatus.QUEUED,
                }
                and callable(
                getattr(self._catalog, "release_execution_capacity", None)
                )
            ):
                self._catalog.release_execution_capacity(
                    snapshot.session_id,
                    owner_id=self._owner_id,
                    status=snapshot.status,
                    activity_summary=summary,
                )
                return
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

    def _release_execution_capacity(
        self,
        snapshot: PortableSessionSnapshot,
    ) -> None:
        if self._catalog is None:
            return
        release = getattr(self._catalog, "release_execution_capacity", None)
        if not callable(release):
            self._persist_snapshot(snapshot)
            return
        summary = snapshot.activity[-1] if snapshot.activity else ""
        release(
            snapshot.session_id,
            owner_id=self._owner_id,
            status=snapshot.status,
            activity_summary=summary,
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
        self._owned_session_ids.add(launch.session_id)
        self._saved_projects = tuple(self._catalog.list_saved_projects())
        return None

    def _acquire_existing_session_lease(self, session_id: str) -> None:
        if self._catalog is None:
            return
        acquire = getattr(self._catalog, "acquire_session_lease", None)
        if callable(acquire):
            acquire(session_id, owner_id=self._owner_id)
            self._owned_session_ids.add(session_id)

    def _release_session_lease(self, session_id: str) -> None:
        if self._catalog is None:
            return
        release = getattr(self._catalog, "release_worktree_lease", None)
        if not callable(release):
            return
        with self._condition:
            if release(
                session_id,
                owner_id=self._owner_id,
            ):
                self._owned_session_ids.discard(session_id)

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

    def _handle_post_launch_failure(
        self,
        launch: PortableSessionLaunch,
        running: _RunningSession,
        error: BaseException,
        *,
        rollback_new_session: bool,
    ) -> None:
        cleanup = terminate_process(
            running.process  # type: ignore[arg-type]
        )
        running.cleanup.record(cleanup)
        if cleanup.state is ProcessTreeState.STOPPED:
            self._close_worker_streams(running)
            if self._running.get(launch.session_id) is running:
                self._running.pop(launch.session_id, None)
            self._finalize_post_launch_failure(
                launch.session_id,
                rollback_new_session=rollback_new_session,
            )
            return
        running.stop_requested = True
        running.launch_failure_rollback_new_session = rollback_new_session
        previous = self._snapshots.get(
            launch.session_id,
            PortableSessionSnapshot(
                session_id=launch.session_id,
                checkout=launch.checkout,
                status=PortableSessionStatus.READY,
            ),
        )
        interrupted = replace(
            previous,
            checkout=launch.checkout,
            status=PortableSessionStatus.INTERRUPTED,
            result=130,
            input_request=None,
            diagnostics=(
                *previous.diagnostics,
                f"Worker launch setup failed: {error}",
                cleanup.detail,
            )[-100:],
            updated_at=time.time(),
        )
        self._snapshots[launch.session_id] = interrupted
        self._launches[launch.session_id] = launch
        self._persist_snapshot(interrupted)
        self._publish(interrupted)
        self._condition.notify_all()
        self._ensure_cleanup_reaper()

    def _finalize_post_launch_failure(
        self,
        session_id: str,
        *,
        rollback_new_session: bool,
    ) -> None:
        rollback = (
            getattr(self._catalog, "rollback_session_start", None)
            if self._catalog is not None
            else None
        )
        if rollback_new_session and callable(rollback):
            self._handle_launch_failure(
                session_id,
                rollback_new_session=True,
            )
            return
        current = self._snapshots.get(session_id)
        if current is None:
            self._handle_launch_failure(
                session_id,
                rollback_new_session=False,
            )
            return
        failed = replace(
            current,
            status=PortableSessionStatus.FAILED,
            result=1,
            input_request=None,
            updated_at=time.time(),
        )
        self._snapshots[session_id] = failed
        self._persist_snapshot(failed)
        self._release_session_lease(session_id)
        self._publish(failed)

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
                self._owned_session_ids.discard(session_id)
                self._snapshots.pop(session_id, None)
                self._launches.pop(session_id, None)
                self._running.pop(session_id, None)
                self._saved_projects = tuple(self._catalog.list_saved_projects())
                return
        self._mark_launch_failed(session_id)
        self._release_session_lease(session_id)


def run_portable_plain_session(
    launch: PortableSessionLaunch,
    operation: Callable[[], int],
    *,
    catalog: PortableSessionCatalogController | None = None,
    owner_id: str | None = None,
    queue_notice: Callable[[str], None] | None = None,
    poll_interval: float = _CAPACITY_REFRESH_INTERVAL_SECONDS,
) -> int:
    """Run one foreground Plain Mode operation under catalog leases and capacity."""
    if poll_interval <= 0:
        raise ValueError("Portable capacity polling interval must be positive.")
    if catalog is None:
        from .portable_session_catalog import PortableSessionCatalog

        catalog = PortableSessionCatalog()
    active_owner_id = owner_id or str(uuid.uuid4())
    notice = queue_notice or print
    from .portable_session_catalog import (
        PORTABLE_SESSION_CATALOG_ENV,
        PORTABLE_SESSION_ID_ENV,
        PORTABLE_SESSION_OWNER_ID_ENV,
    )

    environment_keys = (
        PORTABLE_SESSION_CATALOG_ENV,
        PORTABLE_SESSION_ID_ENV,
        PORTABLE_SESSION_OWNER_ID_ENV,
    )
    previous_environment = {key: os.environ.get(key) for key in environment_keys}
    os.environ[PORTABLE_SESSION_CATALOG_ENV] = str(catalog.path)
    os.environ[PORTABLE_SESSION_ID_ENV] = launch.session_id
    os.environ[PORTABLE_SESSION_OWNER_ID_ENV] = active_owner_id
    created = False
    final_status = PortableSessionStatus.INTERRUPTED
    try:
        create_with_lease = getattr(catalog, "create_session_with_lease", None)
        if not callable(create_with_lease):
            raise RuntimeError(
                "Portable Plain Mode requires catalog worktree leasing."
            )
        create_with_lease(launch, owner_id=active_owner_id)
        created = True
        queued_notice_written = False
        while not catalog.request_execution_capacity(
            launch.session_id,
            owner_id=active_owner_id,
        ):
            if not queued_notice_written:
                notice(f"Portable session {launch.session_id} [QUEUED]")
                queued_notice_written = True
            time.sleep(poll_interval)
        result = operation()
        final_status = (
            PortableSessionStatus.COMPLETED
            if result == 0
            else PortableSessionStatus.FAILED
        )
        return result
    finally:
        if created:
            try:
                catalog.release_execution_capacity(
                    launch.session_id,
                    owner_id=active_owner_id,
                    status=final_status,
                )
            finally:
                release_lease = getattr(catalog, "release_worktree_lease", None)
                if callable(release_lease):
                    release_lease(
                        launch.session_id,
                        owner_id=active_owner_id,
                    )
        for key, previous in previous_environment.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def active_portable_session_execution() -> bool:
    """Return whether this process is already inside supervised execution."""
    from .portable_session_catalog import (
        PORTABLE_SESSION_ID_ENV,
        PORTABLE_SESSION_OWNER_ID_ENV,
        active_process_owns_portable_execution,
    )
    from .portable_runtime import active_portable_runtime

    if (
        not os.environ.get(PORTABLE_SESSION_ID_ENV)
        or not os.environ.get(PORTABLE_SESSION_OWNER_ID_ENV)
    ):
        return False
    if active_portable_runtime() is not None:
        return True
    return active_process_owns_portable_execution()


def _payload_text(frame: PortableProtocolFrame, key: str) -> str:
    value = frame.payload.get(key, "")
    if not isinstance(value, str):
        raise PortableProtocolError(f"Worker payload {key!r} must be text.")
    return value


def _candidate_progress(
    candidate: PortableResumeCandidateRecord,
) -> PortableSessionProgress:
    return PortableSessionProgress(
        stage=getattr(candidate, "active_stage", None) or "",
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
    process = launch_process_tree(
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
    return process
