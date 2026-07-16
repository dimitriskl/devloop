from __future__ import annotations

import io
import os
import tempfile
import unittest
from collections.abc import Iterable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from devloop import cli, statusui
from devloop.codex_runner import RoleResult
from devloop.issue_pack import Issue, parse_issue_index
from devloop.portable_workflow import (
    PortableWorkflowExecutor,
    SECURITY_REVIEW_STEP_ID,
    canonical_workflow_hash,
    default_portable_component_catalog,
    default_portable_workflow,
    load_portable_workflow,
)
from devloop.state import LoopStateWriter
from devloop.workflow_defaults import WorkflowDefaultStore
from tests.terminal_safety import (
    HOSTILE_TERMINAL_TEXT,
    assert_terminal_text_is_safe,
)


class RoleResultRenderingTests(unittest.TestCase):
    def test_role_summaries_cannot_inject_terminal_controls(self) -> None:
        class OutputStream(io.StringIO):
            def __init__(self, *, tty: bool) -> None:
                super().__init__()
                self._tty = tty

            @property
            def encoding(self) -> str:
                return "utf-8"

            def isatty(self) -> bool:
                return self._tty

        env = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
        for tty in (True, False):
            for status in ("PASS", "FAIL"):
                with self.subTest(tty=tty, status=status):
                    output = OutputStream(tty=tty)
                    redirect = redirect_stdout if status == "PASS" else redirect_stderr
                    with mock.patch.dict(
                        os.environ,
                        env,
                        clear=True,
                    ), redirect(output):
                        cli.report_role_result(
                            "0009",
                            "coder",
                            RoleResult(
                                status=status,
                                summary=HOSTILE_TERMINAL_TEXT,
                            ),
                        )

                    assert_terminal_text_is_safe(
                        self,
                        output.getvalue(),
                        redirected=not tty,
                    )


class ResolveRunWorkflowTests(unittest.TestCase):
    def test_new_run_snapshots_latest_user_default_without_changing_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = root / "devloop-plan.json"
            catalog = default_portable_component_catalog()
            first_document = default_portable_workflow().to_dict()
            first_security_review = next(
                step
                for step in first_document["steps"]
                if step["instance_id"] == SECURITY_REVIEW_STEP_ID
            )
            first_security_review["display_name"] = "First Default Review"
            first_default = load_portable_workflow(first_document, catalog)
            store = WorkflowDefaultStore(configuration_path, catalog)
            store.replace(first_default)
            first_index = root / "first-issues.md"
            first_index.write_text("", encoding="utf-8")
            active_writer = LoopStateWriter(first_index)

            active = cli.resolve_run_workflow(
                active_writer,
                catalog,
                user_workflow_path=configuration_path,
            )

            second_document = default_portable_workflow().to_dict()
            second_security_review = next(
                step
                for step in second_document["steps"]
                if step["instance_id"] == SECURITY_REVIEW_STEP_ID
            )
            second_security_review["display_name"] = "Second Default Review"
            second_default = load_portable_workflow(second_document, catalog)
            store.replace(second_default)
            unchanged_active = cli.resolve_run_workflow(
                LoopStateWriter(first_index),
                catalog,
                user_workflow_path=configuration_path,
            )
            second_index = root / "second-issues.md"
            second_index.write_text("", encoding="utf-8")
            subsequent = cli.resolve_run_workflow(
                LoopStateWriter(second_index),
                catalog,
                user_workflow_path=configuration_path,
            )

        self.assertEqual(
            active.step(SECURITY_REVIEW_STEP_ID).display_name,
            "First Default Review",
        )
        self.assertEqual(unchanged_active, active)
        self.assertEqual(
            active_writer.state["resolved_workflow_hash"],
            canonical_workflow_hash(first_default),
        )
        self.assertEqual(
            subsequent.step(SECURITY_REVIEW_STEP_ID).display_name,
            "Second Default Review",
        )

    def test_analysis_snapshot_is_persisted_even_after_future_default_changes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = root / "devloop-plan.json"
            catalog = default_portable_component_catalog()
            analysis_snapshot = default_portable_workflow()
            changed_document = analysis_snapshot.to_dict()
            security_review = next(
                step
                for step in changed_document["steps"]
                if step["instance_id"] == SECURITY_REVIEW_STEP_ID
            )
            security_review["display_name"] = "Future Review"
            WorkflowDefaultStore(configuration_path, catalog).replace(
                load_portable_workflow(changed_document, catalog)
            )
            issue_index = root / "issues.md"
            issue_index.write_text("", encoding="utf-8")
            writer = LoopStateWriter(issue_index)

            resolved = cli.resolve_run_workflow(
                writer,
                catalog,
                user_workflow_path=configuration_path,
                workflow_snapshot=analysis_snapshot,
            )

        self.assertEqual(resolved, analysis_snapshot)
        self.assertEqual(
            writer.state["resolved_workflow_hash"],
            canonical_workflow_hash(analysis_snapshot),
        )


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

    def test_dry_run_renders_only_issue_scoped_step_prompts(self) -> None:
        class DryRunRunner:
            dry_run = True

            def __init__(self) -> None:
                self.roles: list[tuple[object, ...]] = []

            def render_dry_run_prompts(
                self,
                _issue: Issue,
                roles: Iterable[tuple[object, ...]],
            ) -> None:
                self.roles = list(roles)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text(
                "# Issue 0001\n\nCompleted: [ ]\n",
                encoding="utf-8",
            )
            issues_index = root / "README.md"
            issues_index.write_text(
                "- [Issue 0001](./0001-example.md)\n",
                encoding="utf-8",
            )
            runner = DryRunRunner()

            result = cli.run_issue(
                issue=Issue("0001", "Issue 0001", issue_path, completed=False),
                runner=runner,
                state_writer=LoopStateWriter(issues_index),
                max_passes=1,
                component_catalog=default_portable_component_catalog(),
            )

        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            [(step[0], step[1]) for step in runner.roles],
            [
                ("coder", "coder"),
                ("reviewer", "reviewer"),
                ("reviewer", "reviewer"),
                ("qa", "qa"),
            ],
        )

    def test_dry_run_bounds_and_sanitizes_hostile_issue_metadata(self) -> None:
        class DryRunRunner:
            dry_run = True

            def render_dry_run_prompts(
                self,
                _issue: Issue,
                _roles: Iterable[tuple[object, ...]],
            ) -> None:
                pass

        hostile_markdown_title = (
            "Καλημέρα 世界 "
            "\x1b[2JESC-CSI "
            "\x9b2JC1-CSI "
            "\x9d0;C1-OSC\x9c "
            "\u202eBIDI"
        )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001-example.md"
            issue_path.write_text("# Issue 0001\n\nCompleted: [ ]\n", encoding="utf-8")
            issues_index = root / "README.md"
            issues_index.write_text(
                f"- [{hostile_markdown_title} {'x' * 500}]"
                "(./0001-example.md)\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                cli.run_issue(
                    issue=parse_issue_index(issues_index)[0],
                    runner=DryRunRunner(),
                    state_writer=LoopStateWriter(issues_index),
                    max_passes=1,
                    component_catalog=default_portable_component_catalog(),
                )

        rendered = output.getvalue()
        assert_terminal_text_is_safe(self, rendered, redirected=True)
        self.assertNotIn("x" * 500, rendered)

    def test_compact_progress_reaches_development_both_reviews_and_qa(self) -> None:
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
                ("reviewer", "0001 1/26 +25"),
                ("qa", "0001 1/26 +25"),
            ],
        )

    def test_rerunning_the_same_command_resumes_the_portable_step_instance(self) -> None:
        class InterruptingRunner:
            dry_run = False

            def run_role(self, **arguments: object) -> RoleResult:
                if arguments["step_display_name"] == "Security Review":
                    raise RuntimeError("simulated interruption")
                return RoleResult(status="PASS")

        class PassingRunner:
            dry_run = False

            def __init__(self) -> None:
                self.calls: list[str] = []

            def run_role(self, **arguments: object) -> RoleResult:
                display_name = str(arguments["step_display_name"])
                self.calls.append(display_name)
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

            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                cli.run_issue(
                    issue=issue,
                    runner=InterruptingRunner(),
                    state_writer=LoopStateWriter(issues_index),
                    max_passes=1,
                )

            resumed_runner = PassingRunner()
            result = cli.run_issue(
                issue=issue,
                runner=resumed_runner,
                state_writer=LoopStateWriter(issues_index),
                max_passes=1,
            )

        self.assertEqual(result.status, "PASS")
        self.assertEqual(resumed_runner.calls, ["Security Review", "Final Review", "QA"])

    def test_blocked_pre_v2_issue_starts_the_portable_workflow_without_attempt_records(self) -> None:
        class PassingRunner:
            dry_run = False

            def __init__(self) -> None:
                self.calls: list[str] = []

            def run_role(self, **arguments: object) -> RoleResult:
                self.calls.append(str(arguments["step_display_name"]))
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
            writer = LoopStateWriter(issues_index)
            workflow = default_portable_workflow()
            writer.record_resolved_workflow(
                workflow,
                default_portable_component_catalog(),
            )
            writer.issue_state(issue).update(
                {
                    "title": issue.title,
                    "path": str(issue.path),
                    "status": "Blocked",
                }
            )
            writer.flush()
            runner = PassingRunner()

            result = cli.run_issue(
                issue=issue,
                runner=runner,
                state_writer=LoopStateWriter(issues_index),
                max_passes=1,
            )

        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            runner.calls,
            ["Development", "Security Review", "Final Review", "QA"],
        )

    def test_rerunning_a_blocked_issue_retries_the_blocked_step_instance(self) -> None:
        class BlockingRunner:
            dry_run = False

            def run_role(self, **arguments: object) -> RoleResult:
                if arguments["step_display_name"] == "Final Review":
                    return RoleResult(
                        status="BLOCKED",
                        summary="temporary review dependency",
                    )
                return RoleResult(status="PASS")

        class PassingRunner:
            dry_run = False

            def __init__(self) -> None:
                self.calls: list[str] = []

            def run_role(self, **arguments: object) -> RoleResult:
                display_name = str(arguments["step_display_name"])
                self.calls.append(display_name)
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
            blocked = cli.run_issue(
                issue=issue,
                runner=BlockingRunner(),
                state_writer=LoopStateWriter(issues_index),
                max_passes=1,
            )
            self.assertEqual(blocked.status, "BLOCKED")

            resumed_runner = PassingRunner()
            result = cli.run_issue(
                issue=issue,
                runner=resumed_runner,
                state_writer=LoopStateWriter(issues_index),
                max_passes=1,
            )

        self.assertEqual(result.status, "PASS")
        self.assertEqual(resumed_runner.calls, ["Final Review", "QA"])

    def test_rerun_finalizes_a_terminal_checkpoint_without_reexecuting_steps(self) -> None:
        class PassingRunner:
            dry_run = False

            def __init__(self) -> None:
                self.calls: list[str] = []

            def run_role(self, **arguments: object) -> RoleResult:
                self.calls.append(str(arguments["step_display_name"]))
                return RoleResult(status="PASS", summary="step completed")

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
            workflow = default_portable_workflow()
            catalog = default_portable_component_catalog()
            state_writer = LoopStateWriter(issues_index)
            state_writer.record_resolved_workflow(workflow, catalog)
            PortableWorkflowExecutor(
                workflow,
                catalog,
                PassingRunner(),
            ).run(
                issue,
                pass_number=1,
                checkpoint=lambda checkpoint: state_writer.record_portable_checkpoint(
                    issue,
                    checkpoint,
                ),
            )
            rerun = PassingRunner()

            result = cli.run_issue(
                issue=issue,
                runner=rerun,
                state_writer=LoopStateWriter(issues_index),
                max_passes=1,
            )

            issue_text = issue_path.read_text(encoding="utf-8")

        self.assertEqual(result.status, "PASS")
        self.assertEqual(rerun.calls, [])
        self.assertIn("Completed: [x]", issue_text)

    def test_clean_retry_executes_both_configured_review_instances(self) -> None:
        class PassingRunner:
            dry_run = False

            def __init__(self) -> None:
                self.calls: list[tuple[str, str | None, str | None]] = []

            def run_role(self, **arguments: object) -> RoleResult:
                self.calls.append(
                    (
                        str(arguments["role"]),
                        (
                            str(arguments["step_display_name"])
                            if arguments.get("step_display_name") is not None
                            else None
                        ),
                        (
                            str(arguments["step_instance_id"])
                            if arguments.get("step_instance_id") is not None
                            else None
                        ),
                    )
                )
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

            result = cli.run_issue(
                issue=issue,
                runner=runner,
                state_writer=LoopStateWriter(issues_index),
                max_passes=1,
                initial_fix_list=["Retry from a clean context."],
                attempt_label="clean-retry-1",
            )

        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            [(role, display_name) for role, display_name, _ in runner.calls],
            [
                ("coder", "Development"),
                ("reviewer", "Security Review"),
                ("reviewer", "Final Review"),
                ("qa", "QA"),
            ],
        )
        review_ids = [
            instance_id
            for role, _, instance_id in runner.calls
            if role == "reviewer"
        ]
        self.assertNotEqual(review_ids[0], review_ids[1])

    def test_clean_retry_context_includes_v2_changes_requested_details(self) -> None:
        class ChangesRequestedRunner:
            dry_run = False

            def run_role(self, **arguments: object) -> RoleResult:
                if arguments["step_display_name"] == "Security Review":
                    return RoleResult(
                        status="FAIL",
                        summary="Security review requested changes.",
                        findings=["Finding SEC-002."],
                        fix_list=["Fix SEC-002 before retrying."],
                    )
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
            state_writer = LoopStateWriter(issues_index)
            cli.run_issue(
                issue=issue,
                runner=ChangesRequestedRunner(),
                state_writer=state_writer,
                max_passes=1,
            )

            retry_context = cli.build_clean_retry_fix_list(
                state_writer,
                issue,
                retry_round=1,
            )

        self.assertTrue(
            any("Fix SEC-002 before retrying." in line for line in retry_context)
        )

    def test_rerun_after_pass_limit_delivers_each_triggering_review_record(self) -> None:
        review_names = ("Security Review", "Final Review")

        for review_name in review_names:
            with self.subTest(review=review_name), tempfile.TemporaryDirectory() as raw:
                self._assert_rerun_delivers_triggering_review_record(raw, review_name)

    def _assert_rerun_delivers_triggering_review_record(
        self,
        raw: str,
        review_name: str,
    ) -> None:
        class ChangesRequestedRunner:
            dry_run = False

            def run_role(self, **arguments: object) -> RoleResult:
                if arguments["step_display_name"] == review_name:
                    return RoleResult(
                        status="FAIL",
                        findings=[f"{review_name} finding"],
                        fix_list=[f"Fix the exact {review_name} finding."],
                    )
                return RoleResult(status="PASS")

        class PassingRunner:
            dry_run = False

            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def run_role(self, **arguments: object) -> RoleResult:
                self.calls.append(arguments)
                return RoleResult(status="PASS")

        root = Path(raw)
        issue_path = root / "0001-example.md"
        issue_path.write_text("# Issue 0001\n\nCompleted: [ ]\n", encoding="utf-8")
        issues_index = root / "README.md"
        issues_index.write_text(
            "- [Issue 0001](./0001-example.md)\n",
            encoding="utf-8",
        )
        issue = Issue("0001", "Issue 0001", issue_path, completed=False)
        first_result = cli.run_issue(
            issue=issue,
            runner=ChangesRequestedRunner(),
            state_writer=LoopStateWriter(issues_index),
            max_passes=1,
        )
        self.assertEqual(first_result.status, "FAIL")
        persisted = LoopStateWriter(issues_index).state
        triggering_record = next(
            record
            for step_records in persisted["step_attempt_records"].values()
            for record in step_records[issue.number]
            if record["outcome"] == "CHANGES_REQUESTED"
        )

        rerun = PassingRunner()
        result = cli.run_issue(
            issue=issue,
            runner=rerun,
            state_writer=LoopStateWriter(issues_index),
            max_passes=1,
        )

        development_call = rerun.calls[0]
        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            [call["step_display_name"] for call in rerun.calls],
            ["Development", "Security Review", "Final Review", "QA"],
        )
        self.assertEqual(
            development_call["fix_list"],
            [f"Fix the exact {review_name} finding."],
        )
        self.assertEqual(development_call["rework_attempt_record"], triggering_record)

    def test_run_issue_executes_the_resolved_workflow_stored_in_loop_state(self) -> None:
        class PassingRunner:
            dry_run = False

            def __init__(self) -> None:
                self.display_names: list[str] = []

            def run_role(self, **arguments: object) -> RoleResult:
                self.display_names.append(str(arguments["step_display_name"]))
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
            workflow = default_portable_workflow()
            document = workflow.to_dict()
            replacement_ids = {
                "Analysis": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "Development": "11111111-1111-4111-8111-111111111111",
                "Security Review": "22222222-2222-4222-8222-222222222222",
                "Final Review": "33333333-3333-4333-8333-333333333333",
                "QA": "44444444-4444-4444-8444-444444444444",
            }
            original_ids = {
                step["display_name"]: step["instance_id"]
                for step in document["steps"]
            }
            id_replacements = {
                original_ids[name]: replacement
                for name, replacement in replacement_ids.items()
            }
            document["start_step_id"] = id_replacements[document["start_step_id"]]
            for step in document["steps"]:
                step["instance_id"] = id_replacements[step["instance_id"]]
                step["transitions"] = {
                    outcome: id_replacements.get(target, target)
                    for outcome, target in step["transitions"].items()
                }
                for binding in step["input_bindings"].values():
                    binding["producer_step_id"] = id_replacements[
                        binding["producer_step_id"]
                    ]
            security_step = next(
                step
                for step in document["steps"]
                if step["display_name"] == "Security Review"
            )
            security_step["display_name"] = "Configured Security Review"
            catalog = default_portable_component_catalog()
            configured_workflow = load_portable_workflow(document, catalog)
            state_writer = LoopStateWriter(issues_index)
            state_writer.record_resolved_workflow(
                configured_workflow,
                catalog,
            )
            runner = PassingRunner()

            result = cli.run_issue(
                issue=issue,
                runner=runner,
                state_writer=state_writer,
                max_passes=1,
            )

        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            runner.display_names,
            ["Development", "Configured Security Review", "Final Review", "QA"],
        )

    def test_run_issue_completes_and_persists_workflow_without_review_or_qa(self) -> None:
        class PassingRunner:
            dry_run = False

            def __init__(self) -> None:
                self.display_names: list[str] = []

            def run_role(self, **arguments: object) -> RoleResult:
                self.display_names.append(str(arguments["step_display_name"]))
                return RoleResult(
                    status="PASS",
                    summary="Implementation completed.",
                    changed_files=["src/example.py"],
                    verification_commands=["python -m unittest"],
                )

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
            document = default_portable_workflow().to_dict()
            development = next(
                step for step in document["steps"] if step["display_name"] == "Development"
            )
            development["transitions"] = {
                "SUCCEEDED": None,
                "BLOCKED": None,
                "FAILED": None,
                "CANCELLED": None,
            }
            document["start_step_id"] = development["instance_id"]
            document["steps"] = [development]
            catalog = default_portable_component_catalog()
            workflow = load_portable_workflow(document, catalog)
            state_writer = LoopStateWriter(issues_index)
            state_writer.record_resolved_workflow(workflow, catalog)
            runner = PassingRunner()

            result = cli.run_issue(
                issue=issue,
                runner=runner,
                state_writer=state_writer,
                max_passes=1,
            )

            issue_text = issue_path.read_text(encoding="utf-8")
            persisted = LoopStateWriter(issues_index).state

        self.assertEqual(result.status, "PASS")
        self.assertEqual(runner.display_names, ["Development"])
        self.assertIn("Completed: [x]", issue_text)
        issue_state = persisted["issues"]["0001"]
        self.assertEqual(issue_state["status"], "COMPLETED")
        self.assertEqual(issue_state["changed_files"], ["src/example.py"])
        self.assertEqual(
            issue_state["verification_commands"],
            ["python -m unittest"],
        )
        self.assertIn("completed_at", issue_state)
        self.assertEqual(
            [event["type"] for event in persisted["events"]].count("issue-completed"),
            1,
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

            def __init__(self, stream: TtyStream) -> None:
                self.stream = stream
                self.screen_before_role: dict[str, str] = {}

            def run_role(self, **arguments: object) -> RoleResult:
                self.screen_before_role[str(arguments["step_display_name"])] = (
                    self.stream.getvalue()
                )
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
            runner = PassingRunner(output)

            env = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
            with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(output):
                cli.run_issue(
                    issue=issue,
                    runner=runner,
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
        self.assertIn("Security Review", rendered)
        self.assertIn("Final Review", rendered)
        self.assertIn(
            "ACTIVE Security Review · model gpt-5.6-sol · effort xhigh · Fast OFF",
            rendered,
        )
        self.assertIn("Security Review", runner.screen_before_role["Security Review"])
        self.assertIn("Final Review", runner.screen_before_role["Security Review"])

    def test_redirected_progress_is_append_only_and_has_no_cursor_controls(self) -> None:
        class PassingRunner:
            dry_run = False

            def run_role(self, **arguments: object) -> RoleResult:
                activity_callback = arguments.get("activity_callback")
                if callable(activity_callback):
                    activity_callback("\x1b[31mChecking\x1b[0m safe\nevent.")
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
            output = io.StringIO()
            with redirect_stdout(output):
                cli.run_issue(
                    issue=Issue("0001", "Publish a validated catalog", issue_path, False),
                    runner=PassingRunner(),
                    state_writer=LoopStateWriter(issues_index),
                    max_passes=1,
                )

        rendered = output.getvalue()
        self.assertIn("WORKFLOW", rendered)
        self.assertIn("CURRENT ISSUE · 0001", rendered)
        self.assertIn("Security Review", rendered)
        self.assertIn("Final Review", rendered)
        self.assertIn("AI › Checking safe event.", rendered)
        self.assertNotRegex(rendered, r"\x1b\[[0-?]*[ -/]*[@-~]")
        self.assertNotIn("\r", rendered)

    def test_tty_and_redirected_execution_sanitize_hostile_issue_metadata(self) -> None:
        class OutputStream(io.StringIO):
            def __init__(self, *, tty: bool) -> None:
                super().__init__()
                self._tty = tty

            @property
            def encoding(self) -> str:
                return "utf-8"

            def isatty(self) -> bool:
                return self._tty

        class PassingRunner:
            dry_run = False

            def run_role(self, **_: object) -> RoleResult:
                return RoleResult(status="PASS", summary="Gate passed.")

        for tty in (True, False):
            with self.subTest(tty=tty), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                issue_path = root / "0001-example.md"
                issue_path.write_text(
                    "# Issue 0001\n\nCompleted: [ ]\n",
                    encoding="utf-8",
                )
                issues_index = root / "README.md"
                issues_index.write_text(
                    "- [Issue 0001](./0001-example.md)\n",
                    encoding="utf-8",
                )
                output = OutputStream(tty=tty)
                with redirect_stdout(output):
                    cli.run_issue(
                        issue=Issue(
                            f"0001{HOSTILE_TERMINAL_TEXT}",
                            f"{HOSTILE_TERMINAL_TEXT} {'x' * 500}",
                            issue_path,
                            completed=False,
                        ),
                        runner=PassingRunner(),
                        state_writer=LoopStateWriter(issues_index),
                        max_passes=1,
                    )

                rendered = output.getvalue()
                assert_terminal_text_is_safe(
                    self,
                    rendered,
                    redirected=not tty,
                )
                self.assertNotIn("x" * 500, rendered)

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
