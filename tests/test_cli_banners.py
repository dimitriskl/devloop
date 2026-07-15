from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from devloop import cli, statusui
from devloop.codex_runner import RoleResult
from devloop.issue_pack import Issue
from devloop.state import LoopStateWriter


class IssueProgressLabelTests(unittest.TestCase):
    def test_label_contains_position_and_number(self) -> None:
        label = cli.issue_progress_label(2, 5, "0003")
        self.assertEqual(label, "issue 0003 (2/5; 3 after current)")

    def test_single_issue(self) -> None:
        label = cli.issue_progress_label(1, 1, "0001")
        self.assertEqual(label, "issue 0001 (1/1; 0 after current)")

    def test_compact_activity_label_keeps_counts_visible(self) -> None:
        label = cli.issue_activity_label(1, 26, "0001")
        self.assertEqual(label, "0001 1/26 +25")


class RunIssueSignatureTests(unittest.TestCase):
    def test_run_issue_accepts_progress_keyword(self) -> None:
        import inspect

        signature = inspect.signature(cli.run_issue)
        self.assertIn("progress", signature.parameters)

    def test_compact_progress_reaches_development_review_and_qa(self) -> None:
        class PassingRunner:
            dry_run = False

            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def run_role(self, *, role: str, progress: str, **_: object) -> RoleResult:
                self.calls.append((role, progress))
                return RoleResult(status="PASS")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text("# Issue 0001\n\nCompleted: [ ]\n", encoding="utf-8")
            issues_index = root / "README.md"
            issues_index.write_text(
                "- [Issue 0001](./0001-example.md)\n",
                encoding="utf-8",
            )
            issue = Issue("0001", "Issue 0001", issue_path, completed=False)
            runner = PassingRunner()

            cli.run_issue(
                issue=issue,
                runner=runner,
                state_writer=LoopStateWriter(issues_index),
                max_passes=1,
                progress="issue 0001 (1/26; 25 after current)",
                activity_progress="0001 1/26 +25",
            )

        self.assertEqual(
            runner.calls,
            [
                ("coder", "0001 1/26 +25"),
                ("reviewer", "0001 1/26 +25"),
                ("qa", "0001 1/26 +25"),
            ],
        )

    def test_tty_uses_one_borderless_dashboard_instead_of_repeated_banners(self) -> None:
        class TtyStream(io.StringIO):
            @property
            def encoding(self) -> str:
                return "utf-8"

            def isatty(self) -> bool:
                return True

        class PassingRunner:
            dry_run = False

            def run_role(self, **_: object) -> RoleResult:
                return RoleResult(status="PASS", summary="Gate passed.")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text("# Issue 0001\n\nCompleted: [ ]\n", encoding="utf-8")
            issues_index = root / "README.md"
            issues_index.write_text(
                "- [Issue 0001](./0001-example.md)\n",
                encoding="utf-8",
            )
            issue = Issue("0001", "Publish a validated catalog", issue_path, False)
            output = TtyStream()

            env = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
            with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(output):
                cli.run_issue(
                    issue=issue,
                    runner=PassingRunner(),
                    state_writer=LoopStateWriter(issues_index),
                    max_passes=1,
                    dashboard_position=1,
                    dashboard_total=26,
                )

        rendered = output.getvalue()
        self.assertIn("CURRENT ISSUE · 0001 · 1/26 · 25 remaining", rendered)
        self.assertNotIn("devloop ·", rendered)
        self.assertNotIn("[0001] coder: PASS", rendered)
        self.assertFalse(set("│╭╮╰╯┌┐└┘").intersection(rendered))
        self.assertIn("\x1b[1;33mWORKING", rendered)
        self.assertIn("\x1b[1;32mPASS", rendered)

    def test_shared_dashboard_replaces_completed_issue_with_next_issue(self) -> None:
        class TtyStream(io.StringIO):
            @property
            def encoding(self) -> str:
                return "utf-8"

            def isatty(self) -> bool:
                return True

        class PassingRunner:
            dry_run = False

            def run_role(self, **_: object) -> RoleResult:
                return RoleResult(status="PASS", summary="Gate passed.")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first_path = root / "0001-first.md"
            second_path = root / "0002-second.md"
            first_path.write_text("# First\n\nCompleted: [ ]\n", encoding="utf-8")
            second_path.write_text("# Second\n\nCompleted: [ ]\n", encoding="utf-8")
            issues_index = root / "README.md"
            issues_index.write_text(
                "- [First](./0001-first.md)\n- [Second](./0002-second.md)\n",
                encoding="utf-8",
            )
            output = TtyStream()
            dashboard = statusui.IssueDashboard(
                issue_number="0001",
                issue_title="First",
                position=1,
                total=2,
                stream=output,
            )
            runner = PassingRunner()
            state_writer = LoopStateWriter(issues_index)
            env = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}

            with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(output):
                cli.run_issue(
                    Issue("0001", "First", first_path, False),
                    runner,
                    state_writer,
                    max_passes=1,
                    dashboard_position=1,
                    dashboard_total=2,
                    dashboard=dashboard,
                )
                transition_start = len(output.getvalue())
                cli.run_issue(
                    Issue("0002", "Second", second_path, False),
                    runner,
                    state_writer,
                    max_passes=1,
                    dashboard_position=2,
                    dashboard_total=2,
                    dashboard=dashboard,
                )
                dashboard.close()

        transition = output.getvalue()[transition_start:]
        self.assertIn("LAST RESULT · 0001 · \x1b[1;32mPASS", transition)
        self.assertIn("CURRENT ISSUE · 0002 · 2/2 · 0 remaining", transition)
        self.assertNotIn("[0001] Completed.", output.getvalue())


if __name__ == "__main__":
    unittest.main()
