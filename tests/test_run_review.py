from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from devloop import cli
from devloop.issue_pack import Issue
from devloop.run_review import (
    RunReviewAction,
    build_run_review,
    render_run_review,
    run_review_options,
)


class RunReviewTests(unittest.TestCase):
    def test_devloop_repeats_an_attempt_only_after_explicit_rerun(self) -> None:
        parser = mock.Mock()
        args = mock.Mock()
        workflow_snapshot = mock.Mock()
        outcomes = (
            cli.DevLoopAttemptResult(2, RunReviewAction.RERUN_REMAINING),
            cli.DevLoopAttemptResult(0, RunReviewAction.EXIT),
        )
        with mock.patch.object(
            cli,
            "_run_devloop_attempt",
            side_effect=outcomes,
        ) as run_attempt:
            result = cli._run_devloop(parser, args, workflow_snapshot)

        self.assertEqual(result, 0)
        self.assertEqual(run_attempt.call_count, 2)

    def test_review_makes_completion_and_remaining_issues_explicit(self) -> None:
        issues = [
            Issue("0001", "Completed feature", Path("0001.md"), False),
            Issue("0002", "Blocked feature", Path("0002.md"), False),
            Issue(
                "0003",
                "Dependent feature",
                Path("0003.md"),
                False,
                ("0002",),
            ),
        ]
        review = build_run_review(
            issues,
            {
                "0001": {"status": "COMPLETED"},
                "0002": {
                    "status": "BLOCKED",
                    "blocked_summary": "Review needs a correction.",
                },
                "0003": {
                    "status": "WAITING_ON_DEPENDENCY",
                    "waiting_on": ["0002"],
                },
            },
            loop_state_path=Path("README.loop.md"),
            rerun_available=True,
        )

        rendered = render_run_review(review, RunReviewAction.RERUN_REMAINING)

        self.assertIn("WORKFLOW FINISHED - ATTENTION REQUIRED", rendered)
        self.assertIn("Completed: 1/3", rendered)
        self.assertIn("Remaining: 2", rendered)
        self.assertIn("COMPLETED  0001", rendered)
        self.assertIn("BLOCKED    0002", rendered)
        self.assertIn("Review needs a correction.", rendered)
        self.assertIn("WAITING    0003", rendered)
        self.assertIn("waiting on 0002", rendered)
        self.assertIn("only the 2 unfinished issues", rendered)
        self.assertEqual(review.remaining_issue_numbers, ("0002", "0003"))
        self.assertEqual(
            run_review_options(review),
            (
                (RunReviewAction.RERUN_REMAINING.value, "Rerun 2 unfinished issues"),
                (RunReviewAction.EXIT.value, "Exit Dev Loop"),
            ),
        )

    def test_successful_review_has_no_rerun_action(self) -> None:
        issue = Issue("0001", "Done", Path("0001.md"), True)
        review = build_run_review(
            [issue],
            {},
            loop_state_path=Path("README.loop.md"),
            rerun_available=True,
        )

        rendered = render_run_review(review, RunReviewAction.EXIT)

        self.assertIn("WORKFLOW FINISHED - SUCCESS", rendered)
        self.assertIn("Completed: 1/1", rendered)
        self.assertIn("Remaining: 0", rendered)
        self.assertEqual(
            run_review_options(review),
            ((RunReviewAction.EXIT.value, "Exit Dev Loop"),),
        )

    def test_review_uses_the_latest_failed_pass_reason_as_fallback(self) -> None:
        issue = Issue("0004", "Failed", Path("0004.md"), False)
        review = build_run_review(
            [issue],
            {
                "0004": {
                    "status": "FAILED",
                    "passes": [
                        {
                            "role": "qa",
                            "result": {
                                "status": "FAIL",
                                "summary": "Focused verification failed.",
                            },
                        }
                    ],
                }
            },
            loop_state_path=Path("README.loop.md"),
            rerun_available=True,
        )

        rendered = render_run_review(review, RunReviewAction.EXIT)

        self.assertIn("FAILED     0004", rendered)
        self.assertIn("Focused verification failed.", rendered)


if __name__ == "__main__":
    unittest.main()
