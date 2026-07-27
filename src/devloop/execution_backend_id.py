"""Execution Backend identity: the closed set of providers Dev Loop can run.

This lives in its own leaf module, imported by neither the backend package nor
the Model Catalog before the other, because both depend on it: the Execution
Backend boundary is keyed by this identity, and a Model Catalog belongs to one
Execution Backend. Keeping the enum here is what lets the catalog carry a
backend identity without the two modules importing each other.

Every existing import path keeps working: ``portable_execution_backend`` and its
``backend`` module both re-export these names.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ExecutionBackendId(str, Enum):
    """The closed set of Execution Backends Dev Loop can dispatch a step to."""

    CODEX_CLI = "CODEX_CLI"
    CLAUDE_CODE = "CLAUDE_CODE"

    @property
    def display_name(self) -> str:
        """The operator-facing name of this Execution Backend."""
        return _EXECUTION_BACKEND_DISPLAY_NAMES[self]

    @property
    def slug(self) -> str:
        """The lowercase kebab-case form used in filenames and bundle paths."""
        return self.value.lower().replace("_", "-")

    @property
    def advertises_fast(self) -> bool:
        """Whether this Execution Backend offers Fast for any of its models.

        Fast is a Codex service-tier preference. A backend that advertises none
        rejects Fast outright; within a backend that does advertise it, the
        selected model still decides, through its Model Catalog entry.
        """
        return self is ExecutionBackendId.CODEX_CLI


_EXECUTION_BACKEND_DISPLAY_NAMES = {
    ExecutionBackendId.CODEX_CLI: "Codex CLI",
    ExecutionBackendId.CLAUDE_CODE: "Claude Code",
}


def parse_execution_backend_id(value: Any) -> ExecutionBackendId:
    """Parse an external backend name into the closed Execution Backend set.

    This is the single boundary at which persisted documents and command-line
    input become backend identity; nothing downstream compares a bare string.
    """
    if isinstance(value, ExecutionBackendId):
        return value
    supported = ", ".join(member.value for member in ExecutionBackendId)
    if not isinstance(value, str):
        raise ValueError(
            f"Execution Backend must be one of {supported}; got {value!r}."
        )
    try:
        return ExecutionBackendId(value.strip())
    except ValueError as error:
        raise ValueError(
            f"Unsupported Execution Backend {value!r}; expected one of {supported}."
        ) from error
