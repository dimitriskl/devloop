from __future__ import annotations

import unittest

from devloop import cli


class IssueProgressLabelTests(unittest.TestCase):
    def test_label_contains_position_and_number(self) -> None:
        label = cli.issue_progress_label(2, 5, "0003")
        self.assertEqual(label, "issue 0003 (2/5)")

    def test_single_issue(self) -> None:
        label = cli.issue_progress_label(1, 1, "0001")
        self.assertEqual(label, "issue 0001 (1/1)")


class RunIssueSignatureTests(unittest.TestCase):
    def test_run_issue_accepts_progress_keyword(self) -> None:
        import inspect

        signature = inspect.signature(cli.run_issue)
        self.assertIn("progress", signature.parameters)


if __name__ == "__main__":
    unittest.main()
