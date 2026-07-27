"""Backend-neutral activity reported while a Workflow Step attempt runs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class StepActivityKind(str, Enum):
    """The closed set of activity kinds every Execution Backend reports."""

    TOOL_STARTED = "TOOL_STARTED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    MESSAGE = "MESSAGE"
    REASONING = "REASONING"
    RATE_LIMIT = "RATE_LIMIT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TURN_COMPLETED = "TURN_COMPLETED"
    ERROR = "ERROR"


TOOL_ACTIVITY_KINDS = frozenset(
    {StepActivityKind.TOOL_STARTED, StepActivityKind.TOOL_COMPLETED}
)


@dataclass(frozen=True)
class StepActivityEvent:
    """One Execution Backend event translated out of its provider vocabulary.

    ``activity`` is the terminal-safe text the Portable Activity Feed shows, or
    ``None`` when the event carries no display text. ``tool_key`` identifies the
    backend operation a ``TOOL_STARTED`` / ``TOOL_COMPLETED`` pair belongs to so
    the inactivity checkpoint can pause for exactly as long as it runs.
    """

    kind: StepActivityKind
    activity: str | None = None
    tool_key: str | None = None

    def __post_init__(self) -> None:
        if self.kind in TOOL_ACTIVITY_KINDS and not self.tool_key:
            raise ValueError(
                "Tool step activity requires a tool key so the inactivity "
                "checkpoint can pair its start with its completion."
            )


# Both the Portable Activity Feed and the inactivity checkpoint consume the
# neutral event, so the feed can tell a permission denial or a rate limit from an
# ordinary message by its kind instead of sniffing display text. A ``None`` event
# means the backend reported progress with nothing at all to show.
ActivityCallback = Callable[[StepActivityEvent | None], None]
