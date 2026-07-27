"""Provider-process plumbing shared by every Execution Backend.

Every Execution Backend spawns one provider process, supplies the prompt on its
standard input, drains its diagnostics stream as progress, and renders neutral
step activity for Portable Plain Mode. None of that is provider-specific, so it
lives beside the boundary instead of being repeated inside each backend module.
"""

from __future__ import annotations

from typing import TextIO

from ..statusui import Stage
from ..terminal_text import sanitize_terminal_text
from .activity import ActivityCallback, StepActivityEvent


def write_process_input(stream: TextIO, input_text: str) -> None:
    """Supply one attempt's prompt on standard input, then close the stream.

    Closing is what tells the provider the prompt is complete, so it happens
    even when the write failed because the process already exited.
    """
    try:
        stream.write(input_text)
        stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def drain_process_stream(
    stream: TextIO,
    captured: list[str],
    notify_activity: ActivityCallback,
) -> None:
    """Capture a provider's diagnostics stream, counting each line as progress.

    Diagnostics carry no displayable step activity, so the callback receives
    ``None``: the attempt is working even though there is nothing to show.
    """
    try:
        for line in stream:
            captured.append(line)
            notify_activity(None)
    except (OSError, ValueError):
        pass


def print_step_activity(
    stage: Stage,
    context: str,
    event: StepActivityEvent,
) -> None:
    """Print one neutral step activity as a Portable Plain Mode line."""
    prefix = f"[{stage.value}]"
    if context:
        safe_context = sanitize_terminal_text(context, preserve_newlines=False)
        prefix = f"{prefix} {safe_context}:"
    safe_activity = sanitize_terminal_text(
        event.activity or "",
        preserve_newlines=False,
    )
    print(f"{prefix} {safe_activity}")
