"""The registry of installed Execution Backends.

Callers resolve a backend through this registry instead of naming an
implementation, so adding a provider means registering it here and implementing
the boundary. Each factory is called only when a Workflow Step actually needs
that backend, which is what keeps a Workflow that uses one provider independent
of the other provider's installation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..model_catalog import ModelCatalog
from .backend import BackendAvailability, ExecutionBackend, ExecutionBackendId
from .claude_code import CLAUDE_CLI_COMMAND, ClaudeCodeExecutionBackend
from .codex_cli import CODEX_CLI_COMMAND, CodexCliExecutionBackend

ExecutionBackendFactory = Callable[[], ExecutionBackend]
# One Model Catalog per Execution Backend, asked for only when a Workflow Step
# actually names that backend. Run preflight and the Workflow Editor share this
# shape so neither can accidentally load a provider the user is not using.
BackendModelCatalogLoader = Callable[[ExecutionBackendId], ModelCatalog]
# How a caller turns one Execution Backend identity into its implementation. The
# registry below is the default; a test injects a backend driven from recorded
# provider output through the same seam.
BackendResolver = Callable[[ExecutionBackendId], ExecutionBackend]

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


def execution_backend_availability() -> tuple[BackendAvailability, ...]:
    """Report Backend Availability for every registered Execution Backend.

    Resolution only — no provider is started — so the Execution Backend menu can
    annotate each choice with whether the user can actually run it.
    """
    return tuple(
        resolve_execution_backend(backend_id).availability()
        for backend_id in registered_execution_backend_ids()
    )


@dataclass(frozen=True)
class BackendModelCatalogAccess:
    """Per-backend Model Catalog access for one repository checkout.

    The Workflow Editor is handed these as plain callables, so a test drives it
    from fakes and a real session builds one of these from the commands the user
    configured. Each backend is resolved only when a Workflow Step actually
    needs it, which is what keeps a single-backend Workflow independent of the
    other provider's installation.
    """

    cwd: Path
    codex: str = CODEX_CLI_COMMAND
    claude: str = CLAUDE_CLI_COMMAND

    def backend(self, backend_id: ExecutionBackendId) -> ExecutionBackend:
        if backend_id is ExecutionBackendId.CODEX_CLI:
            return CodexCliExecutionBackend.resolved(self.codex)
        if backend_id is ExecutionBackendId.CLAUDE_CODE:
            return ClaudeCodeExecutionBackend.resolved(self.claude)
        return resolve_execution_backend(backend_id)

    def load_catalog(self, backend_id: ExecutionBackendId) -> ModelCatalog:
        return self.backend(backend_id).discover_model_catalog(cwd=self.cwd)

    def verify_model(self, backend_id: ExecutionBackendId, model_id: str) -> str:
        return self.backend(backend_id).verify_selected_model(model_id, cwd=self.cwd)

    def availability(self) -> tuple[BackendAvailability, ...]:
        return tuple(
            self.backend(backend_id).availability()
            for backend_id in registered_execution_backend_ids()
        )
