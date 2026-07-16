from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from devloop.codex_runner import RoleResult
from devloop.portable_workflow import (
    DEVELOPMENT_STEP_ID,
    FINAL_REVIEW_STEP_ID,
    REVIEWER_COMPONENT_ID,
    StepAttemptRecord,
    StepInstanceId,
    StepOutcome,
    StepRuntimeState,
    StepRuntimeStatus,
    WorkflowStep,
    default_portable_component_catalog,
    default_portable_workflow,
)
from devloop.statusui import (
    DashboardStatus,
    WorkflowProgressScope,
    project_workflow_progress,
    render_workflow_progress,
)
from devloop.terminal_editor import display_width


class WorkflowProgressProjectionTests(unittest.TestCase):
    def test_projection_separates_workflow_and_issue_step_instances(self) -> None:
        workflow = default_portable_workflow()
        catalog = default_portable_component_catalog()
        runtime = StepRuntimeState(
            step_instance_id=DEVELOPMENT_STEP_ID,
            issue_id="0009",
            status=StepRuntimeStatus.RUNNING,
            pass_number=1,
        )

        projection = project_workflow_progress(
            workflow,
            catalog,
            (runtime,),
            (),
            issue_id="0009",
        )

        self.assertEqual(
            [(row.display_name, row.scope) for row in projection.workflow_steps],
            [("Analysis", WorkflowProgressScope.WORKFLOW)],
        )
        self.assertEqual(
            [row.display_name for row in projection.issue_steps],
            ["Development", "Security Review", "Final Review", "QA"],
        )
        self.assertEqual(
            len({row.step_instance_id for row in projection.issue_steps}),
            4,
        )
        self.assertEqual(
            projection.by_step_instance_id[str(DEVELOPMENT_STEP_ID)].display_name,
            "Development",
        )
        self.assertEqual(projection.active_step.status, DashboardStatus.WORKING)

    def test_branch_step_appears_only_after_visit_or_expansion(self) -> None:
        workflow = default_portable_workflow()
        catalog = default_portable_component_catalog()
        branch_id = StepInstanceId("5cd93486-2a72-4020-bb61-efda6b8500c8")
        branch = WorkflowStep(
            instance_id=branch_id,
            display_name="Incident Review",
            component_id=REVIEWER_COMPONENT_ID,
            transitions={StepOutcome.SUCCEEDED: FINAL_REVIEW_STEP_ID},
            codex_settings=workflow.step(FINAL_REVIEW_STEP_ID).codex_settings,
        )
        development = workflow.step(DEVELOPMENT_STEP_ID)
        workflow = replace(
            workflow,
            steps=(
                *(
                    replace(
                        step,
                        transitions={**step.transitions, StepOutcome.BLOCKED: branch_id},
                    )
                    if step.instance_id == development.instance_id
                    else step
                    for step in workflow.steps
                ),
                branch,
            ),
        )

        hidden = project_workflow_progress(
            workflow,
            catalog,
            (),
            (),
            issue_id="0009",
        )
        visited = project_workflow_progress(
            workflow,
            catalog,
            (
                StepRuntimeState(
                    step_instance_id=branch_id,
                    issue_id="0009",
                    status=StepRuntimeStatus.COMPLETED,
                    pass_number=1,
                ),
            ),
            (),
            issue_id="0009",
        )
        expanded = project_workflow_progress(
            workflow,
            catalog,
            (),
            (),
            issue_id="0009",
            expanded_branches=True,
        )

        self.assertNotIn("Incident Review", [step.display_name for step in hidden.issue_steps])
        self.assertIn("Incident Review", [step.display_name for step in visited.issue_steps])
        self.assertIn("Incident Review", [step.display_name for step in expanded.issue_steps])

    def test_active_projection_includes_settings_live_timers_and_safe_activity(self) -> None:
        workflow = default_portable_workflow()
        catalog = default_portable_component_catalog()
        runtime = StepRuntimeState(
            step_instance_id=DEVELOPMENT_STEP_ID,
            issue_id="0009",
            status=StepRuntimeStatus.RUNNING,
            pass_number=2,
            attempt_id="attempt-dev-2",
        )

        projection = project_workflow_progress(
            workflow,
            catalog,
            (runtime,),
            (),
            issue_id="0009",
            active_elapsed_seconds=12,
            event_freshness_seconds=3,
            activity="\x1b[31mChecking\x1b[0m\nsecrets and output.\x00",
        )

        active = projection.active_step
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.display_name, "Development")
        self.assertEqual(active.pass_number, 2)
        self.assertEqual(active.elapsed_seconds, 12)
        self.assertEqual(active.model, "gpt-5.6-luna")
        self.assertEqual(active.reasoning_effort, "high")
        self.assertEqual(active.fast, "OFF")
        self.assertEqual(active.attempt_id, "attempt-dev-2")
        self.assertEqual(projection.activity.event_freshness_seconds, 3)
        self.assertEqual(
            projection.activity.safe_text,
            "Checking secrets and output.",
        )

    def test_completed_time_freezes_and_rework_accumulates_with_latest_result(self) -> None:
        workflow = default_portable_workflow()
        catalog = default_portable_component_catalog()
        attempts = (
            StepAttemptRecord(
                attempt_id="attempt-dev-1",
                step_instance_id=DEVELOPMENT_STEP_ID,
                issue_id="0009",
                pass_number=1,
                prompt_session_id="prompt-dev-1",
                outcome=StepOutcome.SUCCEEDED,
                result=RoleResult(status="PASS", summary="First pass complete."),
                outputs={},
                started_at="2026-07-16T09:00:00Z",
                finished_at="2026-07-16T09:00:04Z",
                elapsed_seconds=4,
            ),
            StepAttemptRecord(
                attempt_id="attempt-dev-2",
                step_instance_id=DEVELOPMENT_STEP_ID,
                issue_id="0009",
                pass_number=2,
                prompt_session_id="prompt-dev-2",
                outcome=StepOutcome.SUCCEEDED,
                result=RoleResult(status="PASS", summary="Corrections complete."),
                outputs={},
                started_at="2026-07-16T09:01:00Z",
                finished_at="2026-07-16T09:01:06Z",
                elapsed_seconds=6,
            ),
        )

        completed = project_workflow_progress(
            workflow,
            catalog,
            (),
            attempts,
            issue_id="0009",
            active_elapsed_seconds=99,
        )
        rework = project_workflow_progress(
            workflow,
            catalog,
            (
                StepRuntimeState(
                    step_instance_id=DEVELOPMENT_STEP_ID,
                    issue_id="0009",
                    status=StepRuntimeStatus.RUNNING,
                    pass_number=3,
                    attempt_id="attempt-dev-3",
                ),
            ),
            attempts,
            issue_id="0009",
            active_elapsed_seconds=12,
        )

        completed_step = completed.by_step_instance_id[str(DEVELOPMENT_STEP_ID)]
        rework_step = rework.by_step_instance_id[str(DEVELOPMENT_STEP_ID)]
        self.assertEqual(completed_step.elapsed_seconds, 10)
        self.assertEqual(completed_step.latest_result, "Corrections complete.")
        self.assertEqual(rework_step.elapsed_seconds, 22)
        self.assertEqual(rework_step.pass_number, 3)
        self.assertEqual(rework_step.latest_result, "Corrections complete.")

    def test_renderer_shows_scopes_and_complete_active_activity(self) -> None:
        workflow = default_portable_workflow()
        catalog = default_portable_component_catalog()
        projection = project_workflow_progress(
            workflow,
            catalog,
            (
                StepRuntimeState(
                    step_instance_id=DEVELOPMENT_STEP_ID,
                    issue_id="0009",
                    status=StepRuntimeStatus.RUNNING,
                    pass_number=2,
                ),
            ),
            (),
            issue_id="0009",
            active_elapsed_seconds=12,
            event_freshness_seconds=3,
            activity="Running focused progress tests.",
        )

        rendered = render_workflow_progress(
            projection,
            width=100,
            color=False,
            unicode=False,
            frame="/",
        )

        self.assertIn("WORKFLOW", rendered)
        self.assertIn("CURRENT ISSUE - 0009", rendered)
        self.assertIn("Security Review", rendered)
        self.assertIn("Final Review", rendered)
        self.assertIn(
            "ACTIVE Development - model gpt-5.6-luna - effort high - Fast OFF",
            rendered,
        )
        self.assertIn(
            "WORKING / - pass 2 - elapsed 00:00:12 - event 00:00:03 ago",
            rendered,
        )
        self.assertIn("AI > Running focused progress tests.", rendered)

    def test_long_workflow_window_keeps_active_step_visible(self) -> None:
        workflow = default_portable_workflow()
        catalog = default_portable_component_catalog()
        projection = project_workflow_progress(
            workflow,
            catalog,
            (),
            (),
            issue_id="0009",
        )
        prototype = projection.issue_steps[0]
        issue_steps = tuple(
            replace(
                prototype,
                step_instance_id=f"step-{index}",
                display_name=f"Review {index}",
            )
            for index in range(1, 13)
        )
        projection = replace(
            projection,
            issue_steps=issue_steps,
            active_step_instance_id="step-11",
        )

        rendered = render_workflow_progress(
            projection,
            width=79,
            color=False,
            unicode=True,
            frame="⠋",
            max_step_rows=4,
        )

        self.assertIn("Review 11", rendered)
        self.assertIn("steps hidden", rendered)
        self.assertLessEqual(
            sum(
                "Review " in line and "pass" in line
                for line in rendered.splitlines()
            ),
            3,
        )

    def test_shared_renderer_preserves_status_labels_with_color_or_plain_text(self) -> None:
        workflow = default_portable_workflow()
        projection = project_workflow_progress(
            workflow,
            default_portable_component_catalog(),
            (),
            (),
            issue_id="0009",
        )
        statuses = (
            DashboardStatus.PASS,
            DashboardStatus.FAIL,
            DashboardStatus.WORKING,
            DashboardStatus.BLOCKED,
        )
        projection = replace(
            projection,
            issue_steps=tuple(
                replace(step, status=status)
                for step, status in zip(projection.issue_steps, statuses)
            ),
            active_step_instance_id=projection.issue_steps[2].step_instance_id,
        )

        colored = render_workflow_progress(
            projection,
            width=100,
            color=True,
            unicode=True,
            frame="⠋",
        )
        plain = render_workflow_progress(
            projection,
            width=39,
            color=False,
            unicode=False,
            frame="|",
        )

        self.assertIn("\x1b[1;32mPASS", colored)
        self.assertIn("\x1b[1;31mFAIL", colored)
        self.assertIn("\x1b[1;31mBLOCKED", colored)
        self.assertIn("\x1b[1;33mWORKING", colored)
        self.assertNotIn("\x1b[", plain)
        self.assertTrue(all(display_width(line) <= 39 for line in plain.splitlines()))
        for status in statuses:
            self.assertIn(status.value, plain)


class WorkflowProgressWrapperTests(unittest.TestCase):
    def test_bash_and_powershell_delegate_progress_to_the_same_python_modules(self) -> None:
        root = Path(__file__).resolve().parents[1]
        wrappers = {
            "bash-runner": (root / "bin" / "devloop.sh").read_text(encoding="utf-8"),
            "powershell-runner": (root / "bin" / "devloop.ps1").read_text(
                encoding="utf-8"
            ),
            "bash-planner": (root / "bin" / "devloop-plan.sh").read_text(
                encoding="utf-8"
            ),
            "powershell-planner": (root / "bin" / "devloop-plan.ps1").read_text(
                encoding="utf-8"
            ),
        }

        self.assertIn("-m devloop", wrappers["bash-runner"])
        self.assertIn("-m devloop", wrappers["powershell-runner"])
        self.assertIn("devloop.interactive_runner", wrappers["bash-planner"])
        self.assertIn("devloop.interactive_runner", wrappers["powershell-planner"])
        self.assertTrue(all("statusui" not in text for text in wrappers.values()))


if __name__ == "__main__":
    unittest.main()
