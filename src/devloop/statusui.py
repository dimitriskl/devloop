from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, TextIO

from .terminal_editor import display_width


class Stage(Enum):
    ANALYSIS = "analysis"
    DEVELOPMENT = "development"
    REVIEW = "review"
    QA = "qa"


class DashboardStatus(Enum):
    WAITING = "WAITING"
    WORKING = "WORKING"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


PIPELINE = [Stage.ANALYSIS, Stage.DEVELOPMENT, Stage.REVIEW, Stage.QA]

_ACTIVE_COLOR = "\x1b[1;36m"
_PASS_COLOR = "\x1b[1;32m"
_FAIL_COLOR = "\x1b[1;31m"
_WORKING_COLOR = "\x1b[1;33m"
_RESET = "\x1b[0m"
_BANNER_WIDTH = 79
WAITING_FRAMES = ("|", "/", "-", "\\")
UNICODE_WAITING_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
WAITING_FRAME_SECONDS = 0.12
WAITING_STALL_SECONDS = 120.0
_ERASE_LINE = "\x1b[2K"
_CARRIAGE_RETURN = "\r"
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DELIVERY_STAGES = (Stage.DEVELOPMENT, Stage.REVIEW, Stage.QA)
_STATUS_FIELD_WIDTH = max(len(status.value) for status in DashboardStatus) + 2
_STAGE_FIELD_WIDTH = max(len(stage.value) for stage in _DELIVERY_STAGES)


@dataclass(frozen=True)
class IssueResultSummary:
    issue_number: str
    status: DashboardStatus
    pass_number: int
    elapsed_seconds: float


@dataclass(frozen=True)
class IssueDashboardSnapshot:
    issue_number: str
    issue_title: str
    position: int
    total: int
    pass_number: int
    active_stage: Stage
    statuses: Mapping[Stage, DashboardStatus] = field(default_factory=dict)
    stage_durations: Mapping[Stage, float] = field(default_factory=dict)
    last_result: IssueResultSummary | None = None
    elapsed_seconds: float = 0.0
    inactivity_seconds: float = 0.0
    activity: str = "Waiting for the first Codex update."


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{remaining_seconds:02}"


def render_issue_dashboard(
    snapshot: IssueDashboardSnapshot,
    *,
    width: int,
    color: bool,
    unicode: bool,
    frame: str,
) -> str:
    safe_width = max(1, width)
    rule_character = "─" if unicode else "-"
    separator = " · " if unicode else " - "
    remaining = max(0, snapshot.total - snapshot.position)
    header = separator.join(
        (
            "CURRENT ISSUE",
            snapshot.issue_number,
            f"{snapshot.position}/{snapshot.total}",
            f"{remaining} remaining",
        )
    )
    activity_prefix = "AI › " if unicode else "AI > "
    event_age = format_duration(snapshot.inactivity_seconds)
    live_status = snapshot.statuses.get(
        snapshot.active_stage,
        DashboardStatus.WORKING,
    )
    live_elapsed_seconds = _stage_elapsed_seconds(
        snapshot,
        snapshot.active_stage,
    )
    live_line = separator.join(
        (
            snapshot.active_stage.value.upper(),
            snapshot.issue_number,
            f"pass {snapshot.pass_number}",
        )
    )
    live_line = (
        f"{live_line}    {live_status.value} {frame}    "
        f"{format_duration(live_elapsed_seconds)}    event {event_age} ago"
    )

    plain_lines: list[str] = []
    colored_statuses: list[tuple[int, DashboardStatus]] = []
    if snapshot.last_result is not None:
        last_result = snapshot.last_result
        plain_lines.append(
            _fit_plain_text(
                separator.join(
                    (
                        "LAST RESULT",
                        last_result.issue_number,
                        last_result.status.value,
                        f"pass {last_result.pass_number}",
                        f"total {format_duration(last_result.elapsed_seconds)}",
                    )
                ),
                safe_width,
                unicode=unicode,
            )
        )
        colored_statuses.append((len(plain_lines) - 1, last_result.status))
    plain_lines.extend(
        (
            rule_character * safe_width,
            _fit_plain_text(header, safe_width, unicode=unicode),
            _fit_plain_text(snapshot.issue_title, safe_width, unicode=unicode),
            "",
        )
    )
    for stage in _DELIVERY_STAGES:
        status = snapshot.statuses.get(stage, DashboardStatus.WAITING)
        elapsed = format_duration(_stage_elapsed_seconds(snapshot, stage))
        plain_lines.append(
            _fit_plain_text(
                (
                    f"{status.value:<{_STATUS_FIELD_WIDTH}} "
                    f"{stage.value.upper():<{_STAGE_FIELD_WIDTH}}"
                    f"{separator}pass {snapshot.pass_number}"
                    f"{separator}{elapsed}"
                ),
                safe_width,
                unicode=unicode,
            )
        )
        colored_statuses.append((len(plain_lines) - 1, status))
    live_line_index = len(plain_lines) + 1
    plain_lines.extend(
        (
            rule_character * safe_width,
            _fit_plain_text(live_line, safe_width, unicode=unicode),
            _fit_plain_text(
                f"{activity_prefix}{snapshot.activity}",
                safe_width,
                unicode=unicode,
            ),
            rule_character * safe_width,
        )
    )

    if not color:
        return "\n".join(plain_lines)

    colored_lines = list(plain_lines)
    for index, status in colored_statuses:
        colored_lines[index] = _color_status_word(colored_lines[index], status)
    colored_lines[live_line_index] = _color_status_word(
        colored_lines[live_line_index],
        live_status,
    )
    return "\n".join(colored_lines)


def _stage_elapsed_seconds(
    snapshot: IssueDashboardSnapshot,
    stage: Stage,
) -> float:
    status = snapshot.statuses.get(stage, DashboardStatus.WAITING)
    if stage is snapshot.active_stage and status is DashboardStatus.WORKING:
        return max(
            0.0,
            snapshot.stage_durations.get(stage, 0.0) + snapshot.elapsed_seconds,
        )
    return max(0.0, snapshot.stage_durations.get(stage, 0.0))


def _fit_plain_text(text: str, width: int, *, unicode: bool) -> str:
    if display_width(text) <= width:
        return text
    ellipsis = "…" if unicode else "..."
    ellipsis_width = display_width(ellipsis)
    if width <= ellipsis_width:
        return ellipsis[:width]
    target_width = width - ellipsis_width
    characters: list[str] = []
    current_width = 0
    for character in text:
        character_width = display_width(character)
        if current_width + character_width > target_width:
            break
        characters.append(character)
        current_width += character_width
    return f"{''.join(characters)}{ellipsis}"


def _color_status_word(text: str, status: DashboardStatus) -> str:
    color = {
        DashboardStatus.PASS: _PASS_COLOR,
        DashboardStatus.FAIL: _FAIL_COLOR,
        DashboardStatus.BLOCKED: _FAIL_COLOR,
        DashboardStatus.WORKING: _WORKING_COLOR,
    }.get(status)
    if color is None or status.value not in text:
        return text
    return text.replace(status.value, f"{color}{status.value}{_RESET}", 1)


def render_status(status: str | DashboardStatus, stream=None) -> str:
    parsed = (
        status
        if isinstance(status, DashboardStatus)
        else DashboardStatus(status.upper())
    )
    if not _use_color(stream):
        return parsed.value
    return _color_status_word(parsed.value, parsed)


def _terminal_display_width(text: str) -> int:
    return display_width(_ANSI_ESCAPE_PATTERN.sub("", text))


class IssueDashboard:
    """Maintain one small in-place dashboard for the current delivery Issue."""

    def __init__(
        self,
        *,
        issue_number: str,
        issue_title: str,
        position: int,
        total: int,
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
        frame_seconds: float = WAITING_FRAME_SECONDS,
        terminal_size: Callable[..., os.terminal_size] = shutil.get_terminal_size,
    ) -> None:
        self._issue_number = issue_number
        self._issue_title = " ".join(issue_title.split())
        self._position = position
        self._total = total
        self._stream = sys.stdout if stream is None else stream
        self._clock = clock
        self._frame_seconds = frame_seconds
        self._terminal_size = terminal_size
        isatty = getattr(self._stream, "isatty", None)
        self._enabled = bool(callable(isatty) and isatty())
        self._unicode = _can_encode("─⠋›", self._stream)
        self._statuses = {
            Stage.DEVELOPMENT: DashboardStatus.WAITING,
            Stage.REVIEW: DashboardStatus.WAITING,
            Stage.QA: DashboardStatus.WAITING,
        }
        self._stage_durations = {
            Stage.DEVELOPMENT: 0.0,
            Stage.REVIEW: 0.0,
            Stage.QA: 0.0,
        }
        self._visible_last_result: IssueResultSummary | None = None
        self._pending_last_result: IssueResultSummary | None = None
        self._active_stage = Stage.DEVELOPMENT
        self._pass_number = 1
        self._activity = "Waiting for the first Codex update."
        self._started_at = self._clock()
        self._last_activity_at: float | None = None
        self._frame_index = 0
        self._rendered_lines = 0
        self._opened = False
        self._lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def begin_role(self, stage: Stage, pass_number: int) -> None:
        if stage is Stage.ANALYSIS:
            raise ValueError("The Issue dashboard supports delivery Workflow Steps only.")
        with self._lock:
            self._active_stage = stage
            self._pass_number = pass_number
            self._started_at = self._clock()
            self._last_activity_at = None
            self._activity = "Waiting for the first Codex update."
            active_index = PIPELINE.index(stage)
            for candidate in _DELIVERY_STAGES:
                if candidate is stage:
                    self._statuses[candidate] = DashboardStatus.WORKING
                elif PIPELINE.index(candidate) > active_index:
                    self._statuses[candidate] = DashboardStatus.WAITING
            self._render_locked()
        self._start_animation()

    def show_issue(
        self,
        *,
        issue_number: str,
        issue_title: str,
        position: int,
        total: int,
    ) -> None:
        self._stop_animation()
        with self._lock:
            self._visible_last_result = self._pending_last_result
            self._issue_number = issue_number
            self._issue_title = " ".join(issue_title.split())
            self._position = position
            self._total = total
            for stage in _DELIVERY_STAGES:
                self._statuses[stage] = DashboardStatus.WAITING
                self._stage_durations[stage] = 0.0
            self._active_stage = Stage.DEVELOPMENT
            self._pass_number = 1
            self._activity = "Waiting for the first Codex update."
            self._started_at = self._clock()
            self._last_activity_at = None

    def finish_issue(self, status: str, activity: str = "") -> None:
        self._stop_animation()
        parsed_status = DashboardStatus(status.upper())
        with self._lock:
            if activity:
                self._activity = " ".join(activity.split())
                self._last_activity_at = self._clock()
            self._pending_last_result = IssueResultSummary(
                issue_number=self._issue_number,
                status=parsed_status,
                pass_number=self._pass_number,
                elapsed_seconds=sum(self._stage_durations.values()),
            )
            self._render_locked()

    def restore_role(self, stage: Stage, status: str) -> None:
        if stage is Stage.ANALYSIS:
            raise ValueError("The Issue dashboard supports delivery Workflow Steps only.")
        with self._lock:
            self._statuses[stage] = DashboardStatus(status.upper())

    def notify_activity(self, activity: str | None = None) -> None:
        if not self._enabled:
            return
        with self._lock:
            self._last_activity_at = self._clock()
            if activity:
                normalized = " ".join(activity.split())
                self._activity = normalized.removeprefix("Codex update: ")
                self._render_locked()

    def finish_role(self, stage: Stage, status: str, summary: str = "") -> None:
        parsed_status = DashboardStatus(status.upper())
        with self._lock:
            now = self._clock()
            self._active_stage = stage
            self._statuses[stage] = parsed_status
            self._stage_durations[stage] += max(0.0, now - self._started_at)
            self._last_activity_at = now
            if summary:
                self._activity = " ".join(summary.split())
            else:
                self._activity = f"{stage.value.title()} finished: {parsed_status.value}."
            self._render_locked()

    def close(self, activity: str | None = None) -> None:
        self._stop_animation()
        if not self._enabled or not self._opened:
            return
        with self._lock:
            if activity:
                self._activity = " ".join(activity.split())
                self._last_activity_at = self._clock()
            self._render_locked()
            try:
                self._stream.write("\n")
                self._stream.flush()
            except (OSError, ValueError):
                self._enabled = False
            self._opened = False
            self._rendered_lines = 0

    def _start_animation(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def _stop_animation(self) -> None:
        self._stop_requested.set()
        thread = self._thread
        if thread is not None:
            thread.join()
        self._thread = None

    def _animate(self) -> None:
        while not self._stop_requested.wait(self._frame_seconds):
            with self._lock:
                self._frame_index += 1
                self._render_locked()

    def _render_locked(self) -> None:
        if not self._enabled:
            return
        now = self._clock()
        elapsed_seconds = max(0.0, now - self._started_at)
        inactivity_seconds = (
            elapsed_seconds
            if self._last_activity_at is None
            else max(0.0, now - self._last_activity_at)
        )
        frames = UNICODE_WAITING_FRAMES if self._unicode else WAITING_FRAMES
        frame = frames[self._frame_index % len(frames)]
        columns = self._terminal_size(fallback=(80, 24)).columns
        width = max(1, columns - 1)
        rendered = render_issue_dashboard(
            IssueDashboardSnapshot(
                issue_number=self._issue_number,
                issue_title=self._issue_title,
                position=self._position,
                total=self._total,
                pass_number=self._pass_number,
                active_stage=self._active_stage,
                statuses=dict(self._statuses),
                stage_durations=dict(self._stage_durations),
                last_result=self._visible_last_result,
                elapsed_seconds=elapsed_seconds,
                inactivity_seconds=inactivity_seconds,
                activity=self._activity,
            ),
            width=width,
            color=_use_color(self._stream),
            unicode=self._unicode,
            frame=frame,
        )
        lines = rendered.splitlines()
        try:
            if self._opened and self._rendered_lines > 1:
                self._stream.write(
                    f"\x1b[{self._rendered_lines - 1}A{_CARRIAGE_RETURN}"
                )
            for index, line in enumerate(lines):
                self._stream.write(f"{_ERASE_LINE}{_CARRIAGE_RETURN}{line}")
                if index < len(lines) - 1:
                    self._stream.write("\n")
            self._stream.flush()
        except (OSError, ValueError):
            self._enabled = False
            return
        self._rendered_lines = len(lines)
        self._opened = True


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
            status_width = _terminal_display_width(status_line)
            padding = " " * max(0, self._rendered_width - status_width)
            try:
                self._stream.write(f"\r{status_line}{padding}")
                self._stream.flush()
            except (OSError, ValueError):
                return
            self._rendered_width = max(self._rendered_width, status_width)
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
        working = render_status(DashboardStatus.WORKING, self._stream)
        if self._context:
            prefix = f"{prefix} {self._context} |"

            if inactivity_seconds >= self._stalled_after_seconds:
                return (
                    f"{prefix} STALL? [{frame}] {elapsed} | "
                    f"silent {inactivity} | Ctrl+C"
                )
            if last_activity_at is None:
                return f"{prefix} {working} [{frame}] {elapsed} | awaiting event"
            return f"{prefix} {working} [{frame}] {elapsed} | evt {inactivity} ago"

        if inactivity_seconds >= self._stalled_after_seconds:
            return (
                f"{prefix} POSSIBLY STALLED [{frame}] elapsed {elapsed} | "
                f"silent {inactivity} | Ctrl+C"
            )
        if last_activity_at is None:
            return (
                f"{prefix} Codex is {working} [{frame}] elapsed {elapsed} | "
                "waiting for first event"
            )
        return (
            f"{prefix} Codex is {working} [{frame}] elapsed {elapsed} | "
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
