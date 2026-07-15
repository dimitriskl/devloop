from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from devloop.application.analysis import AnalysisWorkflowService
from devloop.application.config import ApplicationConfig
from devloop.application.development import WorkspaceDevelopmentService
from devloop.application.finalization import FinalizationService
from devloop.application.scheduler import SchedulerAction, WorkflowSchedulerService
from devloop.application.workspace_preflight import RealAppServerWorkspacePreflight
from devloop.domain.development import IssueStatus, WorkspaceChoice
from devloop.domain.execution import ExecutionPhase
from devloop.domain.run import WorkflowRunStatus
from devloop.execution.app_server import AppServerApprovalRequest
from devloop.persistence.run_store import RunStore


def _repository(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "devloop@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Dev Loop Vertical Gate"],
        cwd=path,
        check=True,
    )
    (path / "README.md").write_text("# Vertical gate\n", encoding="utf-8")
    (path / ".gitignore").write_text(
        ".devloop/\n__pycache__/\n*.pyc\n.pytest_cache/\n",
        encoding="ascii",
    )
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "baseline"], cwd=path, check=True)


@pytest.mark.integration
def test_real_one_issue_vertical_gate_uses_only_production_services(tmp_path: Path) -> None:
    if os.environ.get("DEVLOOP_REAL_VERTICAL") != "1":
        pytest.skip("Set DEVLOOP_REAL_VERTICAL=1 to run the real vertical gate.")
    repository = tmp_path / "project"
    _repository(repository)
    config = ApplicationConfig.resolve(
        repository,
        environment={
            "APPDATA": str(tmp_path / "user-config"),
            "LOCALAPPDATA": str(tmp_path / "user-data"),
            "XDG_CONFIG_HOME": str(tmp_path / "user-config"),
            "XDG_DATA_HOME": str(tmp_path / "user-data"),
        },
    )
    analysis = AnalysisWorkflowService(config)
    result = analysis.start(
        "Create exactly one small Issue for a dependency-free greeting.py module. "
        "Its greeting() function returns exactly Hello and a focused pytest test proves it. "
        "The implementation must run python -m pytest -q. Return a complete draft now."
    )
    if result.clarification is not None:
        result = analysis.continue_analysis(
            result.snapshot.run_id,
            "There are no additional requirements. Create exactly the one Issue described.",
        )
    assert result.draft is not None
    assert len(result.draft.issues) == 1
    accepted = analysis.accept(result.snapshot.run_id)

    development = WorkspaceDevelopmentService(
        config,
        workspace_preflight=RealAppServerWorkspacePreflight(),
    )
    prepared = development.prepare(
        accepted.snapshot.run_id,
        WorkspaceChoice.DEDICATED_WORKTREE,
        worktree_parent=tmp_path / "external-worktrees",
    )
    assert prepared.snapshot.workspace is not None
    assert Path(prepared.snapshot.workspace.path).parent == tmp_path / "external-worktrees"
    approvals: list[AppServerApprovalRequest] = []

    def approve(request: AppServerApprovalRequest) -> str | None:
        approvals.append(request)
        assert request.policy_hash
        assert request.command_family
        assert request.workspace_boundary
        return "accept" if "accept" in request.supported_decisions else None

    scheduler = WorkflowSchedulerService(config, development_service=development)
    advanced = scheduler.run_until_pause(
        accepted.snapshot.run_id,
        on_approval=approve,
    )

    assert advanced.action is SchedulerAction.WORKFLOW_DRAINED
    assert advanced.snapshot.issues[0].status is IssueStatus.COMPLETED
    finalized = FinalizationService(config).finalize(accepted.snapshot.run_id)
    assert finalized.snapshot.run_status is WorkflowRunStatus.COMPLETED
    assert approvals
    assert all(request.command_family != "PERMISSIONS" for request in approvals)
    assert all(
        request.workspace_boundary in {"WORKSPACE", "NOT_APPLICABLE"}
        for request in approvals
    )
    persisted = RunStore(config.paths.run_root).load(accepted.snapshot.run_id)
    assert persisted.approval_decisions
    groups = {
        event.component_id: {item.phase for item in persisted.execution_telemetry.events}
        for event in persisted.execution_telemetry.events
        for item in persisted.execution_telemetry.events
        if item.component_id == event.component_id
    }
    for component_id in ("analysis", "development", "code-review", "qa"):
        assert groups[component_id] == set(ExecutionPhase)
