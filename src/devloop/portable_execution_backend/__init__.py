"""The portable Execution Backend boundary for `devloop-plan` + `devloop`.

An Execution Backend is the boundary Dev Loop uses to start or resume an agent
run, independent of the model provider behind it. This package owns the
interface, its frozen request and result types, the neutral step-activity event
both the Portable Activity Feed and Execution Budget checkpointing consume, the
Run-Wide Blocker domain type, and one module per backend.

This package belongs to the portable product and must not import anything from
the separate CodexCLI application packages; `tests/test_product_boundary.py`
enforces that.
"""

from __future__ import annotations

from .activity import (
    TOOL_ACTIVITY_KINDS,
    ActivityCallback,
    StepActivityEvent,
    StepActivityKind,
)
from .backend import (
    BackendAvailability,
    ExecutionBackend,
    ExecutionBackendId,
    LogWriter,
    RefusalRecord,
    StepAttemptRequest,
    StepAttemptResult,
    StepSettingsAuthorization,
    TransientFailurePredicate,
    describe_refusals,
    parse_execution_backend_id,
)
from .blockers import RunWideBlocker, RunWideBlockerKind, RunWideBlockerPolicy
from .checkpoint import CheckpointBudget, update_checkpoint_for_step_activity
from .claude_catalog import (
    ClaudeModelCatalogAdapter,
    ModelVerificationError,
    ModelVerificationFailure,
    load_bundled_model_catalog,
)
from .claude_code import ClaudeCodeExecutionBackend
from .codex_cli import CodexCliExecutionBackend
from .registry import (
    REGISTERED_EXECUTION_BACKENDS,
    BackendModelCatalogAccess,
    BackendModelCatalogLoader,
    BackendResolver,
    execution_backend_availability,
    registered_execution_backend_ids,
    resolve_execution_backend,
)
from .structured_result import extract_json_object
from .transient_retry import (
    TRANSIENT_RETRY_DELAY_SECONDS,
    run_attempt_with_transient_retries,
)

__all__ = [
    "REGISTERED_EXECUTION_BACKENDS",
    "TOOL_ACTIVITY_KINDS",
    "TRANSIENT_RETRY_DELAY_SECONDS",
    "ActivityCallback",
    "BackendAvailability",
    "BackendModelCatalogAccess",
    "BackendModelCatalogLoader",
    "BackendResolver",
    "CheckpointBudget",
    "ClaudeCodeExecutionBackend",
    "ClaudeModelCatalogAdapter",
    "CodexCliExecutionBackend",
    "ExecutionBackend",
    "ExecutionBackendId",
    "LogWriter",
    "ModelVerificationError",
    "ModelVerificationFailure",
    "RefusalRecord",
    "RunWideBlocker",
    "RunWideBlockerKind",
    "RunWideBlockerPolicy",
    "StepActivityEvent",
    "StepActivityKind",
    "StepAttemptRequest",
    "StepAttemptResult",
    "StepSettingsAuthorization",
    "TransientFailurePredicate",
    "describe_refusals",
    "execution_backend_availability",
    "extract_json_object",
    "load_bundled_model_catalog",
    "parse_execution_backend_id",
    "registered_execution_backend_ids",
    "resolve_execution_backend",
    "run_attempt_with_transient_retries",
    "update_checkpoint_for_step_activity",
]
