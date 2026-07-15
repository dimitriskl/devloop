from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from devloop.domain.identifiers import WorkflowRunId

WORKSPACE_PERMISSION_PROFILE_SCHEMA = "devloop.workspace-permission-profile/v1"
WORKSPACE_PROBE_VERSION = "devloop.workspace-probe/v1"


class WorkspaceProbeId(str, Enum):
    ROOT_READ = "ROOT_READ"
    NESTED_WRITE = "NESTED_WRITE"
    NESTED_ENUMERATION = "NESTED_ENUMERATION"
    PARENT_HASH = "PARENT_HASH"
    TEST_EXECUTION = "TEST_EXECUTION"
    GIT_INSPECTION = "GIT_INSPECTION"
    APPROVAL_FRAMING = "APPROVAL_FRAMING"
    WINDOWS_ACL_HANDOFF = "WINDOWS_ACL_HANDOFF"


class WorkspaceProbeStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_REQUIRED = "NOT_REQUIRED"


@dataclass(frozen=True)
class WorkspaceProbeResult:
    probe_id: WorkspaceProbeId
    status: WorkspaceProbeStatus
    evidence: str

    def __post_init__(self) -> None:
        if not self.evidence or len(self.evidence) > 1_000:
            raise ValueError("Workspace probe evidence must be bounded and nonempty.")


@dataclass(frozen=True)
class WorkspacePermissionProfile:
    schema: str
    run_id: WorkflowRunId
    canonical_root: str
    permission_profile: str
    probe_version: str
    real_backend_verified: bool
    requires_windows_acl_handoff: bool
    results: tuple[WorkspaceProbeResult, ...]

    def __post_init__(self) -> None:
        if self.schema != WORKSPACE_PERMISSION_PROFILE_SCHEMA:
            raise ValueError("Unsupported workspace permission profile schema.")
        if not self.canonical_root or not self.permission_profile or not self.probe_version:
            raise ValueError("Workspace permission profile provenance is incomplete.")
        identities = [result.probe_id for result in self.results]
        if len(identities) != len(set(identities)):
            raise ValueError("Workspace permission probes must have unique identities.")

    @property
    def ready(self) -> bool:
        return all(result.status is not WorkspaceProbeStatus.FAILED for result in self.results)

    @property
    def profile_hash(self) -> str:
        encoded = json.dumps(
            workspace_permission_profile_to_dict(self),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def workspace_permission_profile_to_dict(
    profile: WorkspacePermissionProfile,
) -> dict[str, object]:
    return {
        "schema": profile.schema,
        "run_id": profile.run_id.value,
        "canonical_root": profile.canonical_root,
        "permission_profile": profile.permission_profile,
        "probe_version": profile.probe_version,
        "real_backend_verified": profile.real_backend_verified,
        "requires_windows_acl_handoff": profile.requires_windows_acl_handoff,
        "results": [
            {
                "probe_id": result.probe_id.value,
                "status": result.status.value,
                "evidence": result.evidence,
            }
            for result in profile.results
        ],
    }
