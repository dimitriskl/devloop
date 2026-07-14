from __future__ import annotations

import pytest

from devloop.application.retry import run_with_transient_retries
from devloop.domain.scheduler import RetryPolicy
from devloop.execution.app_server import AppServerTransientError


def test_transient_retry_is_bounded_and_switches_to_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    sleeps: list[float] = []
    monkeypatch.setattr("devloop.application.retry.time.sleep", sleeps.append)

    def operation(recover: bool) -> str:
        calls.append(recover)
        if len(calls) < 3:
            raise AppServerTransientError("connection interrupted")
        return "completed"

    result = run_with_transient_retries(operation, RetryPolicy(0, 2))

    assert result == "completed"
    assert calls == [False, True, True]
    assert sleeps == [0.25, 0.5]


def test_transient_retry_continues_from_persisted_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    retries: list[tuple[int, float]] = []
    monkeypatch.setattr("devloop.application.retry.time.sleep", lambda _: None)

    def operation(recover: bool) -> str:
        calls.append(recover)
        if len(calls) == 1:
            raise AppServerTransientError("connection interrupted")
        return "completed"

    result = run_with_transient_retries(
        operation,
        RetryPolicy(0, 2),
        retries_used=1,
        on_retry=lambda number, delay: retries.append((number, delay)),
    )

    assert result == "completed"
    assert calls == [False, True]
    assert retries == [(2, 0.5)]


def test_transient_retry_propagates_after_versioned_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr("devloop.application.retry.time.sleep", lambda _: None)

    def operation(recover: bool) -> None:
        calls.append(recover)
        raise AppServerTransientError("connection interrupted")

    with pytest.raises(AppServerTransientError):
        run_with_transient_retries(operation, RetryPolicy(0, 1))

    assert calls == [False, True]
