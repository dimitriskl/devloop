from __future__ import annotations

import ctypes
import io
import json
import os
import queue
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict
from pathlib import Path

import pytest

import devloop.execution.app_server as app_server_module
from devloop.execution.app_server import (
    AppServerClient,
    AppServerCheckpointDeadline,
    AppServerError,
    AppServerHandshake,
    AppServerTurnResult,
    AppServerTurnStatus,
    AuthenticationReadiness,
    cooperative_cancellation,
    is_transient_turn_failure,
)


def test_checkpoint_deadline_interrupts_and_preserves_exact_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]

    class Reader:
        calls = 0

        def get(self, *, timeout: float) -> bytes:
            del timeout
            self.calls += 1
            if self.calls == 1:
                now[0] = 6.0
                raise queue.Empty
            return json.dumps(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-stalled",
                        "turn": {
                            "id": "turn-stalled",
                            "status": "interrupted",
                            "items": [{"id": "item-read", "type": "commandExecution"}],
                        },
                    },
                }
            ).encode("utf-8")

    client = AppServerClient("codex")
    client._initialized = True
    client._reader_items = Reader()  # type: ignore[assignment]
    interrupts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        client,
        "interrupt_turn",
        lambda thread_id, turn_id: interrupts.append((thread_id, turn_id)),
    )

    with pytest.raises(AppServerCheckpointDeadline) as raised:
        client.wait_for_turn(
            "thread-stalled",
            "turn-stalled",
            timeout_seconds=30.0,
            checkpoint_seconds=5.0,
            clock=lambda: now[0],
        )

    assert interrupts == [("thread-stalled", "turn-stalled")]
    assert raised.value.thread_id == "thread-stalled"
    assert raised.value.turn_id == "turn-stalled"
    assert raised.value.completed_item_ids == ("item-read",)


class _GracefulChildProcess:
    def __init__(self) -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.returncode: int | None = None
        self.pid = 4242
        self.forced_terminations = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.stdin.closed:
            self.returncode = 0
            return self.returncode
        raise subprocess.TimeoutExpired("codex app-server", timeout)

    def terminate(self) -> None:
        self.forced_terminations += 1
        self.returncode = -15

    def kill(self) -> None:
        self.forced_terminations += 1
        self.returncode = -9


class _UncooperativeChildProcess(_GracefulChildProcess):
    def __init__(self) -> None:
        super().__init__()
        self.wait_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0
        self.reaped = False

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired("codex app-server", timeout)
        self.reaped = True
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9


class _EofExitingGroupLeader(_GracefulChildProcess):
    def __init__(self) -> None:
        super().__init__()
        self.reaped = False

    def wait(self, timeout: float | None = None) -> int:
        result = super().wait(timeout)
        self.reaped = True
        return result


class _OwnedWindowsProcessTree:
    def __init__(self, *, stop_result: bool = True) -> None:
        self.assigned: list[object] = []
        self.stopped: list[object] = []
        self.stop_result = stop_result

    def creation_flags(self) -> int:
        return 4

    def assign_and_resume(self, process: object) -> None:
        self.assigned.append(process)

    def stop(self, process: object) -> bool:
        self.stopped.append(process)
        return self.stop_result

    def close(self) -> None:
        pass


_POSIX_PROCESS_TREE_HELPER = textwrap.dedent(
    """\
    import json
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    root = Path(os.environ["DEVLOOP_PROCESS_TEST_ROOT"])
    mode = sys.argv[1]

    def write_record(name, **values):
        (root / f"{name}.json").write_text(json.dumps(values), encoding="utf-8")

    def identity():
        return {"pid": os.getpid(), "pgid": os.getpgrp(), "sid": os.getsid(0)}

    if mode == "grandchild":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        write_record("grandchild", **identity())
        while True:
            time.sleep(1)

    if mode == "child":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        grandchild = subprocess.Popen(
            [sys.executable, __file__, "grandchild"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        write_record("child", grandchild_pid=grandchild.pid, **identity())
        while not (root / "grandchild.json").exists():
            time.sleep(0.01)
        while True:
            time.sleep(1)

    child = subprocess.Popen(
        [sys.executable, __file__, "child"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    write_record("leader", child_pid=child.pid, **identity())
    while not (root / "child.json").exists() or not (root / "grandchild.json").exists():
        time.sleep(0.01)
    (root / "ready").write_text("ready\\n", encoding="ascii")
    sys.stdin.buffer.read()
    """
)

_WINDOWS_PROCESS_TREE_HELPER = textwrap.dedent(
    """\
    import json
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    root = Path(os.environ["DEVLOOP_PROCESS_TEST_ROOT"])
    mode = sys.argv[1] if len(sys.argv) > 1 else "leader"

    def write_record(name, **values):
        (root / f"{name}.json").write_text(json.dumps(values), encoding="utf-8")

    if mode == "grandchild":
        write_record("grandchild", pid=os.getpid())
        while True:
            time.sleep(1)

    if mode == "child":
        grandchild = subprocess.Popen(
            [sys.executable, __file__, "grandchild"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        write_record("child", pid=os.getpid(), grandchild_pid=grandchild.pid)
        while not (root / "grandchild.json").exists():
            time.sleep(0.01)
        while True:
            time.sleep(1)

    child = subprocess.Popen(
        [sys.executable, __file__, "child"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    write_record("leader", pid=os.getpid(), child_pid=child.pid)
    while not (root / "child.json").exists() or not (root / "grandchild.json").exists():
        time.sleep(0.01)
    (root / "ready").write_text("ready\\n", encoding="ascii")
    sys.stdin.buffer.read()
    """
)


def test_posix_process_tree_helper_is_valid_python() -> None:
    compile(_POSIX_PROCESS_TREE_HELPER, "<posix-process-tree-helper>", "exec")


def test_handshake_mapping_does_not_expose_codex_home() -> None:
    handshake = AppServerHandshake.from_result(
        {
            "userAgent": "codexcli/0.144.1",
            "codexHome": "C:/Users/example/.codex",
            "platformFamily": "windows",
            "platformOs": "windows",
        }
    )

    assert handshake.platform_family == "windows"
    assert handshake.platform_os == "windows"
    assert "codexHome" not in asdict(handshake)
    assert "C:/Users/example/.codex" not in repr(handshake)


def test_turn_interrupt_sends_the_checkpointed_thread_and_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []
    client = AppServerClient("codex")
    client._initialized = True

    def request(
        method: str,
        params: dict[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        requests.append((method, params))
        return {}

    monkeypatch.setattr(client, "_request", request)

    client.interrupt_turn("thread-development-1", "turn-development-1")

    assert requests == [
        (
            "turn/interrupt",
            {
                "threadId": "thread-development-1",
                "turnId": "turn-development-1",
            },
        )
    ]


def test_thread_registration_uses_only_the_exact_runtime_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "external-worktree"
    workspace.mkdir()
    requests: list[tuple[str, dict[str, object]]] = []
    client = AppServerClient("codex")
    client._initialized = True

    def request(
        method: str,
        params: dict[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        requests.append((method, params))
        return {"thread": {"id": "thread-external"}}

    monkeypatch.setattr(client, "_request", request)

    client.start_thread(workspace, runtime_workspace_roots=(workspace,))

    assert requests[0][0] == "thread/start"
    assert requests[0][1]["runtimeWorkspaceRoots"] == [str(workspace.resolve())]
    assert str(tmp_path.resolve()) not in requests[0][1]["runtimeWorkspaceRoots"]


def test_worker_cancellation_scope_interrupts_an_app_server_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []
    client = AppServerClient("codex")
    client._initialized = True

    def request(
        method: str,
        params: dict[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        requests.append((method, params))
        if method == "turn/interrupt":
            client._notifications.put(
                app_server_module.RpcNotification(
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {
                            "id": "turn-1",
                            "status": "interrupted",
                            "items": [],
                        },
                    },
                )
            )
        return {}

    monkeypatch.setattr(client, "_request", request)

    with cooperative_cancellation(lambda: True):
        result = client.wait_for_turn(
            "thread-1",
            "turn-1",
            timeout_seconds=1.0,
        )

    assert result.status is AppServerTurnStatus.INTERRUPTED
    assert requests == [
        (
            "turn/interrupt",
            {"threadId": "thread-1", "turnId": "turn-1"},
        )
    ]


def test_worker_cancellation_tears_down_when_turn_completion_is_not_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []
    teardowns: list[bool] = []
    client = AppServerClient("codex")
    client._initialized = True

    def request(
        method: str,
        params: dict[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        requests.append((method, params))
        return {}

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(client, "close", lambda: teardowns.append(True))
    monkeypatch.setattr(
        app_server_module,
        "_TURN_INTERRUPT_COMPLETION_GRACE_SECONDS",
        0.01,
        raising=False,
    )

    with cooperative_cancellation(lambda: True):
        result = client.wait_for_turn(
            "thread-1",
            "turn-1",
            timeout_seconds=1.0,
        )

    assert result.status is AppServerTurnStatus.INTERRUPTED
    assert result.failure_code == "interruptionUnconfirmed"
    assert result.failure_message is not None
    assert "unknown" in result.failure_message.casefold()
    assert teardowns == [True]
    assert requests == [
        (
            "turn/interrupt",
            {"threadId": "thread-1", "turnId": "turn-1"},
        )
    ]


def test_thread_turn_probe_reads_the_exact_cursor_without_resuming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []
    client = AppServerClient("codex")
    client._initialized = True

    def request(
        method: str,
        params: dict[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        requests.append((method, params))
        return {
            "thread": {
                "id": "thread-qa-3",
                "turns": [
                    {"id": "turn-older", "status": "completed"},
                    {"id": "turn-qa-3", "status": "inProgress"},
                ],
            }
        }

    monkeypatch.setattr(client, "_request", request)

    status = client.read_thread_turn_status("thread-qa-3", "turn-qa-3")

    assert status is AppServerTurnStatus.IN_PROGRESS
    assert requests == [
        (
            "thread/read",
            {"threadId": "thread-qa-3", "includeTurns": True},
        )
    ]


def test_stream_disconnect_turn_failure_is_transient() -> None:
    result = AppServerTurnResult(
        "thread-1",
        "turn-1",
        AppServerTurnStatus.FAILED,
        "",
        (),
        failure_code="other",
        failure_message=(
            "stream disconnected before completion: error sending request for url "
            "(https://api.openai.com/v1/responses)"
        ),
    )

    assert is_transient_turn_failure(result)


def test_approval_mapping_retains_only_typed_display_fields() -> None:
    request = app_server_module._approval_request(
        app_server_module.RpcServerRequest(
            request_id="approval-7",
            method="item/commandExecution/requestApproval",
            params={
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "item-1",
                "command": "git status --short",
                "cwd": "C:/repo",
                "reason": "Inspect the selected workspace.",
                "availableDecisions": ["accept", "acceptForSession", "decline", "cancel"],
                "environmentId": "not-persisted",
            },
        )
    )

    assert request.action == "git status --short"
    assert request.target == "C:/repo"
    assert request.reason == "Inspect the selected workspace."
    assert request.supported_decisions == (
        "accept",
        "acceptForSession",
        "decline",
        "cancel",
    )
    assert request.thread_id == "thread-1"
    assert request.turn_id == "turn-1"
    assert request.item_id == "item-1"
    assert "not-persisted" not in repr(request)


def test_file_change_approval_uses_its_protocol_response_choices() -> None:
    request = app_server_module._approval_request(
        app_server_module.RpcServerRequest(
            request_id="file-approval-1",
            method="item/fileChange/requestApproval",
            params={
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "item-1",
                "grantRoot": "/workspace",
            },
        )
    )

    assert request.supported_decisions == (
        "accept",
        "acceptForSession",
        "decline",
        "cancel",
    )


def test_approval_response_uses_only_the_explicit_user_handler_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions: list[str] = []
    encoded: list[str] = []
    client = AppServerClient(
        "codex",
        approval_handler=lambda request: decisions.append(request.action) or "decline",
    )
    monkeypatch.setattr(client, "_write_message", encoded.append)
    client._route_reader_item(
        json.dumps(
            {
                "id": "approval-8",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "command": "git status",
                    "cwd": "C:/repo",
                    "reason": "Inspect state",
                    "availableDecisions": ["accept", "decline", "cancel"],
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )

    assert decisions == ["git status"]
    assert json.loads(encoded[0]) == {
        "id": "approval-8",
        "result": {"decision": "decline"},
    }


@pytest.mark.parametrize(
    ("result", "expected_ready", "expected_mode"),
    [
        ({"account": None, "requiresOpenaiAuth": True}, False, None),
        (
            {
                "account": {
                    "type": "chatgpt",
                    "email": "secret@example.com",
                    "planType": "pro",
                },
                "requiresOpenaiAuth": True,
            },
            True,
            "chatgpt",
        ),
        (
            {"account": {"type": "apiKey"}, "requiresOpenaiAuth": True},
            True,
            "apiKey",
        ),
        (
            {
                "account": {"type": "amazonBedrock", "credentialSource": "codexManaged"},
                "requiresOpenaiAuth": False,
            },
            True,
            "amazonBedrock",
        ),
        (
            {
                "account": {"type": "amazonBedrock", "credentialSource": "awsManaged"},
                "requiresOpenaiAuth": False,
            },
            True,
            "amazonBedrock",
        ),
        (
            {"account": {"type": "amazonBedrock"}, "requiresOpenaiAuth": False},
            True,
            "amazonBedrock",
        ),
        ({"account": None, "requiresOpenaiAuth": False}, True, None),
    ],
)
def test_authentication_mapping_retains_only_readiness_and_safe_mode(
    result: dict[str, object],
    expected_ready: bool,
    expected_mode: str | None,
) -> None:
    readiness = AuthenticationReadiness.from_result(result)

    assert readiness.ready is expected_ready
    assert readiness.mode == expected_mode
    assert "secret@example.com" not in repr(readiness)


def test_authentication_mapping_rejects_an_account_without_a_supported_type() -> None:
    with pytest.raises(AppServerError, match="account type"):
        AuthenticationReadiness.from_result(
            {"account": {}, "requiresOpenaiAuth": True}
        )


@pytest.mark.parametrize(
    "account",
    [
        {"type": "unknown"},
        {"type": "chatgpt", "planType": "pro"},
        {"type": "chatgpt", "email": 42, "planType": "pro"},
        {"type": "chatgpt", "email": None},
        {"type": "chatgpt", "email": None, "planType": 42},
        {"type": "amazonBedrock", "credentialSource": "unknown"},
    ],
)
def test_authentication_mapping_rejects_malformed_supported_account_variants(
    account: dict[str, object],
) -> None:
    with pytest.raises(AppServerError, match="account result"):
        AuthenticationReadiness.from_result(
            {"account": account, "requiresOpenaiAuth": True}
        )


def test_close_prefers_stdio_eof_before_forced_launcher_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _GracefulChildProcess()
    process_tree = _OwnedWindowsProcessTree()

    def launch_process(*args: object, **kwargs: object) -> _GracefulChildProcess:
        return process

    monkeypatch.setattr(app_server_module.subprocess, "Popen", launch_process)
    monkeypatch.setattr(
        app_server_module,
        "_create_windows_process_tree",
        lambda: process_tree,
    )
    client = AppServerClient("codex")

    client.start()
    client.close()

    assert process.stdin.closed
    assert process.forced_terminations == 0


def test_windows_launch_assigns_the_process_to_an_owned_tree_and_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _GracefulChildProcess()
    process_tree = _OwnedWindowsProcessTree()
    launch_options: dict[str, object] = {}

    def wait_for_shutdown(timeout: float | None = None) -> int:
        if process.returncode is not None:
            return process.returncode
        raise subprocess.TimeoutExpired("codex app-server", timeout)

    def launch_process(*args: object, **kwargs: object) -> _GracefulChildProcess:
        launch_options.update(kwargs)
        return process

    monkeypatch.setattr(process, "wait", wait_for_shutdown)
    monkeypatch.setattr(app_server_module.subprocess, "Popen", launch_process)
    monkeypatch.setattr(
        app_server_module,
        "_create_windows_process_tree",
        lambda: process_tree,
        raising=False,
    )
    monkeypatch.setattr(app_server_module.sys, "platform", "win32")
    client = AppServerClient("codex.cmd")

    client.start()
    client.close()

    assert process_tree.assigned == [process]
    assert process_tree.stopped == [process]
    assert launch_options["creationflags"] == 4
    assert process.forced_terminations == 0


def test_close_kills_and_reaps_an_uncooperative_posix_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _UncooperativeChildProcess()

    def launch_process(*args: object, **kwargs: object) -> _UncooperativeChildProcess:
        return process

    monkeypatch.setattr(app_server_module.subprocess, "Popen", launch_process)
    monkeypatch.setattr(app_server_module.sys, "platform", "linux")
    client = AppServerClient("codex")

    client.start()
    client.close()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.reaped is True
    assert process.wait_calls == 3


def test_close_terminates_a_stored_posix_group_after_its_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _EofExitingGroupLeader()
    launch_options: dict[str, object] = {}
    group_signals: list[int] = []
    group_alive = True

    def launch_process(*args: object, **kwargs: object) -> _EofExitingGroupLeader:
        launch_options.update(kwargs)
        return process

    def process_group(process_id: int) -> int:
        assert process_id == process.pid
        return process.pid

    def signal_group(group_id: int, sent_signal: int) -> None:
        nonlocal group_alive
        assert group_id == process.pid
        if sent_signal == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        group_signals.append(sent_signal)
        if sent_signal == 9:
            group_alive = False

    monkeypatch.setattr(app_server_module.subprocess, "Popen", launch_process)
    monkeypatch.setattr(app_server_module.sys, "platform", "linux")
    monkeypatch.setattr(app_server_module.os, "getpgid", process_group, raising=False)
    monkeypatch.setattr(app_server_module.os, "getsid", process_group, raising=False)
    monkeypatch.setattr(app_server_module.os, "killpg", signal_group, raising=False)
    monkeypatch.setattr(app_server_module, "_PROCESS_STOP_GRACE_SECONDS", 0.01)
    client = AppServerClient("codex")

    client.start()
    client.close()

    assert launch_options.get("start_new_session") is True
    assert group_signals == [15, 9]
    assert process.reaped is True
    assert process.forced_terminations == 0


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX process sessions and groups are unavailable on Windows.",
)
def test_close_terminates_a_real_posix_child_and_grandchild_process_group(
    tmp_path: Path,
) -> None:
    process_root = tmp_path / "process-tree"
    process_root.mkdir()
    helper = tmp_path / "fake-codex"
    helper.write_text(
        f"#!{sys.executable}\n{_POSIX_PROCESS_TREE_HELPER}",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    client = AppServerClient(
        str(helper),
        environment_overrides={"DEVLOOP_PROCESS_TEST_ROOT": str(process_root)},
    )
    records: dict[str, dict[str, int]] = {}

    try:
        client.start()
        records = _wait_for_process_tree(process_root)
        leader = records["leader"]
        child = records["child"]
        grandchild = records["grandchild"]

        assert leader["pid"] == leader["pgid"] == leader["sid"]
        assert leader["child_pid"] == child["pid"]
        assert child["grandchild_pid"] == grandchild["pid"]
        assert child["pgid"] == leader["pid"]
        assert child["sid"] == leader["pid"]
        assert grandchild["pgid"] == leader["pid"]
        assert grandchild["sid"] == leader["pid"]

        client.close()

        process_ids = [leader["pid"], child["pid"], grandchild["pid"]]
        assert _wait_for_process_exit(process_ids)
    finally:
        client.close()
        _kill_test_processes(process_root, records)


def test_close_reports_when_owned_windows_process_tree_cleanup_cannot_be_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _UncooperativeChildProcess()
    process_tree = _OwnedWindowsProcessTree(stop_result=False)

    def launch_process(*args: object, **kwargs: object) -> _UncooperativeChildProcess:
        return process

    monkeypatch.setattr(app_server_module.subprocess, "Popen", launch_process)
    monkeypatch.setattr(
        app_server_module,
        "_create_windows_process_tree",
        lambda: process_tree,
        raising=False,
    )
    monkeypatch.setattr(app_server_module.sys, "platform", "win32")
    client = AppServerClient("codex.cmd")

    client.start()
    with pytest.raises(AppServerError, match="process tree"):
        client.close()

    assert process_tree.assigned == [process]
    assert process_tree.stopped == [process]


@pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="Windows Job Objects are unavailable on this platform.",
)
def test_close_terminates_a_real_windows_child_and_grandchild_process_tree(
    tmp_path: Path,
) -> None:
    process_root = tmp_path / "process-tree"
    process_root.mkdir()
    helper = tmp_path / "fake_codex.py"
    helper.write_text(_WINDOWS_PROCESS_TREE_HELPER, encoding="utf-8")
    launcher = tmp_path / "fake-codex.cmd"
    launcher.write_text(
        f'@"{sys.executable}" "{helper}" %*\n',
        encoding="utf-8",
    )
    client = AppServerClient(
        str(launcher),
        environment_overrides={"DEVLOOP_PROCESS_TEST_ROOT": str(process_root)},
    )
    records: dict[str, dict[str, int]] = {}

    try:
        client.start()
        records = _wait_for_process_tree(process_root)
        assert len({records[name]["pid"] for name in ("leader", "child", "grandchild")}) == 3

        client.close()

        process_ids = [records[name]["pid"] for name in ("leader", "child", "grandchild")]
        assert _wait_for_process_exit(process_ids)
    finally:
        client.close()
        _kill_test_processes(process_root, records)


def _wait_for_process_tree(root: Path) -> dict[str, dict[str, int]]:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if (root / "ready").exists():
            try:
                return {
                    name: {
                        key: int(value)
                        for key, value in json.loads(
                            (root / f"{name}.json").read_text(encoding="utf-8")
                        ).items()
                    }
                    for name in ("leader", "child", "grandchild")
                }
            except (FileNotFoundError, json.JSONDecodeError):
                pass
        time.sleep(0.01)
    raise AssertionError("The POSIX process-tree helper did not become ready.")


def _wait_for_process_exit(process_ids: list[int]) -> bool:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not any(_process_is_running(process_id) for process_id in process_ids):
            return True
        time.sleep(0.01)
    return False


def _process_is_running(process_id: int) -> bool:
    if sys.platform.startswith("win"):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x00100000, False, process_id)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = (Path("/proc") / str(process_id) / "stat").read_text(
            encoding="ascii"
        ).split()[2]
    except (FileNotFoundError, IndexError, OSError):
        return True
    return state != "Z"


def _kill_test_processes(root: Path, records: dict[str, dict[str, int]]) -> None:
    process_ids = {
        value
        for record in records.values()
        for key, value in record.items()
        if key == "pid" or key.endswith("_pid")
    }
    for record_path in root.glob("*.json"):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        process_ids.update(
            value
            for key, value in record.items()
            if isinstance(key, str)
            and (key == "pid" or key.endswith("_pid"))
            and isinstance(value, int)
            and not isinstance(value, bool)
        )
    kill_signal = (
        int(getattr(signal, "SIGTERM", 15))
        if sys.platform.startswith("win")
        else int(getattr(signal, "SIGKILL", 9))
    )
    for process_id in sorted(process_ids, reverse=True):
        try:
            os.kill(process_id, kill_signal)
        except OSError:
            pass
