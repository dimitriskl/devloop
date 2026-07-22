from __future__ import annotations

import threading
import unittest
from pathlib import Path

from textual.containers import Horizontal
from textual.widgets import OptionList, Static

from devloop.portable_runtime import PortableRuntimeBridge
from devloop.issue_pack import Issue
from devloop.cli import choose_run_review_action
from devloop.run_review import RunReviewAction, build_run_review
from devloop.portable_ui.app import (
    PortableApplicationShell,
    PortableLogOverlay,
    PortableTextOverlay,
)


class PortableApplicationShellTests(unittest.IsolatedAsyncioTestCase):
    async def test_shell_keeps_one_frame_and_refreshes_selection_preview(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            selected = bridge.choose(
                (("start", "Start a new change"), ("resume", "Resume unfinished PRD")),
                default_key="start",
                cancel_key=None,
                render=lambda key: bridge.show_screen(f"preview:{key}"),
            )
            return 0 if selected == "resume" else 1

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            menu = app.query_one("#portable-navigation", OptionList)
            for _ in range(20):
                await pilot.pause()
                if menu.option_count == 2:
                    break

            self.assertEqual(menu.option_count, 2)
            self.assertEqual(len(app.query("#portable-shell")), 1)
            self.assertEqual(app.query_one("#portable-status", Static).region.height, 1)
            self.assertGreater(app.query_one("#portable-actions", Static).region.height, 0)

            menu.highlighted = 1
            menu.focus()
            await pilot.pause()
            for _ in range(20):
                await pilot.pause()
                if "preview:resume" in str(
                    app.query_one("#portable-detail", Static).render()
                ):
                    break

            self.assertIn(
                "preview:resume",
                str(app.query_one("#portable-detail", Static).render()),
            )
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            self.assertEqual(app.operation_result, 0)
            self.assertIn(
                "Dev Loop > Final Result",
                str(app.query_one("#portable-detail", Static).render()),
            )

    async def test_committed_choice_replaces_stale_menu_and_escape_reports_progress(self) -> None:
        bridge = PortableRuntimeBridge()
        choice_received = threading.Event()
        release_preview = threading.Event()
        release_operation = threading.Event()

        def render_preview(key: str) -> None:
            release_preview.wait(timeout=2)
            bridge.show_screen(f"preview:{key}")

        def operation() -> int:
            bridge.choose(
                (("start", "Start development"), ("quit", "Quit")),
                default_key="start",
                cancel_key="quit",
                render=render_preview,
            )
            choice_received.set()
            release_operation.wait(timeout=2)
            return 0

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            menu = app.query_one("#portable-navigation", OptionList)
            for _ in range(20):
                await pilot.pause()
                if menu.option_count == 2:
                    break

            try:
                await pilot.press("enter")
                release_preview.set()
                for _ in range(20):
                    await pilot.pause()
                    if choice_received.is_set():
                        break

                self.assertTrue(choice_received.is_set())
                self.assertTrue(menu.disabled)
                self.assertEqual(menu.option_count, 0)
                self.assertIn(
                    "Dev Loop > Working",
                    str(app.query_one("#portable-detail", Static).render()),
                )

                await pilot.press("escape")
                self.assertIsInstance(app.screen, PortableTextOverlay)
                self.assertIn(
                    "already accepted",
                    str(
                        app.screen.query_one(
                            ".portable-overlay-content",
                            Static,
                        ).render()
                    ),
                )
            finally:
                release_preview.set()
                release_operation.set()

    async def test_escape_returns_the_current_cancel_action(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            selected = bridge.choose(
                (("start", "Start development"), ("quit", "Quit")),
                default_key="start",
                cancel_key="quit",
                render=lambda key: bridge.show_screen(f"preview:{key}"),
            )
            return 0 if selected == "quit" else 1

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            menu = app.query_one("#portable-navigation", OptionList)
            for _ in range(20):
                await pilot.pause()
                if menu.option_count == 2:
                    break

            await pilot.press("escape")
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            self.assertEqual(app.operation_result, 0)

    async def test_help_and_logs_open_inside_the_application(self) -> None:
        app = PortableApplicationShell(PortableRuntimeBridge(), lambda: 0)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("f1")
            self.assertEqual(len(app.screen_stack), 2)
            self.assertIsInstance(app.screen, PortableTextOverlay)
            await pilot.press("escape")
            self.assertEqual(len(app.screen_stack), 1)

            await pilot.press("f4")
            self.assertIsInstance(app.screen, PortableLogOverlay)

    async def test_small_terminal_shows_a_bounded_resize_view(self) -> None:
        app = PortableApplicationShell(PortableRuntimeBridge(), lambda: 0)
        async with app.run_test(size=(79, 23)) as pilot:
            await pilot.pause()
            warning = app.query_one("#portable-size-warning", Static)

            self.assertTrue(warning.display)
            self.assertTrue(app.query_one("#portable-body", Horizontal).disabled)
            self.assertIn("Required: 80x24", str(warning.render()))

    async def test_shell_layout_is_supported_at_minimum_and_wide_sizes(self) -> None:
        for size in ((80, 24), (160, 40)):
            with self.subTest(size=size):
                app = PortableApplicationShell(PortableRuntimeBridge(), lambda: 0)
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()

                    self.assertFalse(
                        app.query_one("#portable-size-warning", Static).display
                    )
                    self.assertEqual(len(app.query("#portable-shell")), 1)

    async def test_shell_reports_the_detail_pane_size_to_the_runtime(self) -> None:
        bridge = PortableRuntimeBridge()
        app = PortableApplicationShell(bridge, lambda: 0)

        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            detail_size = app.query_one("#portable-detail", Static).content_size

            self.assertEqual(
                bridge.content_size(fallback=(1, 1)),
                (detail_size.width, detail_size.height),
            )

    async def test_input_view_supports_history_and_alt_v(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            value = bridge.read_line("Describe the change", history=("older", "newer"))
            return 0 if value == "/paste" else 1

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            input_widget = app.query_one("#portable-input")
            for _ in range(20):
                await pilot.pause()
                if input_widget.display:
                    break

            await pilot.press("up")
            self.assertEqual(input_widget.value, "newer")
            await pilot.press("alt+v")
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            self.assertEqual(app.operation_result, 0)

    async def test_system_exit_code_is_preserved(self) -> None:
        def operation() -> int:
            raise SystemExit(7)

        app = PortableApplicationShell(PortableRuntimeBridge(), operation)
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            self.assertEqual(app.operation_result, 7)

    async def test_completion_review_remains_visible_after_operation_finishes(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            bridge.show_screen(
                "Dev Loop > Completion Review\n\n"
                "WORKFLOW FINISHED - ATTENTION REQUIRED\n"
                "Completed: 3/8    Remaining: 5"
            )
            return 2

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            detail = str(app.query_one("#portable-detail", Static).render())
            status = str(app.query_one("#portable-status", Static).render())

            self.assertIn("Dev Loop > Completion Review", detail)
            self.assertIn("WORKFLOW FINISHED", detail)
            self.assertNotIn("Last workflow view", detail)
            self.assertIn("WORKFLOW FINISHED", status)

    async def test_f4_starts_with_all_completion_review_failures(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            bridge.show_screen(
                "Dev Loop > Completion Review\n\n"
                "WORKFLOW FINISHED - ATTENTION REQUIRED\n"
                "Completed: 1/3    Remaining: 2\n\n"
                "Issue review\n"
                "COMPLETED  0001  Finished feature\n"
                "BLOCKED    0002  Broken feature - Review found a defect.\n"
                "WAITING    0003  Dependent feature - waiting on 0002"
            )
            return 2

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            await pilot.press("f4")

            self.assertIsInstance(app.screen, PortableLogOverlay)
            review_log = "\n".join(app.screen._lines)
            self.assertIn("COMPLETED  0001", review_log)
            self.assertIn("BLOCKED    0002", review_log)
            self.assertIn("Review found a defect.", review_log)
            self.assertIn("WAITING    0003", review_log)
            self.assertIn("waiting on 0002", review_log)

    async def test_completion_review_can_rerun_unfinished_issues(self) -> None:
        bridge = PortableRuntimeBridge()
        selected_actions: list[RunReviewAction] = []
        review = build_run_review(
            [Issue("0001", "Blocked", Path("0001.md"), False)],
            {"0001": {"status": "BLOCKED"}},
            loop_state_path=Path("README.loop.md"),
            rerun_available=True,
        )

        def operation() -> int:
            selected_actions.append(
                choose_run_review_action(review, interactive=True)
            )
            return 2

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            menu = app.query_one("#portable-navigation", OptionList)
            for _ in range(20):
                await pilot.pause()
                if menu.option_count == 2:
                    break

            self.assertEqual(menu.highlighted, 1)
            await pilot.press("up", "enter")
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            self.assertEqual(
                selected_actions,
                [RunReviewAction.RERUN_REMAINING],
            )
            self.assertIn(
                "Rerun only the 1 unfinished issue now",
                str(app.query_one("#portable-detail", Static).render()),
            )

    async def test_worker_output_is_sanitized_before_display(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            bridge.write_output("unsafe\x1b[2Joutput", is_error=False)
            return 0

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            self.assertNotIn("\x1b", "".join(app._captured_output))

    async def test_contextual_function_key_returns_a_typed_command(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            selected = bridge.choose(
                (("step", "Workflow step"), ("cancel", "Cancel")),
                default_key="step",
                cancel_key="cancel",
                render=lambda _key: None,
                shortcuts={"f3": "graph"},
            )
            return 0 if selected == "graph" else 1

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            menu = app.query_one("#portable-navigation", OptionList)
            for _ in range(20):
                await pilot.pause()
                if menu.option_count == 2:
                    break
            await pilot.press("f3")
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            self.assertEqual(app.operation_result, 0)


if __name__ == "__main__":
    unittest.main()
