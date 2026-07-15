from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from devloop.domain.execution import (
    ExecutionPhase,
    ExecutionProfile,
    ExecutionProfileId,
    ExecutionTelemetry,
    execution_profile_for_issue,
)


def test_execution_profile_is_versioned_and_content_addressed() -> None:
    profile = ExecutionProfile.full(
        component_id="development",
        model="gpt-5.6-sol",
        reasoning_effort="xhigh",
        timeout_seconds=1800.0,
        checkpoint_seconds=300.0,
    )

    assert profile.profile_id is ExecutionProfileId.FULL
    assert len(profile.profile_hash) == 64
    assert profile.budget.version == "1.0.0"


def test_small_issue_may_select_lightweight_without_changing_policy() -> None:
    full = ExecutionProfile.full("development", "gpt-5.6-sol", "xhigh", 1800.0, 300.0)
    light = ExecutionProfile.lightweight("development", "gpt-5.6-sol", "low", 600.0, 120.0)

    selected = execution_profile_for_issue(
        (full, light),
        acceptance_count=2,
        rework_count=0,
    )

    assert selected.profile_id is ExecutionProfileId.LIGHTWEIGHT
    assert selected.model == full.model


def test_telemetry_is_ordered_bounded_and_append_only() -> None:
    start = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
    telemetry = ExecutionTelemetry()
    telemetry = telemetry.record(ExecutionPhase.CONTEXT_LOADED, start)
    telemetry = telemetry.record(ExecutionPhase.FIRST_ACTIVITY, start + timedelta(seconds=2))
    telemetry = telemetry.record(ExecutionPhase.FIRST_FILE_CHANGE, start + timedelta(seconds=4))
    telemetry = telemetry.record(ExecutionPhase.VERIFICATION_STARTED, start + timedelta(seconds=5))
    telemetry = telemetry.record(ExecutionPhase.STRUCTURED_OUTPUT, start + timedelta(seconds=7))
    telemetry = telemetry.record(ExecutionPhase.COMPLETED, start + timedelta(seconds=8))

    assert [item.phase for item in telemetry.events] == list(ExecutionPhase)
    assert telemetry.events[-1].elapsed_ms == 8000

    with pytest.raises(ValueError, match="already complete"):
        telemetry.record(ExecutionPhase.FIRST_ACTIVITY, start + timedelta(seconds=9))
