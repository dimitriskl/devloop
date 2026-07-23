from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Sequence


PROCESS_EXIT_GRACE_SECONDS = 1.0
PROCESS_TERMINATE_GRACE_SECONDS = 5.0
BUDGET_POLL_SECONDS = 0.05
PROCESS_TREE_ATTRIBUTE = "_devloop_process_group_id"
CHECKPOINT_PAUSING_ITEM_TYPES = frozenset(
    {"command_execution", "mcp_tool_call", "web_search"}
)
_ACTIVE_PROCESS_TREES: set[subprocess.Popen[str]] = set()
_ACTIVE_PROCESS_TREES_LOCK = threading.RLock()


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


def update_checkpoint_for_backend_event(
    budget: ProcessExecutionBudget | None,
    event: dict[str, object] | None,
    active_items: set[str],
) -> None:
    """Pause inactivity expiry while Codex reports an active backend operation."""
    if budget is None or event is None:
        return
    event_type = event.get("type")
    if event_type not in {"item.started", "item.completed"}:
        return
    item = event.get("item")
    if not isinstance(item, dict):
        return
    item_type = item.get("type")
    if item_type not in CHECKPOINT_PAUSING_ITEM_TYPES:
        return
    item_id = item.get("id")
    key = f"{item_type}:{item_id if isinstance(item_id, str) else item_type}"
    was_active = bool(active_items)
    if event_type == "item.started":
        active_items.add(key)
    else:
        active_items.discard(key)
    is_active = bool(active_items)
    if not was_active and is_active:
        budget.pause_checkpoint()
    elif was_active and not is_active:
        budget.resume_checkpoint()


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
    if os.name != "nt":
        try:
            process_group_id = os.getpgid(process.pid)
        except (AttributeError, OSError):
            return
        if process_group_id == process.pid:
            setattr(process, PROCESS_TREE_ATTRIBUTE, process_group_id)


def unregister_process_tree(process: subprocess.Popen[str]) -> None:
    with _ACTIVE_PROCESS_TREES_LOCK:
        _ACTIVE_PROCESS_TREES.discard(process)


def terminate_active_process_trees() -> None:
    """Terminate subprocess trees still owned by the active application."""
    with _ACTIVE_PROCESS_TREES_LOCK:
        processes = tuple(_ACTIVE_PROCESS_TREES)
    for process in processes:
        if getattr(process, "poll", lambda: None)() is None:
            terminate_process(process)
        else:
            unregister_process_tree(process)


def terminate_process(process: subprocess.Popen[str]) -> None:
    try:
        _signal_process_tree(process, force=False)
        try:
            process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        if not _process_tree_is_alive(process):
            return

        _signal_process_tree(process, force=True)
        try:
            process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            # Never hold the workflow indefinitely because an OS process refuses
            # to be reaped.
            pass
    finally:
        unregister_process_tree(process)


def _signal_process_tree(process: subprocess.Popen[str], *, force: bool) -> None:
    if os.name == "nt":
        pid = getattr(process, "pid", None)
        if isinstance(pid, int):
            try:
                completed = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=PROCESS_EXIT_GRACE_SECONDS,
                )
                if completed.returncode == 0:
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
        _signal_process(process, force=force)
        return

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
            return
        except (ProcessLookupError, OSError):
            pass
    _signal_process(process, force=force)


def _process_tree_is_alive(process: subprocess.Popen[str]) -> bool:
    if os.name == "nt":
        return getattr(process, "poll", lambda: None)() is None
    process_group_id = getattr(process, PROCESS_TREE_ATTRIBUTE, None)
    if not isinstance(process_group_id, int):
        return False
    try:
        os.killpg(process_group_id, 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def _signal_process(process: subprocess.Popen[str], *, force: bool) -> None:
    try:
        (process.kill if force else process.terminate)()
    except (AttributeError, OSError):
        pass
