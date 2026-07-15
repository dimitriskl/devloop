from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from devloop import codex_runner
from devloop.codex_events import render_safe_codex_activity
from devloop.statusui import Stage


class ResolveCodexExecutableTests(unittest.TestCase):
    def test_uses_shutil_which_when_available(self) -> None:
        with mock.patch.object(
            codex_runner.shutil,
            "which",
            return_value="C:/Users/Dimitris/AppData/Roaming/npm/codex.cmd",
        ):
            result = codex_runner.resolve_codex_executable("codex")

        self.assertEqual(result, "C:/Users/Dimitris/AppData/Roaming/npm/codex.cmd")

    def test_falls_back_to_windows_npm_shim_location(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            appdata = Path(raw) / "Roaming"
            npm_dir = appdata / "npm"
            npm_dir.mkdir(parents=True)
            codex_cmd = npm_dir / "codex.cmd"
            codex_cmd.write_text("@echo off\n", encoding="utf-8")

            with mock.patch.object(codex_runner.shutil, "which", return_value=None), \
                 mock.patch.object(codex_runner.sys, "platform", "win32"), \
                 mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
                result = codex_runner.resolve_codex_executable("codex")

        self.assertEqual(result, str(codex_cmd.resolve()))


class StreamingCodexRunnerTests(unittest.TestCase):
    def test_every_delivery_role_maps_to_its_visible_phase(self) -> None:
        self.assertIs(codex_runner.stage_for_role("coder"), Stage.DEVELOPMENT)
        self.assertIs(codex_runner.stage_for_role("reviewer"), Stage.REVIEW)
        self.assertIs(codex_runner.stage_for_role("qa"), Stage.QA)

    def test_unknown_delivery_role_fails_clearly(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported Dev Loop role"):
            codex_runner.stage_for_role("mystery")

    def test_reasoning_activity_never_exposes_raw_chain_of_thought(self) -> None:
        activity = render_safe_codex_activity(
            {
                "type": "item.started",
                "item": {
                    "type": "reasoning",
                    "text": "private reasoning must not be displayed",
                },
            }
        )

        self.assertEqual(activity, "Codex is reasoning about the task.")
        self.assertNotIn("private reasoning", activity)

    def test_agent_update_strips_terminal_control_characters(self) -> None:
        activity = render_safe_codex_activity(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "Checking files.\x1b[31m",
                },
            }
        )

        self.assertEqual(activity, "Codex update: Checking files.")
        self.assertNotIn("\x1b", activity)

    def test_role_execution_streams_safe_activity_before_process_exit(self) -> None:
        class OpenAfterCompletion:
            def __init__(self) -> None:
                self._lines = iter(
                    [
                        (
                            '{"type":"item.completed","item":'
                            '{"type":"agent_message",'
                            '"text":"Inspecting the acceptance criteria."}}\n'
                        ),
                        (
                            '{"type":"item.started","item":'
                            '{"type":"command_execution","command":"secret command",'
                            '"status":"in_progress"}}\n'
                        ),
                        '{"type":"turn.completed","usage":{}}\n',
                    ]
                )

            def __iter__(self):
                return self

            def __next__(self) -> str:
                try:
                    return next(self._lines)
                except StopIteration as error:
                    raise AssertionError(
                        "runner read past turn.completed and would wait for pipe EOF"
                    ) from error

        class FakeProcess:
            def __init__(self) -> None:
                self.stdin = StringIO()
                self.stdout = OpenAfterCompletion()
                self.stderr: list[str] = []
                self.returncode: int | None = None

            def wait(self, timeout=None):
                self.returncode = 0
                return self.returncode

            def terminate(self) -> None:
                self.returncode = -15

            def kill(self) -> None:
                self.returncode = -9

        runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
        runner.repo_root = Path("/tmp/repository")
        with mock.patch.object(
                 codex_runner.subprocess,
                 "Popen",
                 return_value=FakeProcess(),
             ) as popen, redirect_stdout(StringIO()) as stdout:
            result = runner.run_codex_exec_with_connection_retries(
                command=["codex", "exec", "--json", "-"],
                prompt="Implement the issue.",
                stdout_path=Path("stdout.jsonl"),
                stderr_path=Path("stderr.txt"),
            )

        self.assertEqual(result.returncode, 0)
        popen.assert_called_once()
        rendered = stdout.getvalue()
        self.assertIn(
            "[development] Codex update: Inspecting the acceptance criteria.",
            rendered,
        )
        self.assertIn("[development] Running a repository command.", rendered)
        self.assertNotIn("secret command", rendered)


if __name__ == "__main__":
    unittest.main()
