from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from textual.widgets import Button, RichLog

from devloop.analysis.package import parse_analysis_draft
from devloop.application.analysis import AnalysisRunResult
from devloop.application.commands import launcher_command_registry
from devloop.components.analysis import ANALYSIS_COMPONENT_ID, builtin_component_registry
from devloop.domain.identifiers import FeatureSlug, StepInstanceId, WorkflowRunId
from devloop.domain.run import (
    AnalysisCursor,
    ComponentLock,
    ResolvedWorkflow,
    RunLease,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.persistence.run_store import RUN_SNAPSHOT_SCHEMA
from devloop.ui.analysis import AnalysisView
from devloop.ui.app import RunLauncherApp
from devloop.workflow.definition import load_standard_workflow

RUN_ID = WorkflowRunId("run-20260710t120002-123456abcdef")


def _draft_payload() -> dict[str, object]:
    return {
        "schema": "devloop.analysis-draft/v1",
        "feature_title": "Price comparison",
        "feature_slug": "price-comparison",
        "prd_markdown": """<!-- devloop:prd:v1 -->
<!-- devloop:section:problem -->
REQ-001
<!-- devloop:section:solution -->
Solution
<!-- devloop:section:requirements -->
REQ-001
""",
        "requirements": ["REQ-001"],
        "issues": [
            {
                "id": "ISSUE-001",
                "slug": "compare-totals",
                "title": "Compare totals",
                "requirements": ["REQ-001"],
                "dependencies": [],
                "acceptance_criteria": [
                    {"id": "AC-ISSUE-001-001", "text": "Select the lowest total."}
                ],
                "markdown": """<!-- devloop:issue:v1 -->
<!-- devloop:section:description -->
Compare.
<!-- devloop:section:acceptance -->
AC-ISSUE-001-001
""",
            }
        ],
        "revision": 1,
    }


@pytest.mark.asyncio
async def test_analysis_view_presents_draft_issues_validation_and_actions() -> None:
    workflow = load_standard_workflow()
    manifest, _ = builtin_component_registry().resolve(ANALYSIS_COMPONENT_ID)
    draft = parse_analysis_draft(_draft_payload(), RUN_ID)
    snapshot = WorkflowRunSnapshot(
        RUN_SNAPSHOT_SCHEMA,
        RUN_ID,
        str(Path.cwd()),
        draft.feature_title,
        FeatureSlug("price-comparison"),
        ResolvedWorkflow(workflow.workflow_id, workflow.version, workflow.definition_hash),
        (
            ComponentLock(
                manifest.component_id,
                manifest.version,
                manifest.distribution,
                manifest.package_hash,
            ),
        ),
        StepInstanceId("analysis"),
        WorkflowRunStatus.AWAITING_USER,
        StepRunStatus.AWAITING_USER,
        None,
        AnalysisCursor(draft_revision=1),
        RunLease("lease", 1, datetime.now(timezone.utc).isoformat()),
        3,
        datetime.now(timezone.utc).isoformat(),
    )
    result = AnalysisRunResult(snapshot, draft, (), None)
    app = RunLauncherApp(Path.cwd(), launcher_command_registry())

    async with app.run_test(size=(120, 36)) as pilot:
        view = app.query_one("#analysis-view", AnalysisView)
        view.append_activity("Streaming the persisted analysis thread.")
        view.show_result(result)
        await pilot.pause()
        await pilot.pause()

        assert view.display is True
        assert app.query_one("#analysis-accept", Button).disabled is False
        issue_lines = app.query_one("#analysis-issues", RichLog).lines
        assert any("Compare." in line.text for line in issue_lines)
        activity_lines = app.query_one("#analysis-activity", RichLog).lines
        assert any(
            "Streaming the persisted analysis thread." in line.text
            for line in activity_lines
        )
