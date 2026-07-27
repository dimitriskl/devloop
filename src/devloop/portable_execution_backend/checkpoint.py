"""Execution Budget inactivity checkpointing driven by neutral step activity.

Every Execution Backend feeds the same :class:`StepActivityEvent` stream through
this helper, so the inactivity checkpoint pauses and resumes identically no
matter which provider served the Workflow Step attempt.
"""

from __future__ import annotations

from typing import Protocol

from .activity import StepActivityEvent, StepActivityKind


class CheckpointBudget(Protocol):
    """The Execution Budget operations inactivity checkpointing needs."""

    def pause_checkpoint(self) -> None: ...

    def resume_checkpoint(self) -> None: ...


def update_checkpoint_for_step_activity(
    budget: CheckpointBudget | None,
    event: StepActivityEvent | None,
    active_tools: set[str],
) -> None:
    """Pause inactivity expiry while the backend reports a running tool."""
    if budget is None or event is None or event.tool_key is None:
        return
    if event.kind is StepActivityKind.TOOL_STARTED:
        was_active = bool(active_tools)
        active_tools.add(event.tool_key)
        if not was_active:
            budget.pause_checkpoint()
        return
    if event.kind is not StepActivityKind.TOOL_COMPLETED:
        return
    if not active_tools:
        return
    active_tools.discard(event.tool_key)
    if not active_tools:
        budget.resume_checkpoint()
