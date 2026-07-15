from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence


PROCESS_EXIT_GRACE_SECONDS = 1.0
PROCESS_TERMINATE_GRACE_SECONDS = 5.0


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
