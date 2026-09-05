from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from textual.widgets import Input, OptionList, Static

from devloop import portable_worker
from devloop.portable_runtime import PortableRuntimeBridge
from devloop.portable_session_catalog import (
    PORTABLE_SESSION_CATALOG_ENV,
    PORTABLE_SESSION_ID_ENV,
    PortableSessionCatalog,
)
from devloop.portable_sessions import (
    PortableSessionEvent,
    PortableSessionLaunch,
    PortableSessionProgress,
    PortableSessionSnapshot,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
)
from devloop.portable_ui.app import PortableApplicationShell
from devloop.state import LoopStateWriter
from devloop.subprocess_utils import ProcessIdentity, ProcessTreeState


class PortableSessionHistoryTests(unittest.TestCase):
    def test_startup_requires_completed_issue_files_before_moving_session_to_history(
        self,
    ) -> None:
        for status in (
            PortableSessionStatus.FAILED,
            PortableSessionStatus.CANCELLED,
            PortableSessionStatus.INTERRUPTED,
            PortableSessionStatus.PAUSED,
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                checkout = Path(directory)
                issue_root = checkout / "prd" / "change" / "issues"
                issue_root.mkdir(parents=True)
                prd_path = issue_root.parent / "change.md"
                issues_index = issue_root / "README.md"
                prd_path.write_text("# Change\n", encoding="utf-8")
                issues_index.write_text(
                    "[Issue 0001](./0001-change.md)\n",
                    encoding="utf-8",
                )
                (issue_root / "0001-change.md").write_text(
                    "# Issue\n\nCompleted: [ ]\n",
                    encoding="utf-8",
                )
                catalog = PortableSessionCatalog(checkout / "portable-sessions.sqlite3")
                launch = PortableSessionLaunch(
                    f"unfinished-{status.value.lower()}",
                    checkout,
                    PortableWorkflowOperation.DELIVERY,
                    ("--prd", str(prd_path), "--issues", str(issues_index)),
                )
                catalog.create_session(launch)
                catalog.publish_workflow(
                    launch.session_id,
                    prd_path=prd_path,
                    issues_index_path=issues_index,
                    activity_summary="Published workflow",
                )
                catalog.update_session_status(launch.session_id, status)

                supervisor = PortableSessionSupervisor(
                    catalog=catalog,
                    resume_candidates=(),
                    resume_candidates_loader=lambda: (),
                )
                snapshot = supervisor.snapshot(launch.session_id)
                supervisor.shutdown()

                self.assertIs(snapshot.status, status)
                self.assertIs(catalog.get_session(launch.session_id).status, status)

    def test_startup_moves_session_to_history_only_with_completed_issue_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            issue_root = checkout / "prd" / "change" / "issues"
            issue_root.mkdir(parents=True)
            prd_path = issue_root.parent / "change.md"
            issues_index = issue_root / "README.md"
            prd_path.write_text("# Change\n", encoding="utf-8")
            issues_index.write_text(
                "[Issue 0001](./0001-change.md)\n",
                encoding="utf-8",
            )
            (issue_root / "0001-change.md").write_text(
                "# Issue\n\nCompleted: [x]\n",
                encoding="utf-8",
            )
            catalog = PortableSessionCatalog(checkout / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                "authoritatively-completed",
                checkout,
                PortableWorkflowOperation.DELIVERY,
                ("--prd", str(prd_path), "--issues", str(issues_index)),
            )
            catalog.create_session(launch)
            catalog.publish_workflow(
                launch.session_id,
                prd_path=prd_path,
                issues_index_path=issues_index,
                activity_summary="Published workflow",
            )

            supervisor = PortableSessionSupervisor(
                catalog=catalog,
                resume_candidates=(),
                resume_candidates_loader=lambda: (),
            )
            snapshot = supervisor.snapshot(launch.session_id)
            supervisor.shutdown()

        self.assertIs(snapshot.status, PortableSessionStatus.COMPLETED)
        self.assertEqual(snapshot.result, 0)

    def test_moved_paused_and_interrupted_delivery_relink_then_resume_exact_cursor(
        self,
    ) -> None:
        worker_source = textwrap.dedent(
            """
            import json
            import sys

            session_id = sys.argv[1]
            command = json.loads(sys.stdin.readline())
            assert command["kind"] == "RESUME"
            assert command["payload"]["recovery"]["issue_id"] is None
            for sequence, kind, payload in (
                (1, "HELLO", {}),
                (2, "COMPLETION", {"exit_code": 0}),
            ):
                print(json.dumps({
                    "version": 1,
                    "session_id": session_id,
                    "sequence": sequence,
                    "kind": kind,
                    "payload": payload,
                }), flush=True)
            """
        )
        for status in (
            PortableSessionStatus.PAUSED,
            PortableSessionStatus.INTERRUPTED,
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                checkout = root / "checkout"
                issue_root = checkout / "prd" / "change" / "issues"
                issue_root.mkdir(parents=True)
                prd_path = issue_root.parent / "change.md"
                issues_index = issue_root / "README.md"
                issue_path = issue_root / "0001-change.md"
                prd_path.write_text("# Change\n", encoding="utf-8")
                issues_index.write_text(
                    "[Issue 0001](./0001-change.md)\n",
                    encoding="utf-8",
                )
                issue_path.write_text("# Issue\n\nCompleted: [ ]\n", encoding="utf-8")
                subprocess.run(
                    ["git", "init", "--quiet", str(checkout)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                LoopStateWriter(issues_index).record_run_start(
                    checkout,
                    prd_path,
                    ["0001"],
                    dry_run=False,
                )
                state_path = issues_index.with_name("README.loop.state.json")
                before = state_path.read_bytes()
                catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
                launch = PortableSessionLaunch(
                    f"moved-{status.value.lower()}",
                    checkout,
                    PortableWorkflowOperation.DELIVERY,
                    ("--prd", str(prd_path), "--issues", str(issues_index)),
                )
                catalog.create_session(launch)
                catalog.publish_workflow(
                    launch.session_id,
                    prd_path=prd_path,
                    issues_index_path=issues_index,
                    activity_summary="Published workflow",
                )
                catalog.update_session_status(launch.session_id, status)
                moved = root / "moved"
                checkout.rename(moved)
                catalog.discover_resume_candidates(lambda _checkout: ())

                catalog.relink_unavailable_session(launch.session_id, moved)
                self.assertEqual(
                    (moved / state_path.relative_to(checkout)).read_bytes(),
                    before,
                )
                moved_again = root / "moved-again"
                moved.rename(moved_again)
                catalog.discover_resume_candidates(lambda _checkout: ())
                catalog.relink_unavailable_session(launch.session_id, moved_again)
                moved = moved_again

                def launch_worker(selected: PortableSessionLaunch) -> subprocess.Popen[str]:
                    return subprocess.Popen(
                        [sys.executable, "-u", "-c", worker_source, selected.session_id],
                        cwd=selected.checkout,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                    )

                supervisor = PortableSessionSupervisor(
                    worker_launcher=launch_worker,
                    catalog=catalog,
                    owner_id=f"resume-{status.value.lower()}",
                )
                try:
                    resumed = supervisor.resume_session(launch.session_id)
                    self.assertIn(
                        resumed.status,
                        {PortableSessionStatus.RUNNING, PortableSessionStatus.QUEUED},
                    )
                finally:
                    supervisor.shutdown()
                receipt = catalog.get_relink_receipt(launch.session_id)
                assert receipt is not None
                self.assertEqual(receipt.target_checkout, moved.resolve())
                moved_prd = moved / prd_path.relative_to(checkout)
                moved_issues = moved / issues_index.relative_to(checkout)
                LoopStateWriter(moved_issues).record_run_start(
                    moved,
                    moved_prd,
                    ["0001"],
                    dry_run=False,
                )
                with mock.patch.dict(
                    os.environ,
                    {
                        PORTABLE_SESSION_CATALOG_ENV: str(catalog.path),
                        PORTABLE_SESSION_ID_ENV: launch.session_id,
                    },
                    clear=False,
                ):
                    portable_worker._capture_durable_checkpoint(
                        PortableWorkflowOperation.DELIVERY,
                        launch.arguments,
                    )
                self.assertIsNone(catalog.get_relink_receipt(launch.session_id))

    def test_second_relink_rejects_state_changed_after_exact_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            issue_root = checkout / "prd" / "change" / "issues"
            issue_root.mkdir(parents=True)
            prd_path = issue_root.parent / "change.md"
            issues_index = issue_root / "README.md"
            prd_path.write_text("# Change\n", encoding="utf-8")
            issues_index.write_text(
                "[Issue 0001](./0001-change.md)\n",
                encoding="utf-8",
            )
            (issue_root / "0001-change.md").write_text(
                "# Issue\n\nCompleted: [ ]\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "init", "--quiet", str(checkout)],
                check=True,
                capture_output=True,
                text=True,
            )
            LoopStateWriter(issues_index).record_run_start(
                checkout,
                prd_path,
                ["0001"],
                dry_run=False,
            )
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                "tampered-second-relink",
                checkout,
                PortableWorkflowOperation.DELIVERY,
                ("--prd", str(prd_path), "--issues", str(issues_index)),
            )
            catalog.create_session(launch)
            catalog.publish_workflow(
                launch.session_id,
                prd_path=prd_path,
                issues_index_path=issues_index,
                activity_summary="Published workflow",
            )
            catalog.update_session_status(launch.session_id, PortableSessionStatus.PAUSED)
            moved = root / "moved"
            checkout.rename(moved)
            catalog.discover_resume_candidates(lambda _checkout: ())
            catalog.relink_unavailable_session(launch.session_id, moved)
            second = root / "second"
            moved.rename(second)
            state_path = second / "prd" / "change" / "issues" / "README.loop.state.json"
            state_path.write_text(
                state_path.read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            )
            catalog.discover_resume_candidates(lambda _checkout: ())

            with self.assertRaisesRegex(ValueError, "exact relocation receipt"):
                catalog.relink_unavailable_session(launch.session_id, second)
            unchanged = catalog.get_session(launch.session_id)

        self.assertEqual(unchanged.checkout, moved.resolve())
        self.assertIs(unchanged.status, PortableSessionStatus.UNAVAILABLE)

    def test_forget_is_reconciled_out_of_a_peer_supervisor_view(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                "peer-forgotten",
                checkout,
                PortableWorkflowOperation.PLANNING,
                (),
            )
            catalog.create_session(launch)
            catalog.update_session_status(
                launch.session_id,
                PortableSessionStatus.COMPLETED,
            )
            forgetting = PortableSessionSupervisor(catalog=catalog, owner_id="forgetting")
            observing = PortableSessionSupervisor(catalog=catalog, owner_id="observing")
            self.assertEqual(
                tuple(item.session_id for item in observing.list_sessions()),
                (launch.session_id,),
            )

            forgetting.forget_session(launch.session_id)
            observed_after_forget = observing.list_sessions()
            forgetting.shutdown()
            observing.shutdown()

        self.assertEqual(observed_after_forget, ())

    def test_live_or_ambiguous_owner_keeps_lease_but_gets_unavailable_overlay(self) -> None:
        for owner_state in (ProcessTreeState.RUNNING, ProcessTreeState.UNKNOWN):
            with self.subTest(owner_state=owner_state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                checkout = root / "checkout"
                checkout.mkdir()
                clock = [100.0]
                catalog = PortableSessionCatalog(
                    root / "portable-sessions.sqlite3",
                    clock=lambda current=clock: current[0],
                    process_probe=lambda _identity, state=owner_state: state,
                    lease_timeout_seconds=1.0,
                )
                launch = PortableSessionLaunch(
                    "owned-unavailable",
                    checkout,
                    PortableWorkflowOperation.PLANNING,
                    ("--repo", str(checkout)),
                )
                catalog.create_session_with_lease(
                    launch,
                    owner_id="existing-owner",
                    process_identity=ProcessIdentity(pid=401, creation_time=501),
                )
                checkout.rename(root / "moved")
                clock[0] = 200.0

                catalog.mark_missing_worktrees_unavailable()

                record = catalog.get_session(launch.session_id)
                lease = catalog.get_worktree_lease(checkout)
                with self.assertRaisesRegex(ValueError, "UNAVAILABLE|unavailable"):
                    catalog.request_execution_capacity(
                        launch.session_id,
                        owner_id="existing-owner",
                        process_id=401,
                    )

                self.assertIs(record.status, PortableSessionStatus.UNAVAILABLE)
                self.assertIs(record.unavailable_from_status, PortableSessionStatus.READY)
                self.assertIsNotNone(lease)

    def test_relink_merges_with_existing_replacement_project_without_touching_its_sessions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old"
            replacement = root / "replacement"
            _initialize_workflow_checkout(old)
            _initialize_workflow_checkout(replacement)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            moved_session = PortableSessionLaunch(
                "moved-group-session",
                old,
                PortableWorkflowOperation.PLANNING,
                ("--repo", str(old)),
            )
            existing_session = PortableSessionLaunch(
                "existing-target-session",
                replacement,
                PortableWorkflowOperation.PLANNING,
                ("--repo", str(replacement)),
            )
            catalog.create_session(moved_session)
            catalog.create_session(existing_session)
            existing_before = catalog.get_session(existing_session.session_id)
            old.rename(root / "disconnected-old")
            catalog.mark_missing_worktrees_unavailable()

            catalog.relink_unavailable_session(moved_session.session_id, replacement)

            moved_after = catalog.get_session(moved_session.session_id)
            existing_after = catalog.get_session(existing_session.session_id)

        self.assertEqual(moved_after.project_id, existing_after.project_id)
        self.assertEqual(moved_after.checkout, replacement.resolve())
        self.assertEqual(existing_after, existing_before)

    def test_unavailable_session_rejects_lease_queue_and_capacity_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                "unavailable-capacity",
                checkout,
                PortableWorkflowOperation.PLANNING,
                ("--repo", str(checkout)),
            )
            catalog.create_session(launch)
            checkout.rename(root / "moved")
            catalog.mark_missing_worktrees_unavailable()

            for operation in (
                lambda: catalog.acquire_session_lease(
                    launch.session_id,
                    owner_id="blocked-owner",
                ),
                lambda: catalog.enqueue_execution_capacity(
                    launch.session_id,
                    owner_id="blocked-owner",
                ),
                lambda: catalog.request_execution_capacity(
                    launch.session_id,
                    owner_id="blocked-owner",
                ),
            ):
                with self.assertRaisesRegex(ValueError, "UNAVAILABLE|unavailable"):
                    operation()

            self.assertIsNone(catalog.get_worktree_lease(checkout))

    def test_invalid_relink_keeps_every_catalog_row_and_revision_unchanged(self) -> None:
        for scenario in ("not-git", "missing-index", "empty-index", "malformed-state"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                checkout = root / "checkout"
                _initialize_workflow_checkout(checkout)
                prd = checkout / "prd" / "change" / "change.md"
                index = checkout / "prd" / "change" / "issues" / "README.md"
                catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
                launch = PortableSessionLaunch(
                    "invalid-relink",
                    checkout,
                    PortableWorkflowOperation.DELIVERY,
                    ("--prd", str(prd), "--issues", str(index)),
                )
                catalog.create_session(launch)
                catalog.publish_workflow(
                    launch.session_id,
                    prd_path=prd,
                    issues_index_path=index,
                    activity_summary="Published",
                )
                moved = root / "moved"
                checkout.rename(moved)
                catalog.mark_missing_worktrees_unavailable()
                replacement = moved
                if scenario == "not-git":
                    replacement = root / "plain-folder"
                    replacement.mkdir()
                elif scenario == "missing-index":
                    (moved / "prd" / "change" / "issues" / "README.md").unlink()
                elif scenario == "empty-index":
                    (moved / "prd" / "change" / "issues" / "README.md").write_text(
                        "# No issues\n",
                        encoding="utf-8",
                    )
                else:
                    (moved / "prd" / "change" / "issues" / "README.loop.state.json").write_text(
                        "{not-json",
                        encoding="utf-8",
                    )
                before = catalog.list_sessions()

                with self.assertRaisesRegex(ValueError, "Relink"):
                    catalog.relink_unavailable_session(launch.session_id, replacement)

                self.assertEqual(catalog.list_sessions(), before)

    def test_forget_removes_only_machine_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            _initialize_workflow_checkout(checkout)
            issue_root = checkout / "prd" / "change" / "issues"
            state = issue_root / "README.loop.state.json"
            log = issue_root / "README.loop.logs" / "0001" / "qa.log"
            state.write_text('{"issues": {"0001": {"status": "Completed"}}}\n', encoding="utf-8")
            log.parent.mkdir(parents=True)
            log.write_text("retained diagnostics\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=checkout, check=True, capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Dev Loop Tests",
                    "-c",
                    "user.email=devloop-tests@example.invalid",
                    "commit",
                    "-m",
                    "workflow",
                ],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "branch", "retained-branch"],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            files = tuple(
                path
                for path in checkout.rglob("*")
                if path.is_file() and ".git" not in path.parts
            )
            before_bytes = {path.relative_to(checkout): path.read_bytes() for path in files}
            before_branches = _git_output(checkout, "branch", "--format=%(refname)")
            before_worktrees = _git_output(checkout, "worktree", "list", "--porcelain")
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                "forget-me",
                checkout,
                PortableWorkflowOperation.DELIVERY,
                (
                    "--prd",
                    str(checkout / "prd" / "change" / "change.md"),
                    "--issues",
                    str(issue_root / "README.md"),
                ),
            )
            catalog.create_session(launch)
            catalog.update_session_summary(
                launch.session_id,
                status=PortableSessionStatus.COMPLETED,
                result=0,
                progress=PortableSessionProgress(completed_issues=1, total_issues=1),
            )

            catalog.forget_session(launch.session_id)

            after_bytes = {
                path.relative_to(checkout): path.read_bytes()
                for path in checkout.rglob("*")
                if path.is_file() and ".git" not in path.parts
            }
            after_branches = _git_output(checkout, "branch", "--format=%(refname)")
            after_worktrees = _git_output(checkout, "worktree", "list", "--porcelain")
            remaining_sessions = catalog.list_sessions()
            remaining_projects = catalog.list_saved_projects()

        self.assertEqual(after_bytes, before_bytes)
        self.assertEqual(after_branches, before_branches)
        self.assertEqual(after_worktrees, before_worktrees)
        self.assertEqual(remaining_sessions, ())
        self.assertEqual(remaining_projects, ())

    def test_relink_moves_every_session_pointer_for_only_the_selected_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            other_checkout = root / "other-checkout"
            _initialize_workflow_checkout(checkout)
            _initialize_workflow_checkout(other_checkout)
            prd = checkout / "prd" / "change" / "change.md"
            issues = checkout / "prd" / "change" / "issues" / "README.md"
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            delivery = PortableSessionLaunch(
                session_id="delivery-history",
                checkout=checkout,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=("--prd", str(prd), "--issues", str(issues)),
            )
            planning = PortableSessionLaunch(
                session_id="planning-paused",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(checkout)),
            )
            unrelated = PortableSessionLaunch(
                session_id="unrelated-session",
                checkout=other_checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(other_checkout)),
            )
            catalog.create_session(delivery)
            catalog.publish_workflow(
                delivery.session_id,
                prd_path=prd,
                issues_index_path=issues,
                activity_summary="Published",
            )
            catalog.update_session_summary(
                delivery.session_id,
                status=PortableSessionStatus.COMPLETED,
                result=0,
                progress=PortableSessionProgress(completed_issues=1, total_issues=1),
            )
            catalog.create_session(planning)
            catalog.update_session_status(planning.session_id, PortableSessionStatus.PAUSED)
            catalog.create_session(unrelated)
            moved = root / "moved-checkout"
            checkout.rename(moved)
            catalog.discover_resume_candidates(lambda _checkout: ())

            updated = catalog.relink_unavailable_session(delivery.session_id, moved)

            by_id = {record.session_id: record for record in catalog.list_sessions()}

        self.assertEqual(updated, (delivery.session_id, planning.session_id))
        self.assertEqual(by_id[delivery.session_id].checkout, moved.resolve())
        self.assertEqual(by_id[planning.session_id].checkout, moved.resolve())
        self.assertEqual(
            by_id[delivery.session_id].prd_path,
            moved / "prd" / "change" / "change.md",
        )
        self.assertIs(by_id[delivery.session_id].status, PortableSessionStatus.COMPLETED)
        self.assertIs(by_id[planning.session_id].status, PortableSessionStatus.PAUSED)
        self.assertEqual(by_id[unrelated.session_id].checkout, other_checkout.resolve())

    def test_completed_session_reopens_in_history_with_terminal_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "portable-sessions.sqlite3"
            catalog = PortableSessionCatalog(database)
            launch = PortableSessionLaunch(
                session_id="completed-session",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(checkout)),
            )
            catalog.create_session(launch)
            catalog.update_session_summary(
                launch.session_id,
                status=PortableSessionStatus.COMPLETED,
                result=0,
                progress=PortableSessionProgress(
                    stage="qa",
                    completed_issues=4,
                    total_issues=4,
                    active_issue="0004",
                ),
                activity_summary="All issues completed",
            )

            reopened = PortableSessionSupervisor(catalog=PortableSessionCatalog(database))
            active = reopened.list_active_sessions()
            history = reopened.list_history_sessions()
            reopened.shutdown()

        self.assertEqual(active, ())
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].session_id, launch.session_id)
        self.assertEqual(history[0].result, 0)
        self.assertEqual(history[0].progress.completed_issues, 4)
        self.assertEqual(history[0].activity, ("All issues completed",))

    def test_only_completed_lifecycle_is_classified_as_history(self) -> None:
        snapshots = tuple(
            PortableSessionSnapshot(
                status.value.lower(),
                Path.cwd(),
                status,
                result=1 if status.terminal else None,
            )
            for status in (
                PortableSessionStatus.FAILED,
                PortableSessionStatus.CANCELLED,
                PortableSessionStatus.INTERRUPTED,
                PortableSessionStatus.PAUSED,
            )
        )
        completed = PortableSessionSnapshot(
            "completed", Path.cwd(), PortableSessionStatus.COMPLETED, result=0
        )

        self.assertEqual(tuple(item for item in snapshots if item.in_history), ())
        self.assertTrue(completed.in_history)

    def test_missing_saved_checkout_becomes_unavailable_without_starting_work(self) -> None:
        launched: list[PortableSessionLaunch] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                session_id="moved-session",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(checkout)),
            )
            catalog.create_session(launch)
            checkout.rename(root / "moved-checkout")

            catalog.discover_resume_candidates(lambda _checkout: ())
            supervisor = PortableSessionSupervisor(
                catalog=catalog,
                worker_launcher=lambda selected: launched.append(selected),  # type: ignore[arg-type,return-value]
            )

            snapshot = supervisor.snapshot(launch.session_id)
            with self.assertRaisesRegex(ValueError, "unavailable.*Relink"):
                supervisor.resume_session(launch.session_id)
            supervisor.shutdown()

        self.assertIs(snapshot.status, PortableSessionStatus.UNAVAILABLE)
        self.assertEqual(launched, [])


class PortableSessionHistoryUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_view_survives_sibling_events_and_catalog_reconciliation(
        self,
    ) -> None:
        retained = PortableSessionSnapshot(
            "history-retained",
            Path("history-retained").resolve(),
            PortableSessionStatus.COMPLETED,
            result=0,
        )
        forgotten = PortableSessionSnapshot(
            "history-forgotten",
            Path("history-forgotten").resolve(),
            PortableSessionStatus.COMPLETED,
            result=0,
        )
        sibling = PortableSessionSnapshot(
            "history-sibling",
            Path("history-sibling").resolve(),
            PortableSessionStatus.RUNNING,
        )

        class FakeSupervisor:
            def __init__(self) -> None:
                self.sessions = (retained, forgotten, sibling)
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
            session_supervisor=supervisor,  # type: ignore[arg-type]
            session_launch=PortableSessionLaunch(
                "new-session", Path.cwd(), PortableWorkflowOperation.PLANNING, ()
            ),
        )

        def assert_history_view(*expected_session_ids: str) -> None:
            menu = app.query_one("#portable-navigation", OptionList)
            menu_ids = tuple(
                menu.get_option_at_index(index).id
                for index in range(menu.option_count)
            )
            detail = str(app.query_one("#portable-detail", Static).content)
            input_widget = app.query_one("#portable-input", Input)
            self.assertEqual(
                str(app.query_one("#portable-header", Static).content),
                "Dev Loop > Sessions > History",
            )
            self.assertEqual(
                menu_ids,
                ("__session_history_back__", *expected_session_ids),
            )
            self.assertTrue(detail.startswith("Portable Session History\n"))
            for session_id in expected_session_ids:
                self.assertIn(session_id, detail)
            self.assertTrue(app._session_history_active)
            self.assertFalse(app._session_actions_active)
            self.assertFalse(input_widget.display)
            self.assertIsNone(input_widget._session_id)  # type: ignore[attr-defined]
            self.assertIsNone(input_widget._request_id)  # type: ignore[attr-defined]
            self.assertIsNone(input_widget._request_generation)  # type: ignore[attr-defined]
            self.assertEqual(
                str(app.query_one("#portable-actions", Static).content),
                "Enter Inspect | Esc Back to Sessions",
            )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 2
            await pilot.press("enter")
            await pilot.pause()
            assert_history_view(retained.session_id, forgotten.session_id)

            running_progress = replace(
                sibling,
                progress=PortableSessionProgress(
                    stage="Development",
                    completed_issues=1,
                    total_issues=2,
                ),
                activity=("Sibling development advanced",),
            )
            supervisor.sessions = (retained, forgotten, running_progress)
            supervisor.events.append(PortableSessionEvent(running_progress))
            for _ in range(20):
                await pilot.pause()
                if app._session_snapshots[sibling.session_id] == running_progress:
                    break
            assert_history_view(retained.session_id, forgotten.session_id)

            completed_sibling = replace(
                running_progress,
                status=PortableSessionStatus.COMPLETED,
                progress=PortableSessionProgress(
                    stage="Completed",
                    completed_issues=2,
                    total_issues=2,
                ),
                activity=("Sibling workflow completed",),
                result=0,
            )
            supervisor.sessions = (retained, forgotten, completed_sibling)
            supervisor.events.append(PortableSessionEvent(completed_sibling))
            for _ in range(20):
                await pilot.pause()
                if app._session_snapshots[sibling.session_id] == completed_sibling:
                    break
            assert_history_view(
                retained.session_id,
                forgotten.session_id,
                completed_sibling.session_id,
            )

            reconciled_sibling = replace(
                completed_sibling,
                activity=("Peer catalog reconciliation completed",),
            )
            supervisor.sessions = (retained, reconciled_sibling)
            supervisor.events.append(PortableSessionEvent(reconciled_sibling))
            for _ in range(20):
                await pilot.pause()
                if forgotten.session_id not in app._session_snapshots:
                    break
            assert_history_view(retained.session_id, reconciled_sibling.session_id)
            self.assertNotIn(forgotten.session_id, app._session_snapshots)

            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn(
                "Esc Back to History",
                str(app.query_one("#portable-actions", Static).content),
            )
            await pilot.press("escape")
            await pilot.pause()
            assert_history_view(retained.session_id, reconciled_sibling.session_id)

            await pilot.press("enter")
            await pilot.pause()
            sessions_menu = app.query_one("#portable-navigation", OptionList)
            session_menu_ids = tuple(
                sessions_menu.get_option_at_index(index).id
                for index in range(sessions_menu.option_count)
            )
            self.assertEqual(
                str(app.query_one("#portable-header", Static).content),
                "Dev Loop > Sessions",
            )
            self.assertIn("__session_history__", session_menu_ids)
            self.assertFalse(app._session_history_active)

    async def test_relink_input_survives_a_sibling_session_event(self) -> None:
        unavailable = PortableSessionSnapshot(
            "relink-owner",
            Path("missing-relink-owner").resolve(),
            PortableSessionStatus.UNAVAILABLE,
            unavailable_from_status=PortableSessionStatus.PAUSED,
        )
        sibling = PortableSessionSnapshot(
            "relink-sibling",
            Path("relink-sibling").resolve(),
            PortableSessionStatus.RUNNING,
        )

        class FakeSupervisor:
            def __init__(self) -> None:
                self.sessions = (unavailable, sibling)
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
            session_supervisor=supervisor,  # type: ignore[arg-type]
            session_launch=PortableSessionLaunch(
                "new-session", Path.cwd(), PortableWorkflowOperation.PLANNING, ()
            ),
        )

        def assert_relink_input() -> None:
            menu = app.query_one("#portable-navigation", OptionList)
            input_widget = app.query_one("#portable-input", Input)
            self.assertEqual(
                str(app.query_one("#portable-header", Static).content),
                "Dev Loop > Sessions > Relink",
            )
            self.assertEqual(menu.option_count, 1)
            self.assertEqual(
                menu.get_option_at_index(0).id,
                "__session_actions_back__",
            )
            self.assertTrue(
                str(app.query_one("#portable-detail", Static).content).startswith(
                    "Relink Unavailable Session\n"
                )
            )
            self.assertEqual(app._active_session_id, unavailable.session_id)
            self.assertEqual(app._relink_session_id, unavailable.session_id)
            self.assertTrue(input_widget.display)
            self.assertEqual(input_widget.value, "F:\\moved\\checkout")
            self.assertEqual(
                input_widget.placeholder,
                "Existing canonical Git checkout path",
            )
            self.assertIsNone(input_widget._session_id)  # type: ignore[attr-defined]
            self.assertIsNone(input_widget._request_id)  # type: ignore[attr-defined]
            self.assertIsNone(input_widget._request_generation)  # type: ignore[attr-defined]
            self.assertEqual(
                str(app.query_one("#portable-actions", Static).content),
                "Enter Relink | Esc Cancel",
            )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            input_widget = app.query_one("#portable-input", Input)
            input_widget.value = "F:\\moved\\checkout"

            progressed_sibling = replace(
                sibling,
                progress=PortableSessionProgress(
                    stage="QA",
                    completed_issues=1,
                    total_issues=2,
                ),
                activity=("Sibling QA advanced",),
            )
            supervisor.sessions = (unavailable, progressed_sibling)
            supervisor.events.append(PortableSessionEvent(progressed_sibling))
            for _ in range(20):
                await pilot.pause()
                if app._session_snapshots[sibling.session_id] == progressed_sibling:
                    break
            assert_relink_input()

            await pilot.press("escape")
            await pilot.pause()
            self.assertIsNone(app._relink_session_id)
            self.assertIn("Relink", _menu_labels(app))

            await pilot.press("enter")
            await pilot.pause()
            input_widget = app.query_one("#portable-input", Input)
            input_widget.value = "F:\\moved\\checkout"
            menu = app.query_one("#portable-navigation", OptionList)
            menu.focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsNone(app._relink_session_id)
            self.assertIn("Relink", _menu_labels(app))

    async def test_forget_confirmation_keeps_its_owner_during_a_sibling_event(
        self,
    ) -> None:
        completed = PortableSessionSnapshot(
            "forget-event-owner",
            Path("forget-event-owner").resolve(),
            PortableSessionStatus.COMPLETED,
            result=0,
        )
        sibling = PortableSessionSnapshot(
            "forget-event-sibling",
            Path("forget-event-sibling").resolve(),
            PortableSessionStatus.RUNNING,
        )

        class FakeSupervisor:
            def __init__(self) -> None:
                self.events: list[PortableSessionEvent] = []

            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return (completed, sibling)

            def try_next_event(self) -> PortableSessionEvent | None:
                return self.events.pop(0) if self.events else None

            def shutdown(self) -> None:
                return None

        supervisor = FakeSupervisor()
        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=supervisor,  # type: ignore[arg-type]
            session_launch=PortableSessionLaunch(
                "new-session", Path.cwd(), PortableWorkflowOperation.PLANNING, ()
            ),
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            menu.highlighted = 2
            await pilot.press("enter")
            await pilot.pause()
            menu.highlighted = 1
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(type(app.screen).__name__, "PortableSessionActionConfirmation")

            progressed_sibling = replace(
                sibling,
                progress=PortableSessionProgress(
                    stage="Review",
                    completed_issues=1,
                    total_issues=2,
                ),
            )
            supervisor.events.append(PortableSessionEvent(progressed_sibling))
            for _ in range(20):
                await pilot.pause()
                if app._session_snapshots[sibling.session_id] == progressed_sibling:
                    break

            self.assertEqual(type(app.screen).__name__, "PortableSessionActionConfirmation")
            self.assertEqual(app._active_session_id, completed.session_id)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app._active_session_id, completed.session_id)
            self.assertIn("Forget (metadata only)", _menu_labels(app))

    async def test_forget_failure_explicit_back_returns_to_history(self) -> None:
        completed = PortableSessionSnapshot(
            "forget-owned",
            Path.cwd(),
            PortableSessionStatus.COMPLETED,
        )

        class FakeSupervisor:
            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return (completed,)

            def list_saved_projects(self) -> tuple[object, ...]:
                return ()

            def forget_session(self, session_id: str) -> None:
                del session_id
                raise RuntimeError("execution ownership is live or ambiguous")

            def try_next_event(self) -> None:
                return None

            def shutdown(self) -> None:
                return None

        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=FakeSupervisor(),  # type: ignore[arg-type]
            session_launch=PortableSessionLaunch(
                "new-session", Path.cwd(), PortableWorkflowOperation.PLANNING, ()
            ),
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            await pilot.press("down", "enter", "down", "enter", "enter")
            await pilot.pause()
            await pilot.press("up", "enter")
            await pilot.pause()
            status = str(app.query_one("#portable-status", Static).content)
            detail = str(app.query_one("#portable-detail", Static).content)
            labels = _menu_labels(app)
            menu = app.query_one("#portable-navigation", OptionList)
            highlighted_id = menu.get_option_at_index(menu.highlighted or 0).id
            await pilot.press("enter")
            await pilot.pause()
            final_header = str(app.query_one("#portable-header", Static).content)
            final_labels = _menu_labels(app)

        self.assertEqual(status, "METADATA NOT FORGOTTEN")
        self.assertIn("live or ambiguous", detail)
        self.assertIn("Back", labels)
        self.assertEqual(highlighted_id, "__session_history__")
        self.assertEqual(final_header, "Dev Loop > Sessions > History")
        self.assertIn("Back to Sessions", final_labels)
        self.assertIn(completed.session_id, app._session_snapshots)

    async def test_forget_failure_escape_returns_to_history(self) -> None:
        completed = PortableSessionSnapshot(
            "forget-owned",
            Path.cwd(),
            PortableSessionStatus.COMPLETED,
        )

        class FakeSupervisor:
            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return (completed,)

            def list_saved_projects(self) -> tuple[object, ...]:
                return ()

            def forget_session(self, session_id: str) -> None:
                del session_id
                raise RuntimeError("execution ownership is live or ambiguous")

            def try_next_event(self) -> None:
                return None

            def shutdown(self) -> None:
                return None

        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=FakeSupervisor(),  # type: ignore[arg-type]
            session_launch=PortableSessionLaunch(
                "new-session", Path.cwd(), PortableWorkflowOperation.PLANNING, ()
            ),
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            await pilot.press("down", "enter", "down", "enter", "enter")
            await pilot.pause()
            await pilot.press("up", "enter")
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            highlighted_id = menu.get_option_at_index(menu.highlighted or 0).id
            await pilot.press("escape")
            await pilot.pause()
            final_header = str(app.query_one("#portable-header", Static).content)
            final_labels = _menu_labels(app)

        self.assertEqual(highlighted_id, "__session_history__")
        self.assertEqual(final_header, "Dev Loop > Sessions > History")
        self.assertIn("Back to Sessions", final_labels)

    async def test_successful_relink_refreshes_grouped_sessions_and_history_count(self) -> None:
        missing = Path("missing-group").resolve()
        moved = Path.cwd()
        snapshots = {
            "paused": PortableSessionSnapshot(
                "paused",
                missing,
                PortableSessionStatus.UNAVAILABLE,
                unavailable_from_status=PortableSessionStatus.PAUSED,
            ),
            "completed": PortableSessionSnapshot(
                "completed",
                missing,
                PortableSessionStatus.UNAVAILABLE,
                unavailable_from_status=PortableSessionStatus.COMPLETED,
            ),
        }

        class FakeSupervisor:
            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return tuple(snapshots.values())

            def list_saved_projects(self) -> tuple[object, ...]:
                return ()

            def relink_session(self, session_id: str, checkout: Path) -> PortableSessionSnapshot:
                del checkout
                snapshots["paused"] = replace(
                    snapshots["paused"],
                    checkout=moved,
                    status=PortableSessionStatus.PAUSED,
                    unavailable_from_status=None,
                )
                snapshots["completed"] = replace(
                    snapshots["completed"],
                    checkout=moved,
                    status=PortableSessionStatus.COMPLETED,
                    unavailable_from_status=None,
                )
                return snapshots[session_id]

            def try_next_event(self) -> None:
                return None

            def shutdown(self) -> None:
                return None

        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=FakeSupervisor(),  # type: ignore[arg-type]
            session_launch=PortableSessionLaunch(
                "new-session", Path.cwd(), PortableWorkflowOperation.PLANNING, ()
            ),
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            await pilot.press("down", "enter", "enter")
            await pilot.pause()
            input_widget = app.query_one("#portable-input", Input)
            input_widget.value = str(moved)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            labels = _menu_labels(app)

        self.assertEqual(app._session_snapshots["paused"].checkout, moved)
        self.assertIs(app._session_snapshots["paused"].status, PortableSessionStatus.PAUSED)
        self.assertEqual(app._session_snapshots["completed"].checkout, moved)
        self.assertIn("History (1)", labels)

    async def test_forget_confirmation_cancel_is_noop_then_removes_only_selected_metadata(
        self,
    ) -> None:
        snapshots = {
            session_id: PortableSessionSnapshot(
                session_id,
                Path.cwd(),
                PortableSessionStatus.COMPLETED,
            )
            for session_id in ("first-history", "second-history")
        }
        forgotten: list[str] = []

        class FakeSupervisor:
            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return tuple(snapshots.values())

            def list_saved_projects(self) -> tuple[object, ...]:
                return ()

            def forget_session(self, session_id: str) -> None:
                forgotten.append(session_id)
                snapshots.pop(session_id)

            def try_next_event(self) -> None:
                return None

            def shutdown(self) -> None:
                return None

        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=FakeSupervisor(),  # type: ignore[arg-type]
            session_launch=PortableSessionLaunch(
                "new-session", Path.cwd(), PortableWorkflowOperation.PLANNING, ()
            ),
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            await pilot.press("down", "enter", "down", "enter", "enter")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            cancelled = tuple(forgotten)
            app._finish_forget_confirmation(True)
            await pilot.pause()
            labels = _menu_labels(app)

        self.assertEqual(cancelled, ())
        self.assertEqual(forgotten, ["first-history"])
        self.assertEqual(tuple(snapshots), ("second-history",))
        self.assertIn("History (1)", labels)

    async def test_invalid_relink_keeps_actionable_record_and_escape_cancels_input(self) -> None:
        unavailable = PortableSessionSnapshot(
            session_id="invalid-ui-relink",
            checkout=Path("missing-ui-checkout").resolve(),
            status=PortableSessionStatus.UNAVAILABLE,
            unavailable_from_status=PortableSessionStatus.PAUSED,
        )

        class FakeSupervisor:
            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return (unavailable,)

            def list_saved_projects(self) -> tuple[object, ...]:
                return ()

            def relink_session(self, session_id: str, checkout: Path) -> PortableSessionSnapshot:
                del session_id, checkout
                raise ValueError("Relink requires the referenced PRD and Issue Index")

            def try_next_event(self) -> None:
                return None

            def shutdown(self) -> None:
                return None

        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=FakeSupervisor(),  # type: ignore[arg-type]
            session_launch=PortableSessionLaunch(
                "new-session", Path.cwd(), PortableWorkflowOperation.PLANNING, ()
            ),
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            await pilot.press("down", "enter", "enter")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            cancelled_labels = _menu_labels(app)
            await pilot.press("enter")
            await pilot.pause()
            input_widget = app.query_one("#portable-input", Input)
            input_widget.value = str(Path.cwd())
            await pilot.press("enter")
            await pilot.pause()
            status = str(app.query_one("#portable-status", Static).content)
            labels = _menu_labels(app)

        self.assertIn("Relink", cancelled_labels)
        self.assertEqual(status, "RELINK NOT COMMITTED")
        self.assertIn("Relink", labels)
        self.assertIs(app._session_snapshots[unavailable.session_id], unavailable)

    async def test_history_relink_failure_escape_returns_to_history(self) -> None:
        unavailable = PortableSessionSnapshot(
            session_id="invalid-history-relink",
            checkout=Path("missing-history-checkout").resolve(),
            status=PortableSessionStatus.UNAVAILABLE,
            unavailable_from_status=PortableSessionStatus.COMPLETED,
        )

        class FakeSupervisor:
            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return (unavailable,)

            def list_saved_projects(self) -> tuple[object, ...]:
                return ()

            def relink_session(self, session_id: str, checkout: Path) -> PortableSessionSnapshot:
                del session_id, checkout
                raise ValueError("Relink requires the referenced PRD and Issue Index")

            def try_next_event(self) -> None:
                return None

            def shutdown(self) -> None:
                return None

        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=FakeSupervisor(),  # type: ignore[arg-type]
            session_launch=PortableSessionLaunch(
                "new-session", Path.cwd(), PortableWorkflowOperation.PLANNING, ()
            ),
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            await pilot.press("down", "enter", "down", "enter", "enter")
            await pilot.pause()
            input_widget = app.query_one("#portable-input", Input)
            input_widget.value = str(Path.cwd())
            await pilot.press("enter")
            await pilot.pause()
            failure_status = str(app.query_one("#portable-status", Static).content)
            failure_labels = _menu_labels(app)
            await pilot.press("escape")
            await pilot.pause()
            final_header = str(app.query_one("#portable-header", Static).content)
            final_labels = _menu_labels(app)

        self.assertEqual(failure_status, "RELINK NOT COMMITTED")
        self.assertIn("History", failure_labels)
        self.assertEqual(final_header, "Dev Loop > Sessions > History")
        self.assertIn("Back to Sessions", final_labels)

    async def test_unavailable_session_offers_relink_and_metadata_only_forget(self) -> None:
        unavailable = PortableSessionSnapshot(
            session_id="unavailable-row",
            checkout=Path("missing-checkout").resolve(),
            status=PortableSessionStatus.UNAVAILABLE,
            unavailable_from_status=PortableSessionStatus.PAUSED,
        )

        class FakeSupervisor:
            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return (unavailable,)

            def list_saved_projects(self) -> tuple[object, ...]:
                return ()

            def try_next_event(self) -> None:
                return None

            def shutdown(self) -> None:
                return None

        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=FakeSupervisor(),  # type: ignore[arg-type]
            session_launch=PortableSessionLaunch(
                "new-session",
                Path.cwd(),
                PortableWorkflowOperation.PLANNING,
                (),
            ),
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            await pilot.press("down", "enter")
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            labels = tuple(
                str(menu.get_option_at_index(i).prompt) for i in range(menu.option_count)
            )

        self.assertIn("Relink", labels)
        self.assertIn("Forget (metadata only)", labels)

    async def test_completed_sessions_are_only_listed_in_distinct_history_view(self) -> None:
        completed = PortableSessionSnapshot(
            session_id="history-row",
            checkout=Path.cwd(),
            status=PortableSessionStatus.COMPLETED,
            result=0,
            progress=PortableSessionProgress(completed_issues=2, total_issues=2),
            updated_at=10.0,
        )

        class FakeSupervisor:
            def list_sessions(self) -> tuple[PortableSessionSnapshot, ...]:
                return (completed,)

            def list_saved_projects(self) -> tuple[object, ...]:
                return ()

            def try_next_event(self) -> None:
                return None

            def shutdown(self) -> None:
                return None

        app = PortableApplicationShell(
            PortableRuntimeBridge(),
            session_supervisor=FakeSupervisor(),  # type: ignore[arg-type]
            session_launch=PortableSessionLaunch(
                "new-session",
                Path.cwd(),
                PortableWorkflowOperation.PLANNING,
                (),
            ),
        )

        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            menu = app.query_one("#portable-navigation", OptionList)
            sessions_labels = tuple(
                str(menu.get_option_at_index(index).prompt)
                for index in range(menu.option_count)
            )
            await pilot.press("down", "enter")
            await pilot.pause()
            history_header = str(app.query_one("#portable-header", Static).content)
            history_labels = tuple(
                str(menu.get_option_at_index(i).prompt) for i in range(menu.option_count)
            )

        self.assertIn("History (1)", sessions_labels)
        self.assertNotIn("history-row", " ".join(sessions_labels))
        self.assertIn("History", history_header)
        self.assertIn("[COMPLETED]", " ".join(history_labels))


if __name__ == "__main__":
    unittest.main()


def _initialize_workflow_checkout(checkout: Path) -> None:
    issue_root = checkout / "prd" / "change" / "issues"
    issue_root.mkdir(parents=True)
    (checkout / "prd" / "change" / "change.md").write_text(
        "# Change\n",
        encoding="utf-8",
    )
    (issue_root / "README.md").write_text(
        "# Issues\n\n- [Issue 0001](./0001-change.md)\n",
        encoding="utf-8",
    )
    (issue_root / "0001-change.md").write_text(
        "# Issue 0001\n\nCompleted: [x]\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init", str(checkout)],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_output(checkout: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _menu_labels(app: PortableApplicationShell) -> tuple[str, ...]:
    menu = app.query_one("#portable-navigation", OptionList)
    return tuple(
        str(menu.get_option_at_index(index).prompt)
        for index in range(menu.option_count)
    )
