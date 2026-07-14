from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from textual.widgets import Static

from devloop.application.capabilities import (
    CapabilityProfileService,
    standard_capability_catalog,
)
from devloop.application.commands import launcher_command_registry
from devloop.domain.development import IssueStatus
from devloop.domain.identifiers import (
    AttemptId,
    CapabilityId,
    IssueId,
    StepComponentId,
    StepInstanceId,
)
from devloop.domain.run import BackendActivity, WorkflowRunStatus
from devloop.domain.scheduler import DependencyReadiness, IssueBoardRow
from devloop.ui.app import RunLauncherApp
from devloop.ui.composer import Composer
from devloop.ui.modals import CapabilityOptionsModal, StopModal
from devloop.ui.shared import IssueBoard, WorkflowStatusBar, WorkflowStatusModel


def test_workflow_status_projection_is_typed_and_single_line() -> None:
    status = WorkflowStatusModel(
        workflow_status=WorkflowRunStatus.RUNNING,
        step=StepInstanceId("code-review"),
        issue_id=IssueId("ISSUE-007"),
        issue_position=7,
        issue_total=10,
        issue_status=IssueStatus.IN_REVIEW,
        attempt=AttemptId("attempt-002"),
        backend_activity=BackendActivity.STREAMING,
        elapsed=timedelta(hours=1, minutes=2, seconds=3),
    )

    rendered = status.render()

    assert rendered == (
        "RUNNING | CODE REVIEW | ISSUE-007 7/10 IN_REVIEW | "
        "attempt-002 | STREAMING | 01:02:03"
    )
    assert "\n" not in rendered


@pytest.mark.asyncio
async def test_shell_preserves_multilingual_composer_and_fixed_status_on_resize() -> None:
    content = "Ελληνικά café 漢字 مرحبا\nשורה שנייה\nemoji: 👩🏽‍💻"
    app = RunLauncherApp(Path.cwd(), launcher_command_registry())

    async with app.run_test(size=(120, 32)) as pilot:
        composer = app.query_one("#composer", Composer)
        composer.load_text(content)
        assert composer.text == content
        assert app.query_one("#status", WorkflowStatusBar).region.height == 1

        await pilot.resize_terminal(40, 12)
        await pilot.pause()

        status = app.query_one("#status", WorkflowStatusBar)
        assert status.region.height == 1
        assert composer.region.bottom <= status.region.y
        assert "\n" not in str(status.render())


@pytest.mark.asyncio
async def test_issue_board_supports_inspection_without_scheduling_actions() -> None:
    row = IssueBoardRow(
        IssueId("ISSUE-007"),
        7,
        "Complete shared TUI",
        IssueStatus.IN_QA,
        DependencyReadiness.READY,
        StepInstanceId("qa"),
        2,
        (),
    )
    app = RunLauncherApp(Path.cwd(), launcher_command_registry())

    async with app.run_test(size=(120, 32)) as pilot:
        board = app.query_one("#issue-board", IssueBoard)
        board.show_rows((row,))
        board.display = True
        await pilot.pause()

        assert board.inspect(IssueId("ISSUE-007")) == row
        assert not hasattr(board, "start_issue")
        assert not hasattr(board, "reorder")
        assert board.region.bottom <= app.query_one("#status", Static).region.y


@pytest.mark.asyncio
async def test_options_modal_applies_replaceable_defaults_transactionally(
    tmp_path: Path,
) -> None:
    capabilities = CapabilityProfileService(tmp_path, standard_capability_catalog())
    app = RunLauncherApp(
        Path.cwd(),
        launcher_command_registry(),
        capability_service=capabilities,
    )

    async with app.run_test(size=(120, 32)) as pilot:
        app.post_message(Composer.Submitted("/options"))
        await pilot.pause()
        assert isinstance(app.screen, CapabilityOptionsModal)
        option_list = app.screen.query_one("#capability-options")
        target = next(
            index
            for index in range(option_list.option_count)
            if option_list.get_option_at_index(index).id == "development|tdd"
        )
        option_list.highlighted = target
        option_list.focus()
        await pilot.press("enter")
        await pilot.click("#capability-apply")

    selected = capabilities.begin().profile(StepComponentId("development")).selected
    assert CapabilityId("tdd") not in selected


@pytest.mark.asyncio
async def test_ctrl_c_opens_explicit_stop_actions_instead_of_quitting() -> None:
    app = RunLauncherApp(Path.cwd(), launcher_command_registry())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("ctrl+c")
        await pilot.pause()

        assert isinstance(app.screen, StopModal)
        assert app.screen.query_one("#stop-continue").display is True
        assert app.is_running
