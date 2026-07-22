from __future__ import annotations

import unittest

from textual.containers import Horizontal
from textual.widgets import OptionList, Static

from devloop.portable_runtime import PortableRuntimeBridge
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
