from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

PasteHook = Callable[[], "str | None"]

ESC = "\x1b"
CTRL_C = "\x03"
CTRL_D = "\x04"
ENTER_KEYS = {"\r", "\n"}
BACKSPACE_KEYS = {"\x7f", "\x08"}


@dataclass
class _EditorState:
    buffer: list[str] = field(default_factory=list)
    cursor: int = 0
    history_index: int | None = None
    stash: str = ""

    def text(self) -> str:
        return "".join(self.buffer)

    def set_text(self, text: str) -> None:
        self.buffer = list(text)
        self.cursor = len(self.buffer)


class _KeySource:
    def read(self) -> str | None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class _IteratorKeySource(_KeySource):
    def __init__(self, chars: Iterator[str]) -> None:
        self._iter = chars

    def read(self) -> str | None:
        return next(self._iter, None)


class _PosixKeySource(_KeySource):
    def __init__(self) -> None:
        import termios
        import tty

        self._termios = termios
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)

    def read(self) -> str | None:
        char = sys.stdin.read(1)
        return char or None

    def close(self) -> None:
        self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._old)


class _WindowsKeySource(_KeySource):
    def __init__(self) -> None:
        import msvcrt  # noqa: F401  (import check)

        self._restore = _enable_windows_vt_modes()

    def read(self) -> str | None:
        import msvcrt

        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            msvcrt.getwch()
            return ""
        return char

    def close(self) -> None:
        if self._restore is not None:
            self._restore()


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


def _make_key_source() -> _KeySource | None:
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


class LineEditor:
    """Minimal raw-mode line editor with an Alt+V (ESC+v) paste hook.

    Windows VT input mode and POSIX raw mode both deliver Alt+V as ESC
    followed by "v", so a single parser serves both platforms.
    """

    def __init__(
        self,
        *,
        on_paste_image: PasteHook,
        write: Callable[[str], None] | None = None,
    ) -> None:
        self.on_paste_image = on_paste_image
        self._write = write or _default_write
        self.history: list[str] = []
        self._fallback_hint_shown = False

    def read_line(self, prompt: str) -> str:
        """Read one line from the terminal.

        Must be called from the main thread: on POSIX, cbreak mode leaves
        ISIG enabled, so a real Ctrl+C arrives as SIGINT (Python's default
        handler raises KeyboardInterrupt on the main thread), not as a
        raw \\x03 byte.
        """
        keys = _make_key_source()
        if keys is None:
            if not self._fallback_hint_shown:
                self._fallback_hint_shown = True
                print("(Alt+V unavailable in this terminal; use /paste instead.)")
            line = input(prompt)
            if line.strip():
                self.history.append(line)
            return line

        self._write(prompt)
        try:
            line = self._edit(prompt, keys)
        finally:
            keys.close()
        if line.strip():
            self.history.append(line)
        return line

    def feed(self, prompt: str, chars: Iterable[str]) -> str:
        flattened = (char for item in chars for char in item)
        line = self._edit(prompt, _IteratorKeySource(flattened))
        if line.strip():
            self.history.append(line)
        return line

    def _edit(self, prompt: str, keys: _KeySource) -> str:
        state = _EditorState()
        while True:
            char = keys.read()
            if char is None:
                raise EOFError
            if char == "":
                continue
            if char == CTRL_C:
                raise KeyboardInterrupt
            if char == CTRL_D and not state.buffer:
                raise EOFError
            if char in ENTER_KEYS:
                self._write("\n")
                return state.text()
            if char == ESC:
                self._handle_escape(state, keys)
            elif char in BACKSPACE_KEYS:
                if state.cursor > 0:
                    state.cursor -= 1
                    del state.buffer[state.cursor]
            elif char.isprintable():
                state.buffer.insert(state.cursor, char)
                state.cursor += 1
            self._render(prompt, state)

    def _handle_escape(self, state: _EditorState, keys: _KeySource) -> None:
        follow = keys.read()
        if follow in ("v", "V"):
            token = self.on_paste_image()
            if token:
                for char in token:
                    state.buffer.insert(state.cursor, char)
                    state.cursor += 1
            return
        if follow != "[":
            return
        final = keys.read()
        if final == "D" and state.cursor > 0:
            state.cursor -= 1
        elif final == "C" and state.cursor < len(state.buffer):
            state.cursor += 1
        elif final == "A":
            self._history_previous(state)
        elif final == "B":
            self._history_next(state)
        elif final == "H":
            state.cursor = 0
        elif final == "F":
            state.cursor = len(state.buffer)

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
        text = state.text()
        self._write(f"\r\x1b[K{prompt}{text}")
        back = len(state.buffer) - state.cursor
        if back > 0:
            self._write(f"\x1b[{back}D")
