"""The Execution Backend boundary for one Workflow Step attempt.

Dev Loop reaches an agent provider only through :class:`ExecutionBackend`. The
boundary is deliberately independent of the model provider behind it: the
request carries the prompt, the repository root, the schema and message
locations, the durable log locations, the Step Execution Settings, the Execution
Budget, and the activity callback, and the result carries the process result, the
recovered structured message, any Run-Wide Blocker, and any refusal records.
"""

from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..execution_backend_id import ExecutionBackendId, parse_execution_backend_id
from ..statusui import Stage
from .activity import ActivityCallback
from .blockers import RunWideBlocker, RunWideBlockerPolicy

if TYPE_CHECKING:
    from ..model_catalog import ModelCatalog
    from ..portable_workflow import ExecutionBudget, StepExecutionSettings

__all__ = [
    "BackendAvailability",
    "ExecutionBackend",
    "ExecutionBackendId",
    "LogWriter",
    "RefusalRecord",
    "StepAttemptRequest",
    "StepAttemptResult",
    "StepSettingsAuthorization",
    "describe_refusals",
    "parse_execution_backend_id",
]


# Durable log writing stays with the role runner, which owns log-root
# confinement and long-path compaction; a backend only ever writes through it.
LogWriter = Callable[[Path, str], None]


@dataclass(frozen=True)
class BackendAvailability:
    """Whether one Execution Backend is installed and usable on this machine.

    Reported per backend so a Workflow Step is never configured against a
    backend the user cannot run. ``detail`` is the operator-facing reason, shown
    beside the backend in the `/options` Execution Backend menu.
    """

    backend: ExecutionBackendId
    installed: bool
    detail: str

    @property
    def annotation(self) -> str:
        """The short annotation the Execution Backend menu shows per backend."""
        return self.detail


@dataclass(frozen=True)
class RefusalRecord:
    """One tool or capability an Execution Backend refused during an attempt."""

    target: str
    reason: str = ""


def describe_refusals(refusals: Sequence[RefusalRecord]) -> str:
    """Name the refused targets and their count, as `2 tool calls (Bash, Read)`.

    Both the live Portable Activity Feed and the persisted Step Outcome summary
    have to say how many tool calls were denied and which tools they were, so the
    phrasing lives once, beside the records it describes.
    """
    if not refusals:
        return "no tool calls"
    targets: list[str] = []
    for refusal in refusals:
        if refusal.target not in targets:
            targets.append(refusal.target)
    noun = "tool call" if len(refusals) == 1 else "tool calls"
    return f"{len(refusals)} {noun} ({', '.join(targets)})"


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
    execution_settings: StepExecutionSettings | None = None
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

    ``failure_summary`` is the backend's own words for a failed attempt, used as
    the Step Outcome summary in place of Dev Loop's paraphrase. It is empty
    whenever the provider said nothing usable, which leaves the runner's own
    exit-status summary in charge.
    """

    process: subprocess.CompletedProcess[str]
    message: str = ""
    run_wide_blocker: RunWideBlocker | None = None
    refusals: tuple[RefusalRecord, ...] = ()
    failure_summary: str = ""


@dataclass(frozen=True)
class StepSettingsAuthorization:
    """One Workflow Step's settings offered to its backend for preflight.

    ``settings`` is ``None`` when the Workflow Step is agent-backed but carries
    no settings at all; the backend reports that as its own refusal so every
    preflight message comes from one place.
    """

    step_display_name: str
    settings: StepExecutionSettings | None


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
    def discover_model_catalog(self, *, cwd: Path) -> ModelCatalog:
        """Discover this backend's live account-aware Model Catalog."""

    @abstractmethod
    def authorize_execution_settings(
        self,
        authorizations: Sequence[StepSettingsAuthorization],
        *,
        model_catalog: ModelCatalog,
    ) -> None:
        """Authorize snapshotted settings for run preflight, or refuse clearly."""

    @property
    def provider_command(self) -> str:
        """The command this backend invokes, or empty when it needs none."""
        return ""

    def availability(self) -> BackendAvailability:
        """Report Backend Availability without running the provider.

        Resolution only: the command is looked up on the executable search path
        and on disk, and nothing is started. A backend that needs no executable
        is available by definition.
        """
        command = self.provider_command
        if not command:
            return BackendAvailability(
                backend=self.backend_id,
                installed=True,
                detail="no provider executable required",
            )
        if shutil.which(command) or Path(command).is_file():
            return BackendAvailability(
                backend=self.backend_id,
                installed=True,
                detail="installed",
            )
        return BackendAvailability(
            backend=self.backend_id,
            installed=False,
            detail="not installed",
        )

    def verify_selected_model(self, model_id: str, *, cwd: Path) -> str:
        """Verify one selected model and return the identifier to persist.

        The default is the identity: a backend whose Model Catalog is itself the
        live account-aware list has already told the user what their account can
        run, so a selection needs no further call and carries no alias to
        resolve. A backend whose catalog is bundled reference data overrides
        this, verifies the selection against the operator's own account, and
        returns the concrete identifier the provider resolved it to — never a
        short alias, because a persisted alias could silently change which model
        serves a rerun.
        """
        return model_id
