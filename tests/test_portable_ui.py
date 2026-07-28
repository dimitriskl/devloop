from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from textual.containers import Horizontal
from textual.widgets import Input, OptionList, Static

from devloop.portable_runtime import PortableRunContext, PortableRuntimeBridge
from devloop.portable_session_catalog import PortableSessionCatalog
from devloop.portable_session_targets import PortableSessionTarget
from devloop.portable_sessions import (
    PortableSessionIntent,
    PortableSessionIntentKind,
    PortableSessionEvent,
    PortableSessionInputKind,
    PortableSessionInputRequest,
    PortableSessionLaunch,
    PortableSessionProgress,
    PortableSessionSnapshot,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
)
from devloop.issue_pack import Issue
from devloop.cli import choose_run_review_action
from devloop.run_review import RunReviewAction, build_run_review, render_run_review
from devloop.statusui import IssueDashboard, Stage
from devloop.portable_ui.app import (
    PortableApplicationShell,
    PortableLogOverlay,
    PortableTextOverlay,
    _launch_for_checkout,
)


class PortableApplicationShellTests(unittest.IsolatedAsyncioTestCase):
    async def test_launch_retargeting_preserves_supplied_work_and_isolates_new_work(
        self,
    ) -> None:
        source = Path("source-checkout").resolve()
        target = Path("new-worktree").resolve()
        planning_arguments = (
            "--repo",
            str(source),
            "--goal",
            "preserve this exact planning goal",
            "--codex=custom-codex",
        )
        delivery_arguments = (
            "--prd",
            "prd/change.md",
            "--issues",
            "prd/change/issues/README.md",
            "--all",
            "--codex",
            "custom-codex",
        )

        supplied_planning = _launch_for_checkout(
            PortableSessionLaunch(
                session_id="planning-source",
                checkout=source,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=planning_arguments,
            ),
            session_id="planning-selected",
            checkout=source,
        )
        new_from_delivery = _launch_for_checkout(
            PortableSessionLaunch(
                session_id="delivery-source",
                checkout=source,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=delivery_arguments,
            ),
            session_id="planning-new-worktree",
            checkout=target,
        )

        self.assertEqual(supplied_planning.arguments, planning_arguments)
        self.assertEqual(
            supplied_planning.operation,
            PortableWorkflowOperation.PLANNING,
        )
        self.assertEqual(
            new_from_delivery.operation,
            PortableWorkflowOperation.PLANNING,
        )
        self.assertEqual(
            new_from_delivery.arguments,
            ("--repo", str(target), "--codex", "custom-codex"),
        )

    async def test_new_session_action_offers_all_worktree_target_kinds(self) -> None:
        class FakeSupervisor:
            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return ()

            def list_saved_projects(self):
                return (
                    SimpleNamespace(
                        project_id="saved-project",
                        checkout=Path.cwd(),
                    ),
                )

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                raise AssertionError(f"Unexpected intent: {intent}")

            def try_next_event(self) -> PortableSessionEvent | None:
                return None

            def shutdown(self) -> None:
                return None

        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=FakeSupervisor(),
            session_launch=PortableSessionLaunch(
                session_id="new-session",
                checkout=Path.cwd(),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(Path.cwd())),
            ),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("+")
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            prompts = {
                str(menu.get_option_at_index(index).prompt)
                for index in range(menu.option_count)
            }

        self.assertIn("Available saved worktree", prompts)
        self.assertIn("Register existing checkout", prompts)
        self.assertIn("Create or reuse Git worktree", prompts)
        self.assertIn("Cancel", prompts)

    async def test_new_session_resolution_error_starts_no_worker_or_catalog_claim(
        self,
    ) -> None:
        class FakeSupervisor:
            def __init__(self) -> None:
                self.intents: list[PortableSessionIntent] = []

            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return ()

            def list_saved_projects(self):
                return ()

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                self.intents.append(intent)
                raise AssertionError("A target error must not reach the supervisor")

            def try_next_event(self) -> PortableSessionEvent | None:
                return None

            def shutdown(self) -> None:
                return None

        class RejectingResolver:
            def resolve(self, request):
                del request
                raise RuntimeError("git worktree add failed: branch is already checked out")

        supervisor = FakeSupervisor()
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,
            session_launch=PortableSessionLaunch(
                session_id="new-session",
                checkout=Path.cwd(),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(Path.cwd())),
            ),
            session_target_resolver=RejectingResolver(),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("+")
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()
            input_widget = app.query_one("#portable-input", Input)
            input_widget.value = str(Path.cwd())
            await pilot.press("enter")
            await pilot.pause()
            detail = str(app.query_one("#portable-detail", Static).render())

        self.assertEqual(supervisor.intents, [])
        self.assertIn("Session was not started", detail)
        self.assertIn("git worktree add failed", detail)

    async def test_target_resolution_notices_are_projected_inside_the_shell(
        self,
    ) -> None:
        notice = "Using existing worktree: resolved-checkout"

        class NoticingResolver:
            def resolve(self, request):
                del request
                return PortableSessionTarget(
                    checkout=Path.cwd(),
                    created=False,
                    notices=(notice,),
                )

        class FakeSupervisor:
            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return ()

            def list_saved_projects(self):
                return ()

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                assert intent.launch is not None
                return PortableSessionSnapshot(
                    session_id=intent.launch.session_id,
                    checkout=intent.launch.checkout,
                    status=PortableSessionStatus.RUNNING,
                )

            def try_next_event(self) -> PortableSessionEvent | None:
                return None

            def shutdown(self) -> None:
                return None

        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=FakeSupervisor(),
            session_launch=PortableSessionLaunch(
                session_id="notice-session",
                checkout=Path.cwd(),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(Path.cwd())),
            ),
            session_target_resolver=NoticingResolver(),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("+")
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()
            app.query_one("#portable-input", Input).value = str(Path.cwd())
            await pilot.press("enter")
            await pilot.pause()

        self.assertIn(notice, [item.message for item in app._activity_feed.items])

    async def test_saved_project_launch_does_not_inherit_another_projects_prd(
        self,
    ) -> None:
        class FakeSupervisor:
            def __init__(self) -> None:
                self.intents: list[PortableSessionIntent] = []

            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return ()

            def list_saved_projects(self):
                return (
                    SimpleNamespace(
                        project_id="project-a",
                        checkout=Path("saved-project").resolve(),
                    ),
                )

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                self.intents.append(intent)
                assert intent.launch is not None
                return PortableSessionSnapshot(
                    session_id=intent.launch.session_id,
                    checkout=intent.launch.checkout,
                    status=PortableSessionStatus.RUNNING,
                )

            def try_next_event(self) -> PortableSessionEvent | None:
                return None

            def shutdown(self) -> None:
                return None

        supervisor = FakeSupervisor()
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,
            session_launch=PortableSessionLaunch(
                session_id="session-saved-project",
                checkout=Path("project-a").resolve(),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(
                    "--prd",
                    str(Path("project-a/prd/change-a.md").resolve()),
                    "--goal",
                    "change that belongs only to project A",
                    "--codex",
                    "custom-codex",
                    "--sandbox",
                    "read-only",
                    "--approval-policy",
                    "on-request",
                    "--native-editor",
                ),
            ),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            self.assertEqual(menu.option_count, 2)
            self.assertIn(
                "[SAVED PROJECT]",
                str(menu.get_option_at_index(1).prompt),
            )
            menu.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()

        self.assertEqual(len(supervisor.intents), 1)
        launch = supervisor.intents[0].launch
        assert launch is not None
        self.assertEqual(launch.checkout, Path("saved-project").resolve())
        self.assertEqual(launch.operation, PortableWorkflowOperation.PLANNING)
        self.assertEqual(
            launch.arguments,
            (
                "--repo",
                str(launch.checkout),
                "--goal",
                "change that belongs only to project A",
                "--codex",
                "custom-codex",
                "--sandbox",
                "read-only",
                "--approval-policy",
                "on-request",
                "--native-editor",
            ),
        )

    async def test_supplied_delivery_launch_keeps_every_original_argument(self) -> None:
        supplied_checkout = Path.cwd()
        supplied_arguments = (
            "--prd",
            "prd/change.md",
            "--issues",
            "prd/change/issues/README.md",
            "--all",
            "--start-issue",
            "0003",
            "--no-self-improvement-wiki",
        )

        class FakeSupervisor:
            def __init__(self) -> None:
                self.intent: PortableSessionIntent | None = None

            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return ()

            def list_saved_projects(self):
                return (
                    SimpleNamespace(
                        project_id="supplied-checkout",
                        checkout=supplied_checkout,
                    ),
                )

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                self.intent = intent
                assert intent.launch is not None
                return PortableSessionSnapshot(
                    session_id=intent.launch.session_id,
                    checkout=intent.launch.checkout,
                    status=PortableSessionStatus.RUNNING,
                )

            def try_next_event(self) -> PortableSessionEvent | None:
                return None

            def shutdown(self) -> None:
                return None

        supervisor = FakeSupervisor()
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,
            session_launch=PortableSessionLaunch(
                session_id="delivery-session",
                checkout=supplied_checkout,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=supplied_arguments,
            ),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()

        assert supervisor.intent is not None
        assert supervisor.intent.launch is not None
        self.assertEqual(
            supervisor.intent.launch.operation,
            PortableWorkflowOperation.DELIVERY,
        )
        self.assertEqual(supervisor.intent.launch.arguments, supplied_arguments)

    async def test_post_resolution_launch_error_renders_not_started(self) -> None:
        class FailingSupervisor:
            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return ()

            def list_saved_projects(self):
                return (
                    SimpleNamespace(
                        project_id="failing-project",
                        checkout=Path.cwd(),
                    ),
                )

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                del intent
                raise RuntimeError("Portable worker launch failed")

            def try_next_event(self) -> PortableSessionEvent | None:
                return None

            def shutdown(self) -> None:
                return None

        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=FailingSupervisor(),
            session_launch=PortableSessionLaunch(
                session_id="launch-error",
                checkout=Path.cwd(),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(Path.cwd()), "--goal", "keep me"),
            ),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()
            detail = str(app.query_one("#portable-detail", Static).render())
            status = str(app.query_one("#portable-status", Static).render())

        self.assertIn("Session was not started", detail)
        self.assertIn("Portable worker launch failed", detail)
        self.assertEqual(status, "NOT STARTED")

    async def test_supervisor_checkout_transfer_reaches_active_and_sessions_views(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            worktree = root / "implementation"
            source.mkdir()
            subprocess.run(
                ["git", "init"],
                cwd=source,
                check=True,
                capture_output=True,
                text=True,
            )
            prd_directory = source / "prd" / "delivery-transfer"
            issues_directory = prd_directory / "issues"
            issues_directory.mkdir(parents=True)
            prd = prd_directory / "delivery-transfer.md"
            issues_index = issues_directory / "README.md"
            issue = issues_directory / "0001-transfer.md"
            prd.write_text(
                "# Delivery Transfer\n\n"
                "## Target Product\n\n"
                "Product: devloop-plan + devloop\n",
                encoding="utf-8",
            )
            issues_index.write_text(
                "- [Transfer](./0001-transfer.md)\n",
                encoding="utf-8",
            )
            issue.write_text(
                "# Transfer\n\n"
                "## Target Product\n\n"
                "Product: devloop-plan + devloop\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "."],
                cwd=source,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Dev Loop Tests",
                    "-c",
                    "user.email=devloop-tests@example.invalid",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=source,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    "feature/delivery-transfer",
                    str(worktree),
                ],
                cwd=source,
                check=True,
                capture_output=True,
                text=True,
            )
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            owner_id = "shell-transfer-ui"
            supervisor = PortableSessionSupervisor(
                catalog=catalog,
                owner_id=owner_id,
            )
            app = PortableApplicationShell(
                PortableRuntimeBridge(),
                session_supervisor=supervisor,
                session_launch=PortableSessionLaunch(
                    session_id="session-transfer-ui",
                    checkout=source,
                    operation=PortableWorkflowOperation.DELIVERY,
                    arguments=(
                        "--prd",
                        str(prd),
                        "--issues",
                        str(issues_index),
                        "--all",
                        "--create-worktree",
                        "--worktree-path",
                        str(worktree),
                        "--branch-name",
                        "feature/delivery-transfer",
                        "--non-interactive",
                        "--dry-run",
                        "--no-self-improvement-wiki",
                    ),
                ),
            )

            async with app.run_test(size=(110, 32)) as pilot:
                snapshot = app._launch_new_session_at_checkout(source)
                assert snapshot is not None
                app._active_session_id = snapshot.session_id
                app._show_session_snapshot(snapshot)
                for _attempt in range(200):
                    transferred = supervisor.snapshot(snapshot.session_id)
                    if transferred.context is not None:
                        break
                    await asyncio.sleep(0.01)
                else:
                    self.fail("Delivery worker context did not reach the supervisor")
                source_lease_at_context = catalog.get_worktree_lease(source)
                worktree_lease_at_context = catalog.get_worktree_lease(worktree)
                completed = await asyncio.to_thread(
                    supervisor.wait_for_terminal,
                    snapshot.session_id,
                    timeout=20,
                )
                await pilot.pause()

                app._show_session_snapshot(completed)
                active_detail = str(
                    app.query_one("#portable-detail", Static).render()
                )
                app._show_sessions_tab()
                menu = app.query_one("#portable-navigation", OptionList)
                prompts = [
                    str(menu.get_option_at_index(index).prompt)
                    for index in range(menu.option_count)
                ]
                catalog_session = catalog.get_session(snapshot.session_id)
                cached_launch = supervisor._launches[snapshot.session_id]

            self.assertEqual(completed.status, PortableSessionStatus.COMPLETED)
            self.assertEqual(transferred.checkout, worktree.resolve())
            self.assertIsNotNone(transferred.context)
            self.assertIn(f"Checkout: {worktree.resolve()}", active_detail)
            self.assertIn(f"Worktree: {worktree.resolve()}", active_detail)
            self.assertIsNone(source_lease_at_context)
            self.assertIsNotNone(worktree_lease_at_context)
            assert worktree_lease_at_context is not None
            self.assertEqual(
                worktree_lease_at_context.session_id,
                snapshot.session_id,
            )
            self.assertEqual(catalog_session.checkout, worktree.resolve())
            self.assertEqual(cached_launch.checkout, worktree.resolve())
            self.assertTrue(
                any(
                    prompt == f"{worktree.name} [SAVED PROJECT]"
                    for prompt in prompts
                )
            )
            self.assertTrue(
                any(
                    prompt == f"{worktree.name} [COMPLETED]"
                    for prompt in prompts
                )
            )

    async def test_restored_session_requires_explicit_resume_from_sessions_tab(
        self,
    ) -> None:
        class FakeSupervisor:
            def __init__(self) -> None:
                self.intents: list[PortableSessionIntent] = []
                self.restored = PortableSessionSnapshot(
                    session_id="session-restored",
                    checkout=Path.cwd(),
                    status=PortableSessionStatus.READY,
                )

            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return (self.restored,)

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                self.intents.append(intent)
                return PortableSessionSnapshot(
                    session_id=intent.session_id,
                    checkout=self.restored.checkout,
                    status=PortableSessionStatus.RUNNING,
                )

            def try_next_event(self) -> PortableSessionEvent | None:
                return None

            def shutdown(self) -> None:
                return None

        supervisor = FakeSupervisor()
        launch = PortableSessionLaunch(
            session_id="session-new",
            checkout=Path.cwd(),
            operation=PortableWorkflowOperation.PLANNING,
            arguments=(),
        )
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,
            session_launch=launch,
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            self.assertEqual(supervisor.intents, [])
            self.assertEqual(menu.option_count, 2)

            menu.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(supervisor.intents, [])
            resume_menu = app.query_one("#portable-navigation", OptionList)
            self.assertEqual(
                str(resume_menu.get_option_at_index(0).prompt),
                "Resume",
            )

            await pilot.press("f2")
            await pilot.pause()

        self.assertEqual(len(supervisor.intents), 1)
        self.assertEqual(
            supervisor.intents[0].kind,
            PortableSessionIntentKind.RESUME,
        )
        self.assertEqual(
            supervisor.intents[0].session_id,
            "session-restored",
        )

    async def test_sessions_tab_drops_a_reconciled_synthetic_candidate(self) -> None:
        checkout = Path.cwd()
        real_session = PortableSessionSnapshot(
            session_id="planning-session",
            checkout=checkout,
            status=PortableSessionStatus.RUNNING,
        )
        synthetic_candidate = PortableSessionSnapshot(
            session_id="synthetic-candidate",
            checkout=checkout,
            status=PortableSessionStatus.READY,
            prd_path=checkout / "change.md",
        )

        class FakeSupervisor:
            def __init__(self) -> None:
                self.sessions = (real_session, synthetic_candidate)
                self.events: list[PortableSessionEvent] = []

            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return self.sessions

            def try_next_event(self) -> PortableSessionEvent | None:
                return self.events.pop(0) if self.events else None

            def shutdown(self) -> None:
                return None

        supervisor = FakeSupervisor()
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,
            session_launch=PortableSessionLaunch(
                session_id="new-session",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            ),
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            self.assertEqual(menu.option_count, 3)

            published = PortableSessionSnapshot(
                session_id=real_session.session_id,
                checkout=checkout,
                status=PortableSessionStatus.READY,
                prd_path=synthetic_candidate.prd_path,
            )
            supervisor.sessions = (published,)
            supervisor.events.append(PortableSessionEvent(published))
            await pilot.pause()

            self.assertEqual(menu.option_count, 2)
            self.assertIn(
                "change [READY]",
                str(menu.get_option_at_index(1).prompt),
            )

    async def test_ctrl_c_during_an_active_session_preserves_interrupted_exit_code(
        self,
    ) -> None:
        class FakeSupervisor:
            def __init__(self) -> None:
                self.shutdown_called = False

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                assert intent.launch is not None
                return PortableSessionSnapshot(
                    session_id=intent.launch.session_id,
                    checkout=intent.launch.checkout,
                    status=PortableSessionStatus.RUNNING,
                )

            def try_next_event(self) -> PortableSessionEvent | None:
                return None

            def shutdown(self) -> None:
                self.shutdown_called = True

        supervisor = FakeSupervisor()
        launch = PortableSessionLaunch(
            session_id="session-interrupted",
            checkout=Path.cwd(),
            operation=PortableWorkflowOperation.PLANNING,
            arguments=(),
        )
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,
            session_launch=launch,
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("f2")
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()

        self.assertEqual(app.operation_result, 130)
        self.assertTrue(supervisor.shutdown_called)

    async def test_background_session_updates_sessions_list_without_stealing_focus(
        self,
    ) -> None:
        class FakeSupervisor:
            def __init__(self) -> None:
                self.events: list[PortableSessionEvent] = []

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                assert intent.launch is not None
                return PortableSessionSnapshot(
                    session_id=intent.launch.session_id,
                    checkout=intent.launch.checkout,
                    status=PortableSessionStatus.RUNNING,
                )

            def try_next_event(self) -> PortableSessionEvent | None:
                return self.events.pop(0) if self.events else None

            def shutdown(self) -> None:
                return None

        supervisor = FakeSupervisor()
        launch = PortableSessionLaunch(
            session_id="session-background",
            checkout=Path.cwd(),
            operation=PortableWorkflowOperation.PLANNING,
            arguments=(),
        )
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,
            session_launch=launch,
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("f2")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            supervisor.events.append(
                PortableSessionEvent(
                    PortableSessionSnapshot(
                        session_id=launch.session_id,
                        checkout=launch.checkout,
                        status=PortableSessionStatus.WAITING_FOR_INPUT,
                        activity=("Background worker needs attention",),
                    )
                )
            )
            for _ in range(20):
                await pilot.pause()
                menu = app.query_one("#portable-navigation", OptionList)
                if (
                    menu.option_count == 2
                    and "WAITING_FOR_INPUT"
                    in str(menu.get_option_at_index(1).prompt)
                ):
                    break

            self.assertEqual(
                str(app.query_one("#portable-tabs", Static).render()),
                "Sessions",
            )
            self.assertIn(
                "WAITING_FOR_INPUT",
                str(menu.get_option_at_index(1).prompt),
            )

    async def test_switching_sessions_retains_open_tabs_and_marks_background_activity(
        self,
    ) -> None:
        checkout_a = Path("alpha").resolve()
        checkout_b = Path("beta").resolve()
        session_a = PortableSessionSnapshot(
            session_id="session-alpha",
            checkout=checkout_a,
            status=PortableSessionStatus.RUNNING,
        )
        session_b = PortableSessionSnapshot(
            session_id="session-beta",
            checkout=checkout_b,
            status=PortableSessionStatus.RUNNING,
        )

        class FakeSupervisor:
            def __init__(self) -> None:
                self.events: list[PortableSessionEvent] = []

            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return (session_a, session_b)

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                raise AssertionError(f"Unexpected intent: {intent}")

            def try_next_event(self) -> PortableSessionEvent | None:
                return self.events.pop(0) if self.events else None

            def shutdown(self) -> None:
                return None

        supervisor = FakeSupervisor()
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,
            session_launch=PortableSessionLaunch(
                session_id="new-session",
                checkout=Path.cwd(),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            ),
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()

            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 0
            await pilot.press("enter")
            await pilot.pause()

            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 2
            await pilot.press("enter")
            await pilot.pause()

            supervisor.events.append(
                PortableSessionEvent(
                    PortableSessionSnapshot(
                        session_id=session_a.session_id,
                        checkout=checkout_a,
                        status=PortableSessionStatus.RUNNING,
                        activity=("Alpha advanced in the background",),
                    )
                )
            )
            for _ in range(20):
                await pilot.pause()
                tabs = str(app.query_one("#portable-tabs", Static).render())
                if "Alpha advanced" not in tabs and "*" in tabs:
                    break

            detail = str(app.query_one("#portable-detail", Static).render())

        self.assertIn("alpha [RUNNING] *", tabs)
        self.assertIn("beta [RUNNING]", tabs)
        self.assertIn(str(checkout_b), detail)
        self.assertNotIn(str(checkout_a), detail)

    async def test_switching_to_session_without_context_clears_left_pane_and_f5(
        self,
    ) -> None:
        checkout_a = Path("context-alpha").resolve()
        checkout_b = Path("context-beta").resolve()
        context_a = PortableRunContext(
            project_root=str(checkout_a.parent),
            implementation_branch="feature/context-alpha",
            implementation_worktree=str(checkout_a),
            prd_path=str(checkout_a / "prd" / "alpha.md"),
        )
        session_a = PortableSessionSnapshot(
            session_id="session-context-alpha",
            checkout=checkout_a,
            status=PortableSessionStatus.RUNNING,
            context=context_a,
        )
        session_b = PortableSessionSnapshot(
            session_id="session-context-beta",
            checkout=checkout_b,
            status=PortableSessionStatus.RUNNING,
            context=None,
        )

        class FakeSupervisor:
            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return (session_a, session_b)

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                raise AssertionError(f"Unexpected intent: {intent}")

            def try_next_event(self) -> PortableSessionEvent | None:
                return None

            def shutdown(self) -> None:
                return None

        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=FakeSupervisor(),
            session_launch=PortableSessionLaunch(
                session_id="new-session",
                checkout=Path.cwd(),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            ),
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            sessions_menu = app.query_one("#portable-navigation", OptionList)
            sessions_menu.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()

            context_view = app.query_one("#portable-run-context", Static)
            self.assertTrue(context_view.display)
            self.assertIn(context_a.project_root, str(context_view.render()))

            active_menu = app.query_one("#portable-navigation", OptionList)
            active_menu.highlighted = 0
            await pilot.press("enter")
            await pilot.pause()
            sessions_menu = app.query_one("#portable-navigation", OptionList)
            sessions_menu.highlighted = 2
            await pilot.press("enter")
            await pilot.pause()

            self.assertFalse(context_view.display)
            self.assertNotIn(context_a.project_root, str(context_view.render()))
            await pilot.press("f5")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, PortableTextOverlay)

    async def test_background_approval_marks_attention_without_receiving_active_input(
        self,
    ) -> None:
        checkout_a = Path("alpha").resolve()
        checkout_b = Path("beta").resolve()
        session_a = PortableSessionSnapshot(
            session_id="session-alpha",
            checkout=checkout_a,
            status=PortableSessionStatus.RUNNING,
        )
        session_b = PortableSessionSnapshot(
            session_id="session-beta",
            checkout=checkout_b,
            status=PortableSessionStatus.RUNNING,
        )

        class FakeSupervisor:
            def __init__(self) -> None:
                self.events: list[PortableSessionEvent] = []
                self.intents: list[PortableSessionIntent] = []

            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return (session_a, session_b)

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                self.intents.append(intent)
                return PortableSessionSnapshot(
                    session_id=intent.session_id,
                    checkout=checkout_b,
                    status=PortableSessionStatus.RUNNING,
                )

            def try_next_event(self) -> PortableSessionEvent | None:
                return self.events.pop(0) if self.events else None

            def shutdown(self) -> None:
                return None

        class RecordingShell(PortableApplicationShell):
            def __init__(self, *args, **kwargs) -> None:
                self.bell_count = 0
                super().__init__(*args, **kwargs)

            def bell(self) -> None:
                self.bell_count += 1

        supervisor = FakeSupervisor()
        with mock.patch.dict(
            os.environ,
            {"DEVLOOP_SESSION_ATTENTION_BELL": "1"},
        ):
            app = RecordingShell(
                PortableRuntimeBridge(),
                session_supervisor=supervisor,
                session_launch=PortableSessionLaunch(
                    session_id="new-session",
                    checkout=Path.cwd(),
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                ),
            )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 0
            await pilot.press("enter")
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 2
            await pilot.press("enter")
            await pilot.pause()

            supervisor.events.extend(
                (
                    PortableSessionEvent(
                        PortableSessionSnapshot(
                            session_id=session_a.session_id,
                            checkout=checkout_a,
                            status=PortableSessionStatus.WAITING_FOR_INPUT,
                            input_request=PortableSessionInputRequest(
                                kind=PortableSessionInputKind.APPROVAL,
                                prompt="Approve alpha command?",
                                options=(
                                    ("approve", "Approve"),
                                    ("deny", "Deny"),
                                ),
                                default_key="deny",
                            ),
                        )
                    ),
                    PortableSessionEvent(
                        PortableSessionSnapshot(
                            session_id=session_b.session_id,
                            checkout=checkout_b,
                            status=PortableSessionStatus.WAITING_FOR_INPUT,
                            input_request=PortableSessionInputRequest(
                                kind=PortableSessionInputKind.TEXT,
                                prompt="Beta response",
                            ),
                        )
                    ),
                )
            )
            for _ in range(20):
                await pilot.pause()
                input_widget = app.query_one("#portable-input", Input)
                if input_widget.display:
                    break

            tabs = str(app.query_one("#portable-tabs", Static).render())
            input_widget.value = "beta only"
            await pilot.press("enter")
            await pilot.pause()

        self.assertIn("alpha [WAITING_FOR_INPUT] [INPUT!]", tabs)
        self.assertIn("beta [WAITING_FOR_INPUT]", tabs)
        self.assertEqual(app.bell_count, 1)
        self.assertEqual(len(supervisor.intents), 1)
        self.assertEqual(
            supervisor.intents[0],
            PortableSessionIntent(
                kind=PortableSessionIntentKind.PROVIDE_INPUT,
                session_id=session_b.session_id,
                value="beta only",
            ),
        )

    async def test_active_choice_values_cannot_collide_with_session_or_navigation_ids(
        self,
    ) -> None:
        checkout_a = Path("choice-owner").resolve()
        checkout_b = Path("choice-sibling").resolve()
        adversarial_values = (
            "session-beta",
            "__new_session__",
            "__sessions__",
            "__new_session_back__",
            "+ New Session",
            "Back",
        )

        class FakeSupervisor:
            def __init__(self) -> None:
                self.current_owner = self.waiting_snapshot(adversarial_values[0])
                self.sibling = PortableSessionSnapshot(
                    session_id="session-beta",
                    checkout=checkout_b,
                    status=PortableSessionStatus.RUNNING,
                )
                self.events: list[PortableSessionEvent] = []
                self.intents: list[PortableSessionIntent] = []

            def waiting_snapshot(self, value: str) -> PortableSessionSnapshot:
                return PortableSessionSnapshot(
                    session_id="session-alpha",
                    checkout=checkout_a,
                    status=PortableSessionStatus.WAITING_FOR_INPUT,
                    input_request=PortableSessionInputRequest(
                        kind=PortableSessionInputKind.CHOICE,
                        prompt="Choose the exact worker value",
                        options=((value, f"Send {value}"),),
                        default_key=value,
                    ),
                )

            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return (self.current_owner, self.sibling)

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                self.intents.append(intent)
                return PortableSessionSnapshot(
                    session_id=intent.session_id,
                    checkout=checkout_a,
                    status=PortableSessionStatus.RUNNING,
                )

            def try_next_event(self) -> PortableSessionEvent | None:
                return self.events.pop(0) if self.events else None

            def shutdown(self) -> None:
                return None

        supervisor = FakeSupervisor()
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,
            session_launch=PortableSessionLaunch(
                session_id="new-session",
                checkout=Path.cwd(),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            ),
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()

            for value in adversarial_values:
                supervisor.current_owner = supervisor.waiting_snapshot(value)
                supervisor.events.append(
                    PortableSessionEvent(supervisor.current_owner)
                )
                for _ in range(20):
                    await pilot.pause()
                    menu = app.query_one("#portable-navigation", OptionList)
                    if (
                        menu.option_count == 1
                        and str(menu.get_option_at_index(0).prompt)
                        == f"Send {value}"
                    ):
                        break
                menu.highlighted = 0
                await pilot.press("enter")
                await pilot.pause()

        self.assertEqual(
            supervisor.intents,
            [
                PortableSessionIntent(
                    kind=PortableSessionIntentKind.PROVIDE_INPUT,
                    session_id="session-alpha",
                    value=value,
                )
                for value in adversarial_values
            ],
        )

    async def test_queued_choice_from_previous_tab_cannot_use_rebuilt_mapping(
        self,
    ) -> None:
        session_a = PortableSessionSnapshot(
            session_id="session-alpha",
            checkout=Path("choice-alpha").resolve(),
            status=PortableSessionStatus.WAITING_FOR_INPUT,
            input_request=PortableSessionInputRequest(
                kind=PortableSessionInputKind.CHOICE,
                prompt="Alpha choice",
                options=(("alpha-value", "Send alpha"),),
                default_key="alpha-value",
            ),
        )
        session_b = PortableSessionSnapshot(
            session_id="session-beta",
            checkout=Path("choice-beta").resolve(),
            status=PortableSessionStatus.WAITING_FOR_INPUT,
            input_request=PortableSessionInputRequest(
                kind=PortableSessionInputKind.CHOICE,
                prompt="Beta choice",
                options=(("__sessions__", "Send adversarial beta value"),),
                default_key="__sessions__",
            ),
        )

        class FakeSupervisor:
            def __init__(self) -> None:
                self.intents: list[PortableSessionIntent] = []

            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return (session_a, session_b)

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                self.intents.append(intent)
                return PortableSessionSnapshot(
                    session_id=intent.session_id,
                    checkout=session_b.checkout,
                    status=PortableSessionStatus.RUNNING,
                )

            def try_next_event(self) -> None:
                return None

            def shutdown(self) -> None:
                return None

        supervisor = FakeSupervisor()
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,
            session_launch=PortableSessionLaunch(
                session_id="new-session",
                checkout=Path.cwd(),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            ),
        )

        async with app.run_test(size=(120, 34)) as pilot:
            app._active_session_id = session_a.session_id
            app._show_session_snapshot(session_a)
            menu = app.query_one("#portable-navigation", OptionList)
            queued_alpha_option = menu.get_option_at_index(0)

            app._active_session_id = session_b.session_id
            app._show_session_snapshot(session_b)
            current_beta_option = menu.get_option_at_index(0)
            beta_option_id = current_beta_option.id
            self.assertNotEqual(queued_alpha_option.id, beta_option_id)
            self.assertIn(session_a.session_id, queued_alpha_option.id or "")
            self.assertIn(session_b.session_id, beta_option_id or "")

            app.post_message(
                OptionList.OptionSelected(menu, queued_alpha_option, 0)
            )
            await pilot.pause()

            self.assertEqual(supervisor.intents, [])
            self.assertIn(
                "INPUT NOT SENT",
                str(app.query_one("#portable-status", Static).render()),
            )
            self.assertEqual(
                str(menu.get_option_at_index(0).prompt),
                "Send adversarial beta value",
            )

            menu.highlighted = 0
            await pilot.press("enter")
            await pilot.pause()

            next_session_b = replace(
                session_b,
                input_request=PortableSessionInputRequest(
                    kind=PortableSessionInputKind.CHOICE,
                    prompt="Next beta choice",
                    options=(("__sessions__", "Send next beta value"),),
                    default_key="__sessions__",
                ),
            )
            app._show_session_snapshot(next_session_b)
            self.assertNotEqual(
                current_beta_option.id,
                menu.get_option_at_index(0).id,
            )
            app.post_message(
                OptionList.OptionSelected(menu, current_beta_option, 0)
            )
            await pilot.pause()

        self.assertEqual(
            supervisor.intents,
            [
                PortableSessionIntent(
                    kind=PortableSessionIntentKind.PROVIDE_INPUT,
                    session_id=session_b.session_id,
                    value="__sessions__",
                )
            ],
        )

    async def test_crashed_waiting_session_clears_input_while_sibling_completes(
        self,
    ) -> None:
        worker_source = textwrap.dedent(
            """
            import json
            import sys
            import time
            from pathlib import Path

            session_id = sys.argv[1]
            crash_marker = Path(sys.argv[2])

            def send(sequence, kind, payload):
                print(json.dumps({
                    "version": 1,
                    "session_id": session_id,
                    "sequence": sequence,
                    "kind": kind,
                    "payload": payload,
                }), flush=True)

            json.loads(sys.stdin.readline())
            send(1, "INPUT_REQUEST", {
                "request_kind": "TEXT",
                "prompt": "Value for " + session_id,
            })
            if session_id == "session-crashing":
                while not crash_marker.exists():
                    time.sleep(0.01)
                raise SystemExit(17)

            answer = json.loads(sys.stdin.readline())["payload"]["value"]
            send(2, "ACTIVITY", {"message": "continued with " + answer})
            send(3, "COMPLETION", {"exit_code": 0})
            """
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout_a = root / "alpha"
            checkout_b = root / "beta"
            checkout_a.mkdir()
            checkout_b.mkdir()
            crash_marker = root / "crash-worker"

            def launch_worker(
                launch: PortableSessionLaunch,
            ) -> subprocess.Popen[str]:
                return subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        worker_source,
                        launch.session_id,
                        str(crash_marker),
                    ],
                    cwd=launch.checkout,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )

            crashing = PortableSessionLaunch(
                session_id="session-crashing",
                checkout=checkout_a,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            continuing = PortableSessionLaunch(
                session_id="session-continuing",
                checkout=checkout_b,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=(),
            )
            supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
            supervisor.start_session(crashing)
            supervisor.start_session(continuing)
            for _attempt in range(500):
                if all(
                    supervisor.snapshot(session_id).status
                    is PortableSessionStatus.WAITING_FOR_INPUT
                    for session_id in (crashing.session_id, continuing.session_id)
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("Both workers did not reach their input requests.")

            app = PortableApplicationShell(
                PortableRuntimeBridge(),
                session_supervisor=supervisor,
                session_launch=crashing,
                attention_bell=False,
            )

            async with app.run_test(size=(120, 34)) as pilot:
                app._active_session_id = crashing.session_id
                app._show_session_snapshot(supervisor.snapshot(crashing.session_id))
                app._show_sessions_tab()
                app._active_session_id = continuing.session_id
                app._show_session_snapshot(supervisor.snapshot(continuing.session_id))

                crash_marker.write_text("crash\n", encoding="utf-8")
                for _attempt in range(500):
                    await asyncio.sleep(0.01)
                    failed = supervisor.snapshot(crashing.session_id)
                    if (
                        failed.status is PortableSessionStatus.FAILED
                        and app._session_snapshots.get(crashing.session_id) == failed
                    ):
                        break
                else:
                    self.fail("Crashed worker did not become a visible failed session.")

                background_tabs = str(
                    app.query_one("#portable-tabs", Static).render()
                )
                failed_tab = next(
                    tab
                    for tab in background_tabs.split(" | ")
                    if tab.startswith("alpha ")
                )
                app._active_session_id = crashing.session_id
                app._show_session_snapshot(failed)
                failed_input = app.query_one("#portable-input", Input)
                failed_input_displayed = failed_input.display
                failed_menu = app.query_one("#portable-navigation", OptionList)
                failed_options = [
                    str(failed_menu.get_option_at_index(index).prompt)
                    for index in range(failed_menu.option_count)
                ]

                app._active_session_id = continuing.session_id
                app._show_session_snapshot(
                    supervisor.snapshot(continuing.session_id)
                )
                sibling_input = app.query_one("#portable-input", Input)
                sibling_input.value = "beta-only"
                await pilot.press("enter")
                completed = await asyncio.to_thread(
                    supervisor.wait_for_terminal,
                    continuing.session_id,
                    timeout=5,
                )
                await pilot.pause()

            self.assertEqual(failed.status, PortableSessionStatus.FAILED)
            self.assertIsNone(failed.input_request)
            self.assertIn("alpha [FAILED]", failed_tab)
            self.assertNotIn("[INPUT!]", failed_tab)
            self.assertFalse(failed_input_displayed)
            self.assertEqual(failed_options, ["Sessions", "alpha [FAILED]"])
            self.assertEqual(completed.status, PortableSessionStatus.COMPLETED)
            self.assertEqual(completed.activity, ("continued with beta-only",))

    async def test_stale_input_after_session_retirement_keeps_sibling_usable(
        self,
    ) -> None:
        retiring = PortableSessionSnapshot(
            session_id="session-retiring",
            checkout=Path("retiring").resolve(),
            status=PortableSessionStatus.WAITING_FOR_INPUT,
            input_request=PortableSessionInputRequest(
                kind=PortableSessionInputKind.TEXT,
                prompt="Retiring value",
            ),
        )
        sibling = PortableSessionSnapshot(
            session_id="session-sibling",
            checkout=Path("sibling").resolve(),
            status=PortableSessionStatus.WAITING_FOR_INPUT,
            input_request=PortableSessionInputRequest(
                kind=PortableSessionInputKind.TEXT,
                prompt="Sibling value",
            ),
        )

        class RaceSupervisor:
            def __init__(self) -> None:
                self.snapshots = {
                    retiring.session_id: retiring,
                    sibling.session_id: sibling,
                }
                self.events: list[PortableSessionEvent] = []
                self.retirement_event_released = False

            def retire_before_ui_drain(self) -> None:
                retired = PortableSessionSnapshot(
                    session_id=retiring.session_id,
                    checkout=retiring.checkout,
                    status=PortableSessionStatus.FAILED,
                    diagnostics=("worker exited before input arrived",),
                    result=17,
                )
                self.snapshots[retiring.session_id] = retired
                self.events.append(PortableSessionEvent(retired))

            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return tuple(self.snapshots.values())

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                if intent.session_id == retiring.session_id:
                    self.retirement_event_released = True
                    raise ValueError(
                        "\x1b[31mPortable session is terminal and cannot accept "
                        f"input: {retiring.session_id}\x1b[0m " + ("x" * 500)
                    )
                if intent.session_id != sibling.session_id:
                    raise AssertionError(f"Unexpected intent: {intent}")
                completed = PortableSessionSnapshot(
                    session_id=sibling.session_id,
                    checkout=sibling.checkout,
                    status=PortableSessionStatus.COMPLETED,
                    activity=(f"continued with {intent.value}",),
                    result=0,
                )
                self.snapshots[sibling.session_id] = completed
                self.events.append(PortableSessionEvent(completed))
                return completed

            def try_next_event(self) -> PortableSessionEvent | None:
                if not self.retirement_event_released:
                    return None
                return self.events.pop(0) if self.events else None

            def shutdown(self) -> None:
                return None

        supervisor = RaceSupervisor()
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,
            session_launch=PortableSessionLaunch(
                session_id="new-session",
                checkout=Path.cwd(),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            ),
            attention_bell=False,
        )

        async with app.run_test(size=(120, 34)) as pilot:
            app._active_session_id = retiring.session_id
            app._show_session_snapshot(retiring)
            retiring_input = app.query_one("#portable-input", Input)
            retiring_input.value = "stale value"

            supervisor.retire_before_ui_drain()
            await pilot.press("enter")
            await pilot.pause()

            retired_detail = str(
                app.query_one("#portable-detail", Static).render()
            )
            rejection_status = str(
                app.query_one("#portable-status", Static).render()
            )
            self.assertTrue(app.is_running)
            self.assertIn("Status: FAILED", retired_detail)
            self.assertIn("INPUT NOT SENT", rejection_status)
            self.assertIn("terminal and cannot accept input", rejection_status)
            self.assertNotIn("\x1b", rejection_status)
            self.assertLessEqual(len(rejection_status), 180)
            self.assertFalse(retiring_input.display)

            app._active_session_id = sibling.session_id
            app._show_session_snapshot(supervisor.snapshots[sibling.session_id])
            sibling_input = app.query_one("#portable-input", Input)
            sibling_input.value = "beta-only"
            await pilot.press("enter")
            await pilot.pause()

            self.assertTrue(app.is_running)
            self.assertEqual(
                supervisor.snapshots[sibling.session_id].status,
                PortableSessionStatus.COMPLETED,
            )
            self.assertEqual(
                supervisor.snapshots[sibling.session_id].activity,
                ("continued with beta-only",),
            )

    async def test_escape_hides_only_the_tab_and_reopen_uses_retained_projection(
        self,
    ) -> None:
        checkout = Path("hidden-session").resolve()
        initial = PortableSessionSnapshot(
            session_id="session-hidden",
            checkout=checkout,
            status=PortableSessionStatus.RUNNING,
        )

        class FakeSupervisor:
            def __init__(self) -> None:
                self.events: list[PortableSessionEvent] = []
                self.intents: list[PortableSessionIntent] = []
                self.current = initial

            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return (self.current,)

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                self.intents.append(intent)
                raise AssertionError(f"Unexpected intent: {intent}")

            def try_next_event(self) -> PortableSessionEvent | None:
                if not self.events:
                    return None
                event = self.events.pop(0)
                self.current = event.snapshot
                return event

            def shutdown(self) -> None:
                return None

        supervisor = FakeSupervisor()
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,
            session_launch=PortableSessionLaunch(
                session_id="new-session",
                checkout=Path.cwd(),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            ),
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            hidden_tabs = str(app.query_one("#portable-tabs", Static).render())
            supervisor.events.append(
                PortableSessionEvent(
                    PortableSessionSnapshot(
                        session_id=initial.session_id,
                        checkout=checkout,
                        status=PortableSessionStatus.RUNNING,
                        activity=("Continued while hidden",),
                    )
                )
            )
            for _ in range(20):
                await pilot.pause()
                detail = str(app.query_one("#portable-detail", Static).render())
                if "Continued while hidden" in detail:
                    break

            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()
            reopened_tabs = str(app.query_one("#portable-tabs", Static).render())
            reopened_detail = str(
                app.query_one("#portable-detail", Static).render()
            )

        self.assertEqual(hidden_tabs, "Sessions")
        self.assertIn("hidden-session [RUNNING]", reopened_tabs)
        self.assertIn("Latest activity: Continued while hidden", reopened_detail)
        self.assertEqual(supervisor.intents, [])

    async def test_background_session_failure_is_the_application_result_on_exit(
        self,
    ) -> None:
        class FakeSupervisor:
            def __init__(self) -> None:
                self.events: list[PortableSessionEvent] = []

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                assert intent.launch is not None
                return PortableSessionSnapshot(
                    session_id=intent.launch.session_id,
                    checkout=intent.launch.checkout,
                    status=PortableSessionStatus.RUNNING,
                )

            def try_next_event(self) -> PortableSessionEvent | None:
                return self.events.pop(0) if self.events else None

            def shutdown(self) -> None:
                return None

        supervisor = FakeSupervisor()
        launch = PortableSessionLaunch(
            session_id="session-background-failure",
            checkout=Path.cwd(),
            operation=PortableWorkflowOperation.PLANNING,
            arguments=(),
        )
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,
            session_launch=launch,
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("f2")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            supervisor.events.append(
                PortableSessionEvent(
                    PortableSessionSnapshot(
                        session_id=launch.session_id,
                        checkout=launch.checkout,
                        status=PortableSessionStatus.FAILED,
                        result=7,
                    )
                )
            )
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            await pilot.press("ctrl+c")
            await pilot.pause()

        self.assertEqual(app.operation_result, 7)

    async def test_sessions_tab_is_passive_until_new_session_is_selected(
        self,
    ) -> None:
        class FakeSupervisor:
            def __init__(self) -> None:
                self.intents: list[PortableSessionIntent] = []
                self.events: list[PortableSessionEvent] = []

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                self.intents.append(intent)
                assert intent.launch is not None
                return PortableSessionSnapshot(
                    session_id=intent.launch.session_id,
                    checkout=intent.launch.checkout,
                    status=PortableSessionStatus.RUNNING,
                )

            def try_next_event(self) -> PortableSessionEvent | None:
                return self.events.pop(0) if self.events else None

            def complete(self, launch: PortableSessionLaunch) -> None:
                self.events.append(
                    PortableSessionEvent(
                        PortableSessionSnapshot(
                            session_id=launch.session_id,
                            checkout=launch.checkout,
                            status=PortableSessionStatus.COMPLETED,
                            activity=("Planning completed",),
                            result=0,
                        )
                    )
                )

            def shutdown(self) -> None:
                return None

        supervisor = FakeSupervisor()
        launch = PortableSessionLaunch(
            session_id="session-ui",
            checkout=Path.cwd(),
            operation=PortableWorkflowOperation.PLANNING,
            arguments=(),
        )
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,
            session_launch=launch,
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()

            self.assertEqual(supervisor.intents, [])
            self.assertEqual(
                str(app.query_one("#portable-tabs", Static).render()),
                "Sessions",
            )
            self.assertIn(
                "No workflow worker starts automatically",
                str(app.query_one("#portable-detail", Static).render()),
            )

            await pilot.press("f2")
            await pilot.pause()

            self.assertEqual(len(supervisor.intents), 1)
            self.assertIn(
                "[RUNNING]",
                str(app.query_one("#portable-tabs", Static).render()),
            )

            supervisor.complete(launch)
            for _ in range(20):
                await pilot.pause()
                if "COMPLETED" in str(
                    app.query_one("#portable-status", Static).render()
                ):
                    break
            self.assertIn(
                "Result: 0",
                str(app.query_one("#portable-detail", Static).render()),
            )

            await pilot.press("escape")
            await pilot.pause()

            self.assertEqual(len(supervisor.intents), 1)
            self.assertEqual(
                app.query_one("#portable-navigation", OptionList).option_count,
                2,
            )

    async def test_sessions_tab_aggregates_each_session_monitoring_projection(
        self,
    ) -> None:
        checkout = Path("monitor-worktree").resolve()
        prd_path = checkout / "prd" / "change.md"
        snapshot = PortableSessionSnapshot(
            session_id="session-monitor",
            checkout=checkout,
            status=PortableSessionStatus.RUNNING,
            context=PortableRunContext(
                project_root=str(checkout.parent),
                implementation_branch="feature/monitor",
                implementation_worktree=str(checkout),
                prd_path=str(prd_path),
            ),
            activity=("Reviewing issue 0004",),
            prd_path=prd_path,
            progress=PortableSessionProgress(
                stage="review",
                completed_issues=3,
                total_issues=5,
                active_issue="0004",
            ),
            updated_at=1_720_000_000,
        )

        class FakeSupervisor:
            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return (snapshot,)

            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                raise AssertionError(f"Unexpected intent: {intent}")

            def try_next_event(self) -> PortableSessionEvent | None:
                return None

            def shutdown(self) -> None:
                return None

        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=FakeSupervisor(),
            session_launch=PortableSessionLaunch(
                session_id="new-session",
                checkout=Path.cwd(),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            ),
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            detail = str(app.query_one("#portable-detail", Static).render())

        self.assertIn(f"Project: {checkout.parent}", detail)
        self.assertIn(f"Worktree: {checkout}", detail)
        self.assertIn("Status: RUNNING", detail)
        self.assertIn("Stage: review", detail)
        self.assertIn(f"PRD: {prd_path}", detail)
        self.assertIn("Issue progress: 3/5", detail)
        self.assertIn("Active issue: 0004", detail)
        self.assertIn("Latest activity: Reviewing issue 0004", detail)
        self.assertIn("Last update:", detail)

    async def test_active_session_context_uses_left_pane_and_f5_context_view(
        self,
    ) -> None:
        run_context = PortableRunContext(
            project_root=r"E:\LocalCode\PortableProject",
            implementation_branch="devloop/portable-session",
            implementation_worktree=r"E:\Worktrees\PortableProject-session-dev",
            prd_path=r"E:\LocalCode\PortableProject\prd\portable-session.md",
        )

        class FakeSupervisor:
            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                assert intent.launch is not None
                return PortableSessionSnapshot(
                    session_id=intent.launch.session_id,
                    checkout=intent.launch.checkout,
                    status=PortableSessionStatus.RUNNING,
                    context=run_context,
                )

            def try_next_event(self) -> PortableSessionEvent | None:
                return None

            def shutdown(self) -> None:
                return None

        launch = PortableSessionLaunch(
            session_id="session-context",
            checkout=Path.cwd(),
            operation=PortableWorkflowOperation.PLANNING,
            arguments=(),
        )
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=FakeSupervisor(),
            session_launch=launch,
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.press("f2")
            await pilot.pause()

            compact_context = str(
                app.query_one("#portable-run-context", Static).render()
            )
            self.assertIn(run_context.project_root, compact_context)
            self.assertIn(run_context.implementation_branch, compact_context)
            self.assertIn(run_context.implementation_worktree, compact_context)

            await pilot.press("f5")
            await pilot.pause()

            self.assertIsInstance(app.screen, PortableTextOverlay)
            full_context = str(
                app.screen.query_one(".portable-overlay-content", Static).render()
            )
            self.assertIn(run_context.prd_path, full_context)

    async def test_worker_input_prompt_is_sanitized_before_placeholder_assignment(
        self,
    ) -> None:
        hostile_prompt = (
            "\x1b[2JChoose "
            "\x1b]0;hostile terminal title\x07"
            "safely\x00\x08"
        )

        class FakeSupervisor:
            def handle_intent(
                self,
                intent: PortableSessionIntent,
            ) -> PortableSessionSnapshot:
                assert intent.launch is not None
                return PortableSessionSnapshot(
                    session_id=intent.launch.session_id,
                    checkout=intent.launch.checkout,
                    status=PortableSessionStatus.WAITING_FOR_INPUT,
                    input_request=PortableSessionInputRequest(
                        kind=PortableSessionInputKind.TEXT,
                        prompt=hostile_prompt,
                    ),
                )

            def try_next_event(self) -> PortableSessionEvent | None:
                return None

            def shutdown(self) -> None:
                return None

        launch = PortableSessionLaunch(
            session_id="session-hostile-prompt",
            checkout=Path.cwd(),
            operation=PortableWorkflowOperation.PLANNING,
            arguments=(),
        )
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=FakeSupervisor(),
            session_launch=launch,
        )

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("f2")
            await pilot.pause()

            input_widget = app.query_one("#portable-input", Input)
            self.assertEqual(input_widget.placeholder, "Choose safely")
            self.assertNotIn("\x1b", input_widget.placeholder)
            self.assertNotIn("\x00", input_widget.placeholder)

    async def test_running_workflow_keeps_implementation_context_visible(self) -> None:
        bridge = PortableRuntimeBridge()
        release_operation = threading.Event()

        def operation() -> int:
            bridge.update_run_context(
                PortableRunContext(
                    project_root=r"E:\LocalCode\eConnectorV2",
                    implementation_branch="devloop/fulfillment-tools-repair",
                    implementation_worktree=(
                        r"E:\Worktrees\eConnectorV2-fulfillment-tools-repair-dev"
                    ),
                    prd_path=(
                        r"E:\LocalCode\eConnectorV2\prd\fulfillment-tools-repair"
                        r"\fulfillment-tools-repair.md"
                    ),
                )
            )
            bridge.show_screen("CURRENT ISSUE · 0001\nDEVELOPMENT WORKING pass 1")
            release_operation.wait(timeout=2)
            return 0

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(120, 34)) as pilot:
            try:
                context = app.query_one("#portable-run-context", Static)
                for _ in range(20):
                    await pilot.pause()
                    if context.display:
                        break

                rendered = str(context.render())
                self.assertIn(r"E:\LocalCode\eConnectorV2", rendered)
                self.assertIn("devloop/fulfillment-tools-repair", rendered)
                self.assertIn(
                    r"E:\Worktrees\eConnectorV2-fulfillment-tools-repair-dev",
                    rendered,
                )
                self.assertIn(
                    "CURRENT ISSUE",
                    str(app.query_one("#portable-detail", Static).render()),
                )

                await pilot.press("f5")
                self.assertIsInstance(app.screen, PortableTextOverlay)
                full_context = str(
                    app.screen.query_one(
                        ".portable-overlay-content",
                        Static,
                    ).render()
                )
                self.assertIn("Implementation branch", full_context)
                self.assertIn("fulfillment-tools-repair.md", full_context)
            finally:
                release_operation.set()

    async def test_shell_keeps_one_frame_and_refreshes_selection_preview(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            selected = bridge.choose(
                (("start", "Start a new change"), ("resume", "Resume unfinished PRD")),
                default_key="start",
                cancel_key=None,
                render=lambda key: bridge.show_screen(f"preview:{key}"),
            )
            return 0 if selected == "resume" else 1

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            menu = app.query_one("#portable-navigation", OptionList)
            for _ in range(20):
                await pilot.pause()
                if menu.option_count == 2:
                    break

            self.assertEqual(menu.option_count, 2)
            self.assertEqual(len(app.query("#portable-shell")), 1)
            self.assertEqual(app.query_one("#portable-status", Static).region.height, 1)
            self.assertGreater(app.query_one("#portable-actions", Static).region.height, 0)

            menu.highlighted = 1
            menu.focus()
            await pilot.pause()
            for _ in range(20):
                await pilot.pause()
                if "preview:resume" in str(
                    app.query_one("#portable-detail", Static).render()
                ):
                    break

            self.assertIn(
                "preview:resume",
                str(app.query_one("#portable-detail", Static).render()),
            )
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            self.assertEqual(app.operation_result, 0)
            self.assertIn(
                "Dev Loop > Final Result",
                str(app.query_one("#portable-detail", Static).render()),
            )

    async def test_declared_number_shortcut_selects_the_matching_option(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            selected = bridge.choose(
                (("previous", "1. Previous step"), ("next", "2. Next step")),
                default_key="previous",
                cancel_key=None,
                render=lambda _key: None,
                shortcuts={"1": "previous", "2": "next"},
            )
            return 0 if selected == "next" else 1

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            menu = app.query_one("#portable-navigation", OptionList)
            for _ in range(20):
                await pilot.pause()
                if menu.option_count == 2:
                    break

            await pilot.press("2")
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            self.assertEqual(app.operation_result, 0)

    async def test_committed_choice_replaces_stale_menu_and_escape_reports_progress(self) -> None:
        bridge = PortableRuntimeBridge()
        choice_received = threading.Event()
        release_preview = threading.Event()
        release_operation = threading.Event()

        def render_preview(key: str) -> None:
            release_preview.wait(timeout=2)
            bridge.show_screen(f"preview:{key}")

        def operation() -> int:
            bridge.choose(
                (("start", "Start development"), ("quit", "Quit")),
                default_key="start",
                cancel_key="quit",
                render=render_preview,
            )
            choice_received.set()
            release_operation.wait(timeout=2)
            return 0

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            menu = app.query_one("#portable-navigation", OptionList)
            for _ in range(20):
                await pilot.pause()
                if menu.option_count == 2:
                    break

            try:
                await pilot.press("enter")
                release_preview.set()
                for _ in range(20):
                    await pilot.pause()
                    if choice_received.is_set():
                        break

                self.assertTrue(choice_received.is_set())
                self.assertTrue(menu.disabled)
                self.assertEqual(menu.option_count, 0)
                self.assertIn(
                    "Dev Loop > Working",
                    str(app.query_one("#portable-detail", Static).render()),
                )
                self.assertIn(
                    "F5 Context",
                    str(app.query_one("#portable-actions", Static).render()),
                )

                await pilot.press("escape")
                self.assertIsInstance(app.screen, PortableTextOverlay)
                self.assertIn(
                    "already accepted",
                    str(
                        app.screen.query_one(
                            ".portable-overlay-content",
                            Static,
                        ).render()
                    ),
                )
            finally:
                release_preview.set()
                release_operation.set()

    async def test_escape_returns_the_current_cancel_action(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            selected = bridge.choose(
                (("start", "Start development"), ("quit", "Quit")),
                default_key="start",
                cancel_key="quit",
                render=lambda key: bridge.show_screen(f"preview:{key}"),
            )
            return 0 if selected == "quit" else 1

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            menu = app.query_one("#portable-navigation", OptionList)
            for _ in range(20):
                await pilot.pause()
                if menu.option_count == 2:
                    break

            await pilot.press("escape")
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            self.assertEqual(app.operation_result, 0)

    async def test_escape_closes_the_completed_application(self) -> None:
        app = PortableApplicationShell(PortableRuntimeBridge(), lambda: 0)

        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(20):
                await pilot.pause()
                if app._workflow_complete:
                    break

            self.assertTrue(app._workflow_complete)
            self.assertTrue(app.is_running)
            self.assertIn(
                "Esc Exit",
                str(app.query_one("#portable-actions", Static).render()),
            )

            await pilot.press("escape")

            self.assertFalse(app.is_running)

    async def test_shell_shutdown_releases_worker_and_terminates_processes(
        self,
    ) -> None:
        bridge = PortableRuntimeBridge()
        operation_finished = threading.Event()
        fallback_cleanup_used = threading.Event()

        def operation() -> int:
            try:
                bridge.choose(
                    (("continue", "Continue"), ("quit", "Quit")),
                    default_key="continue",
                    cancel_key="quit",
                    render=lambda _key: None,
                )
                return 0
            finally:
                operation_finished.set()

        app = PortableApplicationShell(bridge, operation)
        with mock.patch(
            "devloop.portable_ui.app.terminate_active_process_trees",
        ) as terminate_processes:
            async with app.run_test(size=(100, 30)) as pilot:
                menu = app.query_one("#portable-navigation", OptionList)
                for _ in range(20):
                    await pilot.pause()
                    if menu.option_count == 2:
                        break

                request_id = app._active_request_id
                self.assertIsNotNone(request_id)

                def release_broken_shutdown() -> None:
                    if not operation_finished.wait(timeout=0.5):
                        fallback_cleanup_used.set()
                        bridge.respond(request_id or 0, "quit")

                cleanup = threading.Thread(
                    target=release_broken_shutdown,
                    daemon=True,
                )
                cleanup.start()
                app.exit()

        cleanup.join(timeout=1)
        self.assertTrue(operation_finished.is_set())
        self.assertFalse(fallback_cleanup_used.is_set())
        self.assertEqual(app.operation_result, 130)
        terminate_processes.assert_called_once_with()

    async def test_ctrl_c_stops_the_worker_and_exits_the_application(self) -> None:
        bridge = PortableRuntimeBridge()
        operation_finished = threading.Event()

        def operation() -> int:
            try:
                bridge.choose(
                    (("continue", "Continue"), ("quit", "Quit")),
                    default_key="continue",
                    cancel_key="quit",
                    render=lambda _key: None,
                )
                return 0
            finally:
                operation_finished.set()

        app = PortableApplicationShell(bridge, operation)
        with mock.patch(
            "devloop.portable_ui.app.terminate_active_process_trees",
        ) as terminate_processes:
            async with app.run_test(size=(100, 30)) as pilot:
                menu = app.query_one("#portable-navigation", OptionList)
                for _ in range(20):
                    await pilot.pause()
                    if menu.option_count == 2:
                        break

                await pilot.press("ctrl+c")
                await pilot.pause()

                self.assertFalse(app.is_running)

        self.assertTrue(operation_finished.wait(timeout=1))
        self.assertEqual(app.operation_result, 130)
        terminate_processes.assert_called_once_with()

    async def test_help_and_logs_open_inside_the_application(self) -> None:
        app = PortableApplicationShell(PortableRuntimeBridge(), lambda: 0)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("f1")
            self.assertEqual(len(app.screen_stack), 2)
            self.assertIsInstance(app.screen, PortableTextOverlay)
            await pilot.press("escape")
            self.assertEqual(len(app.screen_stack), 1)

            await pilot.press("f4")
            self.assertIsInstance(app.screen, PortableLogOverlay)

    async def test_small_terminal_shows_a_bounded_resize_view(self) -> None:
        app = PortableApplicationShell(PortableRuntimeBridge(), lambda: 0)
        async with app.run_test(size=(79, 23)) as pilot:
            await pilot.pause()
            warning = app.query_one("#portable-size-warning", Static)

            self.assertTrue(warning.display)
            self.assertTrue(app.query_one("#portable-body", Horizontal).disabled)
            self.assertIn("Required: 80x24", str(warning.render()))

    async def test_shell_layout_is_supported_at_minimum_and_wide_sizes(self) -> None:
        for size in ((80, 24), (160, 40)):
            with self.subTest(size=size):
                app = PortableApplicationShell(PortableRuntimeBridge(), lambda: 0)
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()

                    self.assertFalse(
                        app.query_one("#portable-size-warning", Static).display
                    )
                    self.assertEqual(len(app.query("#portable-shell")), 1)

    async def test_run_context_keeps_navigation_visible_at_minimum_size(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            bridge.update_run_context(
                PortableRunContext(
                    project_root=r"E:\Code\Project",
                    implementation_branch="devloop/feature",
                    implementation_worktree=r"E:\Worktrees\Project-feature",
                )
            )
            return 0

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(80, 24)) as pilot:
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            context = app.query_one("#portable-run-context", Static)
            navigation = app.query_one("#portable-navigation", OptionList)

            self.assertGreater(context.region.height, 0)
            self.assertGreater(navigation.region.height, 0)
            self.assertIn("Changes:", str(context.render()))

    async def test_shell_reports_the_allocated_detail_pane_size_to_the_runtime(
        self,
    ) -> None:
        bridge = PortableRuntimeBridge()
        release_operation = threading.Event()

        def operation() -> int:
            dashboard = IssueDashboard(
                issue_number="0001",
                issue_title="Trace and Classify One FT Order",
                position=1,
                total=8,
                frame_seconds=0.05,
            )
            try:
                dashboard.show_scheduler_status(
                    "SCHEDULER · BLOCKER RESOLUTION · round 5/5 · "
                    "1 ready · 7 waiting · next 0001"
                )
                dashboard.begin_role(Stage.DEVELOPMENT, 1)
                release_operation.wait(timeout=2)
                return 0
            finally:
                dashboard.close()

        app = PortableApplicationShell(bridge, operation)

        async with app.run_test(size=(100, 30)) as pilot:
            try:
                detail = app.query_one("#portable-detail", Static)
                for _ in range(20):
                    await pilot.pause()
                    if "SCHEDULER" in str(detail.render()):
                        break
                await pilot.resize_terminal(190, 40)
                for _ in range(20):
                    await pilot.pause()
                    if "next 0001" in str(detail.render()):
                        break
                detail_size = detail.content_region.size

                self.assertEqual(
                    bridge.content_size(fallback=(1, 1)),
                    (detail_size.width, detail_size.height),
                )
                self.assertGreaterEqual(detail_size.width, 140)
                self.assertIn("next 0001", str(detail.render()))
            finally:
                release_operation.set()

    async def test_input_view_supports_history_and_alt_v(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            value = bridge.read_line("Describe the change", history=("older", "newer"))
            return 0 if value == "/paste" else 1

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            input_widget = app.query_one("#portable-input")
            for _ in range(20):
                await pilot.pause()
                if input_widget.display:
                    break

            await pilot.press("up")
            self.assertEqual(input_widget.value, "newer")
            await pilot.press("alt+v")
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            self.assertEqual(app.operation_result, 0)

    async def test_system_exit_code_is_preserved(self) -> None:
        def operation() -> int:
            raise SystemExit(7)

        app = PortableApplicationShell(PortableRuntimeBridge(), operation)
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            self.assertEqual(app.operation_result, 7)

    async def test_completion_review_remains_visible_after_operation_finishes(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            bridge.show_screen(
                "Dev Loop > Completion Review\n\n"
                "WORKFLOW FINISHED - ATTENTION REQUIRED\n"
                "Completed: 3/8    Remaining: 5"
            )
            return 2

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            detail = str(app.query_one("#portable-detail", Static).render())
            status = str(app.query_one("#portable-status", Static).render())

            self.assertIn("Dev Loop > Completion Review", detail)
            self.assertIn("WORKFLOW FINISHED", detail)
            self.assertNotIn("Last workflow view", detail)
            self.assertIn("WORKFLOW FINISHED", status)

    async def test_completion_review_wraps_the_full_failure_log_path(self) -> None:
        bridge = PortableRuntimeBridge()
        log_path = (
            r"E:\Worktrees\eConnectorV2-fulfillment-tools-unroutable-order-repair-dev"
            r"\prd\fulfillment-tools-unroutable-order-repair\issues\.loop.logs"
            r"\0001-attempt-9bb7217-760c5f7a-portable-step-step-"
            r"9c30a1c0-57b4-4cf6-8b6d-a568dac11e01-coder-pass1.stderr.txt"
        )
        review = build_run_review(
            [Issue("0001", "Long-running development", Path("0001.md"), False)],
            {
                "0001": {
                    "status": "BLOCKED",
                    "blocked_summary": (
                        "codex exec failed with exit code 124. "
                        f"See {log_path}."
                    ),
                }
            },
            loop_state_path=Path("README.loop.md"),
            rerun_available=True,
        )

        def operation() -> int:
            bridge.show_screen(render_run_review(review, RunReviewAction.EXIT))
            return 2

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            rendered = str(app.query_one("#portable-detail", Static).render())

            self.assertIn("Execution Budget timeout expired", rendered)
            self.assertIn("coder-pass1.stderr.txt", rendered)
            self.assertNotIn("...", rendered)

    async def test_f4_starts_with_all_completion_review_failures(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            bridge.show_screen(
                "Dev Loop > Completion Review\n\n"
                "WORKFLOW FINISHED - ATTENTION REQUIRED\n"
                "Completed: 1/3    Remaining: 2\n\n"
                "Issue review\n"
                "COMPLETED  0001  Finished feature\n"
                "BLOCKED    0002  Broken feature - Review found a defect.\n"
                "WAITING    0003  Dependent feature - waiting on 0002"
            )
            return 2

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            await pilot.press("f4")

            self.assertIsInstance(app.screen, PortableLogOverlay)
            review_log = "\n".join(app.screen._lines)
            self.assertIn("COMPLETED  0001", review_log)
            self.assertIn("BLOCKED    0002", review_log)
            self.assertIn("Review found a defect.", review_log)
            self.assertIn("WAITING    0003", review_log)
            self.assertIn("waiting on 0002", review_log)

    async def test_completion_review_defaults_to_rerun_unfinished_issues(
        self,
    ) -> None:
        bridge = PortableRuntimeBridge()
        selected_actions: list[RunReviewAction] = []
        review = build_run_review(
            [Issue("0001", "Blocked", Path("0001.md"), False)],
            {"0001": {"status": "BLOCKED"}},
            loop_state_path=Path("README.loop.md"),
            rerun_available=True,
        )

        def operation() -> int:
            selected_actions.append(
                choose_run_review_action(review, interactive=True)
            )
            return 2

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            menu = app.query_one("#portable-navigation", OptionList)
            for _ in range(20):
                await pilot.pause()
                if menu.option_count == 2:
                    break

            self.assertEqual(menu.highlighted, 0)
            self.assertIn(
                "Enter Select",
                str(app.query_one("#portable-actions", Static).render()),
            )
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            self.assertEqual(
                selected_actions,
                [RunReviewAction.RERUN_REMAINING],
            )
            self.assertIn(
                "Press Enter to rerun only the 1 unfinished issue",
                str(app.query_one("#portable-detail", Static).render()),
            )

    async def test_worker_output_is_sanitized_before_display(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            bridge.write_output("unsafe\x1b[2Joutput", is_error=False)
            return 0

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            self.assertNotIn("\x1b", "".join(app._captured_output))

    async def test_contextual_function_key_returns_a_typed_command(self) -> None:
        bridge = PortableRuntimeBridge()

        def operation() -> int:
            selected = bridge.choose(
                (("step", "Workflow step"), ("cancel", "Cancel")),
                default_key="step",
                cancel_key="cancel",
                render=lambda _key: None,
                shortcuts={"f3": "graph"},
            )
            return 0 if selected == "graph" else 1

        app = PortableApplicationShell(bridge, operation)
        async with app.run_test(size=(100, 30)) as pilot:
            menu = app.query_one("#portable-navigation", OptionList)
            for _ in range(20):
                await pilot.pause()
                if menu.option_count == 2:
                    break
            await pilot.press("f3")
            for _ in range(20):
                await pilot.pause()
                if app.operation_result is not None:
                    break

            self.assertEqual(app.operation_result, 0)


if __name__ == "__main__":
    unittest.main()
