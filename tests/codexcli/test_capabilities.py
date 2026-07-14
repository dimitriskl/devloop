from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from devloop.application.capabilities import (
    CapabilityProfileService,
    standard_capability_catalog,
)
from devloop.domain.identifiers import (
    CapabilityId,
    FeatureSlug,
    StepComponentId,
    StepInstanceId,
    WorkflowId,
    WorkflowRunId,
)
from devloop.domain.run import (
    AnalysisCursor,
    ResolvedWorkflow,
    RunLease,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.persistence.run_store import (
    RUN_SNAPSHOT_SCHEMA,
    snapshot_from_dict,
    snapshot_to_dict,
)


def test_capability_options_are_transactional_searchable_and_lock_required_items(
    tmp_path: Path,
) -> None:
    service = CapabilityProfileService(tmp_path, standard_capability_catalog())
    development = StepComponentId("development")
    tdd = CapabilityId("tdd")
    required = CapabilityId("implement")

    cancelled = service.begin()
    assert {item.capability_id for item in cancelled.search("test-driven")} == {tdd}
    cancelled.toggle(development, tdd)
    with pytest.raises(ValueError, match="required"):
        cancelled.toggle(development, required)
    cancelled.cancel()

    unchanged = service.begin()
    assert tdd in unchanged.profile(development).selected
    unchanged.toggle(development, tdd)
    applied = unchanged.apply()

    assert tdd not in applied.profile(development).selected
    assert required in applied.profile(development).resolved
    assert tdd not in service.begin().profile(development).selected


def test_run_snapshot_keeps_resolved_capabilities_after_user_defaults_change(
    tmp_path: Path,
) -> None:
    service = CapabilityProfileService(tmp_path, standard_capability_catalog())
    locked_profiles = service.resolved_profiles()
    now = datetime.now(timezone.utc).isoformat()
    snapshot = WorkflowRunSnapshot(
        schema=RUN_SNAPSHOT_SCHEMA,
        run_id=WorkflowRunId("run-20260712t120000-123456abcdef"),
        repository=str(tmp_path),
        feature_title="Unicode capability snapshot",
        feature_slug=FeatureSlug("unicode-capability-snapshot"),
        workflow=ResolvedWorkflow(WorkflowId("standard"), "1.0.0", "hash"),
        component_locks=(),
        active_step=StepInstanceId("analysis"),
        run_status=WorkflowRunStatus.CREATED,
        step_status=StepRunStatus.NOT_STARTED,
        outcome=None,
        analysis=AnalysisCursor(),
        lease=RunLease("lease", 1, now),
        event_sequence=0,
        updated_at=now,
        capability_profiles=locked_profiles,
    )

    options = service.begin()
    options.toggle(StepComponentId("development"), CapabilityId("tdd"))
    options.apply()
    restored = snapshot_from_dict(snapshot_to_dict(snapshot))

    assert restored.capability_profiles == locked_profiles
    development = next(
        item
        for item in restored.capability_profiles
        if item.component_id == StepComponentId("development")
    )
    assert CapabilityId("tdd") in development.capabilities
