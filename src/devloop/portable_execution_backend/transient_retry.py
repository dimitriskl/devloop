"""The bounded transient-retry policy shared by every Execution Backend.

One Workflow Step attempt may need more than one provider process: a dropped
connection or a stream that never opened is worth waiting out, and doing so costs
the Issue nothing. This module owns that loop — the accumulation of every
process's output into one attempt transcript, the single Execution Budget that
spans the retries, the shared delay between them, and the durable log written
before each wait — so both backends retry on identical terms.

What is *worth* retrying is the one decision this module does not make. It asks
the backend, through :class:`~.backend.TransientFailurePredicate`, because only a
backend can read its own provider's diagnostics. That is also what keeps the
promise that a Run-Wide Blocker is never retried: a backend that recognises a
run-wide condition refuses retryability, and this loop then returns the attempt so
the condition can pause the run instead of spending the rest of its budget.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ..subprocess_utils import (
    EXECUTION_BUDGET_EXPIRY_RETURNCODE,
    AttemptExecutionBudget,
    output_text,
)
from .activity import ActivityCallback, StepActivityEvent, StepActivityKind
from .backend import LogWriter, TransientFailurePredicate

if TYPE_CHECKING:
    from ..portable_workflow import ExecutionBudget


# One process run of a Workflow Step attempt. The attempt-wide Execution Budget is
# handed in so every retry shares one deadline and one inactivity checkpoint
# rather than each process starting the clock again.
AttemptRunner = Callable[
    [AttemptExecutionBudget | None],
    "subprocess.CompletedProcess[str]",
]
# The wait between two process runs of one attempt. Long enough for a provider
# connection to recover, short enough to stay well inside a Workflow Step's
# Execution Budget.
TRANSIENT_RETRY_DELAY_SECONDS = 30


def run_attempt_with_transient_retries(
    run_attempt: AttemptRunner,
    *,
    is_retryable: TransientFailurePredicate,
    retry_subject: str,
    retry_delay_seconds: float = TRANSIENT_RETRY_DELAY_SECONDS,
    stdout_path: Path,
    stderr_path: Path,
    write_log: LogWriter,
    activity_callback: ActivityCallback | None = None,
    execution_budget: ExecutionBudget | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one Workflow Step attempt, retrying only what the backend calls transient.

    The returned result carries every process run's output concatenated, so the
    caller classifies and persists one attempt transcript however many provider
    processes it took. ``retry_subject`` names what failed in the operator-facing
    retry notice; it is the provider invocation, never a provider diagnostic, so
    the notice cannot leak captured output.
    """
    attempt = 1
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    attempt_budget = (
        AttemptExecutionBudget(
            timeout_seconds=execution_budget.timeout_seconds,
            checkpoint_seconds=execution_budget.checkpoint_seconds,
        )
        if execution_budget is not None
        else None
    )

    while True:
        result = run_attempt(attempt_budget)
        current_stdout = output_text(result.stdout)
        current_stderr = output_text(result.stderr)
        if attempt_budget is not None and (current_stdout or current_stderr):
            attempt_budget.notify_activity()
        stdout_parts.append(current_stdout)
        stderr_parts.append(current_stderr)
        result.stdout = "".join(stdout_parts)
        result.stderr = "".join(stderr_parts)

        if attempt_budget is not None:
            expiration = attempt_budget.expiration()
            if expiration is not None:
                result.returncode = EXECUTION_BUDGET_EXPIRY_RETURNCODE
                if expiration not in result.stderr:
                    result.stderr += f"{expiration}\n"
                return result

        if result.returncode == 0 or not is_retryable(
            stdout=current_stdout,
            stderr=current_stderr,
        ):
            return result

        retry_message = (
            f"{retry_subject} failed on attempt {attempt}; "
            f"retrying in {retry_delay_seconds} seconds.\n"
        )
        if activity_callback is None:
            print(retry_message.strip())
        else:
            activity_callback(
                StepActivityEvent(
                    kind=StepActivityKind.ERROR,
                    activity=retry_message.strip(),
                )
            )
        stderr_parts.append(retry_message)
        result.stderr = "".join(stderr_parts)
        write_log(stdout_path, result.stdout)
        write_log(stderr_path, result.stderr)
        if attempt_budget is None:
            time.sleep(retry_delay_seconds)
        else:
            expiration = attempt_budget.wait_for_retry(retry_delay_seconds)
            if expiration is not None:
                result.returncode = EXECUTION_BUDGET_EXPIRY_RETURNCODE
                if expiration not in result.stderr:
                    result.stderr += f"{expiration}\n"
                return result
        attempt += 1
