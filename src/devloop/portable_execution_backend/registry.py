"""The registry of installed Execution Backends.

Callers resolve a backend through this registry instead of naming an
implementation, so adding a provider means registering it here and implementing
the boundary. Each factory is called only when a Workflow Step actually needs
that backend, which is what keeps a Workflow that uses one provider independent
of the other provider's installation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .backend import ExecutionBackend, ExecutionBackendId
from .claude_code import ClaudeCodeExecutionBackend
from .codex_cli import CodexCliExecutionBackend

ExecutionBackendFactory = Callable[[], ExecutionBackend]

REGISTERED_EXECUTION_BACKENDS: Mapping[ExecutionBackendId, ExecutionBackendFactory] = {
    ExecutionBackendId.CODEX_CLI: CodexCliExecutionBackend,
    # The Claude CLI is commonly installed as a shim rather than an executable,
    # so its factory resolves the command; resolution happens at factory time,
    # never at import time.
    ExecutionBackendId.CLAUDE_CODE: ClaudeCodeExecutionBackend.resolved,
}


def registered_execution_backend_ids() -> tuple[ExecutionBackendId, ...]:
    return tuple(REGISTERED_EXECUTION_BACKENDS)


def resolve_execution_backend(backend_id: ExecutionBackendId) -> ExecutionBackend:
    """Build the registered implementation of one Execution Backend."""
    try:
        factory = REGISTERED_EXECUTION_BACKENDS[backend_id]
    except KeyError as error:
        raise ValueError(
            f"No implementation is registered for the {backend_id.display_name} "
            "Execution Backend."
        ) from error
    return factory()
