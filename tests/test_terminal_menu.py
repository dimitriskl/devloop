from __future__ import annotations

import io
import sys
import unittest
from unittest import mock

from devloop.terminal_menu import clear_terminal_screen, render_menu_screen


class TerminalMenuTests(unittest.TestCase):
    def test_render_menu_screen_clears_before_printing(self) -> None:
        buffer = io.StringIO()
        with mock.patch.object(sys.stdout, "isatty", return_value=True), mock.patch.object(
            sys.stdout, "write", buffer.write
        ), mock.patch.object(sys.stdout, "flush"):
            render_menu_screen("Dev Loop planning", "", "  1. Start")

        output = buffer.getvalue()
        self.assertTrue(output.startswith("\033[2J\033[H"))
        self.assertIn("Dev Loop planning", output)

    def test_clear_terminal_screen_is_noop_when_not_a_tty(self) -> None:
        buffer = io.StringIO()
        with mock.patch.object(sys.stdout, "isatty", return_value=False), mock.patch.object(
            sys.stdout, "write", buffer.write
        ):
            clear_terminal_screen()

        self.assertEqual(buffer.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
