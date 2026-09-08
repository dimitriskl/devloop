from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from devloop.cli import resolve_run_workflow
from devloop.issue_pack import Issue
from devloop.issue_scheduler import SchedulingPhase
from devloop.portable_execution_backend import RunWideBlocker, RunWideBlockerKind
from devloop.portable_workflow import (
    DEVELOPMENT_STEP_ID,
    SECURITY_REVIEW_STEP_ID,
    default_portable_component_catalog,
    default_portable_workflow,
    load_portable_workflow,
)
from devloop.state import LoopStateWriter
from devloop.usage_limit_retry import retry_usage_limited_run
from devloop.workflow_defaults import WorkflowDefaultStore


@pytest.mark.parametrize("preferences_already_applied", [False, True])
def test_resume_changed_backend_skips_previous_provider_wait(
    tmp_path: Path, preferences_already_applied: bool,
) -> None:
    _resume_after_preferences(tmp_path, "development", preferences_already_applied, 1000)


@pytest.mark.parametrize(
    "change", ["none", "review", "model", "changed_back", "new_pause", "missing_pause_event"],
)
def test_resume_retains_usage_wait_for_same_backend(tmp_path: Path, change: str) -> None:
    _resume_after_preferences(tmp_path, change, False, 1010)


def _resume_after_preferences(
    root: Path, change: str, preferences_already_applied: bool, expected_start: float,
) -> None:
    catalog = default_portable_component_catalog()
    document = default_portable_workflow().to_dict()
    development = next(
        step for step in document["steps"] if step["instance_id"] == DEVELOPMENT_STEP_ID
    )
    development["execution_settings"] = {
        "backend": "CLAUDE_CODE", "model": "claude-sonnet-5",
        "reasoning_effort": "high", "fast": "OFF",
    }
    original = load_portable_workflow(document, catalog)
    index = root / "README.md"
    index.write_text("", encoding="utf-8")
    writer = LoopStateWriter(index)
    writer.record_resolved_workflow(original, catalog)
    issue = Issue("0002", "Continue development", root / "0002.md", False)
    writer.issue_state(issue).update(
        status="IN_PROGRESS", current_pass=1,
        current_step_instance_id=str(DEVELOPMENT_STEP_ID),
    )
    writer.reserve_scheduling_attempt(issue, phase=SchedulingPhase.BLOCKER_RESOLUTION, ordinal=4)
    writer.record_run_paused(
        RunWideBlocker(RunWideBlockerKind.USAGE_LIMIT, "Claude usage is exhausted.", 1010),
        retry_at=1010,
    )
    cursor = deepcopy(writer.issue_state(issue))
    active_attempt = writer.active_scheduling_attempt()
    history = deepcopy(writer.state.get("step_attempt_records"))
    if change in {"development", "changed_back", "new_pause", "missing_pause_event"}:
        development["execution_settings"] = {
            "backend": "CODEX_CLI", "model": "gpt-5.6-terra",
            "reasoning_effort": "high", "fast": "OFF",
        }
    elif change == "model":
        development["execution_settings"]["model"] = "claude-opus-5"
    elif change == "review":
        review = next(
            step for step in document["steps"] if step["instance_id"] == SECURITY_REVIEW_STEP_ID
        )
        review["execution_settings"] = dict(development["execution_settings"])
    preferred = load_portable_workflow(document, catalog)
    if preferences_already_applied or change in {
        "changed_back", "new_pause", "missing_pause_event",
    }:
        # Replay an older runner that saved new preferences but retained its pause.
        writer.refresh_resolved_workflow_execution_preferences(preferred, catalog)
    if change == "changed_back":
        preferred = original
    elif change == "new_pause":
        writer.record_run_paused(
            RunWideBlocker(RunWideBlockerKind.USAGE_LIMIT, "Codex usage is exhausted.", 1010),
            retry_at=1010,
        )
    elif change == "missing_pause_event":
        writer.state["events"] = [
            event for event in writer.state["events"] if event["type"] != "run-paused"
        ]
        writer.flush()
    config = root / "devloop-plan.json"
    WorkflowDefaultStore(config, catalog).replace(preferred)

    writer = LoopStateWriter(index)
    resolved = resolve_run_workflow(writer, catalog, user_workflow_path=config)
    assert resolved.step(DEVELOPMENT_STEP_ID).execution_settings == (
        preferred.step(DEVELOPMENT_STEP_ID).execution_settings
    )
    assert writer.issue_state(issue) == cursor
    assert writer.active_scheduling_attempt() == active_attempt
    assert writer.state.get("step_attempt_records") == history
    # Reload to verify the retry behavior follows durable state, not just memory.
    writer = LoopStateWriter(index)
    now = 1000.0
    starts: list[float] = []
    countdown: list[str] = []

    def wait(seconds: float) -> None:
        nonlocal now
        now += seconds

    retry_usage_limited_run(
        lambda: starts.append(now), writer, countdown.append, clock=lambda: now, wait=wait,
    )
    assert starts == [expected_start]
    assert bool(countdown) is (expected_start > 1000)
    assert writer.active_scheduling_attempt() == active_attempt
