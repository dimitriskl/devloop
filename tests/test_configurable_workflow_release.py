from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from devloop import cli
from devloop.codex_runner import RoleResult
from devloop.issue_pack import Issue
from devloop.portable_workflow import (
    DEVELOPMENT_STEP_ID,
    FINAL_REVIEW_STEP_ID,
    SECURITY_REVIEW_STEP_ID,
    FastPreference,
    StepOutcome,
    default_portable_component_catalog,
    default_portable_workflow,
    load_portable_workflow,
    step_attempt_record_to_dict,
)
from devloop.state import LoopStateWriter
from devloop.statusui import project_workflow_progress, render_workflow_progress


class ConfigurableWorkflowReleaseTests(unittest.TestCase):
    def test_configured_reviews_rework_interrupt_resume_and_preserve_release_evidence(
        self,
    ) -> None:
        catalog = default_portable_component_catalog()
        document = default_portable_workflow().to_dict()
        steps = {step["instance_id"]: step for step in document["steps"]}
        security_review = steps[str(SECURITY_REVIEW_STEP_ID)]
        final_review = steps[str(FINAL_REVIEW_STEP_ID)]
        security_review["codex_settings"] = {
            "model": "gpt-5.6-luna",
            "reasoning_effort": "high",
            "fast": "OFF",
        }
        security_review["capability_profile"]["agent_references"] = []
        security_review["guidance"] = {
            "text": "Prioritize authentication and trust-boundary defects.",
            "review_state": "READY",
        }
        final_review["codex_settings"] = {
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "fast": "ON",
        }
        final_review["guidance"] = {
            "text": "Confirm the complete acceptance evidence before release.",
            "review_state": "READY",
        }
        workflow = load_portable_workflow(document, catalog)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0010-release.md"
            issue_path.write_text(
                "# Validate configurable workflow release\n\nCompleted: [ ]\n",
                encoding="utf-8",
            )
            issue_index = root / "README.md"
            issue_index.write_text(
                "- [Validate release](./0010-release.md)\n",
                encoding="utf-8",
            )
            issue = Issue("0010", "Validate release", issue_path, completed=False)
            writer = LoopStateWriter(issue_index)
            writer.record_resolved_workflow(workflow, catalog)

            first_runner = _ReworkThenInterruptRunner()
            console_output = io.StringIO()
            with redirect_stdout(console_output):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated release interruption",
                ):
                    cli.run_issue(
                        issue=issue,
                        runner=first_runner,
                        state_writer=writer,
                        max_passes=2,
                    )

            interrupted = LoopStateWriter(issue_index)
            trigger = next(
                attempt
                for attempt in interrupted.step_attempt_records(issue.number)
                if attempt.step_instance_id == SECURITY_REVIEW_STEP_ID
                and attempt.outcome is StepOutcome.CHANGES_REQUESTED
            )
            self.assertEqual(
                first_runner.rework_attempt_record,
                step_attempt_record_to_dict(trigger),
            )

            resumed_runner = _PassingReleaseRunner()
            with redirect_stdout(console_output):
                result = cli.run_issue(
                    issue=issue,
                    runner=resumed_runner,
                    state_writer=interrupted,
                    max_passes=2,
                )

            restored = LoopStateWriter(issue_index)
            attempts = restored.step_attempt_records(issue.number)
            runtimes = restored.step_runtime_states(issue.number)
            projection = project_workflow_progress(
                restored.resolved_workflow(catalog),
                catalog,
                runtimes,
                attempts,
                issue_id=issue.number,
            )
            rendered = render_workflow_progress(
                projection,
                width=100,
                color=False,
                unicode=False,
                frame="/",
            )

        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            first_runner.display_names,
            [
                "Development",
                "Security Review",
                "Development",
                "Security Review",
                "Final Review",
            ],
        )
        self.assertEqual(resumed_runner.display_names, ["Final Review", "QA"])
        self.assertEqual(resumed_runner.qa_review_summary, "Final Review passed.")
        self.assertEqual(len(attempts), 6)
        self.assertEqual(len({attempt.attempt_id for attempt in attempts}), 6)
        self.assertEqual(
            [attempt.pass_number for attempt in attempts],
            [1, 1, 2, 2, 2, 2],
        )
        rework = next(
            attempt
            for attempt in attempts
            if attempt.step_instance_id == DEVELOPMENT_STEP_ID
            and attempt.pass_number == 2
        )
        self.assertEqual(rework.rework_attempt_id, trigger.attempt_id)

        security_progress = projection.by_step_instance_id[
            str(SECURITY_REVIEW_STEP_ID)
        ]
        final_progress = projection.by_step_instance_id[str(FINAL_REVIEW_STEP_ID)]
        self.assertEqual(
            (
                security_progress.model,
                security_progress.reasoning_effort,
                security_progress.fast,
                security_progress.pass_number,
            ),
            ("gpt-5.6-luna", "high", FastPreference.OFF.value, 2),
        )
        self.assertEqual(
            (
                final_progress.model,
                final_progress.reasoning_effort,
                final_progress.fast,
                final_progress.pass_number,
            ),
            ("gpt-5.6-sol", "xhigh", FastPreference.ON.value, 2),
        )
        self.assertIn("Security Review", rendered)
        self.assertIn("Final Review", rendered)
        self.assertIn("PASS", rendered)
        self.assertIn("Security Review", console_output.getvalue())
        self.assertIn("Final Review", console_output.getvalue())
        self.assertNotIn("\x1b[", console_output.getvalue())

        security_attempt = next(
            attempt
            for attempt in attempts
            if attempt.step_instance_id == SECURITY_REVIEW_STEP_ID
            and attempt.outcome is StepOutcome.SUCCEEDED
        )
        final_attempt = next(
            attempt
            for attempt in attempts
            if attempt.step_instance_id == FINAL_REVIEW_STEP_ID
        )
        assert security_attempt.attempt_context is not None
        assert final_attempt.attempt_context is not None
        self.assertEqual(
            security_attempt.attempt_context.guidance,
            "Prioritize authentication and trust-boundary defects.",
        )
        self.assertEqual(
            final_attempt.attempt_context.guidance,
            "Confirm the complete acceptance evidence before release.",
        )
        self.assertNotEqual(
            security_attempt.attempt_context.capability_profile,
            final_attempt.attempt_context.capability_profile,
        )


class _ReworkThenInterruptRunner:
    dry_run = False

    def __init__(self) -> None:
        self.display_names: list[str] = []
        self._requested_changes = False
        self.rework_attempt_record: object | None = None

    def run_role(self, **arguments: object) -> RoleResult:
        display_name = str(arguments["step_display_name"])
        self.display_names.append(display_name)
        if display_name == "Security Review" and not self._requested_changes:
            self._requested_changes = True
            return RoleResult(
                status="FAIL",
                summary="Security Review requested authentication corrections.",
                findings=["Authentication boundary evidence is incomplete."],
                fix_list=["Add the missing authentication boundary evidence."],
            )
        if display_name == "Development" and arguments["pass_number"] == 2:
            self.rework_attempt_record = arguments["rework_attempt_record"]
        if display_name == "Final Review":
            raise RuntimeError("simulated release interruption")
        return RoleResult(status="PASS", summary=f"{display_name} passed.")


class _PassingReleaseRunner:
    dry_run = False

    def __init__(self) -> None:
        self.display_names: list[str] = []
        self.qa_review_summary = ""

    def run_role(self, **arguments: object) -> RoleResult:
        display_name = str(arguments["step_display_name"])
        self.display_names.append(display_name)
        if display_name == "QA":
            review_result = arguments["review_result"]
            assert isinstance(review_result, RoleResult)
            self.qa_review_summary = review_result.summary
        return RoleResult(status="PASS", summary=f"{display_name} passed.")


if __name__ == "__main__":
    unittest.main()
