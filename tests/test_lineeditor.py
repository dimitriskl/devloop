from __future__ import annotations

import os
import sys
import io
import unittest
from unittest import mock

from devloop.lineeditor import LineEditor
from devloop.terminal_editor import TerminalEditor, display_width


class _AltKeySource:
    def __init__(self, keys: list[tuple[str, bool] | str]) -> None:
        self._keys = list(keys)
        self._last_alt_pressed = False

    def read(self) -> str | None:
        if not self._keys:
            return None
        item = self._keys.pop(0)
        if isinstance(item, tuple):
            char, alt_pressed = item
            self._last_alt_pressed = alt_pressed
            return char
        self._last_alt_pressed = False
        return item

    def alt_pressed(self) -> bool:
        return self._last_alt_pressed


def editor(paste_result: str | None = "[image 1 attached] ") -> LineEditor:
    return LineEditor(on_paste_image=lambda: paste_result, write=lambda text: None)


class StandaloneComponentTests(unittest.TestCase):
    def test_terminal_editor_is_importable_directly(self) -> None:
        ed = TerminalEditor(on_paste_image=lambda: None, write=lambda text: None)
        self.assertEqual(ed.feed("> ", list("ok") + ["\r"]), "ok")

    def test_line_editor_remains_compatibility_alias(self) -> None:
        self.assertIs(LineEditor, TerminalEditor)


class TypingTests(unittest.TestCase):
    def test_plain_text_and_enter(self) -> None:
        line = editor().feed("> ", list("hello") + ["\r"])
        self.assertEqual(line, "hello")

    def test_windows_vt_enter_sequence_submits_line(self) -> None:
        line = editor().feed("> ", list("hello") + ["\x1b", "O", "M"])
        self.assertEqual(line, "hello")

    def test_backspace_removes_before_cursor(self) -> None:
        line = editor().feed("> ", list("heyy") + ["\x7f", "\r"])
        self.assertEqual(line, "hey")

    def test_left_arrow_then_insert(self) -> None:
        keys = list("ac") + ["\x1b", "[", "D", "b", "\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "abc")

    def test_windows_extended_left_arrow_then_insert(self) -> None:
        keys = list("ac") + ["\xe0", "K", "b", "\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "abc")

    def test_unicode_text_is_preserved(self) -> None:
        text = "Καλημέρα العربية 中文 cafe\u0301 😀"
        line = editor().feed("> ", list(text) + ["\r"])
        self.assertEqual(line, text)

    def test_alt_enter_inserts_newline_and_enter_submits(self) -> None:
        keys = list("first") + ["\x1b", "\r"] + list("second") + ["\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "first\nsecond")

    def test_csi_shift_enter_inserts_newline(self) -> None:
        keys = list("first") + ["\x1b", "[", "13", ";", "2", "u"] + list("second") + ["\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "first\nsecond")


class AltVTests(unittest.TestCase):
    def test_alt_v_inserts_paste_token(self) -> None:
        keys = list("see ") + ["\x1b", "v"] + ["\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "see [image 1 attached] ")

    def test_alt_v_with_no_image_inserts_nothing(self) -> None:
        keys = list("ok") + ["\x1b", "v", "\r"]
        line = editor(paste_result=None).feed("> ", keys)
        self.assertEqual(line, "ok")

    def test_windows_alt_v_scan_code_inserts_paste_token(self) -> None:
        keys = list("see ") + ["\x00", "/"] + ["\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "see [image 1 attached] ")

    def test_windows_alt_v_printable_v_with_alt_state_inserts_paste_token(self) -> None:
        ed = editor()
        line = ed._edit("> ", _AltKeySource(list("see ") + [("v", True), "\r"]))
        self.assertEqual(line, "see [image 1 attached] ")


class HistoryTests(unittest.TestCase):
    def test_up_arrow_recalls_previous_line(self) -> None:
        ed = editor()
        ed.feed("> ", list("first") + ["\r"])
        line = ed.feed("> ", ["\x1b", "[", "A", "\r"])
        self.assertEqual(line, "first")

    def test_down_arrow_restores_stash(self) -> None:
        ed = editor()
        ed.feed("> ", list("first") + ["\r"])
        keys = list("dra") + ["\x1b", "[", "A", "\x1b", "[", "B", "ft", "\r"]
        # up recalls "first", down restores the stashed draft "dra"
        line = ed.feed("> ", [key for key in keys])
        self.assertEqual(line, "draft")


class ControlTests(unittest.TestCase):
    def test_ctrl_c_raises_keyboard_interrupt(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            editor().feed("> ", ["\x03"])

    def test_ctrl_d_on_empty_raises_eof(self) -> None:
        with self.assertRaises(EOFError):
            editor().feed("> ", ["\x04"])

    def test_exhausted_keys_raise_eof(self) -> None:
        with self.assertRaises(EOFError):
            editor().feed("> ", list("abc"))


class CursorAndBoundaryTests(unittest.TestCase):
    def test_right_arrow_moves_cursor_back_right(self) -> None:
        keys = list("ab") + ["\x1b", "[", "D", "\x1b", "[", "C", "c", "\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "abc")

    def test_home_then_insert_at_start(self) -> None:
        keys = list("bc") + ["\x1b", "[", "H", "a", "\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "abc")

    def test_end_returns_cursor_to_end(self) -> None:
        keys = list("ab") + ["\x1b", "[", "H", "\x1b", "[", "F", "c", "\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "abc")

    def test_escape_delete_removes_character_at_cursor(self) -> None:
        keys = list("ac") + ["\x1b", "[", "D", "\x1b", "[", "3", "~", "b", "\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "ab")

    def test_backspace_at_start_is_noop(self) -> None:
        keys = ["\x7f"] + list("ok") + ["\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "ok")

    def test_left_arrow_at_start_is_noop(self) -> None:
        keys = ["\x1b", "[", "D"] + list("ok") + ["\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "ok")

    def test_up_with_no_history_is_noop(self) -> None:
        keys = ["\x1b", "[", "A"] + list("ok") + ["\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "ok")

    def test_down_without_prior_up_is_noop(self) -> None:
        keys = ["\x1b", "[", "B"] + list("ok") + ["\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "ok")

    def test_up_arrow_moves_within_multiline_before_history(self) -> None:
        keys = list("abc") + ["\x1b", "\r"] + list("xy") + ["\x1b", "[", "A", "!", "\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "ab!c\nxy")

    def test_down_arrow_moves_within_multiline(self) -> None:
        keys = (
            list("abc")
            + ["\x1b", "\r"]
            + list("xy")
            + ["\x1b", "[", "A", "\x1b", "[", "B", "!", "\r"]
        )
        line = editor().feed("> ", keys)
        self.assertEqual(line, "abc\nxy!")

    def test_vertical_move_uses_unicode_display_width(self) -> None:
        keys = list("你好") + ["\x1b", "\r"] + list("ab") + ["\x1b", "[", "A", "!", "\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "你!好\nab")

    def test_readline_start_end_and_kill_shortcuts(self) -> None:
        line = editor().feed("> ", list("abc") + ["\x01", "x", "\x05", "y", "\x01", "\x0b", "\r"])
        self.assertEqual(line, "")

    def test_ctrl_u_deletes_before_cursor(self) -> None:
        keys = list("abcdef") + ["\x1b", "[", "D", "\x1b", "[", "D", "\x15", "\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "ef")

    def test_alt_word_motion_and_ctrl_w(self) -> None:
        keys = list("alpha beta") + ["\x1b", "b", "\x17", "\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "beta")


class ReadLineFallbackTests(unittest.TestCase):
    def test_env_can_force_native_input(self) -> None:
        ed = editor()
        with mock.patch.dict(os.environ, {"DEVLOOP_EDITOR": "native"}), \
             mock.patch("builtins.input", return_value="typed line"):
            line = ed.read_line("> ")
        self.assertEqual(line, "typed line")

    def test_non_tty_falls_back_to_input_with_one_time_hint(self) -> None:
        ed = editor()
        fake_stdin = io.StringIO("typed line\n")
        printed: list[str] = []
        with mock.patch("devloop.terminal_editor.sys.stdin", fake_stdin), \
             mock.patch("builtins.input", side_effect=["typed line", "second"]), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            first = ed.read_line("> ")
            second = ed.read_line("> ")
        self.assertEqual(first, "typed line")
        self.assertEqual(second, "second")
        hints = [line for line in printed if "/paste" in line]
        self.assertEqual(len(hints), 1)


class RenderingTests(unittest.TestCase):
    def test_long_input_wraps_and_keeps_the_full_text_visible(self) -> None:
        writes: list[str] = []
        ed = LineEditor(on_paste_image=lambda: None, write=writes.append)
        text = "abcdefghijklmnopqrstuvwxyz"
        with mock.patch(
            "devloop.terminal_editor.shutil.get_terminal_size",
            return_value=os.terminal_size((20, 24)),
        ):
            line = ed.feed("[analysis] > ", list(text) + ["\r"])

        self.assertEqual(line, text)
        rendered = [chunk for chunk in writes if "[analysis] > " in chunk]
        self.assertTrue(rendered)
        final = rendered[-1]
        self.assertIn("\n", final)
        self.assertEqual(final.replace("\n", ""), f"[analysis] > {text}")
        self.assertTrue(all(len(line) <= 19 for line in final.split("\n")))

    def test_unicode_width_handles_combining_and_wide_characters(self) -> None:
        self.assertEqual(display_width("e\u0301"), 1)
        self.assertEqual(display_width("中文"), 4)
        self.assertEqual(display_width("Καλημέρα"), 8)


if __name__ == "__main__":
    unittest.main()
