from __future__ import annotations

import multiprocessing
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from devloop.portable_session_catalog import PortableSessionCatalog
from devloop.portable_session_targets import (
    ExistingCheckoutTarget,
    NewWorktreeTarget,
    PortableSessionTargetResolver,
    SavedWorktreeTarget,
)
from devloop.portable_sessions import (
    PortableSessionLaunch,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkerProcess,
    PortableWorktreeLeaseConflict,
    PortableWorkflowOperation,
)


def _initialize_repository(repository: Path) -> None:
    repository.mkdir()
    subprocess.run(
        ["git", "init", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    (repository / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repository,
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
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


def _hold_worktree_lease(
    database: str,
    checkout: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    catalog = PortableSessionCatalog(Path(database))
    catalog.create_session_with_lease(
        PortableSessionLaunch(
            session_id="session-process-one",
            checkout=Path(checkout),
            operation=PortableWorkflowOperation.PLANNING,
            arguments=("--repo", checkout),
        ),
        owner_id="process-one",
    )
    ready.set()
    release.wait(timeout=10)


class PortableSessionWorktreeTests(unittest.TestCase):
    def test_existing_checkout_selection_resolves_to_canonical_git_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            nested = repository / "src" / "feature"
            nested.mkdir(parents=True)
            subprocess.run(
                ["git", "init", str(repository)],
                check=True,
                capture_output=True,
                text=True,
            )

            target = PortableSessionTargetResolver().resolve(
                ExistingCheckoutTarget(nested)
            )

        self.assertEqual(target.checkout, repository.resolve())
        self.assertFalse(target.created)

    def test_new_worktree_selection_creates_then_reuses_exact_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            worktree = root / "feature-worktree"
            _initialize_repository(repository)
            resolver = PortableSessionTargetResolver()
            request = NewWorktreeTarget(
                repository=repository,
                checkout=worktree,
                branch="feature/session-worktree",
            )

            created = resolver.resolve(request)
            output = StringIO()
            with redirect_stdout(output):
                reused = resolver.resolve(request)

        self.assertEqual(created.checkout, worktree.resolve())
        self.assertTrue(created.created)
        self.assertEqual(reused.checkout, worktree.resolve())
        self.assertFalse(reused.created)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(
            reused.notices,
            (f"Using existing worktree: {worktree.resolve()}",),
        )

    def test_distinct_worktrees_of_one_repository_have_independent_leases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            worktree = root / "feature-worktree"
            _initialize_repository(repository)
            resolver = PortableSessionTargetResolver()
            first_target = resolver.resolve(SavedWorktreeTarget(repository))
            second_target = resolver.resolve(
                NewWorktreeTarget(
                    repository=repository,
                    checkout=worktree,
                    branch="feature/concurrent-session",
                )
            )
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            first = PortableSessionLaunch(
                session_id="session-main-worktree",
                checkout=first_target.checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(first_target.checkout)),
            )
            second = PortableSessionLaunch(
                session_id="session-feature-worktree",
                checkout=second_target.checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(second_target.checkout)),
            )

            catalog.create_session_with_lease(first, owner_id="shell-first")
            catalog.create_session_with_lease(second, owner_id="shell-second")
            projects = catalog.list_saved_projects()
            first_lease = catalog.get_worktree_lease(first_target.checkout)
            second_lease = catalog.get_worktree_lease(second_target.checkout)

        self.assertEqual(
            {project.checkout for project in projects},
            {repository.resolve(), worktree.resolve()},
        )
        assert first_lease is not None
        assert second_lease is not None
        self.assertEqual(first_lease.session_id, first.session_id)
        self.assertEqual(second_lease.session_id, second.session_id)

    def test_separate_process_observes_machine_wide_worktree_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "portable-sessions.sqlite3"
            PortableSessionCatalog(database)
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            process = context.Process(
                target=_hold_worktree_lease,
                args=(str(database), str(checkout), ready, release),
            )
            process.start()
            self.assertTrue(ready.wait(timeout=10))
            catalog = PortableSessionCatalog(database)

            try:
                with self.assertRaises(PortableWorktreeLeaseConflict) as raised:
                    catalog.create_session_with_lease(
                        PortableSessionLaunch(
                            session_id="session-process-two",
                            checkout=checkout,
                            operation=PortableWorkflowOperation.PLANNING,
                            arguments=("--repo", str(checkout)),
                        ),
                        owner_id="process-two",
                    )
            finally:
                release.set()
                process.join(timeout=10)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)

            sessions = catalog.list_sessions()

        self.assertEqual(process.exitcode, 0)
        self.assertEqual(raised.exception.owner_id, "process-one")
        self.assertEqual(
            [session.session_id for session in sessions],
            ["session-process-one"],
        )

    def test_created_implementation_worktree_is_registered_and_released_from_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            worktree = root / "implementation-worktree"
            _initialize_repository(repository)
            target = PortableSessionTargetResolver().resolve(
                NewWorktreeTarget(
                    repository=repository,
                    checkout=worktree,
                    branch="feature/implementation",
                )
            )
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                session_id="session-transferred",
                checkout=repository,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(repository)),
            )
            catalog.create_session_with_lease(launch, owner_id="shell-transfer")

            catalog.bind_session_checkout(
                launch.session_id,
                target.checkout,
                owner_id="shell-transfer",
            )
            session = catalog.get_session(launch.session_id)
            source_lease = catalog.get_worktree_lease(repository)
            implementation_lease = catalog.get_worktree_lease(worktree)
            projects = catalog.list_saved_projects()

        self.assertEqual(session.checkout, worktree.resolve())
        self.assertIsNone(source_lease)
        assert implementation_lease is not None
        self.assertEqual(implementation_lease.session_id, launch.session_id)
        self.assertEqual(
            {project.checkout for project in projects},
            {repository.resolve(), worktree.resolve()},
        )

    def test_supervisor_claims_checkout_before_worker_launch(self) -> None:
        class LaunchObserved(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                session_id="session-owned",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(checkout)),
            )

            def inspect_claim_before_launch(
                selected: PortableSessionLaunch,
            ) -> object:
                lease = catalog.get_worktree_lease(selected.checkout)
                self.assertIsNotNone(lease)
                assert lease is not None
                self.assertEqual(lease.session_id, selected.session_id)
                self.assertEqual(lease.owner_id, "shell-a")
                raise LaunchObserved

            supervisor = PortableSessionSupervisor(
                worker_launcher=inspect_claim_before_launch,
                catalog=catalog,
                owner_id="shell-a",
            )

            with self.assertRaises(LaunchObserved):
                supervisor.start_session(launch)

            sessions = catalog.list_sessions()
            projects = catalog.list_saved_projects()
            remaining_lease = catalog.get_worktree_lease(checkout)

        self.assertEqual(sessions, ())
        self.assertEqual([project.checkout for project in projects], [checkout.resolve()])
        self.assertIsNone(remaining_lease)

    def test_external_owner_blocks_competing_worker_and_session_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            first = PortableSessionLaunch(
                session_id="session-first",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(checkout)),
            )
            second = PortableSessionLaunch(
                session_id="session-second",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(checkout)),
            )
            catalog.create_session_with_lease(first, owner_id="shell-first")
            launched: list[PortableSessionLaunch] = []

            def unexpected_worker(
                selected: PortableSessionLaunch,
            ) -> PortableWorkerProcess:
                launched.append(selected)
                raise AssertionError("External lease conflict must prevent launch")

            supervisor = PortableSessionSupervisor(
                worker_launcher=unexpected_worker,
                catalog=PortableSessionCatalog(catalog.path),
                owner_id="shell-second",
            )

            with self.assertRaises(PortableWorktreeLeaseConflict) as raised:
                supervisor.start_session(second)

            sessions = catalog.list_sessions()

        self.assertEqual(raised.exception.session_id, first.session_id)
        self.assertEqual(raised.exception.owner_id, "shell-first")
        self.assertEqual(launched, [])
        self.assertEqual(
            [session.session_id for session in sessions],
            [first.session_id],
        )

    def test_current_owner_focuses_existing_session_for_leased_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            existing = PortableSessionLaunch(
                session_id="session-existing",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(checkout)),
            )
            catalog.create_session_with_lease(existing, owner_id="shell-local")
            launched: list[PortableSessionLaunch] = []

            def unexpected_worker(
                selected: PortableSessionLaunch,
            ) -> PortableWorkerProcess:
                launched.append(selected)
                raise AssertionError("Local lease focus must prevent launch")

            supervisor = PortableSessionSupervisor(
                worker_launcher=unexpected_worker,
                catalog=PortableSessionCatalog(catalog.path),
                owner_id="shell-local",
            )
            requested = PortableSessionLaunch(
                session_id="session-requested",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(checkout)),
            )

            focused = supervisor.start_session(requested)
            sessions = catalog.list_sessions()

        self.assertEqual(focused.session_id, existing.session_id)
        self.assertEqual(launched, [])
        self.assertEqual(
            [session.session_id for session in sessions],
            [existing.session_id],
        )


if __name__ == "__main__":
    unittest.main()
