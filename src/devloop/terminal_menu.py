from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from .cli_ui import (
    render_choice_menu,
    render_context_path,
    terminal_dimensions,
)
from .terminal_editor import TerminalKeySource, open_key_source, prepare_terminal_output


class NavigationKey(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    HOME = "HOME"
    END = "END"
    ENTER = "ENTER"
    ESCAPE = "ESCAPE"
    F1 = "F1"
    F2 = "F2"
    F3 = "F3"
    F4 = "F4"
    F5 = "F5"
    F6 = "F6"
    F7 = "F7"
    F8 = "F8"
    F9 = "F9"
    F10 = "F10"
    TEXT = "TEXT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class KeyEvent:
    key: NavigationKey
    text: str = ""


@dataclass(frozen=True)
class MenuAction:
    group: str
    label: str
    command: str


RenderSelection = Callable[[str], None]
FallbackChoice = Callable[[], str]
FallbackCommand = Callable[[str], str]

_WORKFLOW_KEY_COMMANDS = {
    NavigationKey.UP: "__previous_step__",
    NavigationKey.DOWN: "__next_step__",
    NavigationKey.F1: "help",
    NavigationKey.F2: "apply",
    NavigationKey.F3: "graph",
    NavigationKey.F4: "advanced",
    NavigationKey.F5: "add",
    NavigationKey.F6: "position",
    NavigationKey.F7: "capabilities",
    NavigationKey.F8: "delete",
    NavigationKey.ESCAPE: "cancel",
}


def clear_terminal_screen() -> None:
    from .portable_runtime import portable_plain_mode_active

    if portable_plain_mode_active():
        return
    if sys.stdout.isatty() and prepare_terminal_output():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def render_app_screen(content: str) -> None:
    from .portable_runtime import active_portable_runtime, portable_plain_mode_active

    portable_runtime = active_portable_runtime()
    if portable_runtime is not None:
        portable_runtime.show_screen(content)
        return
    if portable_plain_mode_active():
        if content:
            print(content)
        return
    clear_terminal_screen()
    if content:
        print(content)


def render_menu_screen(*lines: str) -> None:
    render_app_screen("\n".join(lines))


def choose_menu_option(
    options: Sequence[tuple[str, str]],
    *,
    default_key: str,
    render: RenderSelection,
    fallback: FallbackChoice,
    cancel_key: str | None = None,
) -> str:
    """Choose from a menu with arrows on a TTY and line input everywhere else."""
    from .portable_runtime import active_portable_runtime, portable_plain_mode_active

    portable_runtime = active_portable_runtime()
    if portable_runtime is not None:
        return portable_runtime.choose(
            options,
            default_key=default_key,
            cancel_key=cancel_key,
            render=render,
        )
    if portable_plain_mode_active():
        render(default_key)
        return fallback()
    keys = _open_navigation_source()
    if keys is None:
        render(default_key)
        return fallback()
    _set_cursor_visible(False)
    selected = next(
        (index for index, (key, _label) in enumerate(options) if key == default_key),
        0,
    )
    try:
        while True:
            render(options[selected][0])
            event = read_navigation_key(keys)
            if event.key is NavigationKey.UP:
                selected = (selected - 1) % len(options)
            elif event.key is NavigationKey.DOWN:
                selected = (selected + 1) % len(options)
            elif event.key is NavigationKey.HOME:
                selected = 0
            elif event.key is NavigationKey.END:
                selected = len(options) - 1
            elif event.key is NavigationKey.ENTER:
                return options[selected][0]
            elif event.key is NavigationKey.ESCAPE and cancel_key is not None:
                return cancel_key
            elif event.key is NavigationKey.TEXT:
                matched = next(
                    (
                        key
                        for key, _label in options
                        if key.casefold() == event.text.casefold()
                    ),
                    None,
                )
                if matched is not None:
                    return matched
    finally:
        keys.close()
        _set_cursor_visible(True)


def read_workflow_command(
    prompt: str,
    *,
    fallback: FallbackCommand,
    actions: Sequence[MenuAction],
) -> str:
    """Read one workflow action using navigation keys or the legacy command line."""
    from .portable_runtime import active_portable_runtime, portable_plain_mode_active

    portable_runtime = active_portable_runtime()
    if portable_runtime is not None:
        workflow_options = (
            ("__previous_step__", "Previous workflow step"),
            ("__next_step__", "Next workflow step"),
            *((action.command, f"{action.group} · {action.label}") for action in actions),
        )
        return portable_runtime.choose(
            workflow_options,
            default_key=workflow_options[0][0],
            cancel_key="cancel",
            render=lambda _selected: None,
        )
    if portable_plain_mode_active():
        return fallback(prompt)
    keys = _open_navigation_source()
    if keys is None:
        return fallback(prompt)
    _set_cursor_visible(False)
    try:
        event = read_navigation_key(keys)
        direct_command = _WORKFLOW_KEY_COMMANDS.get(event.key)
        if direct_command is not None:
            return direct_command
        if event.key in {NavigationKey.ENTER, NavigationKey.F9}:
            return _choose_action(keys, actions)
        if event.key is NavigationKey.TEXT:
            if event.text.isdecimal():
                return event.text
            if event.text == "/":
                return _choose_action(keys, actions)
            return _choose_action(keys, actions)
        return ""
    finally:
        keys.close()
        _set_cursor_visible(True)


def read_navigation_key(source: TerminalKeySource) -> KeyEvent:
    char = source.read()
    if char is None:
        raise EOFError
    if char in {"\r", "\n"}:
        return KeyEvent(NavigationKey.ENTER)
    if char in {"\x00", "\xe0"}:
        return _windows_key_event(source.read())
    if char == "\x1b":
        return _escape_key_event(source)
    if char.isprintable():
        return KeyEvent(NavigationKey.TEXT, char)
    return KeyEvent(NavigationKey.UNKNOWN)


def _windows_key_event(code: str | None) -> KeyEvent:
    mapping = {
        "H": NavigationKey.UP,
        "P": NavigationKey.DOWN,
        "G": NavigationKey.HOME,
        "O": NavigationKey.END,
        ";": NavigationKey.F1,
        "<": NavigationKey.F2,
        "=": NavigationKey.F3,
        ">": NavigationKey.F4,
        "?": NavigationKey.F5,
        "@": NavigationKey.F6,
        "A": NavigationKey.F7,
        "B": NavigationKey.F8,
        "C": NavigationKey.F9,
        "D": NavigationKey.F10,
    }
    return KeyEvent(mapping.get(code, NavigationKey.UNKNOWN))


def _escape_key_event(source: TerminalKeySource) -> KeyEvent:
    follow = source.read_pending()
    if follow is None:
        return KeyEvent(NavigationKey.ESCAPE)
    if follow == "O":
        return KeyEvent(
            {
                "P": NavigationKey.F1,
                "Q": NavigationKey.F2,
                "R": NavigationKey.F3,
                "S": NavigationKey.F4,
                "H": NavigationKey.HOME,
                "F": NavigationKey.END,
            }.get(source.read(), NavigationKey.UNKNOWN)
        )
    if follow != "[":
        return KeyEvent(NavigationKey.UNKNOWN)
    sequence = ""
    while True:
        final = source.read()
        if final is None:
            return KeyEvent(NavigationKey.UNKNOWN)
        if final.isalpha() or final == "~":
            break
        sequence += final
    if not sequence:
        return KeyEvent(
            {
                "A": NavigationKey.UP,
                "B": NavigationKey.DOWN,
                "H": NavigationKey.HOME,
                "F": NavigationKey.END,
            }.get(final, NavigationKey.UNKNOWN)
        )
    function_keys = {
        "11": NavigationKey.F1,
        "12": NavigationKey.F2,
        "13": NavigationKey.F3,
        "14": NavigationKey.F4,
        "15": NavigationKey.F5,
        "17": NavigationKey.F6,
        "18": NavigationKey.F7,
        "19": NavigationKey.F8,
        "20": NavigationKey.F9,
        "21": NavigationKey.F10,
    }
    return KeyEvent(function_keys.get(sequence, NavigationKey.UNKNOWN))


def _open_navigation_source() -> TerminalKeySource | None:
    from .portable_runtime import portable_plain_mode_active

    if portable_plain_mode_active():
        return None
    stdout_isatty = getattr(sys.stdout, "isatty", None)
    if not (stdout_isatty and stdout_isatty()):
        return None
    return open_key_source()


def _set_cursor_visible(visible: bool) -> None:
    if not sys.stdout.isatty() or not prepare_terminal_output():
        return
    sys.stdout.write("\x1b[?25h" if visible else "\x1b[?25l")
    sys.stdout.flush()


def _choose_action(
    keys: TerminalKeySource,
    actions: Sequence[MenuAction],
) -> str:
    if not actions:
        return ""
    groups = tuple(dict.fromkeys(action.group for action in actions))
    selected = 0
    active_group: str | None = None
    while True:
        width, height = terminal_dimensions()
        if active_group is None:
            visible_actions: Sequence[MenuAction] = ()
            choices = tuple(
                (
                    str(index + 1),
                    f"{group} ({sum(action.group == group for action in actions)})",
                )
                for index, group in enumerate(groups)
            )
            path = render_context_path("Workflow Editor", "Actions")
            title = "Choose an action group"
            back_label = "Back to Workflow Editor"
        else:
            visible_actions = tuple(
                action for action in actions if action.group == active_group
            )
            choices = tuple(
                (str(index + 1), action.label)
                for index, action in enumerate(visible_actions)
            )
            path = render_context_path("Workflow Editor", "Actions", active_group)
            title = active_group
            back_label = "Back to action groups"
        selected_key = "b" if selected == len(choices) else str(selected + 1)
        render_app_screen(
            render_choice_menu(
                path=path,
                section_title=title,
                choices=choices,
                footer=(("b", back_label),),
                selected_key=selected_key,
                action_bar=(("Up/Down", "Choose"), ("Enter", "Open"), ("Esc", "Back")),
                width=width,
                height=height,
            )
        )
        event = read_navigation_key(keys)
        if event.key is NavigationKey.UP:
            selected = (selected - 1) % (len(choices) + 1)
        elif event.key is NavigationKey.DOWN:
            selected = (selected + 1) % (len(choices) + 1)
        elif event.key is NavigationKey.HOME:
            selected = 0
        elif event.key is NavigationKey.END:
            selected = len(choices)
        elif event.key is NavigationKey.ENTER:
            if selected == len(choices):
                if active_group is None:
                    return ""
                active_group = None
                selected = 0
            elif active_group is None:
                active_group = groups[selected]
                selected = 0
            else:
                return visible_actions[selected].command
        elif event.key is NavigationKey.ESCAPE:
            if active_group is None:
                return ""
            active_group = None
            selected = 0
        elif event.key is NavigationKey.TEXT and event.text.casefold() == "b":
            if active_group is None:
                return ""
            active_group = None
            selected = 0
