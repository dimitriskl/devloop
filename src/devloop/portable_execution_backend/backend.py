"""The Execution Backend boundary for one Workflow Step attempt.

Dev Loop reaches an agent provider only through :class:`ExecutionBackend`. The
boundary is deliberately independent of the model provider behind it: the
request carries the prompt, the repository root, the schema and message
locations, the durable log locations, the Step Execution Settings, the Execution
Budget, and the activity callback, and the result carries the process result, the
recovered structured message, any Run-Wide Blocker, and any refusal records.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ..statusui import Stage
from .activity import ActivityCallback
from .blockers import RunWideBlocker, RunWideBlockerPolicy

if TYPE_CHECKING:
    from ..model_catalog import CodexModelCatalog
    from ..portable_workflow import CodexExecutionSettings, ExecutionBudget


# Durable log writing stays with the role runner, which owns log-root
# confinement and long-path compaction; a backend only ever writes through it.
LogWriter = Callable[[Path, str], None]


class ExecutionBackendId(str, Enum):
    """The closed set of Execution Backends Dev Loop can dispatch a step to."""

    CODEX_CLI = "CODEX_CLI"


@dataclass(frozen=True)
class RefusalRecord:
    """One tool or capability an Execution Backend refused during an attempt."""

    target: str
    reason: str = ""


@dataclass(frozen=True)
class StepAttemptRequest:
    """Everything an Execution Backend needs for one Workflow Step attempt.

    ``run_wide_blocker_policy`` states whether this attempt takes part in
    Run-Wide Blocker detection, so a caller that deliberately ignores run-wide
    conditions still receives the structured message its attempt produced.
    """

    prompt: str
    repo_root: Path
    schema_path: Path
    message_path: Path
    stdout_path: Path
    stderr_path: Path
    write_log: LogWriter
    execution_settings: CodexExecutionSettings | None = None
    execution_budget: ExecutionBudget | None = None
    activity_stage: Stage = Stage.DEVELOPMENT
    activity_context: str = ""
    activity_callback: ActivityCallback | None = None
    run_wide_blocker_policy: RunWideBlockerPolicy = RunWideBlockerPolicy.REPORT


@dataclass(frozen=True)
class StepAttemptResult:
    """What one Workflow Step attempt produced, before it becomes a role result.

    ``refusals`` records tool or capability denials the backend observed. The
    Codex CLI Backend reports none because its sandbox refuses work before the
    agent can claim it ran.
    """

    process: subprocess.CompletedProcess[str]
    message: str = ""
    run_wide_blocker: RunWideBlocker | None = None
    refusals: tuple[RefusalRecord, ...] = ()


@dataclass(frozen=True)
class StepSettingsAuthorization:
    """One Workflow Step's settings offered to its backend for preflight.

    ``settings`` is ``None`` when the Workflow Step is agent-backed but carries
    no settings at all; the backend reports that as its own refusal so every
    preflight message comes from one place.
    """

    step_display_name: str
    settings: CodexExecutionSettings | None


class ExecutionBackend(ABC):
    """One provider Dev Loop can start an agent run on."""

    @property
    @abstractmethod
    def backend_id(self) -> ExecutionBackendId:
        """The closed identity this backend is registered under."""

    @abstractmethod
    def invoke(self, request: StepAttemptRequest) -> StepAttemptResult:
        """Run one Workflow Step attempt to its terminal process result."""

    @abstractmethod
    def discover_model_catalog(self, *, cwd: Path) -> CodexModelCatalog:
        """Discover this backend's live account-aware Model Catalog."""

    @abstractmethod
    def authorize_execution_settings(
        self,
        authorizations: Sequence[StepSettingsAuthorization],
        *,
        model_catalog: CodexModelCatalog,
    ) -> None:
        """Authorize snapshotted settings for run preflight, or refuse clearly."""
