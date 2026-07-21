from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devloop import cli, codex_runner
from devloop.codex_runner import RoleResult
from devloop.issue_pack import Issue
from devloop.portable_workflow import (
    DEVELOPMENT_STEP_ID,
    FINAL_REVIEW_STEP_ID,
    QA_STEP_ID,
    SECURITY_REVIEW_STEP_ID,
    IssueStatus,
    PortableWorkflowCheckpoint,
    default_portable_component_catalog,
    default_portable_workflow,
)
from devloop.state import (
    IssueResumeCursor,
    LoopStateWriter,
    ResumeRole,
    recover_role_passes,
    write_text_creating_parent,
)
from devloop.templates import BundleContext, Preset


class LoopStateLoadingTests(unittest.TestCase):
    def test_generated_portable_artifact_with_truncated_display_name_is_ignored(
        self,
    ) -> None:
        repository_root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Security review\n", encoding="utf-8")
            runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
            runner.bundle = BundleContext(
                root=repository_root,
                prompts=repository_root / "prompts",
                schemas=repository_root / "schemas",
            )
            runner.repo_root = root
            runner.prd_path = root / "prd.md"
            runner.issues_index = root / "README.md"
            runner.preset = Preset(
                name="test",
                required_docs=[],
                roles={"security/reviewer": {"skills": [], "agents": []}},
            )
            runner.codex = "codex"
            runner.sandbox = "workspace-write"
            runner.approval_policy = "never"
            runner.log_root = root / ".loop.logs"
            runner.use_self_improvement_wiki = False
            runner.ensure_log_root()
            issue = Issue("0001", "Security review", issue_path, False)
            step_id = "22222222-2222-4222-8222-222222222222"
            display_name = "a" * 47 + " b"

            def build_command(**arguments: object) -> list[str]:
                return ["codex", "-o", str(arguments["message_path"])]

            def execute(
                command: list[str],
                **_: object,
            ) -> codex_runner.subprocess.CompletedProcess[str]:
                message_path = Path(command[command.index("-o") + 1])
                message_path.write_text(
                    json.dumps({"status": "PASS", "summary": "portable"}),
                    encoding="utf-8",
                )
                return codex_runner.subprocess.CompletedProcess(
                    command,
                    0,
                    stdout='{"status":"PASS"}',
                    stderr="",
                )

            with patch.object(
                codex_runner,
                "build_codex_exec_command",
                side_effect=build_command,
            ), patch.object(
                runner,
                "run_codex_exec_with_connection_retries",
                side_effect=execute,
            ):
                runner.run_role(
                    role="security/reviewer",
                    role_adapter="reviewer",
                    issue=issue,
                    pass_number=1,
                    step_instance_id=step_id,
                    step_display_name=display_name,
                    step_attempt_id="11111111-1111-4111-8111-111111111111",
                )

            artifacts = list(runner.log_root.glob("*.last-message.json"))
            recovered = recover_role_passes(runner.log_root, issue)

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(recovered, [])

    def test_malformed_existing_state_fails_without_replacing_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issues_index = root / "README.md"
            issues_index.write_text("# Issues\n", encoding="utf-8")
            state_path = root / "README.loop.state.json"
            malformed_state = '{"events": ['
            state_path.write_text(malformed_state, encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                r"existing loop state.*valid JSON",
            ):
                LoopStateWriter(issues_index)

            self.assertEqual(
                state_path.read_text(encoding="utf-8"),
                malformed_state,
            )

    def test_non_object_existing_state_fails_without_replacing_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issues_index = root / "README.md"
            issues_index.write_text("# Issues\n", encoding="utf-8")
            state_path = root / "README.loop.state.json"
            non_object_state = '[{"unexpected": "array"}]'
            state_path.write_text(non_object_state, encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                r"existing loop state.*JSON object",
            ):
                LoopStateWriter(issues_index)

            self.assertEqual(
                state_path.read_text(encoding="utf-8"),
                non_object_state,
            )

    def test_unreadable_existing_state_fails_without_replacing_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issues_index = root / "README.md"
            issues_index.write_text("# Issues\n", encoding="utf-8")
            state_path = root / "README.loop.state.json"
            original_state = '{"events": [], "issues": {}}'
            state_path.write_text(original_state, encoding="utf-8")

            with patch.object(
                Path,
                "read_text",
                side_effect=OSError("permission denied"),
            ), self.assertRaisesRegex(
                ValueError,
                r"existing loop state.*could not be read: permission denied",
            ):
                LoopStateWriter(issues_index)

            self.assertEqual(
                state_path.read_text(encoding="utf-8"),
                original_state,
            )


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

    def test_canonical_in_progress_status_restores_the_legacy_role_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text("# Issue 0001\n", encoding="utf-8")
            issues_index = root / "README.md"
            issues_index.write_text("[Issue 0001](./0001-example.md)\n", encoding="utf-8")
            issue = Issue("0001", "Issue 0001", issue_path, completed=False)
            state_path = root / "README.loop.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "events": [],
                        "issues": {
                            issue.number: {
                                "status": "IN_PROGRESS",
                                "passes": [
                                    {
                                        "role": "coder",
                                        "pass": 1,
                                        "result": {"status": "PASS"},
                                    }
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            cursor = LoopStateWriter(issues_index).resume_issue(issue)

        self.assertEqual(cursor.next_role, ResumeRole.REVIEWER)
        self.assertEqual(cursor.pass_number, 1)

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

    def test_recovery_uses_the_newest_duplicate_attempt_result_independent_of_file_order(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text("# Issue 0001\n", encoding="utf-8")
            issues_index = root / "README.md"
            issues_index.write_text(
                "[Issue 0001](./0001-example.md)\n",
                encoding="utf-8",
            )
            issue = Issue("0001", "Issue 0001", issue_path, completed=False)
            logs = root / ".loop.logs"
            logs.mkdir()
            coder = logs / "0001-coder-pass1.last-message.json"
            older_review = logs / (
                "0001-attempt-older-reviewer-pass1.last-message.json"
            )
            newer_review = logs / (
                "0001-attempt-newer-reviewer-pass1.last-message.json"
            )
            coder.write_text(
                json.dumps({"status": "PASS"}),
                encoding="utf-8",
            )
            older_review.write_text(
                json.dumps({"status": "FAIL", "fix_list": ["stale"]}),
                encoding="utf-8",
            )
            newer_review.write_text(
                json.dumps({"status": "PASS", "summary": "newest"}),
                encoding="utf-8",
            )
            os.utime(older_review, ns=(1_000_000_000, 1_000_000_000))
            os.utime(newer_review, ns=(2_000_000_000, 2_000_000_000))
            state_path = root / "README.loop.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "issues_index": str(issues_index),
                        "events": [],
                        "issues": {issue.number: {"status": "IN_PROGRESS"}},
                    }
                ),
                encoding="utf-8",
            )

            for paths in (
                (newer_review, coder, older_review),
                (older_review, newer_review, coder),
            ):
                with self.subTest(enumeration=tuple(path.name for path in paths)):
                    writer = LoopStateWriter(issues_index)
                    with patch.object(Path, "glob", return_value=paths):
                        cursor = writer.resume_issue(issue)

                    self.assertEqual(cursor.next_role, ResumeRole.QA)
                    self.assertEqual(cursor.pass_number, 1)
                    self.assertIsNotNone(cursor.reviewer_result)
                    assert cursor.reviewer_result is not None
                    self.assertEqual(cursor.reviewer_result.summary, "newest")

    def test_portable_attempt_logs_do_not_override_repeated_review_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text("# Issue 0001\n", encoding="utf-8")
            issues_index = root / "README.md"
            issues_index.write_text(
                "[Issue 0001](./0001-example.md)\n",
                encoding="utf-8",
            )
            issue = Issue("0001", "Issue 0001", issue_path, completed=False)
            workflow = default_portable_workflow()
            catalog = default_portable_component_catalog()
            writer = LoopStateWriter(issues_index)
            writer.record_resolved_workflow(workflow, catalog)
            writer.record_portable_checkpoint(
                issue,
                PortableWorkflowCheckpoint(
                    issue_id=issue.number,
                    issue_status=IssueStatus.IN_PROGRESS,
                    current_step_instance_id=FINAL_REVIEW_STEP_ID,
                    pass_number=1,
                    runtime_states=(),
                    attempts=(),
                    cycle_path_step_instance_ids=(
                        DEVELOPMENT_STEP_ID,
                        SECURITY_REVIEW_STEP_ID,
                        FINAL_REVIEW_STEP_ID,
                    ),
                ),
            )
            logs = root / ".loop.logs"
            logs.mkdir()
            for step_id, display_name, role in (
                (DEVELOPMENT_STEP_ID, "development", "coder"),
                (SECURITY_REVIEW_STEP_ID, "security-review", "reviewer"),
                (FINAL_REVIEW_STEP_ID, "final-review", "reviewer"),
            ):
                (logs / (
                    f"0001-attempt-11111111-1111-4111-8111-111111111111-"
                    f"portable-step-{display_name}-{step_id}-{role}-pass1.last-message.json"
                )).write_text(
                    json.dumps({"status": "PASS"}),
                    encoding="utf-8",
                )

            restored = LoopStateWriter(issues_index)
            legacy_cursor = restored.resume_issue(issue)
            portable_checkpoint = restored.resume_portable_workflow(issue, workflow)

        self.assertEqual(legacy_cursor, IssueResumeCursor())
        self.assertIsNotNone(portable_checkpoint)
        assert portable_checkpoint is not None
        self.assertEqual(
            portable_checkpoint.current_step_instance_id,
            FINAL_REVIEW_STEP_ID,
        )
        self.assertEqual(
            portable_checkpoint.cycle_path_step_instance_ids,
            (DEVELOPMENT_STEP_ID, SECURITY_REVIEW_STEP_ID, FINAL_REVIEW_STEP_ID),
        )

    def test_uuid_shaped_legacy_artifact_is_recovered_but_portable_artifact_is_ignored(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text("# Issue 0001\n", encoding="utf-8")
            issue = Issue("0001", "Issue 0001", issue_path, completed=False)
            logs = root / ".loop.logs"
            logs.mkdir()
            legacy = logs / (
                "0001-attempt-11111111-1111-4111-8111-111111111111-"
                "coder-pass1.last-message.json"
            )
            portable = logs / (
                "0001-attempt-11111111-1111-4111-8111-111111111111-"
                "portable-step-development-22222222-2222-4222-8222-222222222222-"
                "coder-pass1.last-message.json"
            )
            legacy.write_text(
                json.dumps({"status": "PASS", "summary": "legacy"}),
                encoding="utf-8",
            )
            portable.write_text(
                json.dumps({"status": "PASS", "summary": "portable"}),
                encoding="utf-8",
            )

            recovered = recover_role_passes(logs, issue)

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["result"]["summary"], "legacy")
        self.assertEqual(recovered[0]["recovered_from"], str(legacy))

    def test_portable_custom_roles_ending_in_legacy_role_names_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text("# Issue 0001\n", encoding="utf-8")
            issue = Issue("0001", "Issue 0001", issue_path, completed=False)
            logs = root / ".loop.logs"
            logs.mkdir()
            for index, role in enumerate(
                ("custom-coder", "custom-reviewer", "custom-qa"),
                1,
            ):
                (logs / (
                    "0001-attempt-11111111-1111-4111-8111-111111111111-"
                    f"portable-step-{role}-00000000-0000-4000-8000-00000000000{index}-"
                    f"{role}-pass1.last-message.json"
                )).write_text(
                    json.dumps({"status": "PASS", "summary": role}),
                    encoding="utf-8",
                )

            recovered = recover_role_passes(logs, issue)

        self.assertEqual(recovered, [])

    def test_cli_resume_prefers_portable_checkpoint_over_portable_attempt_logs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text(
                "# Issue 0001\n\nCompleted: [ ]\n",
                encoding="utf-8",
            )
            issues_index = root / "README.md"
            issues_index.write_text(
                "[Issue 0001](./0001-example.md)\n",
                encoding="utf-8",
            )
            issue = Issue("0001", "Issue 0001", issue_path, completed=False)
            workflow = default_portable_workflow()
            catalog = default_portable_component_catalog()
            writer = LoopStateWriter(issues_index)
            writer.record_resolved_workflow(workflow, catalog)

            with self.assertRaisesRegex(RuntimeError, "simulated resume interruption"):
                cli.run_issue(
                    issue,
                    PortableResumeRunner(FINAL_REVIEW_STEP_ID),
                    writer,
                    max_passes=1,
                )

            logs = root / ".loop.logs"
            logs.mkdir()
            attempt_id = "11111111-1111-4111-8111-111111111111"
            for step_id, display_name, role in (
                (DEVELOPMENT_STEP_ID, "development", "coder"),
                (SECURITY_REVIEW_STEP_ID, "security-review", "reviewer"),
            ):
                (logs / (
                    f"0001-attempt-{attempt_id}-portable-step-{display_name}-{step_id}-"
                    f"{role}-pass1.last-message.json"
                )).write_text(
                    json.dumps({"status": "PASS"}),
                    encoding="utf-8",
                )

            resumed_runner = PortableResumeRunner()
            result = cli.run_issue(
                issue,
                resumed_runner,
                LoopStateWriter(issues_index),
                max_passes=1,
            )

        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            resumed_runner.calls,
            [
                ("reviewer", str(FINAL_REVIEW_STEP_ID)),
                ("qa", str(QA_STEP_ID)),
            ],
        )

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


class PortableResumeRunner:
    dry_run = False

    def __init__(self, interrupt_step_instance_id: str | None = None) -> None:
        self.interrupt_step_instance_id = (
            str(interrupt_step_instance_id)
            if interrupt_step_instance_id is not None
            else None
        )
        self.calls: list[tuple[str, str]] = []

    def run_role(
        self,
        *,
        role: str,
        step_instance_id: str,
        **_: object,
    ) -> RoleResult:
        self.calls.append((role, step_instance_id))
        if step_instance_id == self.interrupt_step_instance_id:
            raise RuntimeError("simulated resume interruption")
        return RoleResult(status="PASS", summary=f"{role} passed")


if __name__ == "__main__":
    unittest.main()
