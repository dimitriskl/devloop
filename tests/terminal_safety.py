from __future__ import annotations

import unittest

from devloop.terminal_text import has_unsafe_terminal_controls


ORDINARY_UNICODE_TEXT = "Καλημέρα 世界"
HOSTILE_TERMINAL_TEXT = (
    f"{ORDINARY_UNICODE_TEXT} "
    "\x1b[2JESC-CSI "
    "\x1b]0;ESC-OSC\x07 "
    "\x1b]1;ESC-ST\x1b\\ "
    "\x9b2JC1-CSI "
    "\x9d0;C1-OSC\x9c "
    "\u202eBIDI"
)


def assert_terminal_text_is_safe(
    test_case: unittest.TestCase,
    rendered: str,
    *,
    redirected: bool,
) -> None:
    test_case.assertIn(ORDINARY_UNICODE_TEXT, rendered)
    for hostile_sequence in (
        "\x1b[2J",
        "\x1b]",
        "\x1b\\",
        "\x07",
        "\x9b",
        "\x9c",
        "\x9d",
        "\u202e",
    ):
        test_case.assertNotIn(hostile_sequence, rendered)
    if redirected:
        executable_controls = [
            character
            for character in rendered
            if character != "\n"
            and has_unsafe_terminal_controls(character)
        ]
        test_case.assertEqual(executable_controls, [])
