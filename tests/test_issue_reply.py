from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from textual.widgets import Input, TextArea

from devloop import codex_runner
from devloop.codex_runner import CodexRunner
from devloop.issue_pack import Issue
from devloop.issue_reply import IssueReply, issue_reply_context, save_issue_reply
from devloop.portable_execution_backend import ExecutionBackendId, StepAttemptResult
from devloop.portable_execution_backend.codex_cli import CodexCliExecutionBackend
from devloop.portable_protocol import PortableProtocolFrame
from devloop.portable_runtime import PortableRuntimeBridge, portable_runtime_session
from devloop.portable_sessions import (
    PortableSessionInputKind,
    PortableSessionInputRequest,
    PortableSessionLaunch,
    PortableSessionSnapshot,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
)
from devloop.portable_ui.app import PortableApplicationShell
from devloop.portable_ui.reply_screen import IssueReplyScreen
from devloop.portable_worker import PortableWorkerRuntimeBridge
from devloop.reply_review import reply_to_issue
from devloop.run_review import RunReviewAction, build_run_review, run_review_options
from devloop.templates import BundleContext, Preset


def review():
    return build_run_review(
        [Issue("0003", "Done", Path("3.md"), True),
         Issue("0004", "Recovery", Path("4.md"), False),
         Issue("0005", "Synchronization", Path("5.md"), False)],
        {"0004": {"status": "BLOCKED", "fix_list": ["May we introduce a Store receipt?"]},
         "0005": {"status": "WAITING_ON_DEPENDENCY", "waiting_on": ["0004"]}},
        loop_state_path=Path("README.loop.md"), rerun_available=True,
    )


def test_reply_is_offered_and_saved_only_after_submission(tmp_path: Path) -> None:
    assert RunReviewAction.REPLY.value in dict(run_review_options(review()))
    runtime = Mock()
    runtime.read_reply.return_value = ""
    with portable_runtime_session(runtime):
        assert not reply_to_issue(review(), tmp_path)
        assert not list(tmp_path.iterdir())
        runtime.read_reply.return_value = IssueReply("Use the existing transaction.").encode()
        assert reply_to_issue(review(), tmp_path)
    assert "May we introduce a Store receipt?" in runtime.read_reply.call_args.args[0]
    assert "Use the existing transaction." in issue_reply_context(tmp_path, "0004")[0]
    assert issue_reply_context(tmp_path, "0005") == ("", ())


def test_unreadable_attachment_reopens_editor_with_original_draft(tmp_path: Path) -> None:
    reply = IssueReply("Keep my answer", (tmp_path / "missing.pdf",))
    runtime = Mock()
    runtime.read_reply.side_effect = [reply.encode(), ""]
    with portable_runtime_session(runtime):
        assert not reply_to_issue(review(), tmp_path)
    assert runtime.read_reply.call_args.kwargs["initial_value"] == reply.encode()
    assert not list(tmp_path.glob("**/reply.json"))


@pytest.mark.parametrize("role", ["coder", "reviewer", "qa"])
def test_reply_and_durable_attachments_reach_real_backend_request(
    tmp_path: Path, role: str,
) -> None:
    source = tmp_path / "screen with spaces.png"
    source.write_bytes(b"image-content")
    document = tmp_path / "decision.txt"
    document.write_text("Supporting evidence", encoding="utf-8")
    log_root = tmp_path / ".loop.logs"
    save_issue_reply(log_root, "0004", IssueReply("Answer\nsecond line", (source, document)))
    source.unlink()
    document.unlink()
    backend = Mock()
    backend.backend_id = ExecutionBackendId.CODEX_CLI
    backend.invoke.return_value = StepAttemptResult(
        process=subprocess.CompletedProcess([], 0, "", ""),
        message=json.dumps({"status": "FAIL", "summary": "Still needs work"}),
    )
    runner = CodexRunner(
        bundle=BundleContext.from_file(Path(codex_runner.__file__).resolve()),
        repo_root=tmp_path, prd_path=tmp_path / "prd.md", issues_index=tmp_path / "README.md",
        preset=Preset(name="reply", required_docs=[], roles={}), execution_backend=backend,
        use_self_improvement_wiki=False, dry_run=False,
    )
    result = runner.run_role(role, Issue("0004", "Recovery", tmp_path / "4.md", False), 1)
    assert result.status == "FAIL"  # A reply cannot manufacture a passing gate.
    request = backend.invoke.call_args.args[0]
    assert "Answer\nsecond line" in request.prompt
    assert "decision.txt" in request.prompt
    assert len(request.image_paths) == 1
    assert request.image_paths[0].read_bytes() == b"image-content"
    with patch(
        "devloop.portable_execution_backend.codex_cli.run_codex_exec_with_connection_retries",
        return_value=subprocess.CompletedProcess([], 1, "", ""),
    ) as execute, patch(
        "devloop.portable_execution_backend.codex_cli.uses_legacy_approval_flag",
        return_value=False,
    ):
        CodexCliExecutionBackend(codex="codex").invoke(request)
    command = execute.call_args.kwargs["command"]
    assert command[command.index("-i") + 1] == str(request.image_paths[0])
    assert command[-1] == "-"


def test_worker_reply_protocol_round_trip() -> None:
    encoded = IssueReply("Multiline\nreply").encode()
    commands = io.StringIO(json.dumps({
        "version": 1, "session_id": "reply", "sequence": 2, "kind": "USER_INPUT",
        "payload": {"value": encoded, "request_id": "request", "request_generation": 1},
    }) + "\n")
    events = io.StringIO()
    bridge = PortableWorkerRuntimeBridge("reply", command_stream=commands, event_stream=events)
    with patch("devloop.portable_worker.uuid.uuid4", return_value="request"):
        assert bridge.read_reply("Question", initial_value=IssueReply().encode()) == encoded
    frame = PortableProtocolFrame.parse(
        events.getvalue().splitlines()[0], expected_session_id="reply",
    )
    assert frame.payload["request_kind"] == "REPLY"
    assert frame.payload["initial_value"] == IssueReply().encode()


def test_supervisor_routes_reply_to_owning_worker_and_rejects_stale_request(tmp_path: Path) -> None:
    worker = tmp_path / "reply_worker.py"
    worker.write_text('''import json, sys
from pathlib import Path
json.loads(sys.stdin.readline())
def send(sequence, kind, payload):
    print(json.dumps(dict(version=1, session_id="reply-worker", sequence=sequence,
                         kind=kind, payload=payload)), flush=True)
send(1, "HELLO", {})
send(2, "INPUT_REQUEST", dict(request_kind="REPLY", prompt="Recovery question",
    request_id="reply-q", request_generation=4,
    initial_value=json.dumps(dict(text="draft", attachments=[]))))
while True:
    value=json.loads(sys.stdin.readline())
    if value["kind"] == "USER_INPUT":
        Path("answer.json").write_text(json.dumps(value), encoding="utf-8")
        break
send(3, "COMPLETION", dict(exit_code=0))
''', encoding="utf-8")

    def launch_worker(launch):
        return subprocess.Popen(
            [sys.executable, "-u", str(worker)], cwd=launch.checkout,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )

    supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
    launch = PortableSessionLaunch(
        session_id="reply-worker", checkout=tmp_path,
        operation=PortableWorkflowOperation.DELIVERY, arguments=(),
    )
    try:
        supervisor.start_session(launch)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            snapshot = supervisor.snapshot(launch.session_id)
            if snapshot.input_request is not None:
                break
            time.sleep(0.01)
        request = snapshot.input_request
        assert request is not None
        assert request.kind is PortableSessionInputKind.REPLY
        assert IssueReply.decode(request.initial_value).text == "draft"
        with pytest.raises(ValueError, match="no longer"):
            supervisor.provide_input(
                launch.session_id, "wrong", request_id="reply-q", request_generation=3,
            )
        answer = IssueReply("Answer\nwith lines").encode()
        supervisor.provide_input(
            launch.session_id, answer, request_id="reply-q", request_generation=4,
        )
        supervisor.wait_for_terminal(launch.session_id, timeout=10)
        assert json.loads((tmp_path / "answer.json").read_text())["payload"]["value"] == answer
    finally:
        supervisor.shutdown()


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
def test_session_reply_editor_survives_updates_and_submits_correlated_input(
    tmp_path: Path, size: tuple[int, int],
) -> None:
    async def check() -> None:
        submitted = Mock()
        supervisor = SimpleNamespace(
            list_sessions=lambda: (), try_next_event=lambda: None, shutdown=lambda: None,
        )
        app = PortableApplicationShell(
            PortableRuntimeBridge(), operation=lambda: 0, session_supervisor=supervisor,
        )
        snapshot = PortableSessionSnapshot(
            session_id="reply", checkout=tmp_path, status=PortableSessionStatus.WAITING_FOR_INPUT,
            input_request=PortableSessionInputRequest(
                kind=PortableSessionInputKind.REPLY, request_id="q", generation=3,
                prompt="May we introduce a Store receipt?",
            ),
        )
        app._provide_session_input = submitted
        async with app.run_test(size=size) as pilot:
            app._active_session_id = "reply"
            app._show_session_snapshot(snapshot)
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, IssueReplyScreen)
            editor = screen.query_one("#reply-body", TextArea)
            await pilot.press("a", "enter", "b")
            assert editor.text == "a\nb"
            app._show_session_snapshot(replace(snapshot, activity=("Background update",)))
            await pilot.pause()
            assert app.screen is screen
            assert editor.text == "a\nb"
            file = tmp_path / "file with spaces.txt"
            file.write_text("evidence", encoding="utf-8")
            path_input = screen.query_one("#reply-file", Input)
            path_input.value = f'"{file}"'
            path_input.focus()
            await pilot.press("enter")
            assert screen.paths == [file.resolve()]
            assert editor.text == "a\nb"
            assert screen.query_one("#reply-actions").region.bottom <= size[1]
            assert screen.query_one("#reply-hints").region.bottom <= size[1]
            await pilot.press("ctrl+enter")
            await pilot.pause()
            submitted.assert_called_once()
            assert submitted.call_args.kwargs == {
                "session_id": "reply", "request_id": "q", "request_generation": 3,
            }
            assert IssueReply.decode(submitted.call_args.args[0]).text == "a\nb"
            app._show_session_snapshot(snapshot)
            await pilot.pause()
            assert not isinstance(app.screen, IssueReplyScreen)
    asyncio.run(check())


def test_empty_submission_stays_open_and_escape_cancels() -> None:
    async def check() -> None:
        app = PortableApplicationShell(PortableRuntimeBridge(), operation=lambda: 0)
        received = Mock()
        async with app.run_test() as pilot:
            app.push_screen(IssueReplyScreen("Question"), received)
            await pilot.pause()
            await pilot.press("ctrl+enter")
            assert isinstance(app.screen, IssueReplyScreen)
            received.assert_not_called()
            await pilot.press("escape")
            await pilot.pause()
            received.assert_called_once_with("")
    asyncio.run(check())
