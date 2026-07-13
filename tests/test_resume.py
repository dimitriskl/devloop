from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devloop.codex_runner import RoleResult
from devloop import cli
from devloop.issue_pack import Issue
from devloop.state import LoopStateWriter, ResumeRole, write_text_creating_parent


class LoopStateResumeTests(unittest.TestCase):
    def test_new_writer_preserves_existing_workflow_history(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text("# Issue 0001\n", encoding="utf-8")
            issues_index = root / "README.md"
            issues_index.write_text("[Issue 0001](./0001-example.md)\n", encoding="utf-8")
            prd_path = root / "example.md"
            prd_path.write_text("# Example PRD\n", encoding="utf-8")
            issue = Issue("0001", "Issue 0001", issue_path, completed=False)

            first_run = LoopStateWriter(issues_index)
            first_run.record_run_start(root, prd_path, [issue.number], dry_run=False)
            first_run.record_issue_start(issue)
            first_run.record_role_result(
                issue,
                "reviewer",
                1,
                RoleResult(status="FAIL", fix_list=["Fix the review blocker."]),
            )

            resumed_run = LoopStateWriter(issues_index)
            resumed_run.record_run_start(root, prd_path, [issue.number], dry_run=False)

            issue_state = resumed_run.state["issues"][issue.number]
            self.assertEqual(issue_state["passes"][0]["role"], "reviewer")
            self.assertEqual(issue_state["passes"][0]["result"]["fix_list"], ["Fix the review blocker."])
            self.assertEqual(
                [event["type"] for event in resumed_run.state["events"]],
                ["run-start", "issue-start", "role-result", "run-start"],
            )

    def test_reviewer_failure_resumes_with_next_coder_pass_and_fix_list(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text("# Issue 0001\n", encoding="utf-8")
            issues_index = root / "README.md"
            issues_index.write_text("[Issue 0001](./0001-example.md)\n", encoding="utf-8")
            prd_path = root / "example.md"
            prd_path.write_text("# Example PRD\n", encoding="utf-8")
            issue = Issue("0001", "Issue 0001", issue_path, completed=False)

            first_run = LoopStateWriter(issues_index)
            first_run.record_run_start(root, prd_path, [issue.number], dry_run=False)
            first_run.record_issue_start(issue)
            first_run.record_role_result(issue, "coder", 1, RoleResult(status="PASS"))
            first_run.record_role_result(
                issue,
                "reviewer",
                1,
                RoleResult(status="FAIL", fix_list=["Fix the review blocker."]),
            )

            resumed_run = LoopStateWriter(issues_index)
            resumed_run.record_run_start(root, prd_path, [issue.number], dry_run=False)
            runner = RecordingRunner()

            cli.run_issue(issue, runner, resumed_run, max_passes=3)

            self.assertEqual(
                runner.calls[0],
                ("coder", 2, ["Fix the review blocker."]),
            )

    def test_coder_pass_resumes_at_reviewer_without_repeating_development(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text("# Issue 0001\n", encoding="utf-8")
            issues_index = root / "README.md"
            issues_index.write_text("[Issue 0001](./0001-example.md)\n", encoding="utf-8")
            prd_path = root / "example.md"
            prd_path.write_text("# Example PRD\n", encoding="utf-8")
            issue = Issue("0001", "Issue 0001", issue_path, completed=False)

            first_run = LoopStateWriter(issues_index)
            first_run.record_run_start(root, prd_path, [issue.number], dry_run=False)
            first_run.record_issue_start(issue)
            first_run.record_role_result(
                issue,
                "coder",
                1,
                RoleResult(status="PASS", changed_files=["src/example.py"]),
            )

            resumed_run = LoopStateWriter(issues_index)
            resumed_run.record_run_start(root, prd_path, [issue.number], dry_run=False)
            runner = RecordingRunner()

            cli.run_issue(issue, runner, resumed_run, max_passes=3)

            self.assertEqual(runner.calls[0], ("reviewer", 1, []))

    def test_reviewer_pass_resumes_at_qa_without_repeating_prior_roles(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text("# Issue 0001\n", encoding="utf-8")
            issues_index = root / "README.md"
            issues_index.write_text("[Issue 0001](./0001-example.md)\n", encoding="utf-8")
            prd_path = root / "example.md"
            prd_path.write_text("# Example PRD\n", encoding="utf-8")
            issue = Issue("0001", "Issue 0001", issue_path, completed=False)

            first_run = LoopStateWriter(issues_index)
            first_run.record_run_start(root, prd_path, [issue.number], dry_run=False)
            first_run.record_issue_start(issue)
            first_run.record_role_result(issue, "coder", 1, RoleResult(status="PASS"))
            first_run.record_role_result(issue, "reviewer", 1, RoleResult(status="PASS"))

            resumed_run = LoopStateWriter(issues_index)
            resumed_run.record_run_start(root, prd_path, [issue.number], dry_run=False)
            runner = RecordingRunner()

            cli.run_issue(issue, runner, resumed_run, max_passes=1)

            self.assertEqual(runner.calls[0], ("qa", 1, []))

    def test_missing_state_history_recovers_cursor_from_role_result_logs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text("# Issue 0001\n", encoding="utf-8")
            issues_index = root / "README.md"
            issues_index.write_text("[Issue 0001](./0001-example.md)\n", encoding="utf-8")
            issue = Issue("0001", "Issue 0001", issue_path, completed=False)
            logs = root / ".loop.logs"
            logs.mkdir()
            (logs / "0001-coder-pass1.last-message.json").write_text(
                json.dumps({"status": "PASS", "changed_files": ["src/example.py"]}),
                encoding="utf-8",
            )
            (logs / "0001-reviewer-pass1.last-message.json").write_text(
                json.dumps({
                    "status": "FAIL",
                    "fix_list": ["Fix the recovered review blocker."],
                }),
                encoding="utf-8",
            )
            state_path = root / "README.loop.state.json"
            state_path.write_text(
                json.dumps({
                    "issues_index": str(issues_index),
                    "events": [],
                    "issues": {issue.number: {"status": "In Progress"}},
                }),
                encoding="utf-8",
            )

            writer = LoopStateWriter(issues_index)
            cursor = writer.resume_issue(issue)

            self.assertEqual(cursor.next_role, ResumeRole.CODER)
            self.assertEqual(cursor.pass_number, 2)
            self.assertEqual(cursor.fix_list, ("Fix the recovered review blocker.",))

    def test_qa_pass_finalizes_issue_without_rerunning_any_role(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text(
                "# Issue 0001\n\nCompleted: [ ]\n\n## Acceptance criteria\n\n- [ ] Done\n",
                encoding="utf-8",
            )
            issues_index = root / "README.md"
            issues_index.write_text("[Issue 0001](./0001-example.md)\n", encoding="utf-8")
            prd_path = root / "example.md"
            prd_path.write_text("# Example PRD\n", encoding="utf-8")
            issue = Issue("0001", "Issue 0001", issue_path, completed=False)

            first_run = LoopStateWriter(issues_index)
            first_run.record_run_start(root, prd_path, [issue.number], dry_run=False)
            first_run.record_issue_start(issue)
            first_run.record_role_result(
                issue,
                "coder",
                1,
                RoleResult(status="PASS", changed_files=["src/example.py"]),
            )
            first_run.record_role_result(issue, "reviewer", 1, RoleResult(status="PASS"))
            first_run.record_role_result(
                issue,
                "qa",
                1,
                RoleResult(status="PASS", verification_commands=["python -m unittest"]),
            )

            resumed_run = LoopStateWriter(issues_index)
            resumed_run.record_run_start(root, prd_path, [issue.number], dry_run=False)
            runner = RecordingRunner()

            result = cli.run_issue(issue, runner, resumed_run, max_passes=3)

            self.assertEqual(result.status, "PASS")
            self.assertEqual(runner.calls, [])
            self.assertIn("Completed: [x]", issue_path.read_text(encoding="utf-8"))
            self.assertEqual(resumed_run.state["issues"][issue.number]["status"], "Completed")


class WorktreeSelectionTests(unittest.TestCase):
    def test_mapping_selected_issues_never_reintroduces_source_completions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_repo = root / "source"
            target_repo = root / "target"
            source_repo.mkdir()
            target_repo.mkdir()
            source_issue_2 = source_repo / "issues" / "0002-second.md"
            target_issue_2 = target_repo / "issues" / "0002-second.md"
            source_issue_2.parent.mkdir()
            target_issue_2.parent.mkdir()
            source_issue_2.write_text("# Issue 0002\n", encoding="utf-8")
            target_issue_2.write_text("# Issue 0002\n", encoding="utf-8")
            selected_source_issues = [
                Issue("0002", "Issue 0002", source_issue_2, completed=False),
            ]

            mapped = cli.map_selected_issues_to_worktree(
                selected_source_issues,
                source_repo,
                target_repo,
            )

            self.assertEqual([issue.number for issue in mapped], ["0002"])

    def test_mapping_drops_an_issue_already_completed_in_the_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_repo = root / "source"
            target_repo = root / "target"
            source_issue = source_repo / "issues" / "0001-first.md"
            target_issue = target_repo / "issues" / "0001-first.md"
            source_issue.parent.mkdir(parents=True)
            target_issue.parent.mkdir(parents=True)
            source_issue.write_text("# Issue 0001\n\nCompleted: [ ]\n", encoding="utf-8")
            target_issue.write_text("# Issue 0001\n\nCompleted: [x]\n", encoding="utf-8")

            mapped = cli.map_selected_issues_to_worktree(
                [Issue("0001", "Issue 0001", source_issue, completed=False)],
                source_repo,
                target_repo,
            )

            self.assertEqual(mapped, [])


class StatePersistenceTests(unittest.TestCase):
    def test_failed_atomic_commit_preserves_previous_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "README.loop.state.json"
            state_path.write_text("previous state", encoding="utf-8")

            with patch.object(Path, "replace", side_effect=OSError("commit interrupted")):
                with self.assertRaisesRegex(OSError, "commit interrupted"):
                    write_text_creating_parent(state_path, "new state")

            self.assertEqual(state_path.read_text(encoding="utf-8"), "previous state")
            self.assertEqual(list(root.glob(f".{state_path.name}.*.tmp")), [])


class RecordingRunner:
    dry_run = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, list[str]]] = []

    def run_role(
        self,
        *,
        role: str,
        issue: Issue,
        pass_number: int,
        fix_list: list[str] | None = None,
        **_: object,
    ) -> RoleResult:
        self.calls.append((role, pass_number, list(fix_list or [])))
        return RoleResult(status="BLOCKED", summary="Stop after the first resumed call.")


if __name__ == "__main__":
    unittest.main()
