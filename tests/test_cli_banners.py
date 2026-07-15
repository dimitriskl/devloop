from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from devloop import cli
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


if __name__ == "__main__":
    unittest.main()
