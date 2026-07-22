from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Sequence

from .lineeditor import display_width
from .terminal_editor import prepare_terminal_output

APP_TITLE = "Dev Loop"

CAPABILITY_TOGGLE_COMMAND_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Actions", ("number toggle", "cancel abort")),
)

STARTUP_ACTION_BAR: tuple[tuple[str, str], ...] = (
    ("Up/Down", "Choose"),
    ("Enter", "Open"),
    ("Esc", "Exit"),
)
RESUME_ACTION_BAR: tuple[tuple[str, str], ...] = (
    ("Up/Down", "Choose"),
    ("Enter", "Resume"),
    ("Esc", "Back"),
)
CAPABILITY_ACTION_BAR: tuple[tuple[str, str], ...] = (
    ("Up/Down", "Choose"),
    ("Enter", "Open"),
    ("Esc", "Back"),
)

_ANSI_RESET = "\x1b[0m"
_ANSI_WINDOW = "\x1b[37;44m"
_ANSI_BORDER = "\x1b[1;36;44m"
_ANSI_HEADER = "\x1b[1;30;46m"
_ANSI_SELECTION = "\x1b[1;30;46m"


def terminal_dimensions(*, fallback: tuple[int, int] = (100, 24)) -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=fallback)
    return max(20, size.columns), max(10, size.lines)


def render_context_path(*segments: str) -> str:
    """Render a breadcrumb-style path such as ``Future Runs > Security Review``."""
    cleaned = [segment for segment in segments if segment]
    if not cleaned:
        return APP_TITLE
    return " > ".join((APP_TITLE, *cleaned))


def format_menu_entry(key: str, label: str, *, selected: bool = False) -> str:
    marker = ">" if selected else " "
    return f"{marker} {key}. {label}"


def format_selected_step_line(
    index: int,
    display_name: str,
    *,
    selected: bool,
) -> str:
    if selected:
        return f"> {index}. {display_name}"
    return f"  {index}. {display_name}"


def render_grouped_commands(
    groups: Sequence[tuple[str, Sequence[str]]],
    *,
    width: int,
    heading: str = "Commands",
    max_lines: int | None = None,
    inner: bool = False,
) -> list[str]:
    usable_width = _inner_content_width(width) if inner else width
    lines: list[str] = [heading]
    for label, commands in groups:
        if not commands:
            continue
        prefix = f"  {label}: "
        current = prefix
        for command in commands:
            separator = "" if current.endswith(": ") else " | "
            candidate = f"{current}{separator}{command}"
            if display_width(candidate) <= usable_width:
                current = candidate
                continue
            if current != prefix:
                lines.append(_fit_line(current, usable_width))
            current = f"    {command}"
        if current:
            lines.append(_fit_line(current, usable_width))
    if max_lines is not None and len(lines) > max_lines:
        hidden = len(lines) - max_lines + 1
        lines = [*lines[: max_lines - 1], f"  ... {hidden} command groups hidden"]
    return lines


def render_choice_menu(
    *,
    path: str,
    section_title: str,
    choices: Sequence[tuple[str, str]],
    description: Sequence[str] = (),
    footer: Sequence[tuple[str, str]] = (),
    command_groups: Sequence[tuple[str, Sequence[str]]] = (),
    action_bar: Sequence[tuple[str, str]] = (),
    selected_key: str | None = None,
    width: int,
    height: int,
) -> str:
    raw_description_lines = expand_wrapped_lines(
        description,
        width=_inner_content_width(max(20, width)),
    )
    action_lines = len(render_action_bar(action_bar, width=max(1, width - 2)))
    screen_budget = max(1, height - 1)
    frame_lines = 2 + (1 + action_lines if action_bar else 0)
    body_budget = max(0, screen_budget - frame_lines)
    footer_block = 1 + len(footer) if footer else 0
    description_budget = max(0, body_budget - 4 - footer_block)
    description_lines = _fit_body_lines(
        raw_description_lines,
        description_budget,
    )
    visible_choices, range_label = _visible_choice_window(
        choices,
        selected_key=selected_key,
        height=height,
        footer_count=len(footer),
        action_bar=action_bar,
        width=width,
        description_count=len(description_lines),
    )
    title = f"{section_title}  ({range_label})" if range_label else section_title
    body = [title]
    if description_lines:
        body.extend(("", *description_lines))
    body.append("")
    body.extend(
        format_menu_entry(key, label, selected=key == selected_key)
        for key, label in visible_choices
    )
    if footer:
        body.append("")
        body.extend(
            format_menu_entry(key, label, selected=key == selected_key)
            for key, label in footer
        )
    return render_screen_frame(
        path=path,
        body=body,
        command_groups=command_groups,
        action_bar=action_bar,
        width=width,
        height=height,
    )


def render_screen_frame(
    *,
    path: str,
    body: Sequence[str],
    command_groups: Sequence[tuple[str, Sequence[str]]] = (),
    action_bar: Sequence[tuple[str, str]] = (),
    width: int,
    height: int,
    reserve_prompt: bool = True,
    unicode_ok: bool | None = None,
    color_ok: bool | None = None,
    command_heading: str = "Commands",
    fill_height: bool = True,
) -> str:
    width = max(20, width)
    height = max(10, height)
    if unicode_ok is None:
        unicode_ok = _supports_box_drawing()
    if color_ok is None:
        color_ok = _supports_color()
    chars = _frame_chars(unicode_ok)
    inner_width = _inner_content_width(width)
    wrapped_body = expand_wrapped_lines(body, width=inner_width)

    footer_label = "Shortcuts"
    footer_lines: list[str] = []
    if action_bar:
        footer_lines = render_action_bar(action_bar, width=inner_width)
    elif command_groups:
        command_budget = max(3, min(8, height // 4))
        footer_label = command_heading
        footer_lines = render_grouped_commands(
            command_groups,
            width=width,
            heading=command_heading,
            inner=True,
            max_lines=command_budget,
        )

    screen_budget = max(1, height - (1 if reserve_prompt else 0))
    fixed_lines = 2 + (1 + len(footer_lines) if footer_lines else 0)
    body_budget = max(0, screen_budget - fixed_lines)
    wrapped_body = _fit_body_lines(wrapped_body, body_budget)
    if fill_height and len(wrapped_body) < body_budget:
        wrapped_body.extend("" for _ in range(body_budget - len(wrapped_body)))

    frame_lines: list[str] = [_top_rule(path, width, chars)]
    frame_lines.extend(_border_line(line, width, chars) for line in wrapped_body)
    footer_start: int | None = None
    if footer_lines:
        footer_start = len(frame_lines)
        frame_lines.append(_section_rule(footer_label, width, chars))
        frame_lines.extend(_border_line(line, width, chars) for line in footer_lines)
    frame_lines.append(_bottom_rule(width, chars))
    return "\n".join(
        _apply_frame_theme(
            frame_lines,
            color_ok=color_ok,
            footer_start=footer_start,
        )
    )


def render_action_bar(
    actions: Sequence[tuple[str, str]],
    *,
    width: int,
) -> list[str]:
    tokens = [f" {key} {label} " for key, label in actions]
    lines: list[str] = []
    current = ""
    for token in tokens:
        separator = "|" if current else ""
        candidate = f"{current}{separator}{token}"
        if display_width(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(_fit_line(current, width))
        current = token
    if current:
        lines.append(_fit_line(current, width))
    return lines or [""]


def render_split_panes(
    *,
    left_title: str,
    left_lines: Sequence[str],
    right_title: str,
    right_lines: Sequence[str],
    width: int,
    height: int,
    unicode_ok: bool | None = None,
) -> list[str]:
    """Render a Midnight Commander-style pair of bounded content panes."""
    width = max(20, width)
    height = max(4, height)
    if unicode_ok is None:
        unicode_ok = _supports_box_drawing()
    chars = _pane_chars(unicode_ok)
    left_inner = max(14, min(40, (width - 3) // 3))
    right_inner = max(1, width - left_inner - 3)
    left_content = expand_wrapped_lines(left_lines, width=left_inner)
    right_content = expand_wrapped_lines(right_lines, width=right_inner)
    row_count = height - 2
    left_content = _fit_body_lines(left_content, row_count)
    right_content = _fit_body_lines(right_content, row_count)
    left_content.extend("" for _ in range(row_count - len(left_content)))
    right_content.extend("" for _ in range(row_count - len(right_content)))

    lines = [
        _pane_top_rule(
            left_title,
            right_title,
            left_inner,
            right_inner,
            chars,
        )
    ]
    for left, right in zip(left_content, right_content):
        lines.append(
            chars["v"]
            + _pad_line(f" {left}" if left else "", left_inner)
            + chars["v"]
            + _pad_line(f" {right}" if right else "", right_inner)
            + chars["v"]
        )
    lines.append(
        chars["bl"]
        + chars["h"] * left_inner
        + chars["bm"]
        + chars["h"] * right_inner
        + chars["br"]
    )
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
            f"... {hidden} lines hidden - widen terminal to see more",
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


def _inner_content_width(width: int) -> int:
    return max(1, width - 2)


def _frame_chars(unicode_ok: bool) -> dict[str, str]:
    if unicode_ok:
        return {
            "tl": "┌",
            "tr": "┐",
            "bl": "└",
            "br": "┘",
            "h": "─",
            "v": "│",
            "lm": "├",
            "rm": "┤",
        }
    return {
        "tl": "+",
        "tr": "+",
        "bl": "+",
        "br": "+",
        "h": "-",
        "v": "|",
        "lm": "+",
        "rm": "+",
    }


def _pane_chars(unicode_ok: bool) -> dict[str, str]:
    if unicode_ok:
        return {
            "tl": "┌",
            "tm": "┬",
            "tr": "┐",
            "bl": "└",
            "bm": "┴",
            "br": "┘",
            "h": "─",
            "v": "│",
        }
    return {
        "tl": "+",
        "tm": "+",
        "tr": "+",
        "bl": "+",
        "bm": "+",
        "br": "+",
        "h": "-",
        "v": "|",
    }


def _supports_box_drawing(stream=None) -> bool:
    target = stream or sys.stdout
    encoding = getattr(target, "encoding", None) or "utf-8"
    try:
        "┌┐└┘│─├┤".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _supports_color(stream=None) -> bool:
    target = stream or sys.stdout
    isatty = getattr(target, "isatty", None)
    return bool(
        isatty
        and isatty()
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "").casefold() != "dumb"
        and prepare_terminal_output()
    )


def _top_rule(title: str, width: int, chars: dict[str, str]) -> str:
    label = _fit_line(title, max(1, width - 5))
    prefix = f"{chars['tl']}{chars['h']} {label} "
    remaining = width - display_width(prefix) - 1
    return prefix + chars["h"] * max(0, remaining) + chars["tr"]


def _section_rule(label: str, width: int, chars: dict[str, str]) -> str:
    prefix = f"{chars['lm']}{chars['h']} {label} "
    remaining = width - display_width(prefix) - 1
    return prefix + chars["h"] * max(0, remaining) + chars["rm"]


def _bottom_rule(width: int, chars: dict[str, str]) -> str:
    return chars["bl"] + chars["h"] * max(0, width - 2) + chars["br"]


def _border_line(content: str, width: int, chars: dict[str, str]) -> str:
    inner = _inner_content_width(width)
    fitted = _fit_line(content, inner)
    padding = max(0, inner - display_width(fitted))
    return chars["v"] + fitted + (" " * padding) + chars["v"]


def _pane_top_rule(
    left_title: str,
    right_title: str,
    left_inner: int,
    right_inner: int,
    chars: dict[str, str],
) -> str:
    left = _titled_rule(left_title, left_inner, chars["h"])
    right = _titled_rule(right_title, right_inner, chars["h"])
    return chars["tl"] + left + chars["tm"] + right + chars["tr"]


def _titled_rule(title: str, width: int, horizontal: str) -> str:
    label = _fit_line(title, max(1, width - 3))
    prefix = f" {label} "
    return prefix + horizontal * max(0, width - display_width(prefix))


def _pad_line(text: str, width: int) -> str:
    fitted = _fit_line(text, width)
    return fitted + " " * max(0, width - display_width(fitted))


def _fit_body_lines(lines: Sequence[str], budget: int) -> list[str]:
    if budget <= 0:
        return []
    if len(lines) <= budget:
        return list(lines)
    hidden = len(lines) - budget + 1
    return [
        *lines[: budget - 1],
        _fit_line(f"... {hidden} more items", max(map(display_width, lines), default=1)),
    ]


def _visible_choice_window(
    choices: Sequence[tuple[str, str]],
    *,
    selected_key: str | None,
    height: int,
    footer_count: int,
    action_bar: Sequence[tuple[str, str]],
    width: int,
    description_count: int = 0,
) -> tuple[Sequence[tuple[str, str]], str | None]:
    action_lines = len(render_action_bar(action_bar, width=max(1, width - 2)))
    screen_budget = max(1, height - 1)
    frame_lines = 2 + (1 + action_lines if action_bar else 0)
    body_budget = max(0, screen_budget - frame_lines)
    description_block = 1 + description_count if description_count else 0
    footer_block = 1 + footer_count if footer_count else 0
    visible_count = max(
        1,
        body_budget - 2 - description_block - footer_block,
    )
    if len(choices) <= visible_count:
        return choices, None
    selected = next(
        (index for index, (key, _label) in enumerate(choices) if key == selected_key),
        0,
    )
    start = min(
        max(0, selected - visible_count // 2),
        len(choices) - visible_count,
    )
    end = start + visible_count
    return choices[start:end], f"items {start + 1}-{end} of {len(choices)}"


def _apply_frame_theme(
    lines: Sequence[str],
    *,
    color_ok: bool,
    footer_start: int | None,
) -> list[str]:
    if not color_ok:
        return list(lines)
    themed: list[str] = []
    last_index = len(lines) - 1
    for index, line in enumerate(lines):
        if index == 0:
            style = _ANSI_HEADER
        elif footer_start is not None and index > footer_start and index < last_index:
            style = _ANSI_HEADER
        elif "> " in line:
            style = _ANSI_SELECTION
        elif index == last_index or (footer_start is not None and index == footer_start):
            style = _ANSI_BORDER
        else:
            style = _ANSI_WINDOW
        themed.append(f"{style}{line}{_ANSI_RESET}")
    return themed


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
    if width <= 3:
        return "." * width
    kept: list[str] = []
    available = width - 3
    used = 0
    for character in text:
        character_width = display_width(character)
        if used + character_width > available:
            break
        kept.append(character)
        used += character_width
    return "".join(kept) + "..."
