from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from devloop import cli
from devloop.portable_session_catalog import (
    PortablePlanningSettings,
    PortableSessionCatalog,
    PortableSessionCatalogError,
)
from devloop.portable_sessions import (
    PortableSessionIntent,
    PortableSessionIntentKind,
    PortableSessionLaunch,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
)


class PortableSessionCatalogTests(unittest.TestCase):
    def test_corrupt_catalog_fails_with_catalog_specific_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "portable-sessions.sqlite3"
            database.write_bytes(b"not a sqlite database")

            with self.assertRaisesRegex(
                PortableSessionCatalogError,
                "corrupt or unreadable",
            ):
                PortableSessionCatalog(database)

    def test_unsupported_catalog_schema_version_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "portable-sessions.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA user_version = 999")
                connection.commit()

            with self.assertRaisesRegex(
                PortableSessionCatalogError,
                "newer than supported version 5",
            ):
                PortableSessionCatalog(database)

    def test_version_three_catalog_adds_durable_session_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "portable-sessions.sqlite3"
            catalog = PortableSessionCatalog(database)
            launch = PortableSessionLaunch(
                session_id="session-before-revisions",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            catalog.create_session(launch)
            with closing(sqlite3.connect(database)) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(sessions)")
                }
                if "revision" in columns:
                    connection.execute("ALTER TABLE sessions DROP COLUMN revision")
                connection.execute("PRAGMA user_version = 3")
                connection.commit()

            migrated = PortableSessionCatalog(database)
            before = migrated.get_session(launch.session_id)
            committed_revision = migrated.update_session_status(
                launch.session_id,
                PortableSessionStatus.RUNNING,
            )
            after = migrated.get_session(launch.session_id)

        self.assertEqual(before.revision, 1)
        self.assertEqual(committed_revision, 2)
        self.assertEqual(after.revision, 2)

    def test_session_revision_follows_commit_order_when_clock_rolls_back(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            session = catalog.create_session(
                PortableSessionLaunch(
                    session_id="rollback-clock-session",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
            )

            with patch(
                "devloop.portable_session_catalog.time.time",
                side_effect=(200.0, 100.0),
            ):
                running_revision = catalog.update_session_status(
                    session.session_id,
                    PortableSessionStatus.RUNNING,
                )
                failed_revision = catalog.update_session_status(
                    session.session_id,
                    PortableSessionStatus.FAILED,
                )
            failed = catalog.get_session(session.session_id)

        self.assertEqual(running_revision, session.revision + 1)
        self.assertEqual(failed_revision, running_revision + 1)
        self.assertEqual(failed.revision, failed_revision)
        self.assertEqual(failed.updated_at, 100.0)

    def test_session_revision_survives_rollback_delete_and_recreate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                session_id="recreated-after-rollback",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            first = catalog.create_session_with_lease(
                launch,
                owner_id="rollback-owner",
            )
            catalog.rollback_session_start(
                launch.session_id,
                owner_id="rollback-owner",
            )
            recreated = catalog.create_session(launch)

        self.assertEqual(first.revision, 1)
        self.assertEqual(recreated.revision, 2)

    def test_version_one_catalog_migrates_without_losing_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "portable-sessions.sqlite3"
            catalog = PortableSessionCatalog(database)
            launch = PortableSessionLaunch(
                session_id="session-before-leases",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(checkout)),
            )
            catalog.create_session(launch)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("DROP TABLE worktree_leases")
                connection.execute("PRAGMA user_version = 1")
                connection.commit()

            migrated = PortableSessionCatalog(database)
            session = migrated.get_session(launch.session_id)
            lease = migrated.acquire_session_lease(
                launch.session_id,
                owner_id="migration-test",
            )

        self.assertEqual(session.checkout, checkout.resolve())
        self.assertEqual(lease.session_id, launch.session_id)

    def test_version_two_catalog_adds_default_concurrency_without_losing_sessions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "portable-sessions.sqlite3"
            catalog = PortableSessionCatalog(database)
            launch = PortableSessionLaunch(
                session_id="session-before-capacity",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(checkout)),
            )
            catalog.create_session(launch)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("DROP TABLE execution_claims")
                connection.execute("DROP TABLE execution_requests")
                connection.execute("DROP TABLE catalog_settings")
                connection.execute("PRAGMA user_version = 2")
                connection.commit()

            migrated = PortableSessionCatalog(database)
            limit = migrated.get_concurrency_limit()
            session = migrated.get_session(launch.session_id)

        self.assertEqual(limit, 2)
        self.assertEqual(
            session.checkout,
            checkout.resolve(),
        )

    def test_failed_catalog_write_preserves_previous_readable_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_checkout = root / "first"
            second_checkout = root / "second"
            first_checkout.mkdir()
            second_checkout.mkdir()
            database = root / "portable-sessions.sqlite3"
            catalog = PortableSessionCatalog(database)
            catalog.create_session(
                PortableSessionLaunch(
                    session_id="session-first",
                    checkout=first_checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
            )
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    CREATE TRIGGER reject_second_session
                    BEFORE INSERT ON sessions
                    WHEN NEW.session_id = 'session-second'
                    BEGIN
                        SELECT RAISE(ABORT, 'forced transaction failure');
                    END
                    """
                )
                connection.commit()

            with self.assertRaises(ValueError):
                catalog.create_session(
                    PortableSessionLaunch(
                        session_id="session-second",
                        checkout=second_checkout,
                        operation=PortableWorkflowOperation.PLANNING,
                        arguments=(),
                    )
                )

            reopened = PortableSessionCatalog(database)
            sessions = reopened.list_sessions()
            projects = reopened.list_saved_projects()

        self.assertEqual(
            [session.session_id for session in sessions],
            ["session-first"],
        )
        self.assertEqual(
            [project.checkout for project in projects],
            [first_checkout.resolve()],
        )

    def test_published_workflow_replaces_planning_state_with_bounded_pointers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            issues = checkout / "prd" / "change" / "issues"
            issues.mkdir(parents=True)
            prd_path = checkout / "prd" / "change" / "change.md"
            issues_index = issues / "README.md"
            prd_path.write_text("# Change\n", encoding="utf-8")
            issues_index.write_text("# Issues\n", encoding="utf-8")
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                session_id="session-published",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            settings = PortablePlanningSettings(
                backend="CODEX_CLI",
                model="gpt-5.6-sol",
                reasoning_effort="xhigh",
                fast="OFF",
                timeout_seconds=900.0,
                checkpoint_seconds=180.0,
            )
            catalog.create_session(launch, settings)
            catalog.save_planning_thread(
                launch.session_id,
                "0198c0de-1111-2222-3333-444455556666",
            )

            catalog.publish_workflow(
                launch.session_id,
                prd_path=prd_path,
                issues_index_path=issues_index,
                activity_summary="Published for delivery",
            )
            published = PortableSessionCatalog(catalog.path).get_session(
                launch.session_id
            )

        self.assertEqual(published.prd_path, prd_path.resolve())
        self.assertEqual(published.issues_index_path, issues_index.resolve())
        self.assertEqual(published.arguments, ("--prd", str(prd_path.resolve())))
        self.assertEqual(published.activity_summary, "Published for delivery")
        self.assertIsNone(published.planning_thread_id)
        self.assertIsNone(published.planning_settings)

    def test_delivery_transfer_rebases_published_workflow_with_the_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            implementation = root / "implementation"
            source_issues = source / "prd" / "change" / "issues"
            implementation_issues = implementation / "prd" / "change" / "issues"
            source_issues.mkdir(parents=True)
            implementation_issues.mkdir(parents=True)
            source_prd = source / "prd" / "change" / "change.md"
            source_index = source_issues / "README.md"
            implementation_prd = implementation / "prd" / "change" / "change.md"
            implementation_index = implementation_issues / "README.md"
            for path in (
                source_prd,
                source_index,
                implementation_prd,
                implementation_index,
            ):
                path.write_text("# Workflow\n", encoding="utf-8")
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                session_id="session-delivery-transfer",
                checkout=source,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(source)),
            )
            catalog.create_session_with_lease(launch, owner_id="shell-transfer")
            catalog.publish_workflow(
                launch.session_id,
                prd_path=source_prd,
                issues_index_path=source_index,
                activity_summary="Published in planning checkout",
            )

            catalog.bind_session_checkout(
                launch.session_id,
                implementation,
                owner_id="shell-transfer",
                prd_path=implementation_prd,
                issues_index_path=implementation_index,
            )
            transferred = PortableSessionCatalog(catalog.path).get_session(
                launch.session_id
            )
            source_lease = catalog.get_worktree_lease(source)
            implementation_lease = catalog.get_worktree_lease(implementation)

        self.assertEqual(transferred.checkout, implementation.resolve())
        self.assertEqual(transferred.prd_path, implementation_prd.resolve())
        self.assertEqual(
            transferred.issues_index_path,
            implementation_index.resolve(),
        )
        self.assertEqual(
            transferred.arguments,
            ("--prd", str(implementation_prd.resolve())),
        )
        self.assertIsNone(source_lease)
        assert implementation_lease is not None
        self.assertEqual(implementation_lease.session_id, launch.session_id)

    def test_failed_delivery_transfer_preserves_source_workflow_and_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            implementation = root / "implementation"
            source.mkdir()
            implementation.mkdir()
            source_prd = source / "change.md"
            source_index = source / "README.md"
            implementation_prd = implementation / "change.md"
            implementation_index = implementation / "README.md"
            for path in (
                source_prd,
                source_index,
                implementation_prd,
                implementation_index,
            ):
                path.write_text("# Workflow\n", encoding="utf-8")
            database = root / "portable-sessions.sqlite3"
            catalog = PortableSessionCatalog(database)
            launch = PortableSessionLaunch(
                session_id="session-transfer-rollback",
                checkout=source,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(source)),
            )
            catalog.create_session_with_lease(launch, owner_id="shell-transfer")
            catalog.publish_workflow(
                launch.session_id,
                prd_path=source_prd,
                issues_index_path=source_index,
                activity_summary="Published in source",
            )
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    CREATE TRIGGER reject_lease_transfer
                    BEFORE UPDATE OF checkout ON worktree_leases
                    BEGIN
                        SELECT RAISE(ABORT, 'forced lease transfer failure');
                    END
                    """
                )
                connection.commit()

            with self.assertRaises(PortableSessionCatalogError):
                catalog.bind_session_checkout(
                    launch.session_id,
                    implementation,
                    owner_id="shell-transfer",
                    prd_path=implementation_prd,
                    issues_index_path=implementation_index,
                )

            preserved = PortableSessionCatalog(database).get_session(
                launch.session_id
            )
            source_lease = catalog.get_worktree_lease(source)
            implementation_lease = catalog.get_worktree_lease(implementation)

        self.assertEqual(preserved.checkout, source.resolve())
        self.assertEqual(preserved.prd_path, source_prd.resolve())
        self.assertEqual(preserved.issues_index_path, source_index.resolve())
        assert source_lease is not None
        self.assertEqual(source_lease.session_id, launch.session_id)
        self.assertIsNone(implementation_lease)

    def test_delivery_transfer_reserves_target_before_preparing_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            source_prd = source / "change.md"
            source_index = source / "README.md"
            source_prd.write_text("# Source PRD\n", encoding="utf-8")
            source_index.write_text("# Source issues\n", encoding="utf-8")
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            source_launch = PortableSessionLaunch(
                session_id="source-session",
                checkout=source,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(source)),
            )
            target_launch = PortableSessionLaunch(
                session_id="target-session",
                checkout=target,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(target)),
            )
            catalog.create_session_with_lease(
                source_launch,
                owner_id="source-shell",
            )
            catalog.publish_workflow(
                source_launch.session_id,
                prd_path=source_prd,
                issues_index_path=source_index,
                activity_summary="Published in source",
            )
            catalog.create_session_with_lease(
                target_launch,
                owner_id="target-shell",
            )
            target_before = tuple(
                (path.relative_to(target), path.read_bytes())
                for path in sorted(target.rglob("*"))
                if path.is_file()
            )
            prepare_calls = 0

            def prepare_artifacts() -> None:
                nonlocal prepare_calls
                prepare_calls += 1
                (target / "change.md").write_text("# Must not copy\n", encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError,
                "already leased",
            ):
                catalog.bind_session_checkout(
                    source_launch.session_id,
                    target,
                    owner_id="source-shell",
                    prd_path=target / "change.md",
                    issues_index_path=target / "README.md",
                    prepare_checkout=prepare_artifacts,
                )

            source_record = catalog.get_session(source_launch.session_id)
            target_after = tuple(
                (path.relative_to(target), path.read_bytes())
                for path in sorted(target.rglob("*"))
                if path.is_file()
            )

        self.assertEqual(prepare_calls, 0)
        self.assertEqual(target_after, target_before)
        self.assertEqual(source_record.checkout, source.resolve())
        self.assertEqual(source_record.prd_path, source_prd.resolve())
        self.assertEqual(source_record.issues_index_path, source_index.resolve())

    def test_planning_artifact_copy_rolls_back_when_pointer_commit_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"
            source_package = source / "prd" / "change"
            target_package = target / "prd" / "change"
            source_issues = source_package / "issues"
            target_issues = target_package / "issues"
            source_issues.mkdir(parents=True)
            target_issues.mkdir(parents=True)
            source_prd = source_package / "change.md"
            source_index = source_issues / "README.md"
            source_prd.write_text("# New PRD\n", encoding="utf-8")
            source_index.write_text("# New issues\n", encoding="utf-8")
            (source_issues / "0001.md").write_text("# New issue\n", encoding="utf-8")
            (target_package / "keep.txt").write_text("keep\n", encoding="utf-8")
            (target_issues / "README.md").write_text(
                "# Existing issues\n",
                encoding="utf-8",
            )
            target_before = tuple(
                (path.relative_to(target), path.read_bytes())
                for path in sorted(target.rglob("*"))
                if path.is_file()
            )

            transfer = cli.PlanningArtifactTransfer(
                prd_path=source_prd,
                issues_index=source_index,
                source_repo=source,
                target_repo=target,
            )
            with self.assertRaisesRegex(RuntimeError, "pointer commit failed"):
                with transfer:
                    transfer.prepare()
                    raise RuntimeError("pointer commit failed")

            target_after = tuple(
                (path.relative_to(target), path.read_bytes())
                for path in sorted(target.rglob("*"))
                if path.is_file()
            )

        self.assertEqual(target_after, target_before)

    def test_catalog_redacts_and_bounds_activity_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            prd_path = checkout / "change.md"
            issues_index = checkout / "README.md"
            prd_path.write_text("# Change\n", encoding="utf-8")
            issues_index.write_text("# Issues\n", encoding="utf-8")
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            catalog.create_session(
                PortableSessionLaunch(
                    session_id="session-redaction",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
            )

            catalog.publish_workflow(
                "session-redaction",
                prd_path=prd_path,
                issues_index_path=issues_index,
                activity_summary="Bearer secret-value " + ("activity " * 100),
            )
            published = catalog.get_session("session-redaction")

        self.assertLessEqual(len(published.activity_summary), 500)
        self.assertNotIn("secret-value", published.activity_summary)
        self.assertIn("[redacted]", published.activity_summary)

    def test_saved_project_exposes_each_unfinished_prd_as_its_own_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            catalog.create_session(
                PortableSessionLaunch(
                    session_id="session-discovery",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
            )
            artifacts = []
            for number in (1, 2):
                folder = checkout / "prd" / f"change-{number}"
                issues = folder / "issues"
                issues.mkdir(parents=True)
                prd_path = folder / f"change-{number}.md"
                issues_index = issues / "README.md"
                prd_path.write_text(f"# Change {number}\n", encoding="utf-8")
                issues_index.write_text("# Issues\n", encoding="utf-8")
                artifacts.append(
                    SimpleNamespace(
                        artifacts=SimpleNamespace(
                            prd_path=prd_path,
                            issues_index=issues_index,
                        ),
                        completed_issues=number - 1,
                        pending_issues=3 - number,
                        total_issues=2,
                        active_issue=f"000{number}",
                        active_status="In Progress",
                        updated_at=float(number),
                    )
                )
            original_contents = {
                path: path.read_bytes()
                for artifact in artifacts
                for path in (
                    artifact.artifacts.prd_path,
                    artifact.artifacts.issues_index,
                )
            }

            candidates = catalog.discover_resume_candidates(
                lambda saved_checkout: (
                    artifacts if saved_checkout == checkout.resolve() else ()
                )
            )
            current_contents = {
                path: path.read_bytes()
                for path in original_contents
            }

        self.assertEqual(len(candidates), 2)
        self.assertNotEqual(candidates[0].candidate_id, candidates[1].candidate_id)
        self.assertEqual(candidates[0].prd_path.name, "change-2.md")
        self.assertEqual(candidates[1].prd_path.name, "change-1.md")
        self.assertEqual(
            current_contents,
            original_contents,
        )

    def test_pre_prd_session_survives_catalog_restart_without_starting_work(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "state" / "portable-sessions.sqlite3"
            launch = PortableSessionLaunch(
                session_id="session-0002",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(checkout)),
            )
            settings = PortablePlanningSettings(
                backend="CODEX_CLI",
                model="gpt-5.6-sol",
                reasoning_effort="xhigh",
                fast="OFF",
                timeout_seconds=900.0,
                checkpoint_seconds=180.0,
            )

            PortableSessionCatalog(database).create_session(launch, settings)
            reopened = PortableSessionCatalog(database)

            sessions = reopened.list_sessions()
            projects = reopened.list_saved_projects()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, launch.session_id)
        self.assertEqual(sessions[0].checkout, checkout.resolve())
        self.assertEqual(sessions[0].status, PortableSessionStatus.READY)
        self.assertEqual(sessions[0].operation, PortableWorkflowOperation.PLANNING)
        self.assertEqual(sessions[0].arguments, launch.arguments)
        self.assertEqual(sessions[0].planning_settings, settings)
        self.assertIsNone(sessions[0].planning_thread_id)
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].checkout, checkout.resolve())

    def test_direct_delivery_session_reconstructs_parser_context_after_restart(
        self,
    ) -> None:
        class FakeWorker:
            def __init__(self) -> None:
                self.stdin = _WritableLines()
                self.stdout = _ReadableLines()
                self.stderr = _ReadableLines()
                self._returncode: int | None = None

            def poll(self) -> int | None:
                return self._returncode

            def terminate(self) -> None:
                self._returncode = 1
                self.stdout.close()
                self.stderr.close()

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                return self._returncode or 0

        launched: list[PortableSessionLaunch] = []
        worker = FakeWorker()

        def launch_worker(selected: PortableSessionLaunch) -> FakeWorker:
            launched.append(selected)
            return worker

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            issues = checkout / "prd" / "change" / "issues"
            issues.mkdir(parents=True)
            prd = issues.parent / "change.md"
            index = issues / "README.md"
            prd.write_text("# Change\n", encoding="utf-8")
            index.write_text("# Issues\n", encoding="utf-8")
            database = root / "portable-sessions.sqlite3"
            launch = PortableSessionLaunch(
                session_id="direct-delivery-restart",
                checkout=checkout,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=(
                    "--prd",
                    str(prd),
                    "--issues",
                    str(index),
                    "--all",
                    "--start-issue",
                    "0003",
                    "--max-passes",
                    "4",
                    "--blocked-retry-rounds",
                    "2",
                    "--no-blocked-retry",
                    "--no-worktree",
                    "--non-interactive",
                    "--plain",
                    "--no-self-improvement-wiki",
                    "--codex",
                    "custom-codex",
                    "--sandbox",
                    "read-only",
                    "--approval-policy",
                    "on-request",
                    "--goal",
                    "Bearer must-not-survive",
                ),
            )
            PortableSessionCatalog(database).create_session(launch)

            restarted = PortableSessionCatalog(database).get_session(
                launch.session_id
            )
            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=PortableSessionCatalog(database),
                owner_id="restarted-shell",
            )
            supervisor.resume_session(launch.session_id)
            worker.stdout.close()
            worker.stderr.close()
            supervisor.shutdown()
            parsed = cli.build_parser().parse_args(launched[0].arguments)

        self.assertEqual(restarted.operation, PortableWorkflowOperation.DELIVERY)
        self.assertEqual(Path(parsed.prd), prd.resolve())
        self.assertEqual(Path(parsed.issues), index.resolve())
        self.assertTrue(parsed.all)
        self.assertEqual(parsed.start_issue, "0003")
        self.assertEqual(parsed.max_passes, 4)
        self.assertEqual(parsed.blocked_retry_rounds, 2)
        self.assertTrue(parsed.no_blocked_retry)
        self.assertTrue(parsed.no_worktree)
        self.assertTrue(parsed.non_interactive)
        self.assertTrue(parsed.plain)
        self.assertFalse(parsed.self_improvement_wiki)
        self.assertEqual(parsed.codex, "custom-codex")
        self.assertEqual(parsed.sandbox, "read-only")
        self.assertEqual(parsed.approval_policy, "on-request")
        self.assertEqual(launched[0].operation, PortableWorkflowOperation.DELIVERY)
        self.assertNotIn("must-not-survive", " ".join(restarted.arguments))

    def test_restarted_supervisor_is_passive_until_explicit_resume(self) -> None:
        class FakeWorker:
            def __init__(self) -> None:
                self.stdin = _WritableLines()
                self.stdout = _ReadableLines()
                self.stderr = _ReadableLines()
                self._returncode: int | None = None

            def poll(self) -> int | None:
                return self._returncode

            def terminate(self) -> None:
                self._returncode = 1
                self.stdout.close()
                self.stderr.close()

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                return self._returncode or 0

        launched: list[PortableSessionLaunch] = []
        worker = FakeWorker()

        def launch_worker(launch: PortableSessionLaunch) -> FakeWorker:
            launched.append(launch)
            return worker

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                session_id="session-resume",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(checkout)),
            )
            settings = PortablePlanningSettings(
                backend="CODEX_CLI",
                model="gpt-5.6-sol",
                reasoning_effort="xhigh",
                fast="OFF",
                timeout_seconds=900.0,
                checkpoint_seconds=180.0,
            )
            catalog.create_session(launch, settings)
            catalog.save_planning_thread(
                launch.session_id,
                "0198c0de-1111-2222-3333-444455556666",
            )

            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=PortableSessionCatalog(catalog.path),
            )

            self.assertEqual(launched, [])
            restored = supervisor.list_sessions()
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0].status, PortableSessionStatus.READY)

            supervisor.handle_intent(
                PortableSessionIntent(
                    kind=PortableSessionIntentKind.RESUME,
                    session_id=launch.session_id,
                )
            )
            command = worker.stdin.lines[0]
            worker.stdout.close()
            worker.stderr.close()
            supervisor.shutdown()

        self.assertEqual(launched, [launch])
        self.assertIn('"kind":"RESUME"', command)
        self.assertIn(
            '"planning_thread_id":"0198c0de-1111-2222-3333-444455556666"',
            command,
        )
        self.assertIn('"model":"gpt-5.6-sol"', command)


class _WritableLines:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, value: str) -> int:
        self.lines.append(value)
        return len(value)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class _ReadableLines:
    def __init__(self) -> None:
        self._closed = threading.Event()

    def __iter__(self) -> _ReadableLines:
        return self

    def __next__(self) -> str:
        self._closed.wait(1)
        raise StopIteration

    def close(self) -> None:
        self._closed.set()


if __name__ == "__main__":
    unittest.main()
