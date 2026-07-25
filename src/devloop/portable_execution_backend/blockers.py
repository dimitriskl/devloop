"""The Run-Wide Blocker domain type shared by every Execution Backend.

A Run-Wide Blocker is a backend condition that prevents every Issue from
executing, so it pauses the run without changing Issue outcomes or consuming
Issue attempt budgets. The condition is domain, not wire format: each Execution
Backend keeps its own classifier that recognises the condition in its provider's
output and reports it with this type.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RunWideBlockerKind(str, Enum):
    USAGE_LIMIT = "USAGE_LIMIT"
    AUTHENTICATION = "AUTHENTICATION"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


@dataclass(frozen=True)
class RunWideBlocker:
    kind: RunWideBlockerKind
    summary: str


class RunWideBlockerPolicy(str, Enum):
    """Whether one Workflow Step attempt takes part in Run-Wide Blocker detection.

    ``REPORT`` is the default an issue-scoped attempt uses: the backend
    classifies its captured output and reports the blocker instead of a
    structured message, so the caller can pause the whole run before the attempt
    is treated as a result. ``IGNORE`` belongs to an attempt that runs outside
    Issue execution and must stand or fall on its own process result, such as the
    post-run self-improvement compiler; the backend then never classifies, so a
    provider diagnostic that merely mentions a run-wide condition cannot suppress
    a message the attempt really did produce.
    """

    REPORT = "REPORT"
    IGNORE = "IGNORE"
