"""The registry of installed Execution Backends.

Exactly one Execution Backend is registered today. Callers resolve a backend
through this registry instead of naming an implementation, so adding a provider
means registering it here and implementing the boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .backend import ExecutionBackend, ExecutionBackendId
from .codex_cli import CodexCliExecutionBackend

ExecutionBackendFactory = Callable[[], ExecutionBackend]

REGISTERED_EXECUTION_BACKENDS: Mapping[ExecutionBackendId, ExecutionBackendFactory] = {
    ExecutionBackendId.CODEX_CLI: CodexCliExecutionBackend,
}


def registered_execution_backend_ids() -> tuple[ExecutionBackendId, ...]:
    return tuple(REGISTERED_EXECUTION_BACKENDS)


def sole_registered_execution_backend() -> ExecutionBackend:
    """The single registered Execution Backend in its default configuration.

    Used where a run has no per-Workflow-Step backend choice to honour yet, such
    as preflight authorization of settings against a Model Catalog.
    """
    backend_ids = registered_execution_backend_ids()
    if len(backend_ids) != 1:
        raise ValueError(
            "Exactly one Execution Backend must be registered to resolve a "
            f"backend without an explicit choice; found {len(backend_ids)}."
        )
    return REGISTERED_EXECUTION_BACKENDS[backend_ids[0]]()
