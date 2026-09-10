from __future__ import annotations

import asyncio
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from textual.widgets import Static

from devloop import codex_runner, operator_handoff
from devloop.cli import _PortableConsoleRoleRunner
from devloop.codex_runner import CodexRunner, RoleResult
from devloop.issue_pack import Issue
from devloop.operator_handoff import OperatorHandoff
from devloop.portable_execution_backend import ExecutionBackendId, StepAttemptResult
from devloop.portable_protocol import MAX_PORTABLE_PROTOCOL_TEXT_CHARACTERS
from devloop.portable_runtime import PortableRuntimeBridge, portable_runtime_session
from devloop.portable_sessions import PortableSessionSnapshot, PortableSessionStatus
from devloop.portable_ui.app import PortableApplicationShell
from devloop.portable_worker import PortableWorkerRuntimeBridge
from devloop.statusui import IssueDashboard, Stage
from devloop.step_configuration import MAX_STEP_GUIDANCE_CHARACTERS
from devloop.templates import BundleContext, Preset
from devloop.verification_feedback import VerificationFailed


@pytest.mark.parametrize("failed", [False, True])
def test_console_handoff_restores_dashboard_before_resuming_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: bool,
) -> None:
    bridge = PortableRuntimeBridge()
    with portable_runtime_session(bridge):
        dashboard = IssueDashboard(
            issue_number="0003", issue_title="Verification", position=1, total=3,
            frame_seconds=60,
        )
        runner = SimpleNamespace(repo_root=tmp_path, run_role=Mock())
        writer = SimpleNamespace(state={})
        issue = Issue("0003", "Verification", tmp_path / "0003.md", False)
        console = _PortableConsoleRoleRunner(
            runner=runner, issue=issue, dashboard=dashboard, progress="", activity_progress="",
            initial_fix_list=[], attempt_label=None, state_writer=writer,
        )
        record = {
            "repository": str(tmp_path), "source_fingerprint": "source",
            "gate": {"kind": "DOTNET_TEST", "project_path": "tests.csproj",
                     "test_filter": "ClassName=Tests", "expected_tests": 12, "reason": "SQL"},
        }
        writer.state[operator_handoff.STATE_KEY] = {json.dumps(["0003", "dev", 1]): record}
        monkeypatch.setattr(operator_handoff, "source_fingerprint", lambda _: "source")

        def verify(_record):
            bridge.show_screen("Verification finished: 12 tests")
            if failed:
                raise VerificationFailed("Failed test needs repair")
            return "12 passed, zero skipped"

        monkeypatch.setattr(console._handoff, "_wait", verify)

        def resumed(**kwargs):
            events = []
            while (event := bridge.try_next_event()) is not None:
                events.append(event)
            screens = [event.content for event in events if event.content]
            assert "WORKING" in screens[-1]
            assert dashboard._thread is not None and dashboard._thread.is_alive()
            assert dashboard._statuses[Stage.DEVELOPMENT].value == "WORKING"
            return RoleResult(status="FAIL", summary="Continue repair")

        runner.run_role.side_effect = resumed
        try:
            console.run_role(
                role="coder", role_adapter="coder", step_display_name="Development",
                step_instance_id="dev", issue=issue, pass_number=1,
            )
        finally:
            dashboard.close()


@pytest.mark.parametrize("role", ["coder", "reviewer", "qa"])
@pytest.mark.parametrize("feedback_size", [7957, 17000])
def test_saved_failure_reaches_real_prompt_and_worker_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, feedback_size: int,
) -> None:
    """Replay the failed-receipt handoff through the real prompt and protocol boundaries."""
    backend = Mock()
    backend.backend_id = ExecutionBackendId.CODEX_CLI
    backend.invoke.return_value = StepAttemptResult(
        process=subprocess.CompletedProcess(["fake-backend"], 0, "", ""),
        message=json.dumps({"status": "FAIL", "summary": "Concrete repair needed"}),
    )
    runner = CodexRunner(
        bundle=BundleContext.from_file(Path(codex_runner.__file__).resolve()),
        repo_root=tmp_path,
        prd_path=tmp_path / "prd.md",
        issues_index=tmp_path / "README.md",
        preset=Preset(name="feedback", required_docs=[], roles={}),
        execution_backend=backend,
        dry_run=False,
        use_self_improvement_wiki=False,
    )
    issue = Issue("0003", "Repair SQL tests", tmp_path / "0003.md", False)
    record = {
        "repository": str(tmp_path),
        "source_fingerprint": "same-source",
        "gate": {
            "kind": "DOTNET_TEST", "project_path": "tests.csproj",
            "test_filter": "ClassName=Tests", "expected_tests": 6, "reason": "SQL gate",
        },
    }
    writer = SimpleNamespace(
        state={operator_handoff.STATE_KEY: {json.dumps(["0003", role, 1]): record}},
    )
    handoff = OperatorHandoff(runner, writer)
    feedback = "Automatic verification FAILED\nTRX: verification.trx\n" + "x" * feedback_size
    feedback += "\nTableExistsAsync missing setup; repair the mock."
    monkeypatch.setattr(handoff, "_wait", Mock(side_effect=VerificationFailed(feedback)))
    monkeypatch.setattr(operator_handoff, "source_fingerprint", lambda _: "same-source")
    output = io.StringIO()
    bridge = PortableWorkerRuntimeBridge(
        "feedback-replay", command_stream=io.StringIO(), event_stream=output,
    )
    monkeypatch.setattr(operator_handoff, "active_portable_runtime", lambda: bridge)
    guidance = "G" * MAX_STEP_GUIDANCE_CHARACTERS

    result = handoff.run_role({
        "role": role, "issue": issue, "pass_number": 1, "step_instance_id": role,
        "step_guidance": guidance,
    })

    assert result.status == "FAIL"
    backend.invoke.assert_called_once()
    prompt = backend.invoke.call_args.args[0].prompt
    assert guidance in prompt
    assert feedback in prompt
    screens = [json.loads(line)["payload"]["content"] for line in output.getvalue().splitlines()
               if json.loads(line)["kind"] == "SCREEN"]
    assert screens
    assert all(len(screen) <= MAX_PORTABLE_PROTOCOL_TEXT_CHARACTERS for screen in screens)
    assert "TRX: verification.trx" in screens[-1]


def test_failed_session_prioritizes_diagnostic_over_frozen_working_screen() -> None:
    async def check() -> None:
        supervisor = SimpleNamespace(
            list_sessions=lambda: (), try_next_event=lambda: None, shutdown=lambda: None,
        )
        app = PortableApplicationShell(
            PortableRuntimeBridge(), operation=lambda: 0, session_supervisor=supervisor,
        )
        snapshot = PortableSessionSnapshot(
            session_id="failed-feedback", checkout=Path.cwd(),
            status=PortableSessionStatus.FAILED, result=1,
            current_screen="WORKING Development elapsed 01:44:24",
            diagnostics=("Step Guidance cannot exceed 4000 characters before redaction.",),
        )
        async with app.run_test(size=(120, 34)):
            app._active_session_id = snapshot.session_id
            app._show_session_snapshot(snapshot)
            detail = str(app.query_one("#portable-detail", Static).render())
            assert "Status: FAILED" in detail
            assert snapshot.diagnostics[0] in detail
            assert snapshot.current_screen not in detail
            assert "Session ended" in detail
    asyncio.run(check())
