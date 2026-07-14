from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from devloop.domain.scheduler import RetryPolicy, transient_retry_delays
from devloop.execution.app_server import AppServerTransientError

_Result = TypeVar("_Result")


def run_with_transient_retries(
    operation: Callable[[bool], _Result],
    policy: RetryPolicy,
    *,
    retries_used: int = 0,
    on_retry: Callable[[int, float], None] | None = None,
) -> _Result:
    delays = transient_retry_delays(policy)
    if retries_used < 0 or retries_used > len(delays):
        raise ValueError("Persisted transient retry count exceeds the Workflow policy.")
    remaining = len(delays) - retries_used
    for index in range(remaining + 1):
        try:
            return operation(index > 0)
        except AppServerTransientError:
            if index == remaining:
                raise
            retry_number = retries_used + index + 1
            delay = delays[retry_number - 1]
            if on_retry is not None:
                on_retry(retry_number, delay)
            time.sleep(delay)
    raise AssertionError("Transient retry loop exhausted without a result.")
