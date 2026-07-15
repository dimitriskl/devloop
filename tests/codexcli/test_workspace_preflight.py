from __future__ import annotations

from pathlib import Path

import pytest

from devloop.application.workspace_preflight import (
    LocalWorkspacePreflight,
    WorkspacePreflightError,
)
from devloop.domain.identifiers import WorkflowRunId
from devloop.domain.workspace import WorkspaceProbeId, WorkspaceProbeStatus


def test_local_workspace_preflight_proves_parent_permissions_without_residue(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    run_id = WorkflowRunId("run-20260714t120100-123456abcdef")
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def git_probe(root: Path, arguments: tuple[str, ...]) -> None:
        calls.append((root, arguments))

    profile = LocalWorkspacePreflight(git_probe=git_probe).probe(workspace, run_id)

    assert profile.canonical_root == str(workspace.resolve())
    assert profile.real_backend_verified is False
    assert all(
        result.status in {WorkspaceProbeStatus.PASSED, WorkspaceProbeStatus.NOT_REQUIRED}
        for result in profile.results
    )
    assert {result.probe_id for result in profile.results} >= {
        WorkspaceProbeId.NESTED_WRITE,
        WorkspaceProbeId.PARENT_HASH,
        WorkspaceProbeId.TEST_EXECUTION,
        WorkspaceProbeId.GIT_INSPECTION,
    }
    assert calls == [(workspace.resolve(), ("status", "--porcelain=v1", "--untracked-files=all"))]
    assert not (workspace / ".devloop" / "runs" / ".permission-probes").exists()


def test_workspace_preflight_rejects_a_symlink_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(workspace, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this platform.")

    with pytest.raises(WorkspacePreflightError, match="canonical"):
        LocalWorkspacePreflight(git_probe=lambda root, arguments: None).probe(
            alias,
            WorkflowRunId("run-20260714t120101-123456abcdef"),
        )
