from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

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
                "newer than supported version 1",
            ):
                PortableSessionCatalog(database)

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
                backend="codex",
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
                backend="codex",
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
