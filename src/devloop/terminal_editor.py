from __future__ import annotations

import atexit
import codecs
import os
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

PasteHook = Callable[[], "str | None"]

ESC = "\x1b"
CTRL_A = "\x01"
CTRL_C = "\x03"
CTRL_D = "\x04"
CTRL_E = "\x05"
CTRL_K = "\x0b"
CTRL_U = "\x15"
CTRL_W = "\x17"
ENTER_KEYS = {"\r", "\n"}
BACKSPACE_KEYS = {"\x7f", "\x08"}
WINDOWS_EXTENDED_PREFIXES = {"\x00", "\xe0"}
ACTION_NONE = "none"
ACTION_SUBMIT = "submit"
ACTION_NEWLINE = "newline"

_windows_output_prepared = False


@dataclass
class _EditorState:
    buffer: list[str] = field(default_factory=list)
    cursor: int = 0
    history_index: int | None = None
    stash: str = ""
    rendered_lines: int = 1
    rendered_cursor_row: int = 0
    rendered_last_column: int = 0
    vertical_column: int | None = None

    def text(self) -> str:
        return "".join(self.buffer)

    def set_text(self, text: str) -> None:
        self.buffer = list(text)
        self.cursor = len(self.buffer)
        self.vertical_column = None


class TerminalKeySource:
    def read(self) -> str | None:
        raise NotImplementedError

    def alt_pressed(self) -> bool:
        return False

    def read_pending(self, timeout_seconds: float = 0.05) -> str | None:
        return self.read()

    def close(self) -> None:
        pass


class IteratorKeySource(TerminalKeySource):
    def __init__(self, chars: Iterator[str]) -> None:
        self._iter = chars

    def read(self) -> str | None:
        return next(self._iter, None)


class _PosixKeySource(TerminalKeySource):
    def __init__(self) -> None:
        import termios
        import tty

        self._termios = termios
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        encoding = getattr(sys.stdin, "encoding", None) or "utf-8"
        errors = getattr(sys.stdin, "errors", None) or "strict"
        self._decoder = codecs.getincrementaldecoder(encoding)(errors=errors)
        tty.setcbreak(self._fd)

    def read(self) -> str | None:
        while True:
            byte = os.read(self._fd, 1)
            if not byte:
                return None
            decoded = self._decoder.decode(byte)
            if decoded:
                return decoded

    def close(self) -> None:
        self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._old)

    def read_pending(self, timeout_seconds: float = 0.05) -> str | None:
        import select

        readable, _writable, _errors = select.select(
            [self._fd],
            [],
            [],
            timeout_seconds,
        )
        return self.read() if readable else None


class _WindowsKeySource(TerminalKeySource):
    def __init__(self) -> None:
        import msvcrt  # noqa: F401  (import check)

        self._restore = _enable_windows_vt_modes()
        self._last_alt_pressed = False

    def read(self) -> str | None:
        import msvcrt

        self._last_alt_pressed = _windows_alt_pressed()
        char = msvcrt.getwch()
        if _is_high_surrogate(char):
            follow = msvcrt.getwch()
            if _is_low_surrogate(follow):
                char = _combine_surrogates(char, follow)
            else:
                char = f"{char}{follow}"
        self._last_alt_pressed = self._last_alt_pressed or _windows_alt_pressed()
        return char

    def alt_pressed(self) -> bool:
        return self._last_alt_pressed

    def close(self) -> None:
        if self._restore is not None:
            self._restore()

    def read_pending(self, timeout_seconds: float = 0.05) -> str | None:
        import msvcrt
        import time

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                return self.read()
            time.sleep(0.005)
        return None


def _windows_alt_pressed() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        # VK_MENU, VK_LMENU, VK_RMENU. The high bit is current key-down state;
        # the low bit catches very fast key presses between polling points.
        return any(user32.GetAsyncKeyState(vk) & 0x8001 for vk in (0x12, 0xA4, 0xA5))
    except Exception:
        return False


def _is_high_surrogate(char: str) -> bool:
    return len(char) == 1 and 0xD800 <= ord(char) <= 0xDBFF


def _is_low_surrogate(char: str) -> bool:
    return len(char) == 1 and 0xDC00 <= ord(char) <= 0xDFFF


def _combine_surrogates(high: str, low: str) -> str:
    high_value = ord(high) - 0xD800
    low_value = ord(low) - 0xDC00
    return chr(0x10000 + ((high_value << 10) | low_value))


def _enable_windows_vt_modes() -> Callable[[], None] | None:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    STD_INPUT_HANDLE = -10
    STD_OUTPUT_HANDLE = -11

    handles: list[tuple[int, int]] = []
    for std_handle, flag in (
        (STD_INPUT_HANDLE, ENABLE_VIRTUAL_TERMINAL_INPUT),
        (STD_OUTPUT_HANDLE, ENABLE_VIRTUAL_TERMINAL_PROCESSING),
    ):
        handle = kernel32.GetStdHandle(std_handle)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            raise OSError("GetConsoleMode failed")
        handles.append((handle, mode.value))
        if not kernel32.SetConsoleMode(handle, mode.value | flag):
            raise OSError("SetConsoleMode failed")

    def restore() -> None:
        for handle, previous in handles:
            kernel32.SetConsoleMode(handle, previous)

    return restore


def prepare_terminal_output() -> bool:
    """Enable ANSI output on Windows for this process, restoring it at exit."""
    global _windows_output_prepared
    if not sys.platform.startswith("win"):
        return True
    if _windows_output_prepared:
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_vt_output = 0x0004
        previous_mode = mode.value
        if not kernel32.SetConsoleMode(handle, previous_mode | enable_vt_output):
            return False
    except Exception:
        return False

    def restore() -> None:
        kernel32.SetConsoleMode(handle, previous_mode)

    _windows_output_prepared = True
    atexit.register(restore)
    return True


def open_key_source() -> TerminalKeySource | None:
    """Open a portable raw-key source, or return ``None`` for line-input terminals."""
    if os.environ.get("DEVLOOP_EDITOR", "").strip().lower() in {"native", "plain", "input"}:
        return None

    stdin_isatty = getattr(sys.stdin, "isatty", None)
    if not (stdin_isatty and stdin_isatty()):
        return None
    try:
        if sys.platform.startswith("win"):
            return _WindowsKeySource()
        return _PosixKeySource()
    except Exception:
        return None


def _default_write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


class TerminalEditor:
    """Reusable terminal composer with history, cursor movement, and paste hooks.

    This component is independent of Dev Loop. Callers provide a paste hook and
    can reuse the editor anywhere a terminal prompt needs Readline-style editing
    plus multi-line composition.
    """

    def __init__(
        self,
        *,
        on_paste_image: PasteHook,
        write: Callable[[str], None] | None = None,
        fallback_hint: str | None = "(Alt+V unavailable in this terminal; use /paste instead.)",
    ) -> None:
        self.on_paste_image = on_paste_image
        self._write = write or _default_write
        self._fallback_hint = fallback_hint
        self.history: list[str] = []
        self._fallback_hint_shown = False

    def read_line(self, prompt: str) -> str:
        """Read one line from the terminal.

        Must be called from the main thread: on POSIX, cbreak mode leaves
        ISIG enabled, so a real Ctrl+C arrives as SIGINT (Python's default
        handler raises KeyboardInterrupt on the main thread), not as a
        raw \\x03 byte.
        """
        keys = open_key_source()
        if keys is None:
            if self._fallback_hint and not self._fallback_hint_shown:
                self._fallback_hint_shown = True
                print(self._fallback_hint)
            line = input(prompt)
            if line.strip():
                self.history.append(line)
            return line

        try:
            line = self._edit(prompt, keys)
        finally:
            keys.close()
        if line.strip():
            self.history.append(line)
        return line

    def feed(self, prompt: str, chars: Iterable[str]) -> str:
        flattened = (char for item in chars for char in item)
        line = self._edit(prompt, IteratorKeySource(flattened))
        if line.strip():
            self.history.append(line)
        return line

    def _edit(self, prompt: str, keys: TerminalKeySource) -> str:
        state = _EditorState()
        self._render(prompt, state)
        while True:
            char = keys.read()
            if char is None:
                raise EOFError
            if char == "":
                continue
            if char == CTRL_C:
                raise KeyboardInterrupt
            if char == CTRL_D:
                if not state.buffer:
                    raise EOFError
                self._delete_at_cursor(state)
            elif char == CTRL_A:
                self._move_to_line_start(state)
            elif char == CTRL_E:
                self._move_to_line_end(state)
            elif char == CTRL_K:
                self._delete_range(state, state.cursor, self._line_end(state))
            elif char == CTRL_U:
                self._delete_range(state, self._line_start(state), state.cursor)
            elif char == CTRL_W:
                self._delete_word_before(state)
            if char in ENTER_KEYS:
                if keys.alt_pressed():
                    self._insert_text(state, "\n")
                else:
                    self._move_to_render_end(state)
                    self._write("\n")
                    return state.text()
            if char in WINDOWS_EXTENDED_PREFIXES:
                self._handle_windows_extended(state, keys)
            elif char == ESC:
                action = self._handle_escape(state, keys)
                if action == ACTION_SUBMIT:
                    self._move_to_render_end(state)
                    self._write("\n")
                    return state.text()
                if action == ACTION_NEWLINE:
                    self._insert_text(state, "\n")
            elif char in BACKSPACE_KEYS:
                if state.cursor > 0:
                    state.cursor -= 1
                    del state.buffer[state.cursor]
                    state.vertical_column = None
            elif char in {"v", "V"} and keys.alt_pressed():
                self._insert_paste_token(state)
            elif char.isprintable():
                self._insert_text(state, char)
            self._render(prompt, state)

    def _handle_escape(self, state: _EditorState, keys: TerminalKeySource) -> str:
        follow = keys.read()
        if follow in ENTER_KEYS:
            return ACTION_NEWLINE
        if follow in ("v", "V"):
            self._insert_paste_token(state)
            return ACTION_NONE
        if follow in ("b", "B"):
            self._move_word_left(state)
            return ACTION_NONE
        if follow in ("f", "F"):
            self._move_word_right(state)
            return ACTION_NONE
        if follow in BACKSPACE_KEYS:
            self._delete_word_before(state)
            return ACTION_NONE
        if follow == "O":
            final = keys.read()
            if final == "M":
                return ACTION_SUBMIT
            if final == "H":
                self._move_to_line_start(state)
            elif final == "F":
                self._move_to_line_end(state)
            return ACTION_NONE
        if follow != "[":
            return ACTION_NONE
        parameters = ""
        while True:
            final = keys.read()
            if final is None:
                return ACTION_NONE
            if final.isalpha() or final == "~":
                break
            parameters += final
        return self._handle_csi_sequence(parameters, final, state)

    def _handle_windows_extended(
        self,
        state: _EditorState,
        keys: TerminalKeySource,
    ) -> None:
        code = keys.read()
        if code is None:
            return
        if code == "K":
            self._move_left(state)
        elif code == "M":
            self._move_right(state)
        elif code == "H":
            self._move_up(state)
        elif code == "P":
            self._move_down(state)
        elif code == "G":
            self._move_to_line_start(state)
        elif code == "O":
            self._move_to_line_end(state)
        elif code == "S":
            self._delete_at_cursor(state)
        elif code in {"/", "v", "V"}:
            # msvcrt reports Alt+V as a NUL-prefixed scan code on some Windows
            # consoles instead of ESC followed by "v".
            self._insert_paste_token(state)

    def _handle_csi_sequence(
        self,
        parameters: str,
        final: str,
        state: _EditorState,
    ) -> str:
        if final == "u" and _csi_key(parameters) == "13":
            return ACTION_NEWLINE
        if final == "D":
            self._move_word_left(state) if _csi_has_modifier(parameters, {"3", "5"}) else self._move_left(state)
        elif final == "C":
            self._move_word_right(state) if _csi_has_modifier(parameters, {"3", "5"}) else self._move_right(state)
        elif final == "A":
            self._move_up(state)
        elif final == "B":
            self._move_down(state)
        elif final == "H":
            self._move_to_line_start(state)
        elif final == "F":
            self._move_to_line_end(state)
        elif final == "~":
            action = self._handle_tilde_sequence(parameters, state)
            if action != ACTION_NONE:
                return action
        return ACTION_NONE

    def _handle_tilde_sequence(self, parameters: str, state: _EditorState) -> str:
        key = _csi_key(parameters)
        if key == "13":
            return ACTION_NEWLINE
        if key == "3":
            self._delete_at_cursor(state)
        elif key in {"1", "7"}:
            self._move_to_line_start(state)
        elif key in {"4", "8"}:
            self._move_to_line_end(state)
        return ACTION_NONE

    def _delete_at_cursor(self, state: _EditorState) -> None:
        if state.cursor < len(state.buffer):
            del state.buffer[state.cursor]
            state.vertical_column = None

    def _insert_paste_token(self, state: _EditorState) -> None:
        token = self.on_paste_image()
        if token:
            self._insert_text(state, token)

    def _insert_text(self, state: _EditorState, text: str) -> None:
        for char in text:
            state.buffer.insert(state.cursor, char)
            state.cursor += 1
        state.vertical_column = None

    def _delete_range(self, state: _EditorState, start: int, end: int) -> None:
        start = max(0, min(start, len(state.buffer)))
        end = max(start, min(end, len(state.buffer)))
        if start == end:
            return
        del state.buffer[start:end]
        state.cursor = start
        state.vertical_column = None

    def _delete_word_before(self, state: _EditorState) -> None:
        index = state.cursor
        while index > 0 and state.buffer[index - 1].isspace():
            index -= 1
        while index > 0 and not state.buffer[index - 1].isspace():
            index -= 1
        self._delete_range(state, index, state.cursor)

    def _move_left(self, state: _EditorState) -> None:
        if state.cursor > 0:
            state.cursor -= 1
        state.vertical_column = None

    def _move_right(self, state: _EditorState) -> None:
        if state.cursor < len(state.buffer):
            state.cursor += 1
        state.vertical_column = None

    def _move_word_left(self, state: _EditorState) -> None:
        while state.cursor > 0 and state.buffer[state.cursor - 1].isspace():
            state.cursor -= 1
        while state.cursor > 0 and not state.buffer[state.cursor - 1].isspace():
            state.cursor -= 1
        state.vertical_column = None

    def _move_word_right(self, state: _EditorState) -> None:
        while state.cursor < len(state.buffer) and state.buffer[state.cursor].isspace():
            state.cursor += 1
        while state.cursor < len(state.buffer) and not state.buffer[state.cursor].isspace():
            state.cursor += 1
        state.vertical_column = None

    def _move_to_line_start(self, state: _EditorState) -> None:
        state.cursor = self._line_start(state)
        state.vertical_column = None

    def _move_to_line_end(self, state: _EditorState) -> None:
        state.cursor = self._line_end(state)
        state.vertical_column = None

    def _move_up(self, state: _EditorState) -> None:
        start = self._line_start(state)
        if start == 0:
            self._history_previous(state)
            return
        target_column = state.vertical_column
        if target_column is None:
            target_column = _text_width("".join(state.buffer[start:state.cursor]))
        previous_end = start - 1
        previous_start = self._line_start(state, previous_end)
        state.cursor = self._index_for_column(previous_start, previous_end, target_column, state)
        state.vertical_column = target_column

    def _move_down(self, state: _EditorState) -> None:
        end = self._line_end(state)
        if end == len(state.buffer):
            self._history_next(state)
            return
        target_column = state.vertical_column
        if target_column is None:
            start = self._line_start(state)
            target_column = _text_width("".join(state.buffer[start:state.cursor]))
        next_start = end + 1
        next_end = self._line_end(state, next_start)
        state.cursor = self._index_for_column(next_start, next_end, target_column, state)
        state.vertical_column = target_column

    def _line_start(self, state: _EditorState, index: int | None = None) -> int:
        index = state.cursor if index is None else index
        while index > 0 and state.buffer[index - 1] != "\n":
            index -= 1
        return index

    def _line_end(self, state: _EditorState, index: int | None = None) -> int:
        index = state.cursor if index is None else index
        while index < len(state.buffer) and state.buffer[index] != "\n":
            index += 1
        return index

    def _index_for_column(
        self,
        start: int,
        end: int,
        target_column: int,
        state: _EditorState,
    ) -> int:
        column = 0
        for index in range(start, end):
            width = _char_width(state.buffer[index])
            if column + width > target_column:
                return index
            column += width
        return end

    def _history_previous(self, state: _EditorState) -> None:
        if not self.history:
            return
        if state.history_index is None:
            state.stash = state.text()
            state.history_index = len(self.history) - 1
        elif state.history_index > 0:
            state.history_index -= 1
        state.set_text(self.history[state.history_index])

    def _history_next(self, state: _EditorState) -> None:
        if state.history_index is None:
            return
        if state.history_index < len(self.history) - 1:
            state.history_index += 1
            state.set_text(self.history[state.history_index])
        else:
            state.history_index = None
            state.set_text(state.stash)

    def _render(self, prompt: str, state: _EditorState) -> None:
        lines, cursor_row, cursor_column = self._render_layout(prompt, state)

        self._write("\r")
        if state.rendered_cursor_row > 0:
            self._write(f"\x1b[{state.rendered_cursor_row}A")
        self._write("\x1b[J")

        self._write("\n".join(lines))

        up = len(lines) - 1 - cursor_row
        if up > 0:
            self._write(f"\x1b[{up}A")
        self._write("\r")
        if cursor_column > 0:
            self._write(f"\x1b[{cursor_column}C")

        state.rendered_lines = len(lines)
        state.rendered_cursor_row = cursor_row
        state.rendered_last_column = _text_width(lines[-1])

    def _render_layout(self, prompt: str, state: _EditorState) -> tuple[list[str], int, int]:
        columns = max(1, shutil.get_terminal_size(fallback=(80, 24)).columns - 1)
        lines = [""]
        widths = [0]
        cursor_row = 0
        cursor_column = 0
        cursor_index = len(prompt) + state.cursor
        logical_index = 0

        for char in f"{prompt}{state.text()}":
            if logical_index == cursor_index:
                cursor_row = len(lines) - 1
                cursor_column = widths[-1]
            if char == "\n":
                lines.append("")
                widths.append(0)
                logical_index += 1
                continue
            width = _char_width(char)
            if width > 0 and widths[-1] > 0 and widths[-1] + width > columns:
                lines.append("")
                widths.append(0)
            lines[-1] += char
            widths[-1] += width
            logical_index += 1

        if logical_index == cursor_index:
            cursor_row = len(lines) - 1
            cursor_column = widths[-1]

        return lines or [""], cursor_row, cursor_column

    def _move_to_render_end(self, state: _EditorState) -> None:
        down = state.rendered_lines - 1 - state.rendered_cursor_row
        if down > 0:
            self._write(f"\x1b[{down}B")
        self._write("\r")
        if state.rendered_last_column > 0:
            self._write(f"\x1b[{state.rendered_last_column}C")


def _csi_parts(parameters: str) -> list[str]:
    return [part for part in parameters.replace(":", ";").split(";") if part]


def _csi_key(parameters: str) -> str:
    parts = _csi_parts(parameters)
    return parts[0] if parts else ""


def _csi_has_modifier(parameters: str, modifiers: set[str]) -> bool:
    parts = _csi_parts(parameters)
    return any(part in modifiers for part in parts[1:])


def _text_width(text: str) -> int:
    return sum(_char_width(char) for char in text if char != "\n")


def display_width(text: str) -> int:
    """Return the terminal display-cell width for Unicode text."""
    return _text_width(text)


def _char_width(char: str) -> int:
    if not char:
        return 0
    if char == "\t":
        return 4
    category = unicodedata.category(char)
    if category.startswith("C"):
        return 0
    if unicodedata.combining(char):
        return 0
    if unicodedata.east_asian_width(char) in {"F", "W"}:
        return 2
    return 1


LineEditor = TerminalEditor

__all__ = ["LineEditor", "PasteHook", "TerminalEditor", "display_width"]
