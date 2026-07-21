from __future__ import annotations

import unittest

from devloop.cli_ui import (
    editor_prompt,
    fit_text_to_screen,
    format_selected_step_line,
    render_context_path,
    render_grouped_commands,
    terminal_dimensions,
)


class CliUiTests(unittest.TestCase):
    def test_render_context_path_joins_segments(self) -> None:
        self.assertEqual(
            render_context_path("Workflow Editor", "Future Runs", "Development"),
            "Workflow Editor > Future Runs > Development",
        )

    def test_format_selected_step_line_marks_the_current_step(self) -> None:
        self.assertEqual(
            format_selected_step_line(2, "Development", selected=True),
            "> 2. Development  (selected)",
        )
        self.assertEqual(
            format_selected_step_line(1, "Analysis", selected=False),
            "  1. Analysis",
        )

    def test_render_grouped_commands_wraps_within_terminal_width(self) -> None:
        lines = render_grouped_commands(
            (("Finish", ("apply", "cancel")),),
            width=20,
        )
        self.assertEqual(lines[0], "Available commands")
        self.assertLessEqual(max(map(len, lines)), 20)

    def test_editor_prompt_includes_selected_step_name(self) -> None:
        self.assertEqual(
            editor_prompt("Security Review"),
            "workflow [Security Review]> ",
        )


    def test_fit_text_to_screen_reserves_one_line_for_prompt(self) -> None:
        fitted = fit_text_to_screen(
            "\n".join(f"line {index}" for index in range(10)),
            width=20,
            max_height=5,
            reserve_prompt=True,
        )
        self.assertLessEqual(len(fitted.splitlines()), 4)

    def test_terminal_dimensions_returns_sensible_minimums(self) -> None:
        width, height = terminal_dimensions(fallback=(80, 24))
        self.assertGreaterEqual(width, 40)
        self.assertGreaterEqual(height, 10)


if __name__ == "__main__":
    unittest.main()
