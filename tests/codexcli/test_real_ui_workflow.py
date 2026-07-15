from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, OptionList, RichLog

from devloop.application.analysis import AnalysisWorkflowService
from devloop.application.capabilities import (
    CapabilityProfileService,
    standard_capability_catalog,
)
from devloop.application.commands import launcher_command_registry
from devloop.application.config import ApplicationConfig
from devloop.application.control import WorkflowControlService
from devloop.application.development import WorkspaceDevelopmentService
from devloop.application.recovery import RecoveryService
from devloop.application.review_qa import ReviewQaService
from devloop.application.scheduler import WorkflowSchedulerService
from devloop.application.workspace_preflight import RealAppServerWorkspacePreflight
from devloop.domain.run import WorkflowRunStatus
from devloop.persistence.run_store import RunStore
from devloop.ui.analysis import AnalysisView
from devloop.ui.app import RunLauncherApp
from devloop.ui.composer import Composer
from devloop.ui.development import DevelopmentView
from devloop.ui.finalization import FinalizationView
from devloop.ui.modals import ApprovalModal, CapabilityOptionsModal
from devloop.ui.qa import QaView
from devloop.ui.review import CodeReviewView
from devloop.ui.workspace import WorkspacePreparationView


async def _wait_for(
    pilot: Pilot[None],
    condition: Callable[[], bool],
    *,
    timeout_seconds: float,
    failure: str,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not condition():
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail(failure)
        await asyncio.sleep(0.1)
        await pilot.pause()


def _initialize_repository(repository: Path) -> None:
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "devloop@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Dev Loop Release Gate"],
        cwd=repository,
        check=True,
    )
    (repository / "README.md").write_text("# Real UI smoke\n", encoding="utf-8")
    (repository / ".gitignore").write_text(
        ".devloop/\n__pycache__/\n*.pyc\n.pytest_cache/\n",
        encoding="ascii",
    )
    subprocess.run(["git", "add", "README.md", ".gitignore"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repository,
        check=True,
        capture_output=True,
    )


def _toggle_development_tdd(modal: CapabilityOptionsModal) -> None:
    options = modal.query_one("#capability-options", OptionList)
    target = next(
        index
        for index in range(options.option_count)
        if options.get_option_at_index(index).id == "development|tdd"
    )
    options.highlighted = target
    options.focus()
    options.action_select()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_ui_runs_the_standard_workflow_with_explicit_approval(
    tmp_path: Path,
) -> None:
    if os.environ.get("DEVLOOP_REAL_UI") != "1":
        pytest.skip("Set DEVLOOP_REAL_UI=1 to run the real UI workflow gate.")

    repository = tmp_path / "project"
    _initialize_repository(repository)
    environment = {
        "APPDATA": str(tmp_path / "user-config"),
        "LOCALAPPDATA": str(tmp_path / "user-data"),
        "XDG_CONFIG_HOME": str(tmp_path / "user-config"),
        "XDG_DATA_HOME": str(tmp_path / "user-data"),
    }
    config = ApplicationConfig.resolve(repository, environment=environment)
    capabilities = CapabilityProfileService(
        config.paths.user_config,
        standard_capability_catalog(),
    )
    development_service = WorkspaceDevelopmentService(
        config,
        workspace_preflight=RealAppServerWorkspacePreflight(),
    )
    review_qa_service = ReviewQaService(config)
    app = RunLauncherApp(
        repository,
        launcher_command_registry(),
        analysis_service=AnalysisWorkflowService(config),
        workspace_service=development_service,
        review_qa_service=review_qa_service,
        scheduler_service=WorkflowSchedulerService(
            config,
            development_service=development_service,
            review_qa_service=review_qa_service,
        ),
        recovery_service=RecoveryService(config),
        capability_service=capabilities,
        control_service=WorkflowControlService(config),
    )
    store = RunStore(config.paths.run_root)
    seen_views: set[str] = set()
    approval_count = 0
    defect_injected = False

    async with app.run_test(size=(140, 40)) as pilot:
        app.post_message(Composer.Submitted("/options"))
        await _wait_for(
            pilot,
            lambda: isinstance(app.screen, CapabilityOptionsModal),
            timeout_seconds=10,
            failure="Capability options did not open.",
        )
        options_modal = app.screen
        assert isinstance(options_modal, CapabilityOptionsModal)
        _toggle_development_tdd(options_modal)
        await pilot.click("#capability-apply")
        locked_profiles = capabilities.resolved_profiles()

        feature_request = (
            "Build a minimal Python greeting module. Produce one dependency-free Issue with one "
            "acceptance criterion: greeting.py exposes greeting() returning the exact text Hello, "
            "a focused pytest test verifies it, and python -m pytest -q passes. The implementation "
            "must run that verification command. Return a complete, valid PRD Package without "
            "asking a clarification question."
        )
        composer = app.query_one("#composer", Composer)
        composer.load_text(feature_request)
        composer.action_submit()
        await _wait_for(
            pilot,
            lambda: app.query_one("#analysis-accept", Button).disabled is False,
            timeout_seconds=900,
            failure="Real analysis did not produce an acceptable PRD draft.",
        )
        seen_views.add("analysis")
        runs = store.list_runs()
        assert len(runs) == 1
        run_id = runs[0].run_id
        assert runs[0].capability_profiles == locked_profiles

        app.post_message(Composer.Submitted("/pause"))
        await _wait_for(
            pilot,
            lambda: store.load(run_id).run_status is WorkflowRunStatus.PAUSED,
            timeout_seconds=30,
            failure="The real analysis run did not pause at its persisted checkpoint.",
        )

        app.post_message(Composer.Submitted("/options"))
        await _wait_for(
            pilot,
            lambda: isinstance(app.screen, CapabilityOptionsModal),
            timeout_seconds=10,
            failure="Capability options did not reopen after pause.",
        )
        options_modal = app.screen
        assert isinstance(options_modal, CapabilityOptionsModal)
        _toggle_development_tdd(options_modal)
        await pilot.click("#capability-apply")
        assert capabilities.resolved_profiles() != locked_profiles

        app.post_message(Composer.Submitted("/resume"))
        await _wait_for(
            pilot,
            lambda: app.query_one("#command-menu", OptionList).option_count == 1,
            timeout_seconds=10,
            failure="The paused real analysis run was not offered for resume.",
        )
        resume_menu = app.query_one("#command-menu", OptionList)
        resume_menu.highlighted = 0
        resume_menu.focus()
        await pilot.press("enter")
        await _wait_for(
            pilot,
            lambda: (
                app.query_one("#analysis-view", AnalysisView).display
                and app.query_one("#analysis-accept", Button).disabled is False
            ),
            timeout_seconds=120,
            failure="The real analysis thread did not resume in the Analysis Step View.",
        )
        assert store.load(run_id).capability_profiles == locked_profiles

        await pilot.click("#analysis-accept")
        await _wait_for(
            pilot,
            lambda: app.query_one(
                "#workspace-view", WorkspacePreparationView
            ).display,
            timeout_seconds=30,
            failure="Accepted analysis did not advance to workspace preparation.",
        )
        seen_views.add("workspace-preparation")
        await pilot.click("#workspace-current-choice")

        deadline = asyncio.get_running_loop().time() + 3600
        finalization_requested = False
        while store.load(run_id).run_status is not WorkflowRunStatus.COMPLETED:
            if asyncio.get_running_loop().time() >= deadline:
                activity = app.query_one("#activity", RichLog)
                diagnostic = " | ".join(line.text for line in activity.lines[-10:])
                pytest.fail(f"Real UI workflow did not complete: {diagnostic}")

            if app.query_one("#development-view", DevelopmentView).display:
                seen_views.add("development")
            if app.query_one("#review-view", CodeReviewView).display:
                seen_views.add("code-review")
            if app.query_one("#qa-view", QaView).display:
                seen_views.add("qa")
            if app.query_one("#finalization-view", FinalizationView).display:
                seen_views.add("workspace-finalization")

            if isinstance(app.screen, ApprovalModal):
                if (
                    not defect_injected
                    and app.screen.request.command_family == "FOCUSED_TEST"
                    and (repository / "greeting.py").exists()
                ):
                    (repository / "greeting.py").write_text(
                        'def greeting() -> str:\n    return "Goodbye"\n',
                        encoding="utf-8",
                    )
                    (repository / "test_greeting.py").write_text(
                        "from greeting import greeting\n\n"
                        "def test_greeting() -> None:\n"
                        '    assert greeting() == "Goodbye"\n',
                        encoding="utf-8",
                    )
                    defect_injected = True
                approval = next(
                    (
                        button
                        for button in app.screen.query(Button)
                        if button.id in {"approval-accept", "approval-acceptForSession"}
                    ),
                    None,
                )
                assert approval is not None, "Backend approval offered no accepting decision."
                approval.press()
                approval_count += 1
            elif (
                not finalization_requested
                and store.load(run_id).active_step.value == "workspace-finalization"
                and store.load(run_id).run_status is WorkflowRunStatus.AWAITING_USER
                and not composer.disabled
            ):
                app.post_message(Composer.Submitted("/finalize"))
                finalization_requested = True

            await asyncio.sleep(0.1)
            await pilot.pause()

    assert seen_views == {
        "analysis",
        "workspace-preparation",
        "development",
        "code-review",
        "qa",
        "workspace-finalization",
    }
    assert approval_count >= 1
    assert defect_injected
    completed = store.load(run_id)
    assert completed.run_status is WorkflowRunStatus.COMPLETED
    assert completed.capability_profiles == locked_profiles
    assert len(completed.attempts) >= 2
    assert any(item.rework_request is not None for item in completed.attempts)
