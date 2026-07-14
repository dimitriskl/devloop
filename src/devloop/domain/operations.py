from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from devloop.domain.identifiers import IssueId, StepInstanceId


class ApprovalDecision(str, Enum):
    ACCEPT = "accept"
    ACCEPT_FOR_SESSION = "acceptForSession"
    DECLINE = "decline"
    CANCEL = "cancel"


@dataclass(frozen=True)
class ApprovalRequest:
    step: StepInstanceId
    issue_id: IssueId | None
    action: str
    target: str | None
    reason: str | None
    supported_decisions: tuple[ApprovalDecision, ...]

    def __post_init__(self) -> None:
        if not self.action.strip():
            raise ValueError("Approval action is required.")
        if len(set(self.supported_decisions)) != len(self.supported_decisions):
            raise ValueError("Approval decisions must be unique.")


class StopAction(str, Enum):
    CONTINUE = "CONTINUE"
    INTERRUPT_TURN = "INTERRUPT_TURN"
    PAUSE_RUN = "PAUSE_RUN"
    CANCEL_RUN = "CANCEL_RUN"


@dataclass(frozen=True)
class StopRequest:
    step: StepInstanceId | None
    issue_id: IssueId | None
    has_active_turn: bool
    has_active_run: bool

    @property
    def supported_actions(self) -> tuple[StopAction, ...]:
        actions = [StopAction.CONTINUE]
        if self.has_active_turn:
            actions.append(StopAction.INTERRUPT_TURN)
        if self.has_active_run:
            actions.extend((StopAction.PAUSE_RUN, StopAction.CANCEL_RUN))
        return tuple(actions)

    @staticmethod
    def requires_confirmation(action: StopAction) -> bool:
        return action is StopAction.CANCEL_RUN
