from __future__ import annotations

import io
import os
import re
import unittest
from unittest import mock

from devloop import statusui
from devloop.statusui import Stage
from devloop.terminal_editor import display_width


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
        self.assertIn("->", banner)

    def test_color_used_only_on_tty_without_no_color(self) -> None:
        env = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
        with mock.patch.dict(os.environ, env, clear=True):
            colored = statusui.render_banner(Stage.ANALYSIS, stream=FakeStream(tty=True))
            plain = statusui.render_banner(Stage.ANALYSIS, stream=FakeStream(tty=False))
        self.assertIn("\x1b[", colored)
        self.assertNotIn("\x1b[", plain)


class IssueDashboardRenderingTests(unittest.TestCase):
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

    def test_next_issue_reuses_card_region_and_shows_only_last_result(self) -> None:
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
        self.assertIn("\x1b[10A", transition)
        self.assertIn(
            "LAST RESULT · 0002 · PASS · pass 1 · total 00:00:05",
            plain,
        )
        self.assertIn("CURRENT ISSUE · 0003 · 3/26 · 23 remaining", plain)


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


if __name__ == "__main__":
    unittest.main()
