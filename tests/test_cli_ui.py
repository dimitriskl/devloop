from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

from devloop.cli_ui import (
    APP_TITLE,
    editor_prompt,
    fit_text_to_screen,
    format_menu_entry,
    format_selected_step_line,
    render_action_bar,
    render_choice_menu,
    render_context_path,
    render_grouped_commands,
    render_screen_frame,
    render_split_panes,
    terminal_dimensions,
)


class CliUiTests(unittest.TestCase):
    def test_render_context_path_prefixes_app_title(self) -> None:
        self.assertEqual(
            render_context_path("Workflow Editor", "Future Runs", "Development"),
            "Dev Loop > Workflow Editor > Future Runs > Development",
        )
        self.assertEqual(render_context_path(), APP_TITLE)

    def test_format_menu_entry_marks_selection(self) -> None:
        self.assertEqual(format_menu_entry("1", "Start", selected=True), "> 1. Start")
        self.assertEqual(format_menu_entry("q", "Exit"), "  q. Exit")

    def test_format_selected_step_line_marks_the_current_step(self) -> None:
        self.assertEqual(
            format_selected_step_line(2, "Development", selected=True),
            "> 2. Development",
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
        self.assertEqual(lines[0], "Commands")
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
        self.assertGreaterEqual(width, 20)
        self.assertGreaterEqual(height, 10)

    def test_render_screen_frame_draws_bordered_layout(self) -> None:
        rendered = render_screen_frame(
            path="Dev Loop > Startup",
            body=("What would you like to do?", "", "> 1. Start"),
            command_groups=(("Navigate", ("1 start", "q exit")),),
            width=40,
            height=12,
            unicode_ok=False,
        )
        self.assertTrue(rendered.startswith("+"))
        self.assertIn("|> 1. Start", rendered)
        self.assertIn("Commands", rendered)

    def test_render_choice_menu_builds_startup_style_screen(self) -> None:
        rendered = render_choice_menu(
            path="Dev Loop > Startup",
            section_title="What would you like to do?",
            choices=(("1", "Start a new change"),),
            footer=(("q", "Exit"),),
            command_groups=(("Navigate", ("1 start", "q exit")),),
            width=50,
            height=14,
        )
        self.assertIn("Start a new change", rendered)
        self.assertIn("Exit", rendered)

    def test_render_choice_menu_scrolls_to_keep_selection_visible(self) -> None:
        rendered = render_choice_menu(
            path="Dev Loop > Resume",
            section_title="Unfinished PRDs",
            choices=tuple((str(index), f"Change {index}") for index in range(1, 21)),
            selected_key="20",
            action_bar=(("Up/Down", "Choose"), ("Enter", "Open")),
            width=50,
            height=12,
        )

        self.assertIn("> 20. Change 20", rendered)
        self.assertNotIn("  1. Change 1", rendered)
        self.assertIn("of 20", rendered)

    def test_render_choice_menu_keeps_choices_visible_below_long_description(self) -> None:
        rendered = render_choice_menu(
            path="Dev Loop > Workflow Editor > Delete",
            section_title="Choose an option",
            description=tuple(f"Impact detail {index}" for index in range(20)),
            choices=(("yes", "Delete this step"),),
            footer=(("no", "Keep this step"),),
            selected_key="no",
            action_bar=(("Up/Down", "Choose"), ("Enter", "Select"), ("Esc", "Back")),
            width=60,
            height=12,
        )

        self.assertIn("Delete this step", rendered)
        self.assertIn("> no. Keep this step", rendered)

    def test_render_action_bar_wraps_complete_shortcut_labels(self) -> None:
        lines = render_action_bar(
            (("F1", "Help"), ("F2", "Apply"), ("Esc", "Cancel")),
            width=24,
        )

        self.assertGreaterEqual(len(lines), 2)
        self.assertLessEqual(max(map(len, lines)), 24)

    def test_render_split_panes_keeps_both_window_titles(self) -> None:
        lines = render_split_panes(
            left_title="Workflow Steps",
            left_lines=("> 2. Development",),
            right_title="Settings — Development",
            right_lines=("Model: gpt-5.6-luna",),
            width=78,
            height=6,
            unicode_ok=False,
        )

        self.assertEqual(len(lines), 6)
        self.assertIn("Workflow Steps", lines[0])
        self.assertIn("Settings", lines[0])

    def test_render_screen_frame_applies_color_only_when_requested(self) -> None:
        rendered = render_screen_frame(
            path="Dev Loop > Startup",
            body=("> 1. Start",),
            action_bar=(("Enter", "Open"),),
            width=40,
            height=10,
            unicode_ok=False,
            color_ok=True,
        )

        self.assertIn("\x1b[", rendered)
        self.assertIn("> 1. Start", rendered)

    def test_render_screen_frame_honors_no_color(self) -> None:
        with mock.patch.object(sys.stdout, "isatty", return_value=True), mock.patch.dict(
            os.environ,
            {"NO_COLOR": "1"},
        ):
            rendered = render_screen_frame(
                path="Dev Loop > Startup",
                body=("> 1. Start",),
                action_bar=(("Enter", "Open"),),
                width=32,
                height=10,
                unicode_ok=False,
            )

        self.assertNotIn("\x1b[", rendered)
        rendered.encode("ascii")
        self.assertLessEqual(max(map(len, rendered.splitlines())), 32)


if __name__ == "__main__":
    unittest.main()
