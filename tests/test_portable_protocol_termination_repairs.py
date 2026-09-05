from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from devloop.portable_session_catalog import PortableSessionCatalog
from devloop.portable_sessions import (
    PortableSessionLaunch,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
)
from devloop.subprocess_utils import ProcessIdentity, ProcessTreeState, process_identity_state


class PortableBinaryTerminationRepairTests(unittest.TestCase):
    def test_force_stop_reaps_the_real_default_binary_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = self._git_checkout(root)
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            supervisor = PortableSessionSupervisor(
                catalog=catalog,
                owner_id="binary-force-stop-shell",
            )
            launch = self._planning_launch("binary-force-stop", checkout)
            try:
                supervisor.start_session(launch)
                self._wait_for_status(
                    supervisor,
                    launch.session_id,
                    PortableSessionStatus.WAITING_FOR_INPUT,
                )
                worker_identity = self._worker_identity(catalog, checkout)

                stopped = supervisor.force_stop_session(launch.session_id)

                self.assertEqual(stopped.status, PortableSessionStatus.INTERRUPTED)
                self.assertFalse(
                    catalog.owns_execution_capacity(
                        launch.session_id,
                        owner_id="binary-force-stop-shell",
                    )
                )
                self.assertIsNone(catalog.get_worktree_lease(checkout))
                self.assertTrue(self._wait_for_process_stopped(worker_identity))
            finally:
                supervisor.shutdown()

    def test_cancel_reaps_the_real_default_binary_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = self._git_checkout(root)
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            owner_id = "binary-cancel-shell"
            supervisor = PortableSessionSupervisor(
                catalog=catalog,
                owner_id=owner_id,
            )
            launch = self._planning_launch("binary-cancel", checkout)
            try:
                supervisor.start_session(launch)
                self._wait_for_status(
                    supervisor,
                    launch.session_id,
                    PortableSessionStatus.WAITING_FOR_INPUT,
                )
                worker_identity = self._worker_identity(catalog, checkout)

                stopped = supervisor.cancel_session(launch.session_id)

                self.assertEqual(stopped.status, PortableSessionStatus.CANCELLED)
                self.assertFalse(
                    catalog.owns_execution_capacity(
                        launch.session_id,
                        owner_id=owner_id,
                    )
                )
                self.assertIsNone(catalog.get_worktree_lease(checkout))
                self.assertTrue(self._wait_for_process_stopped(worker_identity))
            finally:
                supervisor.shutdown()

    def test_application_shutdown_reaps_the_real_default_binary_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = self._git_checkout(root)
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            owner_id = "binary-shutdown-shell"
            supervisor = PortableSessionSupervisor(
                catalog=catalog,
                owner_id=owner_id,
            )
            launch = self._planning_launch("binary-shutdown", checkout)
            try:
                supervisor.start_session(launch)
                self._wait_for_status(
                    supervisor,
                    launch.session_id,
                    PortableSessionStatus.WAITING_FOR_INPUT,
                )
                worker_identity = self._worker_identity(catalog, checkout)

                with mock.patch.object(
                    supervisor,
                    "pause_session",
                    return_value=supervisor.snapshot(launch.session_id),
                ):
                    supervisor.shutdown()

                stopped = supervisor.snapshot(launch.session_id)
                self.assertEqual(stopped.status, PortableSessionStatus.INTERRUPTED)
                self.assertFalse(
                    catalog.owns_execution_capacity(
                        launch.session_id,
                        owner_id=owner_id,
                    )
                )
                self.assertIsNone(catalog.get_worktree_lease(checkout))
                self.assertTrue(self._wait_for_process_stopped(worker_identity))
            finally:
                supervisor.shutdown()

    @staticmethod
    def _git_checkout(root: Path) -> Path:
        checkout = root / "checkout"
        checkout.mkdir()
        subprocess.run(
            ["git", "init"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        return checkout.resolve()

    @staticmethod
    def _planning_launch(session_id: str, checkout: Path) -> PortableSessionLaunch:
        return PortableSessionLaunch(
            session_id=session_id,
            checkout=checkout,
            operation=PortableWorkflowOperation.PLANNING,
            arguments=("--repo", str(checkout)),
            argument_base=checkout,
        )

    @staticmethod
    def _worker_identity(catalog: PortableSessionCatalog, checkout: Path) -> ProcessIdentity:
        lease = catalog.get_worktree_lease(checkout)
        assert lease is not None
        identity = lease.worker_process_identity
        assert identity is not None
        return identity

    @staticmethod
    def _wait_for_status(
        supervisor: PortableSessionSupervisor,
        session_id: str,
        expected: PortableSessionStatus,
    ):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            snapshot = supervisor.snapshot(session_id)
            if snapshot.status is expected:
                return snapshot
            time.sleep(0.01)
        raise AssertionError(supervisor.snapshot(session_id))

    @staticmethod
    def _wait_for_process_stopped(identity: ProcessIdentity) -> bool:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process_identity_state(identity) is ProcessTreeState.STOPPED:
                return True
            time.sleep(0.01)
        return process_identity_state(identity) is ProcessTreeState.STOPPED


if __name__ == "__main__":
    unittest.main()
