from __future__ import annotations

import io
import os
import re
import unittest
from dataclasses import replace
from unittest import mock

from devloop import statusui
from devloop.portable_workflow import (
    ANALYSIS_STEP_ID,
    DEVELOPMENT_STEP_ID,
    StepRuntimeState,
    StepRuntimeStatus,
    default_portable_component_catalog,
    default_portable_workflow,
)
from devloop.statusui import Stage
from devloop.terminal_editor import display_width
from devloop.terminal_text import sanitize_terminal_text
from tests.terminal_safety import (
    HOSTILE_TERMINAL_TEXT,
    assert_terminal_text_is_safe,
)


class FakeStream(io.StringIO):
    def __init__(self, *, encoding: str = "utf-8", tty: bool = True) -> None:
        super().__init__()
        self._encoding = encoding
        self._tty = tty

    @property
    def encoding(self) -> str:
        return self._encoding

    def isatty(self) -> bool:
        return self._tty


class StageTests(unittest.TestCase):
    def test_pipeline_order(self) -> None:
        self.assertEqual(
            [stage.value for stage in statusui.PIPELINE],
            ["analysis", "development", "review", "qa"],
        )


class TerminalUnicodeSafetyTests(unittest.TestCase):
    def test_sanitizer_preserves_rtl_joining_and_joined_emoji(self) -> None:
        multilingual_text = "فارسی: می\u200cروم · emoji: 👩\u200d💻"

        sanitized = sanitize_terminal_text(multilingual_text)

        self.assertEqual(sanitized, multilingual_text)

    def test_sanitizer_discards_unterminated_control_strings(self) -> None:
        unterminated_osc = "prefix " + ("\x1b]unterminated " * 2_048)
        unterminated_dcs = "prefix " + ("\x1bPunterminated " * 2_048)

        self.assertEqual(
            sanitize_terminal_text("Planning response."),
            "Planning response.",
        )
        self.assertEqual(sanitize_terminal_text(unterminated_osc), "prefix ")
        self.assertEqual(sanitize_terminal_text(unterminated_dcs), "prefix ")

    def test_progress_surface_preserves_rtl_joining_and_joined_emoji(self) -> None:
        multilingual_text = "بررسی می\u200cروم 👩\u200d💻"
        progress = statusui.project_workflow_progress(
            default_portable_workflow(),
            default_portable_component_catalog(),
            (
                StepRuntimeState(
                    step_instance_id=DEVELOPMENT_STEP_ID,
                    issue_id="0009",
                    status=StepRuntimeStatus.RUNNING,
                    pass_number=1,
                ),
            ),
            (),
            issue_id="0009",
        )
        progress = replace(
            progress,
            issue_steps=(
                replace(progress.issue_steps[0], display_name=multilingual_text),
                *progress.issue_steps[1:],
            ),
            issue_title=multilingual_text,
            activity=replace(progress.activity, safe_text=multilingual_text),
        )

        rendered = statusui.render_workflow_progress(
            progress,
            width=120,
            color=False,
            unicode=True,
            frame="⠋",
        )

        self.assertGreaterEqual(rendered.count(multilingual_text), 3)


class RenderBannerTests(unittest.TestCase):
    def test_banner_marks_current_stage(self) -> None:
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            banner = statusui.render_banner(Stage.ANALYSIS, stream=FakeStream())
        self.assertIn("analysis ●", banner)
        self.assertIn("development ○", banner)
        self.assertIn("review ○", banner)
        self.assertIn("qa ○", banner)
        self.assertNotIn("\x1b[", banner)

    def test_banner_includes_context(self) -> None:
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            banner = statusui.render_banner(
                Stage.REVIEW, context="issue 2/5 · pass 1", stream=FakeStream()
            )
        self.assertIn("issue 2/5", banner)
        self.assertIn("review ●", banner)

    def test_ascii_fallback_when_stream_cannot_encode(self) -> None:
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            banner = statusui.render_banner(
                Stage.QA, stream=FakeStream(encoding="ascii")
            )
        self.assertNotIn("●", banner)
        self.assertNotIn("→", banner)
        self.assertIn("qa *", banner)
        self.assertIn(" > ", banner)

    def test_color_used_only_on_tty_without_no_color(self) -> None:
        env = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
        with mock.patch.dict(os.environ, env, clear=True):
            colored = statusui.render_banner(Stage.ANALYSIS, stream=FakeStream(tty=True))
            plain = statusui.render_banner(Stage.ANALYSIS, stream=FakeStream(tty=False))
        self.assertIn("\x1b[", colored)
        self.assertNotIn("\x1b[", plain)


class IssueDashboardRenderingTests(unittest.TestCase):
    def test_shared_progress_honors_no_color_on_a_tty(self) -> None:
        workflow = default_portable_workflow()
        projection = statusui.project_workflow_progress(
            workflow,
            default_portable_component_catalog(),
            (
                StepRuntimeState(
                    step_instance_id=ANALYSIS_STEP_ID,
                    issue_id=None,
                    status=StepRuntimeStatus.RUNNING,
                    pass_number=1,
                ),
            ),
            (),
            issue_id=None,
        )

        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            rendered = statusui.render_workflow_progress_for_stream(
                projection,
                stream=FakeStream(tty=True),
            )

        self.assertIn("WORKING", rendered)
        self.assertNotIn("\x1b[", rendered)

    def test_dashboard_activity_and_role_summary_are_terminal_safe(self) -> None:
        progress = statusui.project_workflow_progress(
            default_portable_workflow(),
            default_portable_component_catalog(),
            (
                StepRuntimeState(
                    step_instance_id=DEVELOPMENT_STEP_ID,
                    issue_id="0009",
                    status=StepRuntimeStatus.RUNNING,
                    pass_number=1,
                ),
            ),
            (),
            issue_id="0009",
        )

        for tty in (True, False):
            with self.subTest(tty=tty):
                output = FakeStream(tty=tty)
                dashboard = statusui.IssueDashboard(
                    issue_number="0009",
                    issue_title="Dynamic workflow progress",
                    position=9,
                    total=10,
                    stream=output,
                    frame_seconds=60,
                )
                dashboard.show_workflow_progress(progress)
                dashboard.notify_activity(HOSTILE_TERMINAL_TEXT)
                dashboard.finish_role(
                    Stage.DEVELOPMENT,
                    "PASS",
                    HOSTILE_TERMINAL_TEXT,
                )
                dashboard.close()

                assert_terminal_text_is_safe(
                    self,
                    output.getvalue(),
                    redirected=not tty,
                )

    def test_implementation_surface_sanitizes_every_dynamic_progress_field(self) -> None:
        progress = statusui.project_workflow_progress(
            default_portable_workflow(),
            default_portable_component_catalog(),
            (
                StepRuntimeState(
                    step_instance_id=DEVELOPMENT_STEP_ID,
                    issue_id="0009",
                    status=StepRuntimeStatus.RUNNING,
                    pass_number=1,
                ),
            ),
            (),
            issue_id="0009",
        )
        active = replace(
            progress.active_step,
            step_instance_id=HOSTILE_TERMINAL_TEXT,
            display_name=f"{HOSTILE_TERMINAL_TEXT} {'x' * 500}",
            component_id=HOSTILE_TERMINAL_TEXT,
            issue_id=HOSTILE_TERMINAL_TEXT,
            model=HOSTILE_TERMINAL_TEXT,
            reasoning_effort=HOSTILE_TERMINAL_TEXT,
            fast=HOSTILE_TERMINAL_TEXT,
            latest_result=HOSTILE_TERMINAL_TEXT,
            attempt_id=HOSTILE_TERMINAL_TEXT,
        )
        progress = replace(
            progress,
            workflow_steps=(
                replace(
                    progress.workflow_steps[0],
                    display_name=HOSTILE_TERMINAL_TEXT,
                ),
            ),
            issue_steps=(active, *progress.issue_steps[1:]),
            active_step_instance_id=HOSTILE_TERMINAL_TEXT,
            activity=replace(progress.activity, safe_text=HOSTILE_TERMINAL_TEXT),
            issue_title=HOSTILE_TERMINAL_TEXT,
            issue_history=(
                statusui.IssueResultSummary(
                    issue_number=HOSTILE_TERMINAL_TEXT,
                    status=statusui.DashboardStatus.PASS,
                    pass_number=1,
                    elapsed_seconds=1,
                ),
            ),
        )

        for tty in (True, False):
            with self.subTest(tty=tty):
                output = FakeStream(tty=tty)
                dashboard = statusui.IssueDashboard(
                    issue_number=HOSTILE_TERMINAL_TEXT,
                    issue_title=HOSTILE_TERMINAL_TEXT,
                    position=9,
                    total=10,
                    issue_history=(
                        statusui.IssueResultSummary(
                            issue_number=HOSTILE_TERMINAL_TEXT,
                            status=statusui.DashboardStatus.PASS,
                            pass_number=1,
                            elapsed_seconds=0.0,
                        ),
                    ),
                    stream=output,
                    frame_seconds=60,
                    terminal_size=lambda **_: os.terminal_size((1200, 40)),
                )
                dashboard.show_workflow_progress(progress)
                dashboard.notify_activity(HOSTILE_TERMINAL_TEXT)
                dashboard.close()

                rendered = output.getvalue()
                assert_terminal_text_is_safe(
                    self,
                    rendered,
                    redirected=not tty,
                )
                self.assertNotIn("x" * 500, rendered)
                self.assertIn(
                    "model Καλημέρα 世界 ESC-CSI C1-CSI BIDI",
                    rendered,
                )
                self.assertIn(
                    "effort Καλημέρα 世界 ESC-CSI C1-CSI BIDI",
                    rendered,
                )
                self.assertIn(
                    "Fast Καλημέρα 世界 E...",
                    rendered,
                )

    def test_narrow_dashboard_has_only_horizontal_rules_and_semantic_colors(self) -> None:
        snapshot = statusui.IssueDashboardSnapshot(
            issue_number="0002",
            issue_title=(
                "Publish a validated sample Level Catalog with a deliberately "
                "long title 中文"
            ),
            position=2,
            total=26,
            pass_number=1,
            active_stage=Stage.REVIEW,
            statuses={
                Stage.DEVELOPMENT: statusui.DashboardStatus.PASS,
                Stage.REVIEW: statusui.DashboardStatus.WORKING,
                Stage.QA: statusui.DashboardStatus.FAIL,
            },
            stage_durations={
                Stage.DEVELOPMENT: 125,
                Stage.QA: 7,
            },
            elapsed_seconds=42,
            inactivity_seconds=2,
            activity=(
                "Checking catalog validation and malformed input cases with "
                "a deliberately long activity message."
            ),
        )

        rendered = statusui.render_issue_dashboard(
            snapshot,
            width=39,
            color=True,
            unicode=True,
            frame="⠋",
        )
        plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", rendered)

        self.assertTrue(
            all(display_width(line) <= 39 for line in plain.splitlines())
        )
        self.assertFalse(set("│╭╮╰╯┌┐└┘").intersection(plain))
        self.assertIn("─", plain)
        self.assertIn("\x1b[1;32mPASS", rendered)
        self.assertIn("\x1b[1;31mFAIL", rendered)
        self.assertIn("\x1b[1;33mWORKING", rendered)

    def test_completed_step_duration_stays_visible_while_next_step_counts(self) -> None:
        snapshot = statusui.IssueDashboardSnapshot(
            issue_number="0002",
            issue_title="Publish a validated sample Level Catalog",
            position=2,
            total=26,
            pass_number=1,
            active_stage=Stage.REVIEW,
            statuses={
                Stage.DEVELOPMENT: statusui.DashboardStatus.PASS,
                Stage.REVIEW: statusui.DashboardStatus.WORKING,
                Stage.QA: statusui.DashboardStatus.WAITING,
            },
            stage_durations={Stage.DEVELOPMENT: 125},
            elapsed_seconds=42,
        )

        rendered = statusui.render_issue_dashboard(
            snapshot,
            width=79,
            color=False,
            unicode=True,
            frame="⠋",
        )

        self.assertIn("PASS      DEVELOPMENT · pass 1 · 00:02:05", rendered)
        self.assertIn("WORKING   REVIEW      · pass 1 · 00:00:42", rendered)
        self.assertIn("WAITING   QA          · pass 1 · 00:00:00", rendered)

    def test_dashboard_shows_dependency_scheduler_summary(self) -> None:
        snapshot = statusui.IssueDashboardSnapshot(
            issue_number="0003",
            issue_title="Independent work",
            position=3,
            total=8,
            pass_number=1,
            active_stage=Stage.DEVELOPMENT,
            scheduler_summary=(
                "SCHEDULER · NORMAL SCHEDULING · 2 ready · 5 waiting"
            ),
        )

        rendered = statusui.render_issue_dashboard(
            snapshot,
            width=79,
            color=False,
            unicode=True,
            frame="⠋",
        )

        self.assertIn(
            "SCHEDULER · NORMAL SCHEDULING · 2 ready · 5 waiting",
            rendered,
        )

    def test_dashboard_freezes_each_step_clock_on_transition(self) -> None:
        class Clock:
            value = 0.0

            def __call__(self) -> float:
                return self.value

        clock = Clock()
        output = FakeStream()
        dashboard = statusui.IssueDashboard(
            issue_number="0002",
            issue_title="Publish a validated sample Level Catalog",
            position=2,
            total=26,
            stream=output,
            clock=clock,
            frame_seconds=60,
            terminal_size=lambda **_: os.terminal_size((100, 24)),
        )

        dashboard.begin_role(Stage.DEVELOPMENT, 1)
        clock.value = 12
        dashboard.finish_role(Stage.DEVELOPMENT, "PASS")
        dashboard.begin_role(Stage.REVIEW, 1)
        clock.value = 20
        dashboard.finish_role(Stage.REVIEW, "PASS")
        dashboard.begin_role(Stage.QA, 1)
        clock.value = 23
        dashboard.notify_activity("Running acceptance checks.")
        clock.value = 25
        dashboard.finish_role(Stage.QA, "FAIL")
        dashboard.begin_role(Stage.DEVELOPMENT, 2)
        clock.value = 30
        dashboard.notify_activity("Applying review and QA fixes.")
        dashboard.close()

        rendered = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output.getvalue())
        self.assertIn("PASS      DEVELOPMENT · pass 1 · 00:00:12", rendered)
        self.assertIn("PASS      REVIEW      · pass 1 · 00:00:08", rendered)
        self.assertIn("WORKING   QA          · pass 1 · 00:00:03", rendered)
        self.assertIn("WORKING   DEVELOPMENT · pass 2 · 00:00:17", rendered)
        self.assertIn("WAITING   REVIEW      · pass 2 · 00:00:08", rendered)
        self.assertIn("WAITING   QA          · pass 2 · 00:00:05", rendered)

    def test_next_issue_reuses_card_region_and_shows_run_summary(self) -> None:
        class Clock:
            value = 0.0

            def __call__(self) -> float:
                return self.value

        clock = Clock()
        output = FakeStream()
        dashboard = statusui.IssueDashboard(
            issue_number="0002",
            issue_title="Publish a validated sample Level Catalog",
            position=2,
            total=26,
            issue_history=(
                statusui.IssueResultSummary(
                    issue_number="0001",
                    status=statusui.DashboardStatus.PASS,
                    pass_number=1,
                    elapsed_seconds=0.0,
                ),
            ),
            stream=output,
            clock=clock,
            frame_seconds=60,
            terminal_size=lambda **_: os.terminal_size((100, 24)),
        )
        dashboard.begin_role(Stage.DEVELOPMENT, 1)
        clock.value = 5
        dashboard.finish_role(Stage.DEVELOPMENT, "PASS")
        dashboard.finish_issue("PASS", "Issue completed.")
        transition_start = len(output.getvalue())

        dashboard.show_issue(
            issue_number="0003",
            issue_title="Add durable local save data",
            position=3,
            total=26,
        )
        dashboard.begin_role(Stage.DEVELOPMENT, 1)
        dashboard.close()

        transition = output.getvalue()[transition_start:]
        plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", transition)
        self.assertIn("\x1b[11A", transition)
        self.assertIn("RUN · 0001 · 0002", plain)
        self.assertNotIn("LAST RESULT", plain)
        self.assertNotIn("FINISHED ISSUES", plain)
        self.assertIn("CURRENT ISSUE · 0003 · 3/26 · 23 remaining", plain)

    def test_failed_retry_does_not_add_issue_to_run_summary_until_pass(self) -> None:
        output = FakeStream()
        dashboard = statusui.IssueDashboard(
            issue_number="0002",
            issue_title="Publish catalog",
            position=2,
            total=3,
            stream=output,
            frame_seconds=60,
        )

        dashboard.finish_issue("FAIL", "Needs rework.")
        dashboard.show_issue(
            issue_number="0002",
            issue_title="Publish catalog",
            position=2,
            total=3,
        )
        dashboard.finish_issue("PASS", "Completed.")
        transition_start = len(output.getvalue())
        dashboard.show_issue(
            issue_number="0003",
            issue_title="Persist profile",
            position=3,
            total=3,
        )
        dashboard.begin_role(Stage.DEVELOPMENT, 1)
        dashboard.close()

        transition = re.sub(
            r"\x1b\[[0-?]*[ -/]*[@-~]",
            "",
            output.getvalue()[transition_start:],
        )
        self.assertIn("RUN · 0002", transition)
        self.assertNotIn("RUN · 0002 · 0002", transition)
        self.assertNotIn("FINISHED ISSUES", transition)

    def test_small_terminal_windows_rows_around_the_active_step(self) -> None:
        workflow = default_portable_workflow()
        projection = statusui.project_workflow_progress(
            workflow,
            default_portable_component_catalog(),
            (),
            (),
            issue_id="0009",
        )
        prototype = projection.issue_steps[0]
        projection = replace(
            projection,
            issue_steps=tuple(
                replace(
                    prototype,
                    step_instance_id=f"step-{index}",
                    display_name=f"Review {index}",
                )
                for index in range(1, 13)
            ),
            active_step_instance_id="step-11",
        )
        output = FakeStream()
        dashboard = statusui.IssueDashboard(
            issue_number="0009",
            issue_title="Dynamic workflow progress",
            position=9,
            total=10,
            stream=output,
            frame_seconds=60,
            terminal_size=lambda **_: os.terminal_size((80, 12)),
        )

        dashboard.show_workflow_progress(projection)
        dashboard.close()

        plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output.getvalue())
        self.assertIn("Review 11", plain)
        self.assertIn("steps hidden", plain)

    def test_same_step_rework_starts_a_new_live_timer_without_double_counting(self) -> None:
        class Clock:
            value = 0.0

            def __call__(self) -> float:
                return self.value

        progress = statusui.project_workflow_progress(
            default_portable_workflow(),
            default_portable_component_catalog(),
            (
                StepRuntimeState(
                    step_instance_id=DEVELOPMENT_STEP_ID,
                    issue_id="0009",
                    status=StepRuntimeStatus.RUNNING,
                    pass_number=1,
                    attempt_id="attempt-1",
                ),
            ),
            (),
            issue_id="0009",
        )
        clock = Clock()
        output = FakeStream()
        dashboard = statusui.IssueDashboard(
            issue_number="0009",
            issue_title="Dynamic workflow progress",
            position=9,
            total=10,
            stream=output,
            clock=clock,
            frame_seconds=60,
        )
        dashboard.show_workflow_progress(progress)
        dashboard.begin_role(Stage.DEVELOPMENT, 1)
        clock.value = 10
        dashboard.finish_role(Stage.DEVELOPMENT, "PASS")
        rework = replace(
            progress,
            issue_steps=tuple(
                replace(
                    step,
                    status=(
                        statusui.DashboardStatus.WORKING
                        if step.step_instance_id == progress.active_step_instance_id
                        else step.status
                    ),
                    pass_number=(
                        2
                        if step.step_instance_id == progress.active_step_instance_id
                        else step.pass_number
                    ),
                    elapsed_seconds=(
                        10
                        if step.step_instance_id == progress.active_step_instance_id
                        else step.elapsed_seconds
                    ),
                    attempt_id=(
                        "attempt-2"
                        if step.step_instance_id == progress.active_step_instance_id
                        else step.attempt_id
                    ),
                )
                for step in progress.issue_steps
            ),
        )
        transition_start = len(output.getvalue())

        dashboard.show_workflow_progress(rework)
        dashboard.close()

        transition = re.sub(
            r"\x1b\[[0-?]*[ -/]*[@-~]",
            "",
            output.getvalue()[transition_start:],
        )
        self.assertIn("WORKING   Development · pass 2 · 00:00:10", transition)

    def test_tty_redraw_clears_rows_when_active_details_disappear(self) -> None:
        progress = statusui.project_workflow_progress(
            default_portable_workflow(),
            default_portable_component_catalog(),
            (
                StepRuntimeState(
                    step_instance_id=DEVELOPMENT_STEP_ID,
                    issue_id="0009",
                    status=StepRuntimeStatus.RUNNING,
                    pass_number=1,
                ),
            ),
            (),
            issue_id="0009",
        )
        output = FakeStream()
        dashboard = statusui.IssueDashboard(
            issue_number="0009",
            issue_title="Dynamic workflow progress",
            position=9,
            total=10,
            stream=output,
            frame_seconds=60,
        )
        dashboard.show_workflow_progress(progress)
        completed = replace(
            progress,
            issue_steps=tuple(
                replace(step, status=statusui.DashboardStatus.PASS)
                if step.step_instance_id == progress.active_step_instance_id
                else step
                for step in progress.issue_steps
            ),
            active_step_instance_id=None,
        )
        transition_start = len(output.getvalue())

        dashboard.show_workflow_progress(completed)

        transition = output.getvalue()[transition_start:]
        self.assertEqual(transition.count("\x1b[2K"), 12)
        self.assertTrue(transition.endswith("\x1b[4A\r"))


class StagePromptTests(unittest.TestCase):
    def test_prompt_names_stage(self) -> None:
        self.assertEqual(statusui.stage_prompt(Stage.ANALYSIS), "[analysis] > ")
        self.assertEqual(statusui.stage_prompt(Stage.QA), "[qa] > ")


class WaitingIndicatorTests(unittest.TestCase):
    def test_shows_phase_issue_progress_and_activity(self) -> None:
        class Clock:
            value = 0.0

            def __call__(self) -> float:
                return self.value

        clock = Clock()
        indicator = statusui.WaitingIndicator(
            clock=clock,
            stage=Stage.REVIEW,
            context="0001 1/26 +25 p1",
        )

        clock.value = 12.0
        indicator.notify_activity()
        clock.value = 15.0
        line = indicator._status_line("/")

        self.assertIn("[review]", line)
        self.assertIn("0001 1/26 +25 p1", line)
        self.assertIn("WORKING [/] 00:00:15", line)
        self.assertIn("evt 00:00:03 ago", line)
        self.assertLessEqual(len(line), 79)

        clock.value = 200.0
        stalled = indicator._status_line("-")
        self.assertIn("STALL?", stalled)
        self.assertIn("Ctrl+C", stalled)
        self.assertLessEqual(len(stalled), 79)

    def test_workflow_indicator_advances_shared_active_timers_and_activity(self) -> None:
        class Clock:
            value = 0.0

            def __call__(self) -> float:
                return self.value

        workflow = default_portable_workflow()
        progress = statusui.project_workflow_progress(
            workflow,
            default_portable_component_catalog(),
            (
                StepRuntimeState(
                    step_instance_id=ANALYSIS_STEP_ID,
                    issue_id=None,
                    status=StepRuntimeStatus.RUNNING,
                    pass_number=1,
                ),
            ),
            (),
            issue_id=None,
        )
        clock = Clock()
        indicator = statusui.WaitingIndicator(
            FakeStream(),
            clock=clock,
            workflow_progress=progress,
        )

        clock.value = 15
        indicator.notify_activity("Inspecting repository guidance.")
        clock.value = 20
        rendered = indicator._progress_panel("/")

        self.assertIn("ACTIVE Analysis", rendered)
        self.assertIn("model gpt-5.6-sol", rendered)
        self.assertIn("elapsed 00:00:20", rendered)
        self.assertIn("event 00:00:05 ago", rendered)
        self.assertIn("AI › Inspecting repository guidance.", rendered)


if __name__ == "__main__":
    unittest.main()
