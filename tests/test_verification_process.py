from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from devloop import verification_process as execution
from devloop.portable_runtime import PortableRuntimeStopped
from devloop.subprocess_utils import ProcessTerminationResult


def test_owned_process_uses_null_stdin_captures_redacted_output_and_preserves_exit(
    tmp_path: Path, monkeypatch
) -> None:
    process = Mock(stdout=io.StringIO("Password=secret-value\nTests failed\n"), returncode=1)
    process.poll.side_effect = [None, 1]
    launch = Mock(return_value=process)
    stop = Mock(return_value=ProcessTerminationResult(tree_terminated=True, detail="Stopped"))
    runtime = Mock()
    monkeypatch.setattr(execution, "launch_process_tree", launch)
    monkeypatch.setattr(execution, "terminate_process", stop)
    monkeypatch.setattr(execution, "active_portable_runtime", lambda: runtime)
    command = ["dotnet", "test", "tests.csproj", "--filter", "Name~Test"]
    assert execution.run_verification_command(command, tmp_path, tmp_path) == 1
    assert launch.call_args.args[0] == command
    assert launch.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert launch.call_args.kwargs["stderr"] == subprocess.STDOUT
    assert launch.call_args.kwargs["cwd"] == tmp_path
    assert "shell" not in launch.call_args.kwargs
    assert "env" not in launch.call_args.kwargs  # Inherit the session account/environment.
    stop.assert_called_once_with(process)
    log = (tmp_path / execution.VERIFICATION_LOG).read_text()
    assert "secret-value" not in log
    assert "Tests failed" in log
    assert "secret-value" not in str(runtime.write_output.call_args_list)


def test_pause_before_launch_does_not_start_tests(tmp_path: Path, monkeypatch) -> None:
    runtime = Mock()
    runtime.wait_for_retry.side_effect = PortableRuntimeStopped("Paused")
    launch = Mock()
    monkeypatch.setattr(execution, "active_portable_runtime", lambda: runtime)
    monkeypatch.setattr(execution, "launch_process_tree", launch)
    with pytest.raises(PortableRuntimeStopped):
        execution.run_verification_command(["dotnet", "test"], tmp_path, tmp_path)
    assert not launch.called


@pytest.mark.parametrize("action", ["Paused", "Cancelled", "Shutdown", "Control stream closed"])
def test_lifecycle_interruption_stops_test_tree(tmp_path: Path, monkeypatch, action: str) -> None:
    process = Mock(stdout=io.StringIO("Starting tests\n"), returncode=None)
    process.poll.return_value = None
    runtime = Mock()
    runtime.wait_for_retry.side_effect = [None, PortableRuntimeStopped(action)]
    stop = Mock(return_value=ProcessTerminationResult(tree_terminated=True, detail="Stopped"))
    monkeypatch.setattr(execution, "active_portable_runtime", lambda: runtime)
    monkeypatch.setattr(execution, "launch_process_tree", Mock(return_value=process))
    monkeypatch.setattr(execution, "terminate_process", stop)
    with pytest.raises(PortableRuntimeStopped, match=action):
        execution.run_verification_command(["dotnet", "test"], tmp_path, tmp_path)
    stop.assert_called_once_with(process)


def test_unconfirmed_process_cleanup_cannot_pass(tmp_path: Path, monkeypatch) -> None:
    process = Mock(stdout=io.StringIO(""), returncode=0)
    process.poll.return_value = 0
    monkeypatch.setattr(execution, "launch_process_tree", Mock(return_value=process))
    monkeypatch.setattr(execution, "terminate_process", Mock(return_value=(
        ProcessTerminationResult(tree_terminated=False, detail="Unknown")
    )))
    with pytest.raises(OSError, match="did not stop"):
        execution.run_verification_command(["dotnet", "test"], tmp_path, tmp_path)


def test_real_local_child_runs_without_a_terminal_and_leaves_a_log(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "import sys; print('local child completed'); sys.exit(3)"]
    assert execution.run_verification_command(command, tmp_path, tmp_path) == 3
    assert "local child completed" in (tmp_path / execution.VERIFICATION_LOG).read_text()
