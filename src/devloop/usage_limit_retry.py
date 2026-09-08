"""Wait outside agent execution budgets, then recover the durable workflow cursor."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TypeVar

from .codex_runner import RunWideBlockerError
from .portable_execution_backend import RunWideBlockerKind
from .portable_execution_backend.blockers import valid_reset_timestamp
from .portable_runtime import active_portable_runtime
from .state import LoopStateWriter

USAGE_LIMIT_RETRY_SECONDS = 300
RETRY_DISPLAY_INTERVAL_SECONDS = 1
_Result = TypeVar("_Result")


def wait_for_usage_retry(seconds: float) -> None:
    runtime = active_portable_runtime()
    if runtime is None:
        time.sleep(seconds)
    else:
        runtime.wait_for_retry(seconds)


def retry_usage_limited_run(
    operation: Callable[[], _Result],
    state_writer: LoopStateWriter,
    show_countdown: Callable[[str], None],
    *,
    clock: Callable[[], float] = time.time,
    wait: Callable[[float], None] = wait_for_usage_retry,
) -> _Result:
    pause = state_writer.run_pause()
    retry_at = valid_reset_timestamp(pause.get("retry_at")) if pause else None
    retried = False
    if pause and pause.get("kind") == RunWideBlockerKind.USAGE_LIMIT.value:
        if retry_at is not None:
            _wait_until(retry_at, show_countdown, clock, wait)
            retried = True
    while True:
        try:
            result = operation()
        except RunWideBlockerError as error:
            if error.blocker.kind is not RunWideBlockerKind.USAGE_LIMIT:
                raise
            # A completed issue clears the pause; a later limit starts a new wait.
            retried = retried and state_writer.run_pause() is not None
            now = clock()
            reset_at = valid_reset_timestamp(error.blocker.reset_at)
            retry_at = (
                reset_at
                if not retried and reset_at is not None and reset_at > now
                else now + USAGE_LIMIT_RETRY_SECONDS
            )
            state_writer.record_run_paused(error.blocker, retry_at=retry_at)
            _wait_until(retry_at, show_countdown, clock, wait)
            retried = True
        else:
            state_writer.clear_run_pause()
            return result


def _wait_until(
    retry_at: float,
    show_countdown: Callable[[str], None],
    clock: Callable[[], float],
    wait: Callable[[float], None],
) -> None:
    deadline = datetime.fromtimestamp(retry_at, timezone.utc).isoformat(timespec="seconds")
    while True:
        # Check lifecycle even when resuming a deadline that has already passed.
        wait(0)
        remaining = max(0, math.ceil(retry_at - clock()))
        if remaining == 0:
            return
        minutes, seconds = divmod(remaining, 60)
        show_countdown(
            f"USAGE LIMIT · retry in {minutes:02d}:{seconds:02d} · {deadline}"
        )
        wait(min(RETRY_DISPLAY_INTERVAL_SECONDS, remaining))
