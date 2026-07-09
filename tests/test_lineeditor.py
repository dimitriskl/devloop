from __future__ import annotations

import sys
import io
import unittest
from unittest import mock

from devloop.lineeditor import LineEditor


def editor(paste_result: str | None = "[image 1 attached] ") -> LineEditor:
    return LineEditor(on_paste_image=lambda: paste_result, write=lambda text: None)


class TypingTests(unittest.TestCase):
    def test_plain_text_and_enter(self) -> None:
        line = editor().feed("> ", list("hello") + ["\r"])
        self.assertEqual(line, "hello")

    def test_backspace_removes_before_cursor(self) -> None:
        line = editor().feed("> ", list("heyy") + ["\x7f", "\r"])
        self.assertEqual(line, "hey")

    def test_left_arrow_then_insert(self) -> None:
        keys = list("ac") + ["\x1b", "[", "D", "b", "\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "abc")


class AltVTests(unittest.TestCase):
    def test_alt_v_inserts_paste_token(self) -> None:
        keys = list("see ") + ["\x1b", "v"] + ["\r"]
        line = editor().feed("> ", keys)
        self.assertEqual(line, "see [image 1 attached] ")

    def test_alt_v_with_no_image_inserts_nothing(self) -> None:
        keys = list("ok") + ["\x1b", "v", "\r"]
        line = editor(paste_result=None).feed("> ", keys)
        self.assertEqual(line, "ok")


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


class ReadLineFallbackTests(unittest.TestCase):
    def test_non_tty_falls_back_to_input_with_one_time_hint(self) -> None:
        ed = editor()
        fake_stdin = io.StringIO("typed line\n")
        printed: list[str] = []
        with mock.patch("devloop.lineeditor.sys.stdin", fake_stdin), \
             mock.patch("builtins.input", side_effect=["typed line", "second"]), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            first = ed.read_line("> ")
            second = ed.read_line("> ")
        self.assertEqual(first, "typed line")
        self.assertEqual(second, "second")
        hints = [line for line in printed if "/paste" in line]
        self.assertEqual(len(hints), 1)


if __name__ == "__main__":
    unittest.main()
