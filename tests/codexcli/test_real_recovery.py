from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from textual.widgets import OptionList

from devloop.application.commands import launcher_command_registry
from devloop.application.config import ApplicationConfig
from devloop.application.recovery import RecoveryDisposition, RecoveryService
from devloop.components.builtin import installed_component_registry
from devloop.domain.development import (
    IssueRuntimeState,
    IssueStatus,
    WorkspaceKind,
    WorkspaceRef,
)
from devloop.domain.identifiers import (
    AttemptId,
    ExecutionThreadId,
    ExecutionTurnId,
    FeatureSlug,
    IssueId,
    StepInstanceId,
    WorkflowRunId,
)
from devloop.domain.review_qa import QaCursor
from devloop.domain.run import (
    AnalysisCursor,
    ComponentLock,
    OperationState,
    ResolvedWorkflow,
    RunEventType,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.execution.app_server import (
    AppServerClient,
    AppServerReasoningEffort,
    AppServerSandboxMode,
    AppServerTurnStatus,
)
from devloop.infrastructure.codex import resolve_codex_executable
from devloop.infrastructure.git import (
    capture_repository_state_hash,
    current_branch,
    head_commit,
)
from devloop.persistence.run_store import RUN_SNAPSHOT_SCHEMA, RunStore, new_run_lease
from devloop.ui.app import RunLauncherApp
from devloop.ui.composer import Composer
from devloop.ui.qa import QaView
from devloop.workflow.definition import load_standard_workflow


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_app_server_recovers_issue_three_qa_in_a_ten_issue_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.environ.get("DEVLOOP_REAL_RECOVERY") != "1":
        pytest.skip("Set DEVLOOP_REAL_RECOVERY=1 to run the real recovery release gate.")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    xdg_config = tmp_path / "xdg-config"
    xdg_config.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    repository = tmp_path / "project"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "devloop@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Dev Loop Tests"],
        check=True,
    )
    (repository / "README.md").write_text("recovery release gate\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "baseline"],
        check=True,
    )

    executable = resolve_codex_executable()
    with AppServerClient(
        str(executable),
        experimental_api=True,
        process_cwd=repository,
    ) as client:
        client.initialize()
        thread = client.start_thread(
            repository,
            model="gpt-5.6-sol",
            reasoning_effort=AppServerReasoningEffort.ULTRA,
            sandbox=AppServerSandboxMode.READ_ONLY,
        )
        turn = client.start_turn(
            thread.thread_id,
            "For QA, run a read-only command that waits for 60 seconds before returning.",
        )
        interrupt_after = time.monotonic() + 0.5
        interrupted = client.wait_for_turn(
            thread.thread_id,
            turn.turn_id,
            timeout_seconds=120.0,
            interrupt_requested=lambda: time.monotonic() >= interrupt_after,
        )
    assert interrupted.status is AppServerTurnStatus.INTERRUPTED

    config = ApplicationConfig.resolve(repository)
    store = RunStore(config.paths.run_root)
    workflow = load_standard_workflow()
    run_id = WorkflowRunId("run-20260712t180000-123456abcdef")
    snapshot = WorkflowRunSnapshot(
        schema=RUN_SNAPSHOT_SCHEMA,
        run_id=run_id,
        repository=str(repository.resolve()),
        feature_title="Real exact recovery release gate",
        feature_slug=FeatureSlug("real-exact-recovery-release-gate"),
        workflow=ResolvedWorkflow(
            workflow.workflow_id,
            workflow.version,
            workflow.definition_hash,
        ),
        component_locks=tuple(
            ComponentLock(
                manifest.component_id,
                manifest.version,
                manifest.distribution,
                manifest.package_hash,
            )
            for manifest in installed_component_registry().manifests
        ),
        active_step=StepInstanceId("qa"),
        run_status=WorkflowRunStatus.RUNNING,
        step_status=StepRunStatus.RUNNING,
        outcome=None,
        analysis=AnalysisCursor(),
        lease=new_run_lease(),
        event_sequence=0,
        updated_at=datetime.now(timezone.utc).isoformat(),
        workspace=WorkspaceRef(
            WorkspaceKind.CURRENT_CHECKOUT,
            str(repository.resolve()),
            str(repository.resolve()),
            current_branch(repository),
            head_commit(repository),
        ),
        issues=tuple(
            IssueRuntimeState(
                IssueId(f"ISSUE-{number:03}"),
                IssueStatus.COMPLETED
                if number <= 2
                else IssueStatus.IN_QA
                if number == 3
                else IssueStatus.PENDING,
                StepInstanceId("qa") if number == 3 else None,
            )
            for number in range(1, 11)
        ),
        operation=OperationState(),
        workspace_state_hash=capture_repository_state_hash(repository),
    )
    created = store.create(snapshot)
    qa_artifact = store.save_json_artifact(
        run_id,
        Path("qa-inputs/ISSUE-003-release-recovery.json"),
        {"schema": "devloop.qa-input/v1", "issue_id": "ISSUE-003"},
    )
    checkpoint = store.record(
        replace(
            created,
            qa=QaCursor(
                IssueId("ISSUE-003"),
                AttemptId("attempt-001"),
                qa_artifact,
                ExecutionThreadId(thread.thread_id),
                ExecutionTurnId(turn.turn_id),
            ),
        ),
        event_type=RunEventType.QA_TURN_STARTED,
    )
    lease_path = store.run_directory(run_id) / "lease.json"
    lease = json.loads(lease_path.read_text(encoding="utf-8"))
    lease["process_id"] = 2_147_483_647
    lease_path.write_text(json.dumps(lease), encoding="utf-8")

    restarted = RecoveryService(config)
    candidate = restarted.list_candidates()[0]
    plan = restarted.inspect(checkpoint.run_id)

    assert candidate.issue_id == IssueId("ISSUE-003")
    assert candidate.step == StepInstanceId("qa")
    assert plan.disposition is RecoveryDisposition.FRESH_ATTEMPT
    assert plan.snapshot.qa is not None
    assert plan.snapshot.qa.thread_id == ExecutionThreadId(thread.thread_id)
    assert plan.snapshot.qa.turn_id == ExecutionTurnId(turn.turn_id)
    assert [issue.status for issue in plan.snapshot.issues[:3]] == [
        IssueStatus.COMPLETED,
        IssueStatus.COMPLETED,
        IssueStatus.IN_QA,
    ]
    assert all(issue.status is IssueStatus.PENDING for issue in plan.snapshot.issues[3:])

    app = RunLauncherApp(
        repository,
        launcher_command_registry(),
        recovery_service=restarted,
    )
    async with app.run_test(size=(140, 40)) as pilot:
        app.post_message(Composer.Submitted("/resume"))
        await pilot.pause()
        menu = app.query_one("#command-menu", OptionList)
        assert menu.option_count == 1
        prompt = str(menu.get_option_at_index(0).prompt)
        assert "ISSUE-003" in prompt
        assert "qa" in prompt

        menu.highlighted = 0
        menu.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert menu.option_count == 1
        assert "transcript-free Recovery Attempt" in str(
            menu.get_option_at_index(0).prompt
        )

        menu.highlighted = 0
        menu.focus()
        await pilot.press("enter")
        for _ in range(50):
            await pilot.pause()
            restored = store.load(run_id)
            if restored.qa is not None and restored.qa.thread_id is None:
                break
        else:
            pytest.fail("The selected QA Recovery Attempt did not start.")

        assert app.query_one("#qa-view", QaView).display

    restored = store.load(run_id)
    assert restored.workspace == checkpoint.workspace
    assert restored.qa is not None
    assert restored.qa.issue_id == IssueId("ISSUE-003")
    assert restored.qa.attempt_id == AttemptId("attempt-001")
    assert restored.qa.input_manifest == qa_artifact
    assert restored.qa.thread_id is None
    assert restored.qa.turn_id is None
    assert [issue.status for issue in restored.issues[:3]] == [
        IssueStatus.COMPLETED,
        IssueStatus.COMPLETED,
        IssueStatus.IN_QA,
    ]
    assert all(issue.status is IssueStatus.PENDING for issue in restored.issues[3:])
    store.release_lease(restored)
