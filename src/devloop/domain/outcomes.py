from __future__ import annotations

from enum import Enum


class StepOutcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
