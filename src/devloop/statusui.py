from __future__ import annotations

import os
import sys
from enum import Enum


class Stage(Enum):
    ANALYSIS = "analysis"
    DEVELOPMENT = "development"
    REVIEW = "review"
    QA = "qa"


PIPELINE = [Stage.ANALYSIS, Stage.DEVELOPMENT, Stage.REVIEW, Stage.QA]

_ACTIVE_COLOR = "\x1b[1;36m"
_RESET = "\x1b[0m"
_BANNER_WIDTH = 79


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
