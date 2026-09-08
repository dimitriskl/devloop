from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest
from textual.widgets import RichLog, Static

from devloop.portable_protocol import PortableProtocolFrame
from devloop.portable_runtime import PortableRuntimeBridge
from devloop.portable_sessions import (
    PortableSessionInputKind,
    PortableSessionInputRequest,
    PortableSessionLaunch,
    PortableSessionSnapshot,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
    _RunningSession,
)
from devloop.portable_ui.app import PortableApplicationShell
from devloop.portable_worker import PortableWorkerRuntimeBridge


def test_worker_screen_refreshes_replace_progress_without_filling_activity(tmp_path: Path) -> None:
    output = io.StringIO()
    bridge = PortableWorkerRuntimeBridge(
        "progress", command_stream=io.StringIO(), event_stream=output,
    )
    bridge.write_output("Starting self-improvement", is_error=False)
    for frame in range(120):
        bridge.show_screen(f"[qa] self-improvement | WORKING [/] 00:01:{frame:02d}")
    bridge.write_output("Wiki saved", is_error=False)
    bridge.show_screen("Self-improvement complete\x1b[2J")
    supervisor = PortableSessionSupervisor()
    running = _RunningSession(process=mock.Mock(), generation=1)
    supervisor._running["progress"] = running
    supervisor._snapshots["progress"] = PortableSessionSnapshot(
        session_id="progress", checkout=tmp_path, status=PortableSessionStatus.RUNNING,
    )
    for sequence, line in enumerate(output.getvalue().splitlines(), start=1):
        frame = PortableProtocolFrame.parse(
            line, expected_session_id="progress", expected_sequence=sequence,
        )
        assert supervisor._apply_worker_frame("progress", frame, running)

    snapshot = supervisor.snapshot("progress")
    assert snapshot.activity == ("Starting self-improvement", "Wiki saved")
    assert snapshot.current_screen == "Self-improvement complete"


@pytest.mark.asyncio
async def test_session_view_replaces_live_frame_and_keeps_completion_actions(
    tmp_path: Path,
) -> None:
    supervisor = PortableSessionSupervisor()
    snapshot = PortableSessionSnapshot(
        session_id="progress", checkout=tmp_path, status=PortableSessionStatus.RUNNING,
        activity=("Starting self-improvement",),
    )
    supervisor._snapshots[snapshot.session_id] = snapshot
    app = PortableApplicationShell(
        PortableRuntimeBridge(), session_supervisor=supervisor,
        session_launch=PortableSessionLaunch(
            session_id="new", checkout=tmp_path,
            operation=PortableWorkflowOperation.PLANNING, arguments=(),
        ),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        app._active_session_id = snapshot.session_id
        for frame in range(5):
            snapshot = replace(snapshot, current_screen=f"Self-improvement frame {frame}")
            app._show_session_snapshot(snapshot)
            await pilot.pause()
        activity = "\n".join(
            line.text for line in app.query_one("#portable-activity", RichLog).lines
        )
        assert "Starting self-improvement" in activity
        assert "Self-improvement frame 4" in activity
        assert "Self-improvement frame 3" not in activity
        completion = (
            "Dev Loop > Completion Review\nWhat to do next\nRestore the SQL test environment."
        )
        snapshot = replace(
            snapshot, current_screen=completion, status=PortableSessionStatus.WAITING_FOR_INPUT,
            input_request=PortableSessionInputRequest(
                kind=PortableSessionInputKind.CHOICE,
                options=(("exit", "Exit Dev Loop"),), default_key="exit", cancel_key="exit",
            ),
        )
        app._show_session_snapshot(snapshot)
        await pilot.pause()
        detail = str(app.query_one("#portable-detail", Static).render())
        assert completion in detail
        activity = "\n".join(
            line.text for line in app.query_one("#portable-activity", RichLog).lines
        )
        assert "Restore the SQL test environment" not in activity
        await pilot.press("f4")
        await pilot.pause()
        logs = "\n".join(
            line.text for line in app.screen.query_one("#portable-log-content", RichLog).lines
        )
        assert "Restore the SQL test environment" in logs
        assert "Starting self-improvement" in logs
        assert "Self-improvement frame 3" not in logs
        await pilot.press("escape")
