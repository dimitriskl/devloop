from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

from devloop import cli
from devloop.cli import execute_dependency_schedule
from devloop.codex_runner import RoleResult
from devloop.issue_pack import Issue, parse_issue_index
from devloop.issue_scheduler import (
    DependencyScheduler,
    IssueDependencyGraph,
    SchedulingPhase,
)
from devloop.portable_workflow import IssueStatus
from devloop.state import LoopStateWriter


class IssueDependencyParsingTests(unittest.TestCase):
    def test_only_blocked_by_links_create_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "0001-first.md"
            second = root / "0002-second.md"
            first.write_text(
                "# First\n\n## Related\n\n[Second](./0002-second.md)\n\n"
                "## Blocked by\n\nNone.\n",
                encoding="utf-8",
            )
            second.write_text(
                "# Second\n\n## Blocked by\n\n"
                "- Blocked by [First](./0001-first.md)\n",
                encoding="utf-8",
            )
            index = root / "README.md"
            index.write_text(
                "1. [Second](./0002-second.md)\n"
                "2. [First](./0001-first.md)\n",
                encoding="utf-8",
            )

            issues = parse_issue_index(index)

            self.assertEqual([issue.number for issue in issues], ["0002", "0001"])
            self.assertEqual(issues[0].dependencies, ("0001",))
            self.assertEqual(issues[1].dependencies, ())

    def test_unknown_dependency_fails_with_issue_and_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            issue = root / "0001-first.md"
            issue.write_text(
                "# First\n\n## Blocked by\n\n"
                "- Blocked by [Missing](./0099-missing.md)\n",
                encoding="utf-8",
            )
            index = root / "README.md"
            index.write_text("1. [First](./0001-first.md)\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                r"0001-first\.md.*0099-missing\.md",
            ):
                parse_issue_index(index)

    def test_dependency_outside_issue_pack_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pack = root / "pack"
            pack.mkdir()
            outside = root / "0002-outside.md"
            outside.write_text("# Outside\n", encoding="utf-8")
            issue = pack / "0001-first.md"
            issue.write_text(
                "# First\n\n## Blocked by\n\n"
                "- [Outside](../0002-outside.md)\n",
                encoding="utf-8",
            )
            index = pack / "README.md"
            index.write_text("- [First](./0001-first.md)\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"outside the issue pack"):
                parse_issue_index(index)

    def test_cli_preflight_rejects_invalid_graph_before_codex_runner_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prd = root / "prd.md"
            prd.write_text(
                "# PRD\n\n## Target Product\n\n"
                "Product: devloop-plan + devloop\n",
                encoding="utf-8",
            )
            issue = root / "0001-first.md"
            issue.write_text(
                "# First\n\n## Blocked by\n\n"
                "- [Missing](./0099-missing.md)\n",
                encoding="utf-8",
            )
            index = root / "README.md"
            index.write_text("- [First](./0001-first.md)\n", encoding="utf-8")
            stderr = StringIO()

            with mock.patch.object(cli, "find_repo_root", return_value=root), \
                 mock.patch.object(cli, "git_current_branch", return_value="main"), \
                 mock.patch.object(cli, "CodexRunner") as runner_type, \
                 redirect_stderr(stderr), \
                 self.assertRaises(SystemExit):
                cli.main(
                    [
                        "--prd",
                        str(prd),
                        "--issues",
                        str(index),
                        "--dry-run",
                        "--no-worktree",
                    ]
                )

            runner_type.assert_not_called()
            self.assertIn("Issue dependency preflight failed", stderr.getvalue())


class IssueDependencyGraphTests(unittest.TestCase):
    def test_self_dependency_is_rejected(self) -> None:
        issue = Issue(
            "0001",
            "First",
            Path("0001-first.md"),
            completed=False,
            dependencies=("0001",),
        )

        with self.assertRaisesRegex(ValueError, r"0001.*depends on itself"):
            IssueDependencyGraph([issue])

    def test_cycle_reports_the_complete_dependency_path(self) -> None:
        issues = [
            Issue("0001", "First", Path("0001.md"), False, ("0002",)),
            Issue("0002", "Second", Path("0002.md"), False, ("0003",)),
            Issue("0003", "Third", Path("0003.md"), False, ("0001",)),
        ]

        with self.assertRaisesRegex(
            ValueError,
            r"0001 -> 0002 -> 0003 -> 0001",
        ):
            IssueDependencyGraph(issues)

    def test_duplicate_dependency_is_rejected(self) -> None:
        first = Issue("0001", "First", Path("0001.md"), False)
        second = Issue(
            "0002",
            "Second",
            Path("0002.md"),
            False,
            ("0001", "0001"),
        )

        with self.assertRaisesRegex(ValueError, r"0002.*declares duplicate.*0001"):
            IssueDependencyGraph([first, second])

    def test_selection_requires_only_unfinished_prerequisites(self) -> None:
        first = Issue("0001", "First", Path("0001.md"), False)
        second = Issue("0002", "Second", Path("0002.md"), False, ("0001",))
        graph = IssueDependencyGraph([first, second])

        with self.assertRaisesRegex(
            ValueError,
            r"0002 requires unfinished issue 0001",
        ):
            graph.validate_selection([second])

        completed_first = Issue("0001", "First", Path("0001.md"), True)
        IssueDependencyGraph([completed_first, second]).validate_selection([second])


class DependencySchedulerTests(unittest.TestCase):
    def test_user_requested_rerun_renews_only_unfinished_retry_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            index = root / "README.md"
            index.write_text("", encoding="utf-8")
            writer = LoopStateWriter(index)
            writer.state["dependency_scheduler"] = {
                "normal_attempted": ["0001", "0002"],
                "additional_passes": {"0001": 5, "0002": 3},
                "phase": "EXHAUSTED",
            }
            writer.reset_scheduler_retry_budget(("0001",))

            reloaded = LoopStateWriter(index)

        self.assertEqual(reloaded.normal_attempted_issues(), frozenset({"0002"}))
        self.assertEqual(reloaded.additional_passes(), {"0002": 3})
        self.assertEqual(
            reloaded.state["events"][-1]["type"],
            "unfinished-rerun-requested",
        )

    def test_rerun_unlocks_issue_whose_completed_dependency_was_filtered_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            index = root / "README.md"
            index.write_text("", encoding="utf-8")
            first = Issue("0001", "First", root / "0001.md", False)
            second = Issue(
                "0002",
                "Second",
                root / "0002.md",
                False,
                ("0001",),
            )
            writer = LoopStateWriter(index)
            writer.issue_state(first)["status"] = IssueStatus.COMPLETED.value
            calls: list[str] = []

            result = execute_dependency_schedule(
                issues=[second],
                graph=IssueDependencyGraph([first, second]),
                state_writer=writer,
                execute_issue=lambda issue, _phase, _ordinal: (
                    calls.append(issue.number) or RoleResult(status="PASS")
                ),
            )

        self.assertTrue(result.completed)
        self.assertEqual(calls, ["0002"])

    def test_blocked_root_defers_descendants_but_not_independent_work(self) -> None:
        first = Issue("0001", "First", Path("0001.md"), False)
        second = Issue("0002", "Second", Path("0002.md"), False, ("0001",))
        third = Issue("0003", "Independent", Path("0003.md"), False)
        scheduler = DependencyScheduler(IssueDependencyGraph([first, second, third]))

        initial = scheduler.project(completed=(), normal_attempted=())
        self.assertEqual(initial.next_normal.issue.number, "0001")

        after_first_blocks = scheduler.project(
            completed=(),
            normal_attempted=("0001",),
        )
        self.assertEqual(after_first_blocks.next_normal.issue.number, "0003")
        self.assertEqual(
            after_first_blocks.waiting_dependencies,
            {"0002": ("0001",)},
        )

    def test_diamond_unlocks_only_after_both_middle_issues_complete(self) -> None:
        issues = [
            Issue("0001", "Root", Path("0001.md"), False),
            Issue("0002", "Left", Path("0002.md"), False, ("0001",)),
            Issue("0003", "Right", Path("0003.md"), False, ("0001",)),
            Issue("0004", "Join", Path("0004.md"), False, ("0002", "0003")),
        ]
        scheduler = DependencyScheduler(IssueDependencyGraph(issues))

        after_root = scheduler.project(
            completed=("0001",),
            normal_attempted=(),
        )
        self.assertEqual(
            [node.number for node in after_root.ready],
            ["0002", "0003"],
        )
        self.assertEqual(
            after_root.waiting_dependencies,
            {"0004": ("0002", "0003")},
        )

        after_left = scheduler.project(
            completed=("0001", "0002"),
            normal_attempted=(),
        )
        self.assertEqual(after_left.next_normal.number, "0003")
        self.assertEqual(after_left.waiting_dependencies, {"0004": ("0003",)})

    def test_projection_persists_waiting_without_overwriting_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            index = root / "README.md"
            index.write_text("", encoding="utf-8")
            first = Issue("0001", "First", root / "0001.md", False)
            second = Issue("0002", "Second", root / "0002.md", False, ("0001",))
            third = Issue("0003", "Independent", root / "0003.md", False)
            writer = LoopStateWriter(index)
            writer.issue_state(first)["status"] = "BLOCKED"

            writer.record_dependency_projection(
                [first, second, third],
                ready=("0003",),
                waiting={"0002": ("0001",)},
            )

            self.assertEqual(writer.issue_state(first)["status"], "BLOCKED")
            self.assertEqual(
                writer.issue_state(second),
                {
                    "title": "Second",
                    "path": str(second.path),
                    "status": "WAITING_ON_DEPENDENCY",
                    "waiting_on": ["0001"],
                },
            )
            self.assertEqual(writer.issue_state(third)["status"], "READY")
            board = writer.board_path.read_text(encoding="utf-8")
            self.assertIn("| 0002 | Second | WAITING_ON_DEPENDENCY | 0001 |", board)

    def test_blocker_resolution_is_round_robin_and_bounded_to_five(self) -> None:
        first = Issue("0001", "First", Path("0001.md"), False)
        second = Issue("0002", "Second", Path("0002.md"), False)
        scheduler = DependencyScheduler(IssueDependencyGraph([first, second]))

        first_round = scheduler.project(
            completed=(),
            normal_attempted=("0001", "0002"),
            additional_passes={"0001": 0, "0002": 0},
        )
        self.assertEqual(first_round.next_blocker.issue.number, "0001")
        self.assertEqual(first_round.blocker_round, 1)

        second_turn = scheduler.project(
            completed=(),
            normal_attempted=("0001", "0002"),
            additional_passes={"0001": 1, "0002": 0},
        )
        self.assertEqual(second_turn.next_blocker.issue.number, "0002")
        self.assertEqual(second_turn.blocker_round, 1)

        exhausted_first = scheduler.project(
            completed=(),
            normal_attempted=("0001", "0002"),
            additional_passes={"0001": 5, "0002": 4},
        )
        self.assertEqual(exhausted_first.next_blocker.issue.number, "0002")
        self.assertEqual(exhausted_first.blocker_round, 5)

    def test_attempt_reservation_survives_reload_without_spending_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            index = root / "README.md"
            index.write_text("", encoding="utf-8")
            issue = Issue("0001", "First", root / "0001.md", False)
            writer = LoopStateWriter(index)

            writer.reserve_scheduling_attempt(
                issue,
                phase=SchedulingPhase.BLOCKER_RESOLUTION,
                ordinal=1,
            )
            reloaded = LoopStateWriter(index)

            self.assertEqual(
                reloaded.active_scheduling_attempt(),
                {
                    "issue": "0001",
                    "phase": "BLOCKER_RESOLUTION",
                    "ordinal": 1,
                },
            )
            self.assertEqual(reloaded.additional_passes(), {})

            reloaded.complete_scheduling_attempt(
                issue,
                outcome=IssueStatus.BLOCKED,
            )
            completed = LoopStateWriter(index)
            self.assertIsNone(completed.active_scheduling_attempt())
            self.assertEqual(completed.additional_passes(), {"0001": 1})

    def test_execution_skips_descendant_until_root_blocker_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            index = root / "README.md"
            index.write_text("", encoding="utf-8")
            first = Issue("0001", "First", root / "0001.md", False)
            second = Issue("0002", "Second", root / "0002.md", False, ("0001",))
            third = Issue("0003", "Independent", root / "0003.md", False)
            issues = [first, second, third]
            calls: list[tuple[str, SchedulingPhase, int]] = []

            def execute(
                issue: Issue,
                phase: SchedulingPhase,
                ordinal: int,
            ) -> RoleResult:
                calls.append((issue.number, phase, ordinal))
                if issue.number == "0001" and phase is SchedulingPhase.NORMAL_SCHEDULING:
                    return RoleResult(status="BLOCKED", summary="needs another pass")
                return RoleResult(status="PASS")

            writer = LoopStateWriter(index)
            result = execute_dependency_schedule(
                issues=issues,
                graph=IssueDependencyGraph(issues),
                state_writer=writer,
                execute_issue=execute,
            )

            self.assertTrue(result.completed)
            self.assertEqual(
                writer.state["dependency_scheduler"]["phase"],
                "COMPLETE",
            )
            self.assertEqual(
                calls,
                [
                    ("0001", SchedulingPhase.NORMAL_SCHEDULING, 1),
                    ("0003", SchedulingPhase.NORMAL_SCHEDULING, 1),
                    ("0001", SchedulingPhase.BLOCKER_RESOLUTION, 1),
                    ("0002", SchedulingPhase.NORMAL_SCHEDULING, 1),
                ],
            )

    def test_persisted_result_finalizes_reserved_attempt_without_reexecution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            index = root / "README.md"
            index.write_text("", encoding="utf-8")
            issue = Issue("0001", "First", root / "0001.md", False)
            writer = LoopStateWriter(index)
            writer.reserve_scheduling_attempt(
                issue,
                phase=SchedulingPhase.NORMAL_SCHEDULING,
                ordinal=1,
            )
            writer.issue_state(issue).update(
                {"status": "COMPLETED", "completed_at": "9999-01-01T00:00:00"}
            )
            writer.flush()
            calls: list[str] = []

            result = execute_dependency_schedule(
                issues=[issue],
                graph=IssueDependencyGraph([issue]),
                state_writer=LoopStateWriter(index),
                execute_issue=lambda issue, _phase, _ordinal: (
                    calls.append(issue.number) or RoleResult(status="PASS")
                ),
            )

            self.assertTrue(result.completed)
            self.assertEqual(calls, [])
            reloaded = LoopStateWriter(index)
            self.assertIsNone(reloaded.active_scheduling_attempt())
            self.assertEqual(reloaded.normal_attempted_issues(), frozenset({"0001"}))

    def test_exhaustion_reports_root_blocker_and_never_runs_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            index = root / "README.md"
            index.write_text("", encoding="utf-8")
            first = Issue("0001", "First", root / "0001.md", False)
            second = Issue("0002", "Second", root / "0002.md", False, ("0001",))
            third = Issue("0003", "Independent", root / "0003.md", False)
            issues = [first, second, third]
            calls: list[tuple[str, SchedulingPhase, int]] = []

            def execute(
                issue: Issue,
                phase: SchedulingPhase,
                ordinal: int,
            ) -> RoleResult:
                calls.append((issue.number, phase, ordinal))
                return RoleResult(
                    status="PASS" if issue.number == "0003" else "BLOCKED"
                )

            writer = LoopStateWriter(index)
            result = execute_dependency_schedule(
                issues=issues,
                graph=IssueDependencyGraph(issues),
                state_writer=writer,
                execute_issue=execute,
            )

            self.assertFalse(result.completed)
            self.assertEqual(
                writer.state["dependency_scheduler"]["phase"],
                "EXHAUSTED",
            )
            self.assertEqual(result.unresolved_blockers, ("0001",))
            self.assertEqual(result.waiting_dependencies, {"0002": ("0001",)})
            self.assertNotIn("0002", [issue_number for issue_number, _, _ in calls])
            self.assertEqual(
                [ordinal for issue_number, phase, ordinal in calls if issue_number == "0001" and phase is SchedulingPhase.BLOCKER_RESOLUTION],
                [1, 2, 3, 4, 5],
            )

    def test_fifth_additional_pass_unlocks_normal_descendant_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            index = root / "README.md"
            index.write_text("", encoding="utf-8")
            first = Issue("0001", "First", root / "0001.md", False)
            second = Issue("0002", "Second", root / "0002.md", False, ("0001",))
            issues = [first, second]
            calls: list[tuple[str, SchedulingPhase, int]] = []

            def execute(
                issue: Issue,
                phase: SchedulingPhase,
                ordinal: int,
            ) -> RoleResult:
                calls.append((issue.number, phase, ordinal))
                if issue.number == "0001" and (
                    phase is SchedulingPhase.NORMAL_SCHEDULING or ordinal < 5
                ):
                    return RoleResult(status="BLOCKED")
                return RoleResult(status="PASS")

            result = execute_dependency_schedule(
                issues=issues,
                graph=IssueDependencyGraph(issues),
                state_writer=LoopStateWriter(index),
                execute_issue=execute,
            )

            self.assertTrue(result.completed)
            self.assertEqual(calls[-1][0], "0002")
            self.assertEqual(
                [
                    ordinal
                    for issue_number, phase, ordinal in calls
                    if issue_number == "0001"
                    and phase is SchedulingPhase.BLOCKER_RESOLUTION
                ],
                [1, 2, 3, 4, 5],
            )

    def test_previous_dry_run_does_not_spend_normal_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            index = root / "README.md"
            index.write_text("", encoding="utf-8")
            issue = Issue("0001", "First", root / "0001.md", False)
            writer = LoopStateWriter(index)
            preview_calls: list[str] = []
            preview = execute_dependency_schedule(
                issues=[issue],
                graph=IssueDependencyGraph([issue]),
                state_writer=writer,
                execute_issue=lambda current, _phase, _ordinal: (
                    preview_calls.append(current.number)
                    or RoleResult(status="PASS")
                ),
                simulation=True,
            )
            self.assertTrue(preview.completed)
            self.assertEqual(preview_calls, ["0001"])
            self.assertEqual(writer.normal_attempted_issues(), frozenset())
            self.assertIsNone(writer.active_scheduling_attempt())
            calls: list[str] = []

            result = execute_dependency_schedule(
                issues=[issue],
                graph=IssueDependencyGraph([issue]),
                state_writer=LoopStateWriter(index),
                execute_issue=lambda current, _phase, _ordinal: (
                    calls.append(current.number) or RoleResult(status="PASS")
                ),
            )

            self.assertTrue(result.completed)
            self.assertEqual(calls, ["0001"])

    def test_cancellation_is_not_retried_or_charged_to_attempt_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            index = root / "README.md"
            index.write_text("", encoding="utf-8")
            issue = Issue("0001", "First", root / "0001.md", False)
            writer = LoopStateWriter(index)
            calls: list[str] = []

            def cancel(
                current: Issue,
                _phase: SchedulingPhase,
                _ordinal: int,
            ) -> RoleResult:
                calls.append(current.number)
                writer.issue_state(current)["status"] = "CANCELLED"
                writer.flush()
                return RoleResult(status="BLOCKED", summary="approval denied")

            result = execute_dependency_schedule(
                issues=[issue],
                graph=IssueDependencyGraph([issue]),
                state_writer=writer,
                execute_issue=cancel,
            )

            self.assertFalse(result.completed)
            self.assertEqual(calls, ["0001"])
            self.assertEqual(writer.normal_attempted_issues(), frozenset())
            self.assertEqual(writer.additional_passes(), {})
            self.assertIsNone(writer.active_scheduling_attempt())


if __name__ == "__main__":
    unittest.main()
