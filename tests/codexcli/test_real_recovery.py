from __future__ import annotations

import asyncio
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from textual.widgets import Button, OptionList

from devloop.application.analysis import AnalysisWorkflowService
from devloop.application.commands import launcher_command_registry
from devloop.application.config import ApplicationConfig
from devloop.application.development import WorkspaceDevelopmentService
from devloop.application.recovery import RecoveryDisposition, RecoveryService
from devloop.application.review_qa import ReviewQaInterrupted, ReviewQaService
from devloop.application.scheduler import WorkflowSchedulerService
from devloop.application.workspace_preflight import RealAppServerWorkspacePreflight
from devloop.domain.development import IssueStatus, WorkspaceChoice
from devloop.domain.identifiers import AttemptId, IssueId, StepInstanceId
from devloop.domain.run import OperationStatus, WorkflowRunStatus
from devloop.execution.app_server import AppServerApprovalRequest
from devloop.persistence.run_store import RunStore
from devloop.ui.app import RunLauncherApp
from devloop.ui.composer import Composer
from devloop.ui.modals import ApprovalModal
from devloop.ui.qa import QaView


def _repository(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "devloop@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Dev Loop Recovery Gate"],
        cwd=path,
        check=True,
    )
    (path / "README.md").write_text("# Recovery gate\n", encoding="utf-8")
    (path / ".gitignore").write_text(
        ".devloop/\n__pycache__/\n*.pyc\n.pytest_cache/\n",
        encoding="ascii",
    )
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "baseline"], cwd=path, check=True)


def _approve(request: AppServerApprovalRequest) -> str | None:
    assert request.policy_hash
    assert request.command_family
    assert request.workspace_boundary
    return "accept" if "accept" in request.supported_decisions else None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_app_server_recovers_issue_three_qa_in_a_ten_issue_run(
    tmp_path: Path,
) -> None:
    if os.environ.get("DEVLOOP_REAL_RECOVERY") != "1":
        pytest.skip("Set DEVLOOP_REAL_RECOVERY=1 to run the real recovery release gate.")
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
        "Create exactly ten Issues in a linear dependency chain: Issue 2 depends on Issue 1, "
        "and every later Issue depends only on its immediate predecessor. Issue N adds "
        "module_n.py with value_N() returning integer N and test_module_n.py proving it with "
        "pytest. Keep every Issue independently implementable and small. Each Issue has exactly "
        "one acceptance criterion and python -m pytest -q is the verification command. Return "
        "the full draft now."
    )
    if result.clarification is not None:
        result = analysis.continue_analysis(
            result.snapshot.run_id,
            "No clarification is needed. Produce exactly the ten chained Issues described.",
        )
    assert result.draft is not None
    assert len(result.draft.issues) == 10
    accepted = analysis.accept(result.snapshot.run_id)
    run_id = accepted.snapshot.run_id

    development = WorkspaceDevelopmentService(
        config,
        workspace_preflight=RealAppServerWorkspacePreflight(),
    )
    development.prepare(run_id, WorkspaceChoice.CURRENT_CHECKOUT)
    verification = ReviewQaService(config)
    scheduler = WorkflowSchedulerService(
        config,
        development_service=development,
        review_qa_service=verification,
    )
    store = RunStore(config.paths.run_root)

    for _ in range(20):
        checkpoint = store.load(run_id)
        if (
            checkpoint.active_step == StepInstanceId("qa")
            and checkpoint.qa is not None
            and checkpoint.qa.issue_id == IssueId("ISSUE-003")
            and checkpoint.qa.turn_id is None
        ):
            break
        scheduler.advance(run_id, on_approval=_approve)
    else:
        pytest.fail("The production scheduler did not reach Issue 3 QA.")

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(scheduler.advance, run_id, on_approval=_approve)
        deadline = asyncio.get_running_loop().time() + 300
        while True:
            active = store.load(run_id)
            if (
                active.qa is not None
                and active.qa.turn_id is not None
                and active.operation.status is OperationStatus.RUNNING
            ):
                original_workspace = active.workspace
                original_cursor = active.qa
                verification.request_interrupt(run_id)
                break
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("The real Issue 3 QA turn did not reach an interruptible operation.")
            await asyncio.sleep(0.1)
        with pytest.raises(ReviewQaInterrupted):
            future.result(timeout=120)

    interrupted = store.load(run_id)
    assert interrupted.workspace == original_workspace
    assert interrupted.qa is not None
    assert interrupted.qa.issue_id == IssueId("ISSUE-003")
    assert interrupted.qa.attempt_id == AttemptId("attempt-001")
    assert interrupted.qa.thread_id == original_cursor.thread_id
    assert interrupted.qa.turn_id == original_cursor.turn_id
    assert [issue.status for issue in interrupted.issues[:3]] == [
        IssueStatus.COMPLETED,
        IssueStatus.COMPLETED,
        IssueStatus.IN_QA,
    ]
    assert all(issue.status is IssueStatus.PENDING for issue in interrupted.issues[3:])

    restarted_recovery = RecoveryService(config)
    plan = restarted_recovery.inspect(run_id)
    assert plan.disposition is RecoveryDisposition.FRESH_ATTEMPT
    assert plan.snapshot.qa == interrupted.qa

    restarted_review_qa = ReviewQaService(config)
    app = RunLauncherApp(
        repository,
        launcher_command_registry(),
        recovery_service=restarted_recovery,
        review_qa_service=restarted_review_qa,
    )
    async with app.run_test(size=(140, 40)) as pilot:
        app.post_message(Composer.Submitted("/resume"))
        await pilot.pause()
        menu = app.query_one("#command-menu", OptionList)
        assert menu.option_count == 1
        assert "ISSUE-003" in str(menu.get_option_at_index(0).prompt)
        assert "qa" in str(menu.get_option_at_index(0).prompt)
        menu.highlighted = 0
        menu.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "transcript-free Recovery Attempt" in str(
            menu.get_option_at_index(0).prompt
        )
        menu.highlighted = 0
        menu.focus()
        await pilot.press("enter")
        replacement_thread = None
        for _ in range(30_000):
            await asyncio.sleep(0.01)
            await pilot.pause()
            restored = store.load(run_id)
            if (
                restored.qa is not None
                and restored.qa.thread_id is not None
                and restored.qa.turn_id is not None
                and restored.qa.thread_id != original_cursor.thread_id
            ):
                replacement_thread = restored.qa.thread_id
                restarted_review_qa.request_interrupt(run_id)
                break
        else:
            pytest.fail("The explicit QA Recovery Attempt did not bind a fresh real thread.")
        for _ in range(12_000):
            await asyncio.sleep(0.01)
            await pilot.pause()
            if isinstance(app.screen, ApprovalModal):
                decision = next(
                    (
                        button
                        for button in app.screen.query(Button)
                        if button.id in {"approval-accept", "approval-decline"}
                    ),
                    None,
                )
                assert decision is not None
                decision.press()
            if store.load(run_id).run_status is WorkflowRunStatus.AWAITING_USER:
                break
        else:
            pytest.fail("The fresh QA Recovery Attempt did not preserve its interruption.")
        assert app.query_one("#qa-view", QaView).display

    restored = store.load(run_id)
    assert restored.workspace == original_workspace
    assert restored.qa is not None
    assert restored.qa.issue_id == IssueId("ISSUE-003")
    assert restored.qa.attempt_id == AttemptId("attempt-001")
    assert restored.qa.input_manifest == original_cursor.input_manifest
    assert restored.qa.thread_id == replacement_thread
    assert restored.qa.thread_id != original_cursor.thread_id
    assert [issue.status for issue in restored.issues[:3]] == [
        IssueStatus.COMPLETED,
        IssueStatus.COMPLETED,
        IssueStatus.IN_QA,
    ]
    assert all(issue.status is IssueStatus.PENDING for issue in restored.issues[3:])
    store.release_lease(restored)
