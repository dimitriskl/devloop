from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

from devloop import catalog as planner_catalog
from devloop import interactive_runner
from devloop.portable_protocol import (
    PORTABLE_PROTOCOL_VERSION,
    PortableProtocolFrame,
    WorkerMessageKind,
)
from devloop.portable_session_catalog import (
    PortablePlanningSettings,
    PortableSessionCatalog,
    PortableSessionCatalogError,
)
from devloop.portable_sessions import (
    PortableSessionLaunch,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
)


class PortableSessionCatalogCompatibilityTests(unittest.TestCase):
    def test_planning_settings_reject_timeout_above_runtime_maximum_before_write(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "timeout cannot exceed 3600 seconds",
        ):
            PortablePlanningSettings(
                backend="CODEX_CLI",
                model="gpt-5",
                reasoning_effort="high",
                fast="OFF",
                timeout_seconds=3601,
                checkpoint_seconds=30,
            )

    def test_planning_settings_reject_backend_fast_mismatch_before_write(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "Fast cannot be enabled for the Claude Code Backend",
        ):
            PortablePlanningSettings(
                backend="CLAUDE_CODE",
                model="claude-sonnet-5",
                reasoning_effort="high",
                fast="ON",
                timeout_seconds=60,
                checkpoint_seconds=30,
            )

    def test_planning_settings_enforce_remaining_runtime_invariants_before_write(
        self,
    ) -> None:
        valid_settings = {
            "backend": "CODEX_CLI",
            "model": "gpt-5",
            "reasoning_effort": "high",
            "fast": "OFF",
            "timeout_seconds": 60,
            "checkpoint_seconds": 30,
        }
        for field_name, invalid_value, expected_error in (
            (
                "checkpoint_seconds",
                61,
                "checkpoint deadline must fit inside the timeout",
            ),
            ("model", " gpt-5", "model must be a non-empty single-line value"),
            (
                "reasoning_effort",
                "high\t",
                "reasoning effort must be a non-empty single-line value",
            ),
        ):
            with self.subTest(field_name=field_name):
                settings = {**valid_settings, field_name: invalid_value}
                with self.assertRaisesRegex(ValueError, expected_error):
                    PortablePlanningSettings(**settings)

    def test_catalog_open_rejects_negative_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA user_version = -1")

            with self.assertRaisesRegex(
                PortableSessionCatalogError,
                "unsupported schema version -1",
            ):
                PortableSessionCatalog(database)

    def test_catalog_open_rejects_orphaned_session_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "catalog.sqlite3"
            catalog = PortableSessionCatalog(database)
            catalog.create_session(
                PortableSessionLaunch(
                    session_id="orphaned-session",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
            )
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("DELETE FROM saved_projects")
                connection.commit()

            with self.assertRaisesRegex(
                PortableSessionCatalogError,
                "foreign key check failed",
            ):
                PortableSessionCatalog(database)

    def test_catalog_open_rejects_surplus_planning_setting_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "catalog.sqlite3"
            catalog = PortableSessionCatalog(database)
            persisted_settings = PortablePlanningSettings(
                backend="CODEX_CLI",
                model="gpt-5",
                reasoning_effort="high",
                fast="OFF",
                timeout_seconds=60,
                checkpoint_seconds=30,
            ).to_dict()
            catalog.create_session(
                PortableSessionLaunch(
                    session_id="surplus-settings",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                ),
                PortablePlanningSettings.from_mapping(persisted_settings),
            )
            persisted_settings["api_token"] = "must-not-be-accepted"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    UPDATE sessions
                    SET planning_settings_json = ?
                    """,
                    (json.dumps(persisted_settings, separators=(",", ":")),),
                )
                connection.commit()

            with self.assertRaisesRegex(
                PortableSessionCatalogError,
                "planning settings are corrupt",
            ):
                PortableSessionCatalog(database)

    def test_catalog_open_rejects_unsupported_closed_planning_settings(self) -> None:
        for field_name, invalid_value, expected_error in (
            ("backend", "SHELL", "Unsupported Execution Backend"),
            ("fast", "AUTO", "Unsupported Fast preference"),
        ):
            with self.subTest(field_name=field_name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    checkout = root / "checkout"
                    checkout.mkdir()
                    database = root / "catalog.sqlite3"
                    catalog = PortableSessionCatalog(database)
                    persisted_settings = PortablePlanningSettings(
                        backend="CODEX_CLI",
                        model="gpt-5",
                        reasoning_effort="high",
                        fast="OFF",
                        timeout_seconds=60,
                        checkpoint_seconds=30,
                    ).to_dict()
                    catalog.create_session(
                        PortableSessionLaunch(
                            session_id=f"unsupported-{field_name}",
                            checkout=checkout,
                            operation=PortableWorkflowOperation.PLANNING,
                            arguments=(),
                        ),
                        PortablePlanningSettings.from_mapping(persisted_settings),
                    )
                    persisted_settings[field_name] = invalid_value
                    with closing(sqlite3.connect(database)) as connection:
                        connection.execute(
                            """
                            UPDATE sessions
                            SET planning_settings_json = ?
                            """,
                            (
                                json.dumps(
                                    persisted_settings,
                                    separators=(",", ":"),
                                ),
                            ),
                        )
                        connection.commit()

                    with self.assertRaisesRegex(
                        PortableSessionCatalogError,
                        expected_error,
                    ):
                        PortableSessionCatalog(database)

    def test_catalog_open_rejects_non_finite_planning_numbers(self) -> None:
        for field_name in ("timeout_seconds", "checkpoint_seconds"):
            for invalid_number in (float("nan"), float("inf")):
                with self.subTest(
                    field_name=field_name,
                    invalid_number=invalid_number,
                ):
                    self._assert_non_finite_planning_number_rejected(
                        field_name,
                        invalid_number,
                    )

    def test_catalog_open_translates_runtime_invalid_planning_settings(
        self,
    ) -> None:
        valid_settings = {
            "backend": "CODEX_CLI",
            "model": "gpt-5",
            "reasoning_effort": "high",
            "fast": "OFF",
            "timeout_seconds": 60,
            "checkpoint_seconds": 30,
        }
        for overrides in (
            {"timeout_seconds": 3601},
            {"checkpoint_seconds": 61},
            {
                "backend": "CLAUDE_CODE",
                "model": "claude-sonnet-5",
                "fast": "ON",
            },
            {"model": " gpt-5"},
            {"reasoning_effort": "high\t"},
        ):
            with self.subTest(overrides=overrides):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    checkout = root / "checkout"
                    checkout.mkdir()
                    database = root / "catalog.sqlite3"
                    catalog = PortableSessionCatalog(database)
                    catalog.create_session(
                        PortableSessionLaunch(
                            session_id="runtime-invalid-settings",
                            checkout=checkout,
                            operation=PortableWorkflowOperation.PLANNING,
                            arguments=(),
                        )
                    )
                    persisted_settings = {**valid_settings, **overrides}
                    with closing(sqlite3.connect(database)) as connection:
                        connection.execute(
                            """
                            UPDATE sessions
                            SET planning_settings_json = ?
                            """,
                            (
                                json.dumps(
                                    persisted_settings,
                                    separators=(",", ":"),
                                ),
                            ),
                        )
                        connection.commit()

                    with self.assertRaisesRegex(
                        PortableSessionCatalogError,
                        "planning settings are corrupt",
                    ):
                        PortableSessionCatalog(database)

    def _assert_non_finite_planning_number_rejected(
        self,
        field_name: str,
        invalid_number: float,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "catalog.sqlite3"
            catalog = PortableSessionCatalog(database)
            catalog.create_session(
                PortableSessionLaunch(
                    session_id="non-finite-settings",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
            )
            persisted_settings = {
                "backend": "CODEX_CLI",
                "model": "gpt-5",
                "reasoning_effort": "high",
                "fast": "OFF",
                "timeout_seconds": 60,
                "checkpoint_seconds": 30,
            }
            persisted_settings[field_name] = invalid_number
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    UPDATE sessions
                    SET planning_settings_json = ?
                    """,
                    (json.dumps(persisted_settings, separators=(",", ":")),),
                )
                connection.commit()

            with self.assertRaisesRegex(
                PortableSessionCatalogError,
                "planning settings are corrupt",
            ):
                PortableSessionCatalog(database)

    def test_catalog_writes_do_not_require_sqlite_unixepoch_subsec(self) -> None:
        real_connect = sqlite3.connect

        def connect_without_unixepoch(*args, **kwargs):
            connection = real_connect(*args, **kwargs)

            def unsupported_unixepoch(*_arguments):
                raise sqlite3.OperationalError("no such function: unixepoch")

            connection.create_function("unixepoch", -1, unsupported_unixepoch)
            return connection

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            with patch(
                "devloop.portable_session_catalog.sqlite3.connect",
                side_effect=connect_without_unixepoch,
            ):
                catalog = PortableSessionCatalog(root / "catalog.sqlite3")
                session = catalog.create_session(
                    PortableSessionLaunch(
                        session_id="timestamp-compatible",
                        checkout=checkout,
                        operation=PortableWorkflowOperation.PLANNING,
                        arguments=(),
                    )
                )

        self.assertGreater(session.created_at, 0)
        self.assertGreaterEqual(session.updated_at, session.created_at)

    def test_catalog_persists_only_allowlisted_launch_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "catalog.sqlite3"
            catalog = PortableSessionCatalog(database)

            catalog.create_session(
                PortableSessionLaunch(
                    session_id="bounded-launch",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(
                        "--repo",
                        str(checkout),
                        "--goal",
                        "password=must-never-persist",
                        "--codex",
                        "C:/tools/codex.exe",
                        "--sandbox",
                        "read-only",
                        "--approval-policy",
                        "on-request",
                        "--native-editor",
                    ),
                )
            )
            restored = PortableSessionCatalog(database).get_session("bounded-launch")
            database_bytes = database.read_bytes()

        self.assertNotIn(b"must-never-persist", database_bytes)
        self.assertNotIn(b"--goal", database_bytes)
        self.assertEqual(
            restored.arguments,
            (
                "--repo",
                str(checkout.resolve()),
                "--codex",
                "C:/tools/codex.exe",
                "--sandbox",
                "read-only",
                "--approval-policy",
                "on-request",
                "--native-editor",
            ),
        )

    def test_thread_identity_must_be_a_bounded_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            catalog.create_session(
                PortableSessionLaunch(
                    session_id="thread-validation",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
            )

            for invalid in (
                "",
                "not-a-thread",
                "Bearer secret-value",
                "a" * 500,
            ):
                with self.subTest(invalid=invalid[:20]):
                    with self.assertRaisesRegex(ValueError, "UUID"):
                        catalog.save_planning_thread("thread-validation", invalid)

    def test_catalog_open_rejects_an_invalid_persisted_thread_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "catalog.sqlite3"
            catalog = PortableSessionCatalog(database)
            catalog.create_session(
                PortableSessionLaunch(
                    session_id="corrupt-thread",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
            )
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "UPDATE sessions SET planning_thread_id = ?",
                    ("x" * 36,),
                )
                connection.commit()

            with self.assertRaisesRegex(
                PortableSessionCatalogError,
                "integrity check failed|invalid session record",
            ):
                PortableSessionCatalog(database)

    def test_catalog_open_rejects_extra_columns_and_missing_foreign_keys(self) -> None:
        for mutation in ("extra-column", "missing-foreign-key"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    checkout = root / "checkout"
                    checkout.mkdir()
                    database = root / "catalog.sqlite3"
                    catalog = PortableSessionCatalog(database)
                    catalog.create_session(
                        PortableSessionLaunch(
                            session_id="schema-check",
                            checkout=checkout,
                            operation=PortableWorkflowOperation.PLANNING,
                            arguments=(),
                        )
                    )
                    with closing(sqlite3.connect(database)) as connection:
                        if mutation == "extra-column":
                            connection.execute(
                                "ALTER TABLE sessions ADD COLUMN unexpected TEXT"
                            )
                        else:
                            connection.executescript(
                                """
                                PRAGMA foreign_keys = OFF;
                                ALTER TABLE sessions RENAME TO old_sessions;
                                CREATE TABLE sessions (
                                    session_id TEXT PRIMARY KEY,
                                    project_id TEXT NOT NULL,
                                    status TEXT NOT NULL,
                                    operation TEXT NOT NULL,
                                    arguments_json TEXT NOT NULL,
                                    planning_thread_id TEXT,
                                    planning_settings_json TEXT,
                                    prd_path TEXT,
                                    issues_index_path TEXT,
                                    activity_summary TEXT NOT NULL DEFAULT '',
                                    created_at REAL NOT NULL,
                                    updated_at REAL NOT NULL
                                );
                                INSERT INTO sessions SELECT * FROM old_sessions;
                                DROP TABLE old_sessions;
                                """
                            )
                        connection.commit()

                    with self.assertRaisesRegex(
                        PortableSessionCatalogError,
                        "schema is incompatible",
                    ):
                        PortableSessionCatalog(database)

    def test_catalog_open_rejects_oversized_persisted_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "catalog.sqlite3"
            catalog = PortableSessionCatalog(database)
            catalog.create_session(
                PortableSessionLaunch(
                    session_id="bounded-record",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
            )
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute(
                    "UPDATE sessions SET activity_summary = ?",
                    ("x" * 501,),
                )
                connection.commit()

            with self.assertRaisesRegex(
                PortableSessionCatalogError,
                "integrity check failed|invalid session record",
            ):
                PortableSessionCatalog(database)

    def test_pre_first_thread_session_restarts_with_start_not_resume(self) -> None:
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

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            launch = PortableSessionLaunch(
                session_id="pre-thread",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--approval-policy", "on-request"),
            )
            catalog.create_session(launch)
            worker = FakeWorker()
            supervisor = PortableSessionSupervisor(
                worker_launcher=lambda _launch: worker,
                catalog=PortableSessionCatalog(catalog.path),
            )

            supervisor.resume_session(launch.session_id)
            command = worker.stdin.lines[0]
            worker.stdout.close()
            worker.stderr.close()
            supervisor.shutdown()

        self.assertIn('"kind":"START"', command)
        self.assertNotIn('"kind":"RESUME"', command)
        self.assertIn('"--approval-policy","on-request"', command)

    def test_selected_checkout_binding_creates_project_and_session_atomically(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "selected"
            checkout.mkdir()
            database = root / "catalog.sqlite3"
            catalog = PortableSessionCatalog(database)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    CREATE TRIGGER reject_selected_session
                    BEFORE INSERT ON sessions
                    BEGIN
                        SELECT RAISE(ABORT, 'forced bind failure');
                    END
                    """
                )
                connection.commit()

            with self.assertRaises(PortableSessionCatalogError):
                catalog.bind_or_create_session(
                    PortableSessionLaunch(
                        session_id="atomic-bind",
                        checkout=checkout,
                        operation=PortableWorkflowOperation.PLANNING,
                        arguments=(),
                    )
                )

            reopened = PortableSessionCatalog(database)
            self.assertEqual(reopened.list_saved_projects(), ())
            self.assertEqual(reopened.list_sessions(), ())

    def test_restored_session_selects_its_checkout_before_global_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout_a = root / "repo-a"
            checkout_b = root / "repo-b"
            checkout_a.mkdir()
            checkout_b.mkdir()
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            catalog.create_session(
                PortableSessionLaunch(
                    session_id="restore-a",
                    checkout=checkout_a,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
            )
            parser = interactive_runner.build_parser()
            args = parser.parse_args(["--goal", "continue"])
            contexts = []

            with (
                patch.dict(
                    "os.environ",
                    {
                        "DEVLOOP_PORTABLE_SESSION_CATALOG": str(catalog.path),
                        "DEVLOOP_PORTABLE_SESSION_ID": "restore-a",
                        "DEVLOOP_PORTABLE_SESSION_RESTORE": "1",
                    },
                    clear=False,
                ),
                patch.object(
                    interactive_runner.BundleContext,
                    "from_file",
                    return_value=SimpleNamespace(root=root),
                ),
                patch.object(
                    interactive_runner,
                    "plan_state_path",
                    return_value=root / "planner.json",
                ),
                patch.object(
                    interactive_runner.catalog_module,
                    "load_selection",
                    return_value=planner_catalog.Selection.defaults(),
                ),
                patch.object(
                    interactive_runner,
                    "choose_target_repo",
                    side_effect=AssertionError(
                        f"must not prompt for global default {checkout_b}"
                    ),
                ),
                patch.object(
                    interactive_runner,
                    "apply_branch_strategy",
                    side_effect=AssertionError("must not prompt for a branch"),
                ),
                patch.object(
                    interactive_runner,
                    "publish_planning_run_context",
                    side_effect=lambda **value: contexts.append(value),
                ),
                patch.object(
                    interactive_runner,
                    "current_branch",
                    return_value="feature-a",
                ),
                patch.object(
                    interactive_runner,
                    "snapshot_artifacts",
                    return_value={},
                ),
                patch.object(
                    interactive_runner,
                    "build_portable_component_catalog",
                    return_value=object(),
                ),
                patch.object(
                    interactive_runner,
                    "BackendModelCatalogAccess",
                    return_value=object(),
                ),
                patch.object(
                    interactive_runner,
                    "preflight_analysis_workflow",
                    return_value=None,
                ),
                patch.object(
                    interactive_runner,
                    "load_last_target_repo",
                    return_value=checkout_b,
                ),
            ):
                result = interactive_runner._run_planning(parser, args)

        self.assertEqual(result, 0)
        self.assertEqual(contexts[0]["project_root"], checkout_a.resolve())
        self.assertEqual(contexts[0]["implementation_worktree"], checkout_a.resolve())

    def test_startup_prd_selection_is_published_before_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            prd = checkout / "change.md"
            issues = checkout / "README.md"
            prd.write_text("# Change\n", encoding="utf-8")
            issues.write_text("# Issues\n", encoding="utf-8")
            artifacts = interactive_runner.PlanningArtifacts(prd, issues)
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            catalog.create_session(
                PortableSessionLaunch(
                    session_id="startup-prd",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=("--repo", str(checkout)),
                )
            )
            parser = interactive_runner.build_parser()
            args = parser.parse_args(["--repo", str(checkout)])
            published_before_handoff = []

            def run_handoff(*_args, **_kwargs):
                published_before_handoff.append(catalog.get_session("startup-prd"))
                return 23

            with (
                patch.dict(
                    "os.environ",
                    {
                        "DEVLOOP_PORTABLE_SESSION_CATALOG": str(catalog.path),
                        "DEVLOOP_PORTABLE_SESSION_ID": "startup-prd",
                        "DEVLOOP_PORTABLE_SESSION_RESTORE": "0",
                    },
                    clear=False,
                ),
                patch.object(
                    interactive_runner.BundleContext,
                    "from_file",
                    return_value=SimpleNamespace(root=root),
                ),
                patch.object(
                    interactive_runner,
                    "plan_state_path",
                    return_value=root / "planner.json",
                ),
                patch.object(
                    interactive_runner,
                    "choose_target_repo",
                    return_value=checkout,
                ),
                patch.object(
                    interactive_runner,
                    "choose_startup_artifacts",
                    return_value=interactive_runner.StartupMenuResult(
                        artifacts=artifacts
                    ),
                ),
                patch.object(
                    interactive_runner,
                    "current_branch",
                    return_value="main",
                ),
                patch.object(interactive_runner, "publish_planning_run_context"),
                patch.object(interactive_runner, "print_prd_status"),
                patch.object(
                    interactive_runner,
                    "run_handoff",
                    side_effect=run_handoff,
                ),
            ):
                result = interactive_runner._run_planning(parser, args)

            reopened_supervisor = PortableSessionSupervisor(
                worker_launcher=lambda _launch: self.fail("must remain passive"),
                catalog=PortableSessionCatalog(catalog.path),
                resume_candidates=(
                    SimpleNamespace(
                        candidate_id="startup-prd-candidate",
                        checkout=checkout.resolve(),
                        prd_path=prd.resolve(),
                    ),
                ),
            )
            sessions_after_restart = reopened_supervisor.list_sessions()

        self.assertEqual(result, 23)
        self.assertEqual(len(published_before_handoff), 1)
        published = published_before_handoff[0]
        self.assertEqual(published.prd_path, prd.resolve())
        self.assertEqual(published.issues_index_path, issues.resolve())
        self.assertEqual(published.arguments, ("--prd", str(prd.resolve())))
        self.assertEqual(len(sessions_after_restart), 1)
        self.assertEqual(sessions_after_restart[0].session_id, "startup-prd")

    def test_authoritative_unfinished_candidate_reopens_completed_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            prd = checkout / "change.md"
            issues = checkout / "README.md"
            prd.write_text("# Change\n", encoding="utf-8")
            issues.write_text("# Issues\n", encoding="utf-8")
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            catalog.create_session(
                PortableSessionLaunch(
                    session_id="unfinished-summary",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
            )
            catalog.publish_workflow(
                "unfinished-summary",
                prd_path=prd,
                issues_index_path=issues,
                activity_summary="handoff quit",
            )
            catalog.update_session_status(
                "unfinished-summary",
                PortableSessionStatus.COMPLETED,
            )
            candidate = SimpleNamespace(
                candidate_id="candidate-id",
                checkout=checkout.resolve(),
                prd_path=prd.resolve(),
            )

            supervisor = PortableSessionSupervisor(
                worker_launcher=lambda _launch: self.fail("must remain passive"),
                catalog=PortableSessionCatalog(catalog.path),
                resume_candidates=(candidate,),
            )

            self.assertEqual(
                supervisor.list_sessions()[0].status,
                PortableSessionStatus.READY,
            )
            self.assertEqual(
                catalog.get_session("unfinished-summary").status,
                PortableSessionStatus.READY,
            )

    def test_authoritative_absence_retires_stale_published_session(self) -> None:
        for stale_status in (
            PortableSessionStatus.READY,
            PortableSessionStatus.FAILED,
        ):
            with self.subTest(stale_status=stale_status):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    checkout = root / "checkout"
                    checkout.mkdir()
                    prd = checkout / "change.md"
                    issues = checkout / "README.md"
                    prd.write_text("# Change\n", encoding="utf-8")
                    issues.write_text("# Issues\n", encoding="utf-8")
                    catalog = PortableSessionCatalog(root / "catalog.sqlite3")
                    catalog.create_session(
                        PortableSessionLaunch(
                            session_id="stale-published",
                            checkout=checkout,
                            operation=PortableWorkflowOperation.PLANNING,
                            arguments=(),
                        )
                    )
                    catalog.publish_workflow(
                        "stale-published",
                        prd_path=prd,
                        issues_index_path=issues,
                        activity_summary="previous discovery",
                    )
                    catalog.update_session_status(
                        "stale-published",
                        stale_status,
                    )

                    supervisor = PortableSessionSupervisor(
                        worker_launcher=lambda _launch: self.fail(
                            "must remain passive"
                        ),
                        catalog=PortableSessionCatalog(catalog.path),
                        resume_candidates=(),
                        resume_candidates_loader=lambda: (),
                    )

                    self.assertEqual(
                        supervisor.list_sessions()[0].status,
                        PortableSessionStatus.COMPLETED,
                    )
                    self.assertEqual(
                        catalog.get_session("stale-published").status,
                        PortableSessionStatus.COMPLETED,
                    )

    def test_authoritative_reconciliation_preserves_pre_prd_planning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            catalog.create_session(
                PortableSessionLaunch(
                    session_id="pre-prd-planning",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
            )

            supervisor = PortableSessionSupervisor(
                worker_launcher=lambda _launch: self.fail("must remain passive"),
                catalog=PortableSessionCatalog(catalog.path),
                resume_candidates=(),
                resume_candidates_loader=lambda: (),
            )

            self.assertEqual(
                supervisor.list_sessions()[0].status,
                PortableSessionStatus.READY,
            )
            self.assertIsNone(
                catalog.get_session("pre-prd-planning").prd_path,
            )

    def test_zero_exit_before_prd_keeps_planning_session_resumable(self) -> None:
        class FakeWorker:
            def __init__(self) -> None:
                self.stdin = _WritableLines()
                self.stdout = _QueueReadableLines()
                self.stderr = _QueueReadableLines()
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

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            launch = PortableSessionLaunch(
                session_id="planning-abort",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            catalog.create_session(launch)
            worker = FakeWorker()
            supervisor = PortableSessionSupervisor(
                worker_launcher=lambda _launch: worker,
                catalog=catalog,
            )
            supervisor.resume_session(launch.session_id)
            worker.stdout.put(
                PortableProtocolFrame(
                    version=PORTABLE_PROTOCOL_VERSION,
                    session_id=launch.session_id,
                    sequence=1,
                    kind=WorkerMessageKind.HELLO.value,
                    payload={},
                ).to_json_line()
                + "\n"
            )
            worker.stdout.put(
                PortableProtocolFrame(
                    version=PORTABLE_PROTOCOL_VERSION,
                    session_id=launch.session_id,
                    sequence=2,
                    kind=WorkerMessageKind.COMPLETION.value,
                    payload={"exit_code": 0},
                ).to_json_line()
                + "\n"
            )
            deadline = time.monotonic() + 1
            while (
                supervisor.snapshot(launch.session_id).status
                is PortableSessionStatus.RUNNING
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            snapshot = supervisor.snapshot(launch.session_id)
            worker.stdout.close()
            worker.stderr.close()
            supervisor.shutdown()
            catalog_status = catalog.get_session(launch.session_id).status

        self.assertEqual(snapshot.status, PortableSessionStatus.READY)
        self.assertEqual(catalog_status, PortableSessionStatus.READY)


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

    def __iter__(self):
        return self

    def __next__(self) -> str:
        self._closed.wait(1)
        raise StopIteration

    def close(self) -> None:
        self._closed.set()


class _QueueReadableLines:
    _END = object()

    def __init__(self) -> None:
        self._lines: Queue[object] = Queue()

    def put(self, value: str) -> None:
        self._lines.put(value)

    def __iter__(self):
        return self

    def __next__(self) -> str:
        value = self._lines.get(timeout=1)
        if value is self._END:
            raise StopIteration
        assert isinstance(value, str)
        return value

    def close(self) -> None:
        self._lines.put(self._END)


if __name__ == "__main__":
    unittest.main()
