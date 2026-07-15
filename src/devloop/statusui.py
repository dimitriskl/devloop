from __future__ import annotations

import os
import sys
import threading
import time
from enum import Enum
from typing import Callable, TextIO


class Stage(Enum):
    ANALYSIS = "analysis"
    DEVELOPMENT = "development"
    REVIEW = "review"
    QA = "qa"


PIPELINE = [Stage.ANALYSIS, Stage.DEVELOPMENT, Stage.REVIEW, Stage.QA]

_ACTIVE_COLOR = "\x1b[1;36m"
_RESET = "\x1b[0m"
_BANNER_WIDTH = 79
WAITING_FRAMES = ("|", "/", "-", "\\")
WAITING_FRAME_SECONDS = 0.12
WAITING_STALL_SECONDS = 120.0


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{remaining_seconds:02}"


class WaitingIndicator:
    def __init__(
        self,
        stream: TextIO | None = None,
        frame_seconds: float = WAITING_FRAME_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        stalled_after_seconds: float = WAITING_STALL_SECONDS,
        *,
        stage: Stage = Stage.ANALYSIS,
        context: str = "",
    ) -> None:
        self._stream = sys.stdout if stream is None else stream
        self._frame_seconds = frame_seconds
        self._clock = clock
        self._stalled_after_seconds = stalled_after_seconds
        self._stage = stage
        self._context = " ".join(context.split())
        isatty = getattr(self._stream, "isatty", None)
        self._enabled = bool(callable(isatty) and isatty())
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._activity_lock = threading.Lock()
        self._started_at = self._clock()
        self._last_activity_at: float | None = None
        self._rendered_width = 0

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_requested.set()
        self._thread.join()
        self._thread = None
        self._clear()

    def notify_activity(self) -> None:
        with self._activity_lock:
            self._last_activity_at = self._clock()

    def _animate(self) -> None:
        frame_index = 0
        while True:
            frame = WAITING_FRAMES[frame_index % len(WAITING_FRAMES)]
            status_line = self._status_line(frame)
            padding = " " * max(0, self._rendered_width - len(status_line))
            try:
                self._stream.write(f"\r{status_line}{padding}")
                self._stream.flush()
            except (OSError, ValueError):
                return
            self._rendered_width = max(self._rendered_width, len(status_line))
            if self._stop_requested.wait(self._frame_seconds):
                return
            frame_index += 1

    def _status_line(self, frame: str) -> str:
        now = self._clock()
        with self._activity_lock:
            last_activity_at = self._last_activity_at

        elapsed_seconds = max(0.0, now - self._started_at)
        inactivity_seconds = (
            elapsed_seconds
            if last_activity_at is None
            else max(0.0, now - last_activity_at)
        )
        elapsed = format_duration(elapsed_seconds)
        inactivity = format_duration(inactivity_seconds)
        prefix = f"[{self._stage.value}]"
        if self._context:
            prefix = f"{prefix} {self._context} |"

            if inactivity_seconds >= self._stalled_after_seconds:
                return (
                    f"{prefix} STALL? [{frame}] {elapsed} | "
                    f"silent {inactivity} | Ctrl+C"
                )
            if last_activity_at is None:
                return f"{prefix} working [{frame}] {elapsed} | awaiting event"
            return f"{prefix} working [{frame}] {elapsed} | evt {inactivity} ago"

        if inactivity_seconds >= self._stalled_after_seconds:
            return (
                f"{prefix} POSSIBLY STALLED [{frame}] elapsed {elapsed} | "
                f"silent {inactivity} | Ctrl+C"
            )
        if last_activity_at is None:
            return (
                f"{prefix} Codex is working [{frame}] elapsed {elapsed} | "
                "waiting for first event"
            )
        return (
            f"{prefix} Codex is working [{frame}] elapsed {elapsed} | "
            f"last event {inactivity} ago"
        )

    def _clear(self) -> None:
        try:
            self._stream.write(f"\r{' ' * self._rendered_width}\r")
            self._stream.flush()
        except (OSError, ValueError):
            pass


def _stream(stream=None):
    return stream if stream is not None else sys.stdout


def _use_color(stream=None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    stream = _stream(stream)
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _can_encode(text: str, stream=None) -> bool:
    encoding = getattr(_stream(stream), "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def render_banner(stage: Stage, context: str = "", stream=None) -> str:
    unicode_ok = _can_encode("●○→·─", stream)
    active_marker = "●" if unicode_ok else "*"
    idle_marker = "○" if unicode_ok else "."
    arrow = " → " if unicode_ok else " -> "
    dot = " · " if unicode_ok else " - "
    rule_char = "─" if unicode_ok else "-"
    color = _use_color(stream)

    parts: list[str] = []
    for item in PIPELINE:
        marker = active_marker if item is stage else idle_marker
        label = f"{item.value} {marker}"
        if item is stage and color:
            label = f"{_ACTIVE_COLOR}{label}{_RESET}"
        parts.append(label)

    suffix = f"{dot}{context}" if context else ""
    line = f" devloop{dot}{arrow.join(parts)}{suffix} "
    rule = rule_char * _BANNER_WIDTH
    return f"{rule}\n{line}\n{rule}"


def stage_prompt(stage: Stage) -> str:
    return f"[{stage.value}] > "
