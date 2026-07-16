from __future__ import annotations

import unittest
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest import mock

from devloop import codex_runner
from devloop.codex_events import RunWideBlockerKind, classify_run_wide_blocker
from devloop.issue_pack import Issue
from devloop.issue_scheduler import SchedulingPhase
from devloop.state import LoopStateWriter
from devloop.templates import BundleContext


class RunWideBlockerClassificationTests(unittest.TestCase):
    def test_usage_limit_event_is_a_run_wide_blocker(self) -> None:
        stdout = (
            '{"type":"error","message":"You have hit your usage limit. '
            'Try again later."}\n'
        )

        blocker = classify_run_wide_blocker(stdout, "")

        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertIs(blocker.kind, RunWideBlockerKind.USAGE_LIMIT)
        self.assertNotIn("Try again later", blocker.summary)

    def test_repository_command_failure_is_not_run_wide(self) -> None:
        stdout = (
            '{"type":"item.completed","item":{"type":"command_execution",'
            '"status":"failed","message":"dotnet test failed"}}\n'
        )

        self.assertIsNone(classify_run_wide_blocker(stdout, ""))

    def test_authentication_and_service_failures_are_typed(self) -> None:
        cases = (
            ("Authentication failed: invalid API key", RunWideBlockerKind.AUTHENTICATION),
            ("HTTP 503: service unavailable", RunWideBlockerKind.SERVICE_UNAVAILABLE),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                blocker = classify_run_wide_blocker(
                    f'{{"type":"turn.failed","error":"{message}"}}\n',
                    "",
                )
                self.assertIsNotNone(blocker)
                assert blocker is not None
                self.assertIs(blocker.kind, expected)


class RunWideBlockerExecutionTests(unittest.TestCase):
    def test_role_execution_raises_instead_of_blocking_the_issue(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
            runner.bundle = BundleContext(root=root, prompts=root, schemas=root)
            runner.repo_root = root
            runner.prd_path = root / "prd.md"
            runner.issues_index = root / "README.md"
            runner.log_root = root / ".loop.logs"
            runner.codex = "codex"
            runner.sandbox = "workspace-write"
            runner.approval_policy = "never"
            runner.ensure_log_root()
            runner.run_codex_exec_with_connection_retries = mock.Mock(
                return_value=CompletedProcess(
                    ["codex"],
                    1,
                    stdout=(
                        '{"type":"error","message":"You have hit your usage limit"}\n'
                    ),
                    stderr="",
                )
            )
            issue = Issue("0001", "First", root / "0001.md", False)

            with mock.patch.object(runner, "build_prompt", return_value="prompt"), \
                 mock.patch.object(codex_runner, "build_codex_exec_command", return_value=["codex"]), \
                 self.assertRaises(codex_runner.RunWideBlockerError) as raised:
                runner.run_role("coder", issue, pass_number=1)

        self.assertIs(raised.exception.blocker.kind, RunWideBlockerKind.USAGE_LIMIT)

    def test_pause_state_preserves_exact_cursor_without_spending_budget(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            index = root / "README.md"
            index.write_text("", encoding="utf-8")
            issue = Issue("0001", "First", root / "0001.md", False)
            writer = LoopStateWriter(index)
            writer.issue_state(issue).update(
                {
                    "status": "IN_PROGRESS",
                    "current_step_instance_id": "step-id",
                    "current_pass": 2,
                }
            )
            writer.reserve_scheduling_attempt(
                issue,
                phase=SchedulingPhase.BLOCKER_RESOLUTION,
                ordinal=3,
            )
            blocker = classify_run_wide_blocker(
                '{"type":"error","message":"You hit your usage limit"}\n',
                "",
            )
            assert blocker is not None

            writer.record_run_paused(blocker)
            writer.record_run_paused(blocker)
            reloaded = LoopStateWriter(index)

            self.assertEqual(reloaded.run_pause()["issue"], "0001")
            self.assertEqual(reloaded.run_pause()["step_instance_id"], "step-id")
            self.assertEqual(reloaded.run_pause()["pass"], 2)
            self.assertEqual(reloaded.run_pause()["ordinal"], 3)
            self.assertEqual(reloaded.run_pause()["occurrences"], 2)
            self.assertEqual(reloaded.additional_passes(), {})
            self.assertEqual(reloaded.issue_state(issue)["status"], "IN_PROGRESS")
            self.assertEqual(
                reloaded.state["dependency_scheduler"]["phase"],
                "RUN_PAUSED",
            )


if __name__ == "__main__":
    unittest.main()
