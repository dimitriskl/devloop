from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from devloop.domain.development import ArtifactRef
from devloop.domain.identifiers import IssueId, WorkflowRunId

HANDOFF_SUMMARY_SCHEMA = "devloop.handoff-summary/v1"


class WorkspaceDisposition(str, Enum):
    LEAVE_INTACT = "LEAVE_INTACT"


@dataclass(frozen=True)
class HandoffSummary:
    schema: str
    run_id: WorkflowRunId
    completed_issues: tuple[IssueId, ...]
    verification_evidence: tuple[str, ...]
    changed_files: tuple[str, ...]
    residual_risks: tuple[str, ...]
    workspace_disposition: WorkspaceDisposition
    workspace_path: str
    approval_decisions: tuple[str, ...] = ()
    execution_profiles: tuple[str, ...] = ()
    execution_telemetry: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinalizationCursor:
    handoff_summary: ArtifactRef
    workspace_disposition: WorkspaceDisposition
