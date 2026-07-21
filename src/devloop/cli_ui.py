from __future__ import annotations

import shutil
from collections.abc import Sequence

from .lineeditor import display_width


def terminal_dimensions(*, fallback: tuple[int, int] = (100, 24)) -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=fallback)
    return max(40, size.columns), max(10, size.lines)


def render_context_path(*segments: str) -> str:
    """Render a breadcrumb-style path such as ``Future Runs > Security Review``."""
    return " > ".join(segment for segment in segments if segment)


def format_selected_step_line(
    index: int,
    display_name: str,
    *,
    selected: bool,
) -> str:
    if selected:
        return f"> {index}. {display_name}  (selected)"
    return f"  {index}. {display_name}"


def render_grouped_commands(
    groups: Sequence[tuple[str, Sequence[str]]],
    *,
    width: int,
    heading: str = "Available commands",
    max_lines: int | None = None,
) -> list[str]:
    lines: list[str] = [heading]
    for label, commands in groups:
        if not commands:
            continue
        prefix = f"  {label}: "
        current = prefix
        for command in commands:
            separator = "" if current.endswith(": ") else " | "
            candidate = f"{current}{separator}{command}"
            if display_width(candidate) <= width:
                current = candidate
                continue
            if current != prefix:
                lines.append(_fit_line(current, width))
            current = f"    {command}"
        if current:
            lines.append(_fit_line(current, width))
    if max_lines is not None and len(lines) > max_lines:
        hidden = len(lines) - max_lines + 1
        lines = [*lines[: max_lines - 1], f"  … {hidden} command groups hidden"]
    return lines


def expand_wrapped_lines(lines: Sequence[str], *, width: int) -> list[str]:
    wrapped: list[str] = []
    for line in lines:
        segments = _wrap_to_width(line, max(1, width))
        wrapped.extend(segments if segments else [""])
    return wrapped


def fit_text_to_screen(
    text: str,
    *,
    width: int,
    max_height: int,
    reserve_prompt: bool = True,
) -> str:
    budget = max(1, max_height - (1 if reserve_prompt else 0))
    wrapped = expand_wrapped_lines(text.splitlines(), width=max(1, width))
    if len(wrapped) <= budget:
        return "\n".join(wrapped)
    hidden = len(wrapped) - budget + 1
    truncated = wrapped[: budget - 1]
    truncated.append(
        _fit_line(
            f"… {hidden} lines hidden — type graph or widen terminal",
            max(1, width),
        )
    )
    return "\n".join(truncated)


def editor_prompt(selected_step_name: str | None) -> str:
    if selected_step_name:
        return f"workflow [{selected_step_name}]> "
    return "workflow> "


def menu_prompt(context: str, *, default: str) -> str:
    return f"{context} [{default}]: "


def _wrap_to_width(text: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = word if not current else f"{current} {word}"
        if display_width(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        while display_width(word) > width:
            prefix, word = _split_display_prefix(word, width)
            lines.append(prefix)
        current = word
    if current or not lines:
        lines.append(current)
    return lines


def _split_display_prefix(text: str, width: int) -> tuple[str, str]:
    used = 0
    split_at = 0
    for index, character in enumerate(text):
        character_width = display_width(character)
        if used + character_width > width:
            break
        used += character_width
        split_at = index + 1
    if split_at == 0:
        split_at = 1
    return text[:split_at], text[split_at:]


def _fit_line(text: str, width: int) -> str:
    if width < 1 or display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    kept: list[str] = []
    available = width - 1
    used = 0
    for character in text:
        character_width = display_width(character)
        if used + character_width > available:
            break
        kept.append(character)
        used += character_width
    return "".join(kept) + "…"
