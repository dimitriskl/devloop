from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from devloop import cli, interactive_runner, portable_sessions
from devloop.portable_session_catalog import (
    PortableSessionCatalog,
    portable_session_catalog_path,
)
from devloop.portable_sessions import (
    PortableSessionLaunch,
    PortableSessionSupervisor,
    PortableSessionStatus,
    PortableWorkflowOperation,
    PortableWorktreeLeaseConflict,
    run_portable_plain_session,
)
from devloop.worktree import resolve_worktree


class PortablePlainModeContractTests(unittest.TestCase):
    def test_delivery_application_creates_relative_worktree_after_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "worktrees" / "feature"
            source.mkdir()
            self._initialize_repository_with_commit(source)
            prd, issues = self._write_delivery_package(source)
            catalog = PortableSessionCatalog(root / "sessions.sqlite3")
            observed_worker_directories: list[Path] = []
            sessions: list[str] = []

            def launch_process(
                command: object,
                *,
                cwd: Path,
                **options: object,
            ) -> subprocess.Popen[str]:
                del command, options
                session = catalog.list_sessions()[0]
                sessions.append(session.session_id)
                lease = catalog.get_worktree_lease(target)
                self.assertIsNotNone(lease)
                assert lease is not None
                self.assertEqual(lease.session_id, session.session_id)
                self.assertTrue(
                    catalog.owns_execution_capacity(
                        session.session_id,
                        owner_id="application-shell",
                    )
                )
                self.assertFalse(target.exists())
                observed_worker_directories.append(cwd.resolve())
                selection = resolve_worktree(
                    source_repo=source,
                    create_worktree=True,
                    no_worktree=False,
                    worktree_path=target,
                    branch_name="feature/application-contract",
                    interactive=False,
                    dry_run=False,
                )
                self.assertTrue(selection.created)
                return self._completion_worker(cwd, session.session_id)

            def run_application(launch: PortableSessionLaunch) -> int:
                supervisor = PortableSessionSupervisor(
                    catalog=catalog,
                    owner_id="application-shell",
                )
                try:
                    started = supervisor.start_session(launch)
                    try:
                        terminal = supervisor.wait_for_terminal(
                            started.session_id,
                            timeout=10,
                        )
                    except TimeoutError as error:
                        self.fail(f"{error}: {supervisor.snapshot(started.session_id)!r}")
                    return terminal.result or 0
                finally:
                    supervisor.shutdown()

            original_cwd = Path.cwd()
            try:
                os.chdir(root)
                with (
                    mock.patch.dict(os.environ, {"DEVLOOP_UI_MODE": "application"}),
                    mock.patch(
                        "devloop.portable_ui.app.run_portable_sessions_application",
                        side_effect=run_application,
                    ),
                    mock.patch.object(
                        portable_sessions,
                        "launch_process_tree",
                        side_effect=launch_process,
                    ),
                ):
                    result = cli.main(
                        [
                            "--prd",
                            str(prd.relative_to(root)),
                            "--issues",
                            str(issues.relative_to(root)),
                            "--create-worktree",
                            "--worktree-path",
                            str(target.relative_to(root)),
                            "--branch-name",
                            "feature/application-contract",
                            "--non-interactive",
                        ]
                    )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(result, 0)
            self.assertEqual(observed_worker_directories, [root.resolve()])
            self.assertTrue((target / ".git").is_file())
            self.assertEqual(len(sessions), 1)
            record = catalog.get_session(sessions[0])
            self.assertIs(record.status, PortableSessionStatus.COMPLETED)
            self.assertIsNone(catalog.get_worktree_lease(target))
            self.assertFalse(
                catalog.owns_execution_capacity(
                    sessions[0],
                    owner_id="application-shell",
                )
            )

    def test_planning_application_preserves_relative_entry_argument_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "selected"
            selected.mkdir()
            self._initialize_repository_with_commit(selected)
            catalog = PortableSessionCatalog(root / "sessions.sqlite3")
            observed_worker_directories: list[Path] = []

            def launch_process(
                command: object,
                *,
                cwd: Path,
                **options: object,
            ) -> subprocess.Popen[str]:
                del command, options
                observed_worker_directories.append(cwd.resolve())
                session_id = catalog.list_sessions()[0].session_id
                return self._completion_worker(cwd, session_id)

            def run_application(launch: PortableSessionLaunch) -> int:
                supervisor = PortableSessionSupervisor(
                    catalog=catalog,
                    owner_id="planning-shell",
                )
                try:
                    supervisor.start_session(launch)
                    deadline = time.monotonic() + 5
                    while (
                        not observed_worker_directories
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    self.assertTrue(observed_worker_directories)
                    return 0
                finally:
                    supervisor.shutdown()

            original_cwd = Path.cwd()
            try:
                os.chdir(root)
                with (
                    mock.patch.dict(os.environ, {"DEVLOOP_UI_MODE": "application"}),
                    mock.patch(
                        "devloop.portable_ui.app.run_portable_sessions_application",
                        side_effect=run_application,
                    ),
                    mock.patch.object(
                        portable_sessions,
                        "launch_process_tree",
                        side_effect=launch_process,
                    ),
                ):
                    result = interactive_runner.main(["--repo", "selected"])
            finally:
                os.chdir(original_cwd)

        self.assertEqual(result, 0)
        self.assertEqual(observed_worker_directories, [root.resolve()])

    def test_catalog_restart_preserves_bootstrap_before_and_after_worktree_creation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            self._initialize_repository_with_commit(source)
            prd, issues = self._write_delivery_package(source)

            for create_target in (False, True):
                with self.subTest(create_target=create_target):
                    target = root / f"target-{create_target}"
                    catalog = PortableSessionCatalog(root / f"catalog-{create_target}.sqlite3")
                    launch = PortableSessionLaunch(
                        session_id=f"restart-{create_target}",
                        checkout=target,
                        operation=PortableWorkflowOperation.DELIVERY,
                        arguments=(
                            "--prd",
                            str(prd.relative_to(root)),
                            "--issues",
                            str(issues.relative_to(root)),
                            "--create-worktree",
                            f"--worktree-path={target.relative_to(root)}",
                            "--branch-name",
                            f"feature/restart-{create_target}",
                            "--non-interactive",
                        ),
                        argument_base=root,
                    )
                    catalog.create_session_with_lease(
                        launch,
                        owner_id="crashed-shell",
                    )
                    catalog.release_worktree_lease(
                        launch.session_id,
                        owner_id="crashed-shell",
                    )
                    if create_target:
                        resolve_worktree(
                            source_repo=source,
                            create_worktree=True,
                            no_worktree=False,
                            worktree_path=target,
                            branch_name=f"feature/restart-{create_target}",
                            interactive=False,
                            dry_run=False,
                        )

                    restored = PortableSessionCatalog(catalog.path).get_session(
                        launch.session_id
                    ).launch

                    self.assertEqual(restored.argument_base, root.resolve())
                    self.assertIn(str(prd.resolve()), restored.arguments)
                    self.assertIn(str(issues.resolve()), restored.arguments)
                    self.assertIn(str(target.resolve()), restored.arguments)

                    observed_worker_directories: list[Path] = []

                    def launch_process(
                        command: object,
                        *,
                        cwd: Path,
                        observed: list[Path] = observed_worker_directories,
                        session_id: str = launch.session_id,
                        **options: object,
                    ) -> subprocess.Popen[str]:
                        del command, options
                        observed.append(cwd.resolve())
                        return self._completion_worker(cwd, session_id)

                    supervisor = PortableSessionSupervisor(
                        catalog=PortableSessionCatalog(catalog.path),
                        owner_id="restarted-shell",
                    )
                    try:
                        with mock.patch.object(
                            portable_sessions,
                            "launch_process_tree",
                            side_effect=launch_process,
                        ):
                            supervisor.resume_session(launch.session_id)
                            terminal = supervisor.wait_for_terminal(
                                launch.session_id,
                                timeout=10,
                            )
                    finally:
                        supervisor.shutdown()

                    self.assertIs(terminal.status, PortableSessionStatus.COMPLETED)
                    self.assertEqual(observed_worker_directories, [root.resolve()])
                    self.assertIsNone(catalog.get_worktree_lease(target))
                    self.assertFalse(
                        catalog.owns_execution_capacity(
                            launch.session_id,
                            owner_id="restarted-shell",
                        )
                    )

    def test_bound_created_worktree_reloads_and_resumes_from_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            self._initialize_repository_with_commit(source)
            prd, issues = self._write_delivery_package(source)
            subprocess.run(
                ["git", "-C", str(source), "add", "prd"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "commit", "--quiet", "-m", "package"],
                check=True,
                capture_output=True,
            )
            target = root / "target"
            catalog = PortableSessionCatalog(root / "sessions.sqlite3")
            launch = PortableSessionLaunch(
                session_id="bound-created-target",
                checkout=target,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=(
                    "--prd",
                    str(prd),
                    "--issues",
                    str(issues),
                    "--start-issue",
                    "0001",
                    "--no-blocked-retry",
                    "--create-worktree",
                    "--worktree-path",
                    str(target),
                    "--branch-name",
                    "feature/bound-created-target",
                    "--non-interactive",
                ),
                argument_base=root,
            )
            catalog.create_session_with_lease(launch, owner_id="creating-shell")
            selection = resolve_worktree(
                source_repo=source,
                create_worktree=True,
                no_worktree=False,
                worktree_path=target,
                branch_name="feature/bound-created-target",
                interactive=False,
                dry_run=False,
            )
            target_prd = target / prd.relative_to(source)
            target_issues = target / issues.relative_to(source)
            catalog.bind_session_checkout(
                launch.session_id,
                selection.repo_root,
                owner_id="creating-shell",
                prd_path=target_prd,
                issues_index_path=target_issues,
            )
            catalog.release_worktree_lease(
                launch.session_id,
                owner_id="creating-shell",
            )

            reloaded = PortableSessionCatalog(catalog.path)
            rebound = reloaded.get_session(launch.session_id)
            self.assertEqual(rebound.checkout, target.resolve())
            self.assertEqual(rebound.prd_path, target_prd.resolve())
            self.assertEqual(rebound.issues_index_path, target_issues.resolve())
            self.assertIn("--start-issue", rebound.arguments)
            self.assertIn("0001", rebound.arguments)
            self.assertIn("--no-blocked-retry", rebound.arguments)
            self.assertIn("--non-interactive", rebound.arguments)
            self.assertNotIn("--create-worktree", rebound.arguments)
            self.assertNotIn("--worktree-path", rebound.arguments)
            self.assertNotIn("--branch-name", rebound.arguments)

            observed_worker_directories: list[Path] = []
            observed_resources: list[tuple[str, bool]] = []

            def launch_process(
                command: object,
                *,
                cwd: Path,
                **options: object,
            ) -> subprocess.Popen[str]:
                del command, options
                observed_worker_directories.append(cwd.resolve())
                lease = reloaded.get_worktree_lease(target)
                observed_resources.append(
                    (
                        lease.owner_id if lease is not None else "",
                        reloaded.owns_execution_capacity(
                            launch.session_id,
                            owner_id="restarted-shell",
                        ),
                    )
                )
                return self._completion_worker(cwd, launch.session_id)

            supervisor = PortableSessionSupervisor(
                catalog=reloaded,
                owner_id="restarted-shell",
            )
            try:
                with mock.patch.object(
                    portable_sessions,
                    "launch_process_tree",
                    side_effect=launch_process,
                ):
                    resumed = supervisor.resume_session(launch.session_id)
                    self.assertIn(
                        resumed.status,
                        {PortableSessionStatus.QUEUED, PortableSessionStatus.RUNNING},
                    )
                    terminal = supervisor.wait_for_terminal(
                        launch.session_id,
                        timeout=10,
                    )
            finally:
                supervisor.shutdown()

            self.assertIs(terminal.status, PortableSessionStatus.COMPLETED)
            self.assertEqual(terminal.checkout, target.resolve())
            self.assertEqual(observed_worker_directories, [target.resolve()])
            self.assertEqual(observed_resources, [("restarted-shell", True)])
            self.assertIsNone(reloaded.get_worktree_lease(target))
            self.assertFalse(
                reloaded.owns_execution_capacity(
                    launch.session_id,
                    owner_id="restarted-shell",
                )
            )

    def test_catalog_restart_rejects_tampered_missing_argument_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            self._initialize_repository_with_commit(source)
            prd, issues = self._write_delivery_package(source)
            target = root / "target"
            catalog = PortableSessionCatalog(root / "sessions.sqlite3")
            launch = PortableSessionLaunch(
                "tampered-bootstrap",
                target,
                PortableWorkflowOperation.DELIVERY,
                (
                    "--prd",
                    str(prd),
                    "--issues",
                    str(issues),
                    "--create-worktree",
                    "--worktree-path",
                    str(target),
                    "--branch-name",
                    "feature/tampered-bootstrap",
                    "--non-interactive",
                ),
                argument_base=root,
            )
            catalog.create_session_with_lease(launch, owner_id="first-shell")
            catalog.release_worktree_lease(
                launch.session_id,
                owner_id="first-shell",
            )
            connection = sqlite3.connect(catalog.path)
            try:
                row = connection.execute(
                    "SELECT arguments_json FROM sessions WHERE session_id = ?",
                    (launch.session_id,),
                ).fetchone()
                settings = json.loads(row[0])
                settings["argument_base"] = str(root / "missing-bootstrap")
                connection.execute(
                    "UPDATE sessions SET arguments_json = ? WHERE session_id = ?",
                    (json.dumps(settings), launch.session_id),
                )
                connection.commit()
            finally:
                connection.close()

            supervisor = PortableSessionSupervisor(
                catalog=PortableSessionCatalog(catalog.path),
                owner_id="restarted-shell",
            )
            try:
                with self.assertRaisesRegex(ValueError, "argument base does not exist"):
                    supervisor.resume_session(launch.session_id)
            finally:
                supervisor.shutdown()

            self.assertFalse(target.exists())
            self.assertIsNone(catalog.get_worktree_lease(target))

    def test_create_worktree_dry_run_completes_without_target_or_live_ownership(
        self,
    ) -> None:
        for application_mode in (False, True):
            with self.subTest(application_mode=application_mode):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / "source"
                    target = root / "target"
                    source.mkdir()
                    self._initialize_repository_with_commit(source)
                    prd, issues = self._write_delivery_package(source)
                    catalog = PortableSessionCatalog(root / "sessions.sqlite3")
                    catalog.set_concurrency_limit(1)
                    output = StringIO()
                    terminal_snapshots = []

                    def run_application(
                        launch: PortableSessionLaunch,
                        *,
                        active_catalog: PortableSessionCatalog = catalog,
                        snapshots: list[object] = terminal_snapshots,
                    ) -> int:
                        supervisor = PortableSessionSupervisor(
                            catalog=active_catalog,
                            owner_id="dry-run-application",
                        )
                        try:
                            started = supervisor.start_session(launch)
                            terminal = supervisor.wait_for_terminal(
                                started.session_id,
                                timeout=20,
                            )
                            snapshots.append(terminal)
                            return terminal.result or 0
                        finally:
                            supervisor.shutdown()

                    environment = {
                        "DEVLOOP_UI_MODE": (
                            "application" if application_mode else "plain"
                        ),
                        "PYTHONPATH": os.pathsep.join(
                            (
                                str(Path(__file__).resolve().parents[1] / "src"),
                                os.environ.get("PYTHONPATH", ""),
                            )
                        ),
                    }
                    patches = [
                        mock.patch.dict(os.environ, environment),
                        mock.patch(
                            "devloop.portable_session_catalog.PortableSessionCatalog",
                            return_value=catalog,
                        ),
                    ]
                    if application_mode:
                        patches.append(
                            mock.patch(
                                "devloop.portable_ui.app.run_portable_sessions_application",
                                side_effect=run_application,
                            )
                        )
                    original_cwd = Path.cwd()
                    try:
                        os.chdir(root)
                        with ExitStack() as stack:
                            for patcher in patches:
                                stack.enter_context(patcher)
                            stack.enter_context(redirect_stdout(output))
                            result = cli.main(
                                [
                                    *( [] if application_mode else ["--plain"] ),
                                    "--prd",
                                    str(prd.relative_to(root)),
                                    "--issues",
                                    str(issues.relative_to(root)),
                                    "--create-worktree",
                                    "--worktree-path",
                                    str(target.relative_to(root)),
                                    "--branch-name",
                                    "feature/dry-run",
                                    "--dry-run",
                                    "--non-interactive",
                                    "--no-self-improvement-wiki",
                                ]
                            )
                    finally:
                        os.chdir(original_cwd)

                    self.assertEqual(result, 0)
                    dry_run_output = (
                        "\n".join(terminal_snapshots[0].activity)
                        if application_mode
                        else output.getvalue()
                    )
                    self.assertIn("[dry-run] Would run", dry_run_output)
                    self.assertFalse(target.exists())
                    records = catalog.list_sessions()
                    self.assertEqual(len(records), 1)
                    self.assertIs(records[0].status, PortableSessionStatus.COMPLETED)
                    self.assertIsNone(catalog.get_worktree_lease(records[0].checkout))
                    probe_checkout = root / "probe"
                    probe_checkout.mkdir()
                    probe = PortableSessionLaunch(
                        "capacity-probe",
                        probe_checkout,
                        PortableWorkflowOperation.PLANNING,
                        ("--repo", str(probe_checkout)),
                    )
                    catalog.create_session_with_lease(probe, owner_id="probe-owner")
                    self.assertTrue(
                        catalog.request_execution_capacity(
                            probe.session_id,
                            owner_id="probe-owner",
                        )
                    )

    def test_planning_plain_mode_leases_the_explicit_repository_before_work_starts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "target"
            repository.mkdir()
            subprocess.run(
                ["git", "init", "--quiet", str(repository)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "core.excludesFile", ""],
                check=True,
                capture_output=True,
            )
            captured_checkouts: list[Path] = []

            def run_plain(launch: object, operation: object) -> int:
                del operation
                captured_checkouts.append(getattr(launch, "checkout"))
                return 0

            with (
                mock.patch.dict(os.environ, {"DEVLOOP_UI_MODE": "plain"}),
                mock.patch.object(
                    interactive_runner,
                    "run_portable_plain_session",
                    side_effect=run_plain,
                ),
            ):
                result = interactive_runner.main(
                    ["--plain", "--repo", str(repository)]
                )

        self.assertEqual(result, 0)
        self.assertEqual(captured_checkouts, [repository.resolve()])

    def test_planning_plain_mode_reports_an_interactive_worktree_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            subprocess.run(
                ["git", "init", "--quiet", str(repository)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "core.excludesFile", ""],
                check=True,
                capture_output=True,
            )
            catalog = PortableSessionCatalog(repository / "sessions.sqlite3")
            interactive_launch = PortableSessionLaunch(
                session_id="interactive-session",
                checkout=repository,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(repository)),
            )
            catalog.create_session_with_lease(
                interactive_launch,
                owner_id="interactive-shell",
            )
            error = StringIO()

            with (
                mock.patch.dict(os.environ, {"DEVLOOP_UI_MODE": "plain"}),
                mock.patch(
                    "devloop.portable_session_catalog.PortableSessionCatalog",
                    return_value=catalog,
                ),
                redirect_stderr(error),
            ):
                result = interactive_runner.main(
                    ["--plain", "--repo", str(repository)]
                )

        self.assertEqual(result, 73)
        diagnostic = error.getvalue()
        self.assertIn("Portable Plain Mode could not start", diagnostic)
        self.assertIn("interactive-session", diagnostic)
        self.assertIn("interactive-shell", diagnostic)

    def test_planning_relative_repo_is_canonical_and_mismatched_prd_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "selected"
            other = root / "other"
            selected.mkdir()
            other.mkdir()
            self._initialize_repository_with_commit(selected)
            self._initialize_repository_with_commit(other)
            prd = other / "feature.md"
            prd.write_text("# Feature\n", encoding="utf-8")
            captured: list[Path] = []
            original_cwd = Path.cwd()
            try:
                os.chdir(root)
                with (
                    mock.patch.dict(os.environ, {"DEVLOOP_UI_MODE": "plain"}),
                    mock.patch.object(
                        interactive_runner,
                        "run_portable_plain_session",
                        side_effect=lambda launch, _operation: (
                            captured.append(launch.checkout) or 0
                        ),
                    ),
                ):
                    self.assertEqual(
                        interactive_runner.main(["--plain", "--repo", "selected"]),
                        0,
                    )
                error = StringIO()
                with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                    interactive_runner.main(
                        [
                            "--plain",
                            "--repo",
                            "selected",
                            "--prd",
                            str(prd),
                        ]
                    )
            finally:
                os.chdir(original_cwd)

        self.assertEqual(captured, [selected.resolve()])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn(
            "--repo and --prd resolve to different Git checkouts",
            error.getvalue(),
        )

    def test_delivery_plain_mode_leases_source_or_explicit_new_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "worktrees" / "feature"
            source.mkdir()
            self._initialize_repository_with_commit(source)
            prd, issues = self._write_delivery_package(source)
            captured: list[PortableSessionLaunch] = []

            def capture(launch: PortableSessionLaunch, _operation: object) -> int:
                captured.append(launch)
                return 0

            with (
                mock.patch.dict(os.environ, {"DEVLOOP_UI_MODE": "plain"}),
                mock.patch(
                    "devloop.portable_sessions.run_portable_plain_session",
                    side_effect=capture,
                ),
            ):
                source_result = cli.main(
                    [
                        "--plain",
                        "--prd",
                        str(prd),
                        "--issues",
                        str(issues),
                        "--no-worktree",
                        "--non-interactive",
                    ]
                )
                worktree_result = cli.main(
                    [
                        "--plain",
                        "--prd",
                        str(prd),
                        "--issues",
                        str(issues),
                        "--create-worktree",
                        "--worktree-path",
                        str(target),
                        "--branch-name",
                        "feature/plain-contract",
                        "--non-interactive",
                    ]
                )

        self.assertEqual((source_result, worktree_result), (0, 0))
        self.assertEqual(
            [launch.checkout for launch in captured],
            [source.resolve(), target.resolve()],
        )
        self.assertIn(str(prd.resolve()), captured[1].arguments)
        self.assertIn(str(issues.resolve()), captured[1].arguments)
        self.assertIn(str(target.resolve()), captured[1].arguments)

    def test_new_worktree_is_leased_and_has_capacity_before_git_creates_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "worktrees" / "feature"
            source.mkdir()
            self._initialize_repository_with_commit(source)
            catalog = PortableSessionCatalog(root / "sessions.sqlite3")
            launch = PortableSessionLaunch(
                session_id="plain-new-worktree",
                checkout=target,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=(
                    "--prd",
                    str(source / "feature.md"),
                    "--issues",
                    str(source / "issues" / "README.md"),
                    "--create-worktree",
                    "--worktree-path",
                    str(target),
                    "--branch-name",
                    "feature/plain-contract",
                    "--non-interactive",
                ),
                argument_base=root,
            )

            def create_worktree() -> int:
                lease = catalog.get_worktree_lease(target)
                self.assertIsNotNone(lease)
                self.assertEqual(lease.session_id, launch.session_id)
                self.assertTrue(
                    catalog.owns_execution_capacity(
                        launch.session_id,
                        owner_id="plain-process",
                    )
                )
                self.assertFalse(target.exists())
                selection = resolve_worktree(
                    source_repo=source,
                    create_worktree=True,
                    no_worktree=False,
                    worktree_path=target,
                    branch_name="feature/plain-contract",
                    interactive=False,
                    dry_run=False,
                )
                self.assertTrue(selection.created)
                self.assertEqual(selection.repo_root, target.resolve())
                return 0

            result = run_portable_plain_session(
                launch,
                create_worktree,
                catalog=catalog,
                owner_id="plain-process",
            )

            self.assertEqual(result, 0)
            self.assertTrue((target / ".git").is_file())
            self.assertIsNone(catalog.get_worktree_lease(target))
            self.assertFalse(
                catalog.owns_execution_capacity(
                    launch.session_id,
                    owner_id="plain-process",
                )
            )

    def test_new_worktree_preflight_rejects_unsafe_existing_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            self._initialize_repository_with_commit(source)
            prd, issues = self._write_delivery_package(source)
            nested = source / "nested-worktree"
            non_worktree = root / "occupied"
            non_worktree.mkdir()
            (non_worktree / "user-data.txt").write_text("keep\n", encoding="utf-8")
            other_repository = root / "other-repository"
            other_repository.mkdir()
            self._initialize_repository_with_commit(other_repository)
            cases = (
                (nested, "outside the source checkout"),
                (non_worktree, "not a Git worktree root or empty directory"),
                (other_repository, "belongs to a different Git repository"),
            )

            with mock.patch(
                "devloop.portable_sessions.run_portable_plain_session"
            ) as run_plain:
                for target, expected in cases:
                    error = StringIO()
                    with (
                        self.subTest(target=target),
                        redirect_stderr(error),
                        self.assertRaises(SystemExit) as raised,
                    ):
                            cli.main(
                                [
                                    "--plain",
                                    "--prd",
                                    str(prd),
                                    "--issues",
                                    str(issues),
                                    "--create-worktree",
                                    "--worktree-path",
                                    str(target),
                                    "--branch-name",
                                    "feature/invalid-target",
                                    "--non-interactive",
                                ]
                            )
                    self.assertEqual(raised.exception.code, 2)
                    run_plain.assert_not_called()
                    run_plain.reset_mock()
                    self.assertIn(expected, error.getvalue())

            self.assertEqual(
                (non_worktree / "user-data.txt").read_text(encoding="utf-8"),
                "keep\n",
            )

    def test_canonical_missing_target_conflict_creates_no_directory_or_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            self._initialize_repository_with_commit(source)
            target = root / "worktrees" / "reserved"
            catalog = PortableSessionCatalog(root / "sessions.sqlite3")
            arguments = (
                "--prd",
                str(source / "feature.md"),
                "--issues",
                str(source / "issues" / "README.md"),
                "--create-worktree",
                "--worktree-path",
                str(target),
                "--branch-name",
                "feature/reserved",
                "--non-interactive",
            )
            owner_launch = PortableSessionLaunch(
                "interactive-reservation",
                target,
                PortableWorkflowOperation.DELIVERY,
                arguments,
                argument_base=root,
            )
            catalog.create_session_with_lease(
                owner_launch,
                owner_id="interactive-shell",
            )
            alias = target.parent / ".." / target.parent.name / target.name
            plain_launch = PortableSessionLaunch(
                "plain-conflict",
                alias,
                PortableWorkflowOperation.DELIVERY,
                arguments,
                argument_base=root,
            )

            result = run_portable_plain_session(
                plain_launch,
                lambda: self.fail("conflicting operation started"),
                catalog=catalog,
                owner_id="plain-process",
            )
            self.assertEqual(result, 73)
            self.assertFalse(target.exists())
            self.assertEqual(
                tuple(record.session_id for record in catalog.list_sessions()),
                ("interactive-reservation",),
            )

    def test_real_cli_dry_run_is_deterministic_and_releases_shared_ownership(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "source"
            repository.mkdir()
            self._initialize_repository_with_commit(repository)
            prd, issues = self._write_delivery_package(repository)
            catalog = PortableSessionCatalog(root / "sessions.sqlite3")
            catalog.set_concurrency_limit(1)
            arguments = [
                "--plain",
                "--prd",
                str(prd),
                "--issues",
                str(issues),
                "--all",
                "--dry-run",
                "--no-worktree",
                "--non-interactive",
                "--no-self-improvement-wiki",
            ]

            outputs: list[str] = []
            results: list[int] = []
            with mock.patch(
                "devloop.portable_session_catalog.PortableSessionCatalog",
                return_value=catalog,
            ):
                for _ in range(2):
                    output = StringIO()
                    with redirect_stdout(output):
                        results.append(cli.main(arguments))
                    outputs.append(output.getvalue())

            sessions = catalog.list_sessions()
            self.assertEqual(results, [0, 0])
            self.assertEqual(outputs[0], outputs[1])
            self.assertNotIn("\x1b", outputs[0])
            self.assertIn("Selected issues: 0001", outputs[0])
            self.assertEqual(len(sessions), 2)
            self.assertTrue(
                all(
                    session.status is PortableSessionStatus.COMPLETED
                    for session in sessions
                )
            )
            self.assertIsNone(catalog.get_worktree_lease(repository))

            probe_checkout = root / "probe"
            probe_checkout.mkdir()
            probe = PortableSessionLaunch(
                "capacity-probe",
                probe_checkout,
                PortableWorkflowOperation.PLANNING,
                ("--repo", str(probe_checkout)),
            )
            catalog.create_session_with_lease(probe, owner_id="probe-owner")
            self.assertTrue(
                catalog.request_execution_capacity(
                    probe.session_id,
                    owner_id="probe-owner",
                )
            )

    def test_real_process_plain_and_interactive_entries_exclude_each_other(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "source"
            repository.mkdir()
            self._initialize_repository_with_commit(repository)
            catalog_path = root / "sessions.sqlite3"
            ready = root / "ready"
            release = root / "release"
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(
                Path(__file__).resolve().parents[1] / "src"
            )
            external_plain = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "from devloop.portable_session_catalog import PortableSessionCatalog\n"
                "from devloop.portable_sessions import PortableSessionLaunch\n"
                "from devloop.portable_sessions import PortableWorkflowOperation\n"
                "from devloop.portable_sessions import run_portable_plain_session\n"
                "catalog_path,checkout,ready,release=map(Path,sys.argv[1:])\n"
                "catalog=PortableSessionCatalog(catalog_path)\n"
                "launch=PortableSessionLaunch('external-plain',checkout,PortableWorkflowOperation.PLANNING,('--repo',str(checkout)))\n"
                "def operation():\n"
                "    ready.write_text('ready',encoding='utf-8')\n"
                "    while not release.exists(): time.sleep(0.01)\n"
                "    return 0\n"
                "result=run_portable_plain_session(launch,operation,catalog=catalog,"
                "owner_id='plain-process')\n"
                "raise SystemExit(result)\n"
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    external_plain,
                    str(catalog_path),
                    str(repository),
                    str(ready),
                    str(release),
                ],
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            observer: PortableSessionSupervisor | None = None
            try:
                self._wait_for_path(ready, process)
                catalog = PortableSessionCatalog(catalog_path)
                launched: list[PortableSessionLaunch] = []
                observer = PortableSessionSupervisor(
                    worker_launcher=lambda launch: launched.append(launch),  # type: ignore[arg-type]
                    catalog=catalog,
                    owner_id="interactive-observer",
                )
                snapshots = {
                    snapshot.session_id: snapshot
                    for snapshot in observer.list_sessions()
                }
                self.assertIs(
                    snapshots["external-plain"].status,
                    PortableSessionStatus.RUNNING,
                )
                with self.assertRaises(PortableWorktreeLeaseConflict) as raised:
                    observer.start_session(
                        PortableSessionLaunch(
                            "interactive-duplicate",
                            repository,
                            PortableWorkflowOperation.PLANNING,
                            ("--repo", str(repository)),
                        )
                    )
                self.assertEqual(raised.exception.session_id, "external-plain")
                self.assertEqual(raised.exception.owner_id, "plain-process")
                self.assertEqual(launched, [])
                self.assertEqual(
                    tuple(record.session_id for record in catalog.list_sessions()),
                    ("external-plain",),
                )
            finally:
                if observer is not None:
                    observer.shutdown()
                release.touch()
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual((process.returncode, stdout, stderr), (0, "", ""))

    def test_real_plain_cli_reports_a_supervisor_owner_without_starting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "source"
            repository.mkdir()
            self._initialize_repository_with_commit(repository)
            local_state = root / "local-state"
            environment = os.environ.copy()
            environment["LOCALAPPDATA"] = str(local_state)
            environment["XDG_STATE_HOME"] = str(local_state)
            environment["PYTHONPATH"] = str(
                Path(__file__).resolve().parents[1] / "src"
            )
            catalog_path = portable_session_catalog_path(
                environment=environment,
                platform=os.name,
                home=root,
            )
            catalog = PortableSessionCatalog(catalog_path)
            worker_ready = root / "worker-ready"
            worker_release = root / "worker-release"
            workers: list[subprocess.Popen[str]] = []

            def launch_worker(
                launch: PortableSessionLaunch,
            ) -> subprocess.Popen[str]:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        "import json,sys,time\n"
                        "from pathlib import Path\n"
                        "ready,release=map(Path,sys.argv[1:])\n"
                        "json.loads(sys.stdin.readline())\n"
                        "ready.write_text('ready',encoding='utf-8')\n"
                        "while not release.exists(): time.sleep(0.01)\n",
                        str(worker_ready),
                        str(worker_release),
                    ],
                    cwd=launch.checkout,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                workers.append(process)
                return process

            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=catalog,
                owner_id="interactive-shell",
            )
            try:
                active = supervisor.start_session(
                    PortableSessionLaunch(
                        "interactive-owner",
                        repository,
                        PortableWorkflowOperation.PLANNING,
                        ("--repo", str(repository)),
                    )
                )
                self.assertIs(active.status, PortableSessionStatus.RUNNING)
                self._wait_for_path(worker_ready, workers[0])

                result = subprocess.run(
                    [
                        sys.executable,
                        "-u",
                        "-m",
                        "devloop.interactive_runner",
                        "--plain",
                        "--repo",
                        str(repository),
                    ],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=10,
                )

                self.assertEqual(result.returncode, 73)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("\x1b", result.stderr)
                self.assertIn("Portable Plain Mode could not start", result.stderr)
                self.assertIn("interactive-owner", result.stderr)
                self.assertIn("interactive-shell", result.stderr)
                self.assertEqual(
                    tuple(record.session_id for record in catalog.list_sessions()),
                    ("interactive-owner",),
                )
                self.assertEqual(len(workers), 1)
            finally:
                worker_release.touch()
                supervisor.shutdown()

    @staticmethod
    def _completion_worker(
        cwd: Path,
        session_id: str,
    ) -> subprocess.Popen[str]:
        source = (
            "import json,sys,time\n"
            "json.loads(sys.stdin.readline())\n"
            f"sid={session_id!r}\n"
            "def send(sequence,kind,payload):\n"
            " print(json.dumps({'version':1,'session_id':sid,'sequence':sequence,"
            "'kind':kind,'payload':payload},separators=(',',':')),flush=True)\n"
            "send(1,'HELLO',{})\n"
            "send(2,'COMPLETION',{'exit_code':0})\n"
            "time.sleep(0.2)\n"
        )
        return subprocess.Popen(
            [sys.executable, "-u", "-c", source],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

    @staticmethod
    def _initialize_repository_with_commit(repository: Path) -> None:
        subprocess.run(
            ["git", "init", "--quiet", str(repository)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "core.excludesFile", ""],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.name", "Test User"],
            check=True,
            capture_output=True,
        )
        (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repository), "add", "tracked.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "commit", "--quiet", "-m", "initial"],
            check=True,
            capture_output=True,
        )

    @staticmethod
    def _write_delivery_package(repository: Path) -> tuple[Path, Path]:
        package = repository / "prd" / "feature"
        issues_directory = package / "issues"
        issues_directory.mkdir(parents=True)
        prd = package / "feature.md"
        issues = issues_directory / "README.md"
        issue = issues_directory / "0001-example.md"
        prd.write_text(
            "# Feature\n\n## Target Product\n\nProduct: devloop-plan + devloop\n",
            encoding="utf-8",
        )
        issues.write_text("- [Example](./0001-example.md)\n", encoding="utf-8")
        issue.write_text(
            "# Example\n\n"
            "## Target Product\n\n"
            "Product: devloop-plan + devloop\n\n"
            "## Blocked by\n\nNone.\n\n"
            "Completed: [ ]\n",
            encoding="utf-8",
        )
        return prd, issues

    @staticmethod
    def _wait_for_path(path: Path, process: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if path.exists():
                return
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(
                    "External Plain Mode exited before becoming active: "
                    f"exit={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
                )
            time.sleep(0.01)
        raise AssertionError("External Plain Mode did not become active.")


if __name__ == "__main__":
    unittest.main()
