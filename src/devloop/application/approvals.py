from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from devloop.domain.approval import (
    ApprovalClassification,
    ApprovalPolicy,
    ClassifiedApproval,
    CommandFamily,
    classify_command,
    classify_non_command,
    decision_evidence,
)
from devloop.domain.identifiers import WorkflowRunId
from devloop.domain.run import RunEventType, WorkflowRunSnapshot
from devloop.execution.app_server import AppServerApprovalKind, AppServerApprovalRequest
from devloop.persistence.run_store import RunStore


def classify_backend_approval(
    request: AppServerApprovalRequest,
    workspace: Path,
    policy: ApprovalPolicy,
) -> tuple[AppServerApprovalRequest, ClassifiedApproval]:
    if request.kind is AppServerApprovalKind.COMMAND:
        classified = classify_command(
            request.action,
            Path(request.target) if request.target is not None else workspace,
            workspace,
            policy,
        )
    else:
        family = {
            AppServerApprovalKind.FILE_CHANGE: CommandFamily.FILE_CHANGE,
            AppServerApprovalKind.PERMISSIONS: CommandFamily.PERMISSIONS,
        }.get(request.kind, CommandFamily.OTHER)
        classified = classify_non_command(
            family=family,
            target=request.target,
            workspace=workspace,
            policy=policy,
        )
    supported = tuple(
        item for item in request.supported_decisions if item in policy.decision_options
    )
    if classified.classification is ApprovalClassification.UNSUPPORTED:
        supported = tuple(item for item in supported if item in {"decline", "cancel"})
    return (
        replace(
            request,
            action=classified.parsed_action,
            supported_decisions=supported,
            command_family=classified.family.value,
            workspace_boundary=classified.boundary.value,
            policy_reason=classified.reason,
            policy_version=policy.version,
            policy_hash=policy.policy_hash,
            command_hash=classified.command_hash,
        ),
        classified,
    )


def persist_approval_decision(
    store: RunStore,
    snapshot: WorkflowRunSnapshot,
    *,
    component_id: str,
    issue_id: str | None,
    attempt_id: str | None,
    request: AppServerApprovalRequest,
    classification: ClassifiedApproval,
    selected_decision: str,
) -> WorkflowRunSnapshot:
    payload = decision_evidence(
        component_id=component_id,
        issue_id=issue_id,
        attempt_id=attempt_id,
        request_id=request.request_id,
        request_kind=request.kind.value,
        classification=classification,
        selected_decision=selected_decision,
        supported_decisions=request.supported_decisions,
    )
    request_identity = "\0".join(
        str(value)
        for value in (
            request.request_id,
            request.thread_id,
            request.turn_id,
            request.item_id,
        )
    )
    token = hashlib.sha256(request_identity.encode("utf-8")).hexdigest()[:16]
    sequence = len(snapshot.approval_decisions) + 1
    relative = Path("approvals") / (
        f"{component_id}-{attempt_id or 'run'}-{sequence:06d}-{token}.json"
    )
    artifact = store.save_json_artifact(WorkflowRunId(snapshot.run_id.value), relative, payload)
    updated = replace(
        snapshot,
        approval_decisions=tuple(dict.fromkeys((*snapshot.approval_decisions, artifact))),
    )
    return store.record(updated, RunEventType.APPROVAL_DECISION_RECORDED)
