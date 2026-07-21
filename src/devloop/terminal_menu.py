from __future__ import annotations

import sys


def clear_terminal_screen() -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def render_menu_screen(*lines: str) -> None:
    clear_terminal_screen()
    for line in lines:
        print(line)
