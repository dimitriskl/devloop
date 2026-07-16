from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Sequence


PROCESS_EXIT_GRACE_SECONDS = 1.0
PROCESS_TERMINATE_GRACE_SECONDS = 5.0
BUDGET_POLL_SECONDS = 0.05


class ProcessExecutionBudget:
    """Enforce a total timeout and an inactivity checkpoint for one process."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        *,
        timeout_seconds: float,
        checkpoint_seconds: float,
    ) -> None:
        self._process = process
        self._timeout_seconds = timeout_seconds
        self._checkpoint_seconds = checkpoint_seconds
        self._finished = threading.Event()
        self._activity_lock = threading.Lock()
        self._started_at = time.monotonic()
        self._last_activity = self._started_at
        self._expiration: str | None = None
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def notify_activity(self) -> None:
        with self._activity_lock:
            self._last_activity = time.monotonic()

    def finish(self) -> str | None:
        self._finished.set()
        self._thread.join(timeout=PROCESS_EXIT_GRACE_SECONDS)
        return self._expiration

    def _watch(self) -> None:
        while not self._finished.wait(timeout=BUDGET_POLL_SECONDS):
            now = time.monotonic()
            with self._activity_lock:
                inactive_seconds = now - self._last_activity
            if now - self._started_at >= self._timeout_seconds:
                self._expiration = (
                    "Execution Budget timeout "
                    f"({self._timeout_seconds:g} seconds) expired."
                )
            elif inactive_seconds >= self._checkpoint_seconds:
                self._expiration = (
                    "Execution Budget checkpoint deadline "
                    f"({self._checkpoint_seconds:g} seconds without backend "
                    "activity) expired."
                )
            else:
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
    try:
        process.wait(timeout=PROCESS_EXIT_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    terminate_process(process)


def terminate_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    process.kill()
    try:
        process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        # Never hold the workflow indefinitely because an OS process refuses
        # to be reaped.
        pass
