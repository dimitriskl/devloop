"""Dev Loop minimal shell.

A single-mode, chrome-free front end for one Portable Workflow Session: a
scrolling activity area with one input line pinned underneath it. Everything
the operator can do is typed into that line or bound to Esc and Ctrl+C.

The delivery engine, protocol, supervisor and worker all stay in ``devloop``;
this package only replaces the terminal presentation.
"""
from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    from .cli import main as _main

    return _main(argv)
