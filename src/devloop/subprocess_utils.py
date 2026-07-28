from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


PROCESS_EXIT_GRACE_SECONDS = 1.0
PROCESS_TERMINATE_GRACE_SECONDS = 5.0
BUDGET_POLL_SECONDS = 0.05
# The exit status every Execution Backend reports when an attempt was terminated
# because its Execution Budget expired. It lives beside the budget itself so both
# backends and the role runner read one convention rather than each repeating the
# number.
EXECUTION_BUDGET_EXPIRY_RETURNCODE = 124
PROCESS_TREE_ATTRIBUTE = "_devloop_process_group_id"
WINDOWS_PROCESS_SNAPSHOT = 0x00000002
WINDOWS_PROCESS_TERMINATE = 0x0001
WINDOWS_SYNCHRONIZE = 0x00100000
WINDOWS_ERROR_INVALID_PARAMETER = 87
WINDOWS_WAIT_OBJECT_0 = 0x00000000
WINDOWS_WAIT_TIMEOUT = 0x00000102
_ACTIVE_PROCESS_TREES: set[subprocess.Popen[str]] = set()
_ACTIVE_WINDOWS_PROCESS_TREE_PIDS: dict[subprocess.Popen[str], set[int]] = {}
_ACTIVE_PROCESS_TREES_LOCK = threading.RLock()


@dataclass(frozen=True)
class ProcessTerminationResult:
    tree_terminated: bool
    detail: str


class AttemptExecutionBudget:
    """Track one timeout and activity checkpoint across process retries."""

    def __init__(self, *, timeout_seconds: float, checkpoint_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds
        self._checkpoint_seconds = checkpoint_seconds
        self._started_at = time.monotonic()
        self._deadline = self._started_at + timeout_seconds
        self._last_activity = self._started_at
        self._checkpoint_paused = False
        self._activity_lock = threading.Lock()

    def notify_activity(self) -> None:
        with self._activity_lock:
            self._last_activity = time.monotonic()

    def pause_checkpoint(self) -> None:
        """Keep the hard deadline active while a backend operation is running."""
        with self._activity_lock:
            self._checkpoint_paused = True

    def resume_checkpoint(self) -> None:
        """Start a fresh inactivity window after the active operation finishes."""
        with self._activity_lock:
            self._checkpoint_paused = False
            self._last_activity = time.monotonic()

    def expiration(self) -> str | None:
        now = time.monotonic()
        with self._activity_lock:
            inactive_seconds = now - self._last_activity
            checkpoint_paused = self._checkpoint_paused
        if now >= self._deadline:
            return (
                "Execution Budget timeout "
                f"({self._timeout_seconds:g} seconds) expired."
            )
        if (
            not checkpoint_paused
            and inactive_seconds >= self._checkpoint_seconds
        ):
            return (
                "Execution Budget checkpoint deadline "
                f"({self._checkpoint_seconds:g} seconds without backend "
                "activity) expired."
            )
        return None

    def wait_for_retry(self, delay_seconds: float) -> str | None:
        retry_deadline = time.monotonic() + delay_seconds
        while True:
            expiration = self.expiration()
            if expiration is not None:
                return expiration
            remaining = min(
                retry_deadline - time.monotonic(),
                self._deadline - time.monotonic(),
            )
            if remaining <= 0:
                return self.expiration()
            time.sleep(min(BUDGET_POLL_SECONDS, remaining))


class ProcessExecutionBudget:
    """Enforce a total timeout and an inactivity checkpoint for one process."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        *,
        timeout_seconds: float,
        checkpoint_seconds: float,
        attempt_budget: AttemptExecutionBudget | None = None,
    ) -> None:
        self._process = process
        self._attempt_budget = attempt_budget or AttemptExecutionBudget(
            timeout_seconds=timeout_seconds,
            checkpoint_seconds=checkpoint_seconds,
        )
        self._finished = threading.Event()
        self._expiration: str | None = None
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def notify_activity(self) -> None:
        self._attempt_budget.notify_activity()

    def pause_checkpoint(self) -> None:
        self._attempt_budget.pause_checkpoint()

    def resume_checkpoint(self) -> None:
        self._attempt_budget.resume_checkpoint()

    def finish(self) -> str | None:
        self._finished.set()
        self._thread.join(timeout=PROCESS_EXIT_GRACE_SECONDS)
        if self._expiration is None:
            self._expiration = self._attempt_budget.expiration()
        return self._expiration

    def _watch(self) -> None:
        while not self._finished.wait(timeout=BUDGET_POLL_SECONDS):
            self._expiration = self._attempt_budget.expiration()
            if self._expiration is None:
                continue
            terminate_process(self._process)
            return


def run_captured_text(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        input=input_text,
        stdin=subprocess.DEVNULL if input_text is None else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env={**os.environ, **env} if env is not None else None,
        capture_output=True,
        check=False,
    )


def output_text(value: str | None) -> str:
    return value or ""


def reap_process_after_terminal_event(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        # Keep the launcher alive while taskkill can enumerate its descendants.
        terminate_process(process)
        return
    try:
        process.wait(timeout=PROCESS_EXIT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass

    terminate_process(process)


def process_tree_creation_kwargs() -> dict[str, int | bool]:
    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        }
    return {"start_new_session": True}


def register_process_tree(process: subprocess.Popen[str]) -> None:
    with _ACTIVE_PROCESS_TREES_LOCK:
        _ACTIVE_PROCESS_TREES.add(process)
        if os.name == "nt":
            process_id = getattr(process, "pid", None)
            if isinstance(process_id, int):
                _ACTIVE_WINDOWS_PROCESS_TREE_PIDS[process] = {process_id}
    if os.name == "nt":
        _refresh_windows_process_tree(process)
    else:
        try:
            process_group_id = os.getpgid(process.pid)
        except (AttributeError, OSError):
            return
        if process_group_id == process.pid:
            setattr(process, PROCESS_TREE_ATTRIBUTE, process_group_id)


def unregister_process_tree(process: subprocess.Popen[str]) -> None:
    with _ACTIVE_PROCESS_TREES_LOCK:
        _ACTIVE_PROCESS_TREES.discard(process)
        _ACTIVE_WINDOWS_PROCESS_TREE_PIDS.pop(process, None)


def release_process_tree_if_stopped(process: subprocess.Popen[str]) -> bool:
    """Release ownership only after the entire registered tree has stopped."""
    if _process_tree_is_alive(process):
        return False
    unregister_process_tree(process)
    return True


def terminate_active_process_trees() -> tuple[ProcessTerminationResult, ...]:
    """Terminate subprocess trees still owned by the active application."""
    with _ACTIVE_PROCESS_TREES_LOCK:
        processes = tuple(_ACTIVE_PROCESS_TREES)
    results: list[ProcessTerminationResult] = []
    for process in processes:
        if _process_tree_is_alive(process):
            results.append(terminate_process(process))
        else:
            unregister_process_tree(process)
            results.append(
                ProcessTerminationResult(
                    tree_terminated=True,
                    detail="Owned process tree had already exited.",
                )
            )
    return tuple(results)


def owned_process_trees_are_stopped() -> bool:
    """Confirm that every registered backend tree has reached a terminal state."""
    with _ACTIVE_PROCESS_TREES_LOCK:
        processes = tuple(_ACTIVE_PROCESS_TREES)
    for process in processes:
        if _process_tree_is_alive(process):
            return False
        unregister_process_tree(process)
    return True


def terminate_process(
    process: subprocess.Popen[str],
) -> ProcessTerminationResult:
    tree_signal_confirmed = _signal_process_tree(process, force=False)
    try:
        process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    if _process_tree_is_alive(process):
        tree_signal_confirmed = (
            _signal_process_tree(process, force=True) or tree_signal_confirmed
        )
        try:
            process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    tree_is_alive = _process_tree_is_alive(process)
    process_id = getattr(process, "pid", None)
    tree_terminated = not tree_is_alive and (
        tree_signal_confirmed or not isinstance(process_id, int)
    )
    if tree_terminated:
        unregister_process_tree(process)
        return ProcessTerminationResult(
            tree_terminated=True,
            detail="Owned process tree termination was confirmed.",
        )
    return ProcessTerminationResult(
        tree_terminated=False,
        detail=(
            "Owned process tree termination could not be confirmed; "
            "ownership remains registered."
        ),
    )


def _signal_process_tree(
    process: subprocess.Popen[str],
    *,
    force: bool,
) -> bool:
    if os.name == "nt":
        pid = getattr(process, "pid", None)
        if isinstance(pid, int):
            retained_pids = _refresh_windows_process_tree(process)
            if _terminate_windows_process_tree(
                pid,
                retained_pids=retained_pids,
            ):
                return True
            try:
                completed = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=PROCESS_EXIT_GRACE_SECONDS,
                )
                if completed.returncode == 0:
                    return True
            except (OSError, subprocess.TimeoutExpired):
                pass
        _signal_process(process, force=force)
        return False

    process_group_id = getattr(process, PROCESS_TREE_ATTRIBUTE, None)
    if not isinstance(process_group_id, int):
        try:
            process_group_id = os.getpgid(process.pid)
        except (AttributeError, OSError):
            process_group_id = None
    if isinstance(process_group_id, int) and process_group_id == process.pid:
        try:
            os.killpg(
                process_group_id,
                signal.SIGKILL if force else signal.SIGTERM,
            )
            return True
        except ProcessLookupError:
            return True
        except OSError:
            pass
    _signal_process(process, force=force)
    return False


def _terminate_windows_process_tree(
    root_pid: int,
    *,
    retained_pids: set[int] | None = None,
) -> bool:
    """Terminate and verify a Windows tree without relying on taskkill."""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("process_id", wintypes.DWORD),
            ("default_heap_id", ctypes.c_size_t),
            ("module_id", wintypes.DWORD),
            ("thread_count", wintypes.DWORD),
            ("parent_process_id", wintypes.DWORD),
            ("base_priority", ctypes.c_long),
            ("flags", wintypes.DWORD),
            ("executable_file", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32))
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32))
    process_next.restype = wintypes.BOOL
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    terminate = kernel32.TerminateProcess
    terminate.argtypes = (wintypes.HANDLE, wintypes.UINT)
    terminate.restype = wintypes.BOOL
    wait = kernel32.WaitForSingleObject
    wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait.restype = wintypes.DWORD
    close = kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL

    invalid_handle = wintypes.HANDLE(-1).value
    snapshot = create_snapshot(WINDOWS_PROCESS_SNAPSHOT, 0)
    if snapshot == invalid_handle:
        return False
    parent_by_pid: dict[int, int] = {}
    try:
        entry = ProcessEntry32()
        entry.size = ctypes.sizeof(ProcessEntry32)
        if process_first(snapshot, ctypes.byref(entry)):
            while True:
                parent_by_pid[int(entry.process_id)] = int(entry.parent_process_id)
                if not process_next(snapshot, ctypes.byref(entry)):
                    break
    finally:
        close(snapshot)

    descendants = {root_pid, *(retained_pids or ())}
    while True:
        discovered = {
            pid for pid, parent in parent_by_pid.items() if parent in descendants
        }
        expanded = descendants | discovered
        if expanded == descendants:
            break
        descendants = expanded

    confirmed = True
    ordered_pids = sorted(
        descendants,
        key=lambda pid: _windows_process_depth(pid, parent_by_pid, root_pid),
        reverse=True,
    )
    handles: list[object] = []
    for pid in ordered_pids:
        handle = open_process(
            WINDOWS_PROCESS_TERMINATE | WINDOWS_SYNCHRONIZE,
            False,
            pid,
        )
        if not handle:
            if ctypes.get_last_error() != WINDOWS_ERROR_INVALID_PARAMETER:
                confirmed = False
            continue
        handles.append(handle)
    try:
        for handle in handles:
            if not terminate(handle, 1) and wait(handle, 0) != WINDOWS_WAIT_OBJECT_0:
                confirmed = False
        deadline = time.monotonic() + PROCESS_TERMINATE_GRACE_SECONDS
        for handle in handles:
            remaining_milliseconds = max(
                0,
                int((deadline - time.monotonic()) * 1000),
            )
            if wait(handle, remaining_milliseconds) == WINDOWS_WAIT_TIMEOUT:
                confirmed = False
    finally:
        for handle in handles:
            close(handle)
    return confirmed


def _windows_process_depth(
    pid: int,
    parent_by_pid: dict[int, int],
    root_pid: int,
) -> int:
    depth = 0
    visited: set[int] = set()
    while pid != root_pid and pid not in visited:
        visited.add(pid)
        pid = parent_by_pid.get(pid, root_pid)
        depth += 1
    return depth


def _process_tree_is_alive(process: subprocess.Popen[str]) -> bool:
    if os.name == "nt":
        retained_pids = _refresh_windows_process_tree(process)
        return any(_windows_process_is_alive(process_id) for process_id in retained_pids)
    process_group_id = getattr(process, PROCESS_TREE_ATTRIBUTE, None)
    if not isinstance(process_group_id, int):
        return False
    try:
        os.killpg(process_group_id, 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def _refresh_windows_process_tree(
    process: subprocess.Popen[str],
) -> set[int]:
    root_pid = getattr(process, "pid", None)
    if not isinstance(root_pid, int):
        return set()
    parent_by_pid = _windows_parent_process_ids()
    with _ACTIVE_PROCESS_TREES_LOCK:
        retained = set(
            _ACTIVE_WINDOWS_PROCESS_TREE_PIDS.get(process, {root_pid})
        )
        retained.add(root_pid)
        while True:
            discovered = {
                process_id
                for process_id, parent_id in parent_by_pid.items()
                if parent_id in retained
            }
            expanded = retained | discovered
            if expanded == retained:
                break
            retained = expanded
        if process in _ACTIVE_PROCESS_TREES:
            _ACTIVE_WINDOWS_PROCESS_TREE_PIDS[process] = retained
        return retained


def _windows_parent_process_ids() -> dict[int, int]:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return {}

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("process_id", wintypes.DWORD),
            ("default_heap_id", ctypes.c_size_t),
            ("module_id", wintypes.DWORD),
            ("thread_count", wintypes.DWORD),
            ("parent_process_id", wintypes.DWORD),
            ("base_priority", ctypes.c_long),
            ("flags", wintypes.DWORD),
            ("executable_file", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32))
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32))
    process_next.restype = wintypes.BOOL
    close = kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL

    invalid_handle = wintypes.HANDLE(-1).value
    snapshot = create_snapshot(WINDOWS_PROCESS_SNAPSHOT, 0)
    if snapshot == invalid_handle:
        return {}
    parent_by_pid: dict[int, int] = {}
    try:
        entry = ProcessEntry32()
        entry.size = ctypes.sizeof(ProcessEntry32)
        if process_first(snapshot, ctypes.byref(entry)):
            while True:
                parent_by_pid[int(entry.process_id)] = int(entry.parent_process_id)
                if not process_next(snapshot, ctypes.byref(entry)):
                    break
    finally:
        close(snapshot)
    return parent_by_pid


def _windows_process_is_alive(process_id: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    wait = kernel32.WaitForSingleObject
    wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait.restype = wintypes.DWORD
    close = kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    handle = open_process(WINDOWS_SYNCHRONIZE, False, process_id)
    if not handle:
        return False
    try:
        return wait(handle, 0) == WINDOWS_WAIT_TIMEOUT
    finally:
        close(handle)


def _signal_process(process: subprocess.Popen[str], *, force: bool) -> None:
    try:
        (process.kill if force else process.terminate)()
    except (AttributeError, OSError):
        pass
