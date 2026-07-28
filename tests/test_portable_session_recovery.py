from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from devloop.portable_session_catalog import PortableSessionCatalog
from devloop.portable_sessions import (
    PortableSessionLaunch,
    PortableSessionSnapshot,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorktreeLeaseConflict,
    PortableWorkflowOperation,
)
from devloop.portable_worker import (
    PortableWorkerHeartbeatEmitter,
    PortableWorkerRuntimeBridge,
)
from devloop.subprocess_utils import (
    ProcessIdentity,
    ProcessTreeState,
    capture_process_identity,
)


class PortableSessionRecoveryTests(unittest.TestCase):
    def test_exact_worker_heartbeat_renews_only_its_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                session_id="heartbeat-session",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            catalog.create_session_with_lease(
                launch,
                owner_id="heartbeat-owner",
                process_identity=ProcessIdentity(pid=4101, creation_time=5101),
                worker_generation=7,
            )
            before = catalog.get_session(launch.session_id)
            before_lease = catalog.get_worktree_lease(checkout)
            assert before_lease is not None

            renewed = catalog.renew_worktree_lease(
                launch.session_id,
                owner_id="heartbeat-owner",
                process_identity=ProcessIdentity(pid=4101, creation_time=5101),
                worker_generation=7,
                heartbeat_at=before_lease.heartbeat_at + 10,
            )
            after = catalog.get_session(launch.session_id)

        self.assertEqual(after.revision, before.revision)
        self.assertEqual(after.status, before.status)
        self.assertEqual(renewed.process_id, 4101)
        self.assertEqual(renewed.process_start_fingerprint, 5101)
        self.assertEqual(renewed.worker_generation, 7)
        self.assertEqual(renewed.heartbeat_at, before_lease.heartbeat_at + 10)

    def test_expired_lease_is_reclaimed_only_after_exact_owner_is_dead(self) -> None:
        now = [100.0]
        probed: list[ProcessIdentity] = []

        def probe(identity: ProcessIdentity) -> ProcessTreeState:
            probed.append(identity)
            return ProcessTreeState.STOPPED

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(
                root / "portable-sessions.sqlite3",
                clock=lambda: now[0],
                process_probe=probe,
                lease_timeout_seconds=30.0,
            )
            first = PortableSessionLaunch(
                session_id="dead-owner",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            replacement = PortableSessionLaunch(
                session_id="replacement-owner",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            catalog.create_session_with_lease(
                first,
                owner_id="old-shell",
                process_identity=ProcessIdentity(pid=4201, creation_time=5201),
                worker_generation=1,
            )
            now[0] = 131.0

            catalog.create_session_with_lease(
                replacement,
                owner_id="new-shell",
                process_identity=ProcessIdentity(pid=4202, creation_time=5202),
                worker_generation=1,
            )

            old_session = catalog.get_session(first.session_id)
            current = catalog.get_worktree_lease(checkout)
            assert current is not None

        self.assertEqual(
            probed,
            [ProcessIdentity(pid=4201, creation_time=5201)],
        )
        self.assertEqual(current.session_id, replacement.session_id)
        self.assertEqual(old_session.status.value, "INTERRUPTED")

    def test_ambiguous_expired_lease_remains_inspectable_and_blocks_recovery(
        self,
    ) -> None:
        now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(
                root / "portable-sessions.sqlite3",
                clock=lambda: now[0],
                process_probe=lambda _identity: ProcessTreeState.UNKNOWN,
                lease_timeout_seconds=30.0,
            )
            first = PortableSessionLaunch(
                session_id="ambiguous-owner",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            replacement = PortableSessionLaunch(
                session_id="blocked-replacement",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            original = catalog.create_session_with_lease(
                first,
                owner_id="old-shell",
                process_identity=ProcessIdentity(pid=4301, creation_time=5301),
                worker_generation=1,
            )
            catalog.create_session(replacement)
            now[0] = 131.0

            with self.assertRaises(PortableWorktreeLeaseConflict) as raised:
                catalog.acquire_session_lease(
                    replacement.session_id,
                    owner_id="new-shell",
                    process_identity=ProcessIdentity(
                        pid=4302,
                        creation_time=5302,
                    ),
                    worker_generation=1,
                )

            retained = catalog.get_worktree_lease(checkout)

        self.assertEqual(original.session_id, first.session_id)
        self.assertEqual(raised.exception.lease, retained)
        self.assertEqual(retained.session_id, first.session_id)
        self.assertNotIn("force", str(raised.exception).lower())

    def test_supervisor_accepts_only_exact_worker_heartbeat_generation(self) -> None:
        worker_source = (
            "import json,os,sys,time\n"
            "from pathlib import Path\n"
            "session_id=sys.argv[1]\n"
            "start=json.loads(sys.stdin.readline())\n"
            "payload=start['payload']\n"
            "def send(seq,kind,data):\n"
            " print(json.dumps({'version':1,'session_id':session_id,"
            "'sequence':seq,'kind':kind,'payload':data}),flush=True)\n"
            "send(1,'HELLO',{})\n"
            "Path('ready.json').write_text(json.dumps(start),encoding='utf-8')\n"
            "while not Path('emit').exists(): time.sleep(0.01)\n"
            "send(2,'HEARTBEAT',{'owner_id':payload['owner_id'],"
            "'worker_generation':payload['worker_generation']})\n"
            "Path('sent').touch()\n"
            "while not Path('finish').exists(): time.sleep(0.01)\n"
            "send(3,'COMPLETION',{'exit_code':0})\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            prd = checkout / "change.md"
            issues = checkout / "README.md"
            prd.write_text("# Change\n", encoding="utf-8")
            issues.write_text("# Issues\n", encoding="utf-8")

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
                    ],
                    cwd=launch.checkout,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )

            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=catalog,
                owner_id="heartbeat-shell",
            )
            launch = PortableSessionLaunch(
                session_id="heartbeat-worker",
                checkout=checkout,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=("--prd", str(prd), "--issues", str(issues)),
            )
            try:
                supervisor.start_session(launch)
                self._wait_for_path(checkout / "ready.json")
                before = catalog.get_session(launch.session_id)
                before_lease = catalog.get_worktree_lease(checkout)
                assert before_lease is not None
                (checkout / "emit").touch()
                self._wait_for_path(checkout / "sent")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    renewed = catalog.get_worktree_lease(checkout)
                    assert renewed is not None
                    if renewed.heartbeat_at > before_lease.heartbeat_at:
                        break
                    time.sleep(0.01)
                else:
                    self.fail("Worker heartbeat did not renew its lease.")
                after = catalog.get_session(launch.session_id)
                start = json.loads(
                    (checkout / "ready.json").read_text(encoding="utf-8")
                )
                (checkout / "finish").touch()
                try:
                    completed = supervisor.wait_for_terminal(
                        launch.session_id,
                        timeout=5,
                    )
                except TimeoutError as error:
                    raise AssertionError(
                        supervisor.snapshot(launch.session_id)
                    ) from error
            finally:
                (checkout / "finish").touch()
                supervisor.shutdown()

        self.assertEqual(before.revision, after.revision)
        self.assertEqual(before.status, PortableSessionStatus.RUNNING)
        self.assertEqual(after.status, PortableSessionStatus.RUNNING)
        self.assertEqual(renewed.owner_id, "heartbeat-shell")
        self.assertEqual(
            renewed.worker_generation,
            start["payload"]["worker_generation"],
        )
        self.assertIsNotNone(renewed.process_start_fingerprint)
        self.assertEqual(completed.status, PortableSessionStatus.COMPLETED)

    def test_interrupted_worker_stays_stopped_until_explicit_fresh_resume(
        self,
    ) -> None:
        crashing_source = (
            "import json,sys\n"
            "from pathlib import Path\n"
            "json.loads(sys.stdin.readline())\n"
            "Path('partial.txt').write_text('preserved',encoding='utf-8')\n"
            "print('partial diagnostic',file=sys.stderr,flush=True)\n"
            "raise SystemExit(17)\n"
        )
        recovery_source = (
            "import json,sys\n"
            "from pathlib import Path\n"
            "session_id=sys.argv[1]\n"
            "start=json.loads(sys.stdin.readline())\n"
            "Path('recovery.json').write_text(json.dumps(start),encoding='utf-8')\n"
            "print(json.dumps({'version':1,'session_id':session_id,"
            "'sequence':1,'kind':'COMPLETION','payload':{'exit_code':0}}),"
            "flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            prd = checkout / "change.md"
            issues = checkout / "README.md"
            prd.write_text("# Change\n", encoding="utf-8")
            issues.write_text("# Issues\n", encoding="utf-8")
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch_count = 0

            def launch_worker(
                launch: PortableSessionLaunch,
            ) -> subprocess.Popen[str]:
                nonlocal launch_count
                launch_count += 1
                source = crashing_source if launch_count == 1 else recovery_source
                return subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        source,
                        launch.session_id,
                    ],
                    cwd=launch.checkout,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )

            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=catalog,
                owner_id="recovery-shell",
            )
            launch = PortableSessionLaunch(
                session_id="explicit-recovery",
                checkout=checkout,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=("--prd", str(prd), "--issues", str(issues)),
            )
            try:
                supervisor.start_session(launch)
                interrupted = self._wait_for_status(
                    supervisor,
                    launch.session_id,
                    PortableSessionStatus.INTERRUPTED,
                )
                time.sleep(0.1)
                self.assertEqual(launch_count, 1)
                resumed = supervisor.resume_session(launch.session_id)
                self.assertEqual(resumed.status, PortableSessionStatus.RUNNING)
                completed = supervisor.wait_for_terminal(
                    launch.session_id,
                    timeout=5,
                )
                recovery = json.loads(
                    (checkout / "recovery.json").read_text(encoding="utf-8")
                )
                partial = (checkout / "partial.txt").read_text(encoding="utf-8")
            finally:
                supervisor.shutdown()

        self.assertEqual(interrupted.status, PortableSessionStatus.INTERRUPTED)
        self.assertEqual(interrupted.result, 17)
        self.assertEqual(partial, "preserved")
        self.assertEqual(recovery["payload"]["worker_generation"], 2)
        self.assertIn(
            "partial diagnostic",
            recovery["payload"]["partial_work_context"]["diagnostics"],
        )
        self.assertEqual(completed.status, PortableSessionStatus.COMPLETED)

    def test_application_death_allows_only_confirmed_dead_lease_recovery(
        self,
    ) -> None:
        helper_source = (
            "import os,sys\n"
            "from pathlib import Path\n"
            "from devloop.portable_session_catalog import PortableSessionCatalog\n"
            "from devloop.portable_sessions import PortableSessionLaunch,"
            "PortableWorkflowOperation\n"
            "from devloop.subprocess_utils import capture_process_identity\n"
            "root=Path(sys.argv[1]); checkout=Path(sys.argv[2])\n"
            "catalog=PortableSessionCatalog(root/'portable-sessions.sqlite3')\n"
            "catalog.create_session_with_lease("
            "PortableSessionLaunch('dead-app',checkout,"
            "PortableWorkflowOperation.PLANNING,()),"
            "owner_id='dead-application',"
            "process_identity=capture_process_identity(),worker_generation=1)\n"
            "(root/'app-ready').touch()\n"
            "os._exit(23)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(
                Path(__file__).resolve().parents[1] / "src"
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    helper_source,
                    str(root),
                    str(checkout),
                ],
                env=environment,
            )
            try:
                self._wait_for_path(root / "app-ready")
                self.assertEqual(process.wait(timeout=5), 23)
                time.sleep(0.06)
                catalog = PortableSessionCatalog(
                    root / "portable-sessions.sqlite3",
                    lease_timeout_seconds=0.05,
                )
                replacement = PortableSessionLaunch(
                    session_id="after-dead-app",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
                catalog.create_session(replacement)
                lease = catalog.acquire_session_lease(
                    replacement.session_id,
                    owner_id="replacement-app",
                    process_identity=capture_process_identity(),
                    worker_generation=1,
                )
                dead_session = catalog.get_session("dead-app")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

        self.assertEqual(lease.session_id, replacement.session_id)
        self.assertEqual(dead_session.status, PortableSessionStatus.INTERRUPTED)

    def test_heartbeat_emitter_serializes_frames_and_stops_before_exit(
        self,
    ) -> None:
        event_stream = io.StringIO()
        bridge = PortableWorkerRuntimeBridge(
            "serialized-heartbeat",
            command_stream=io.StringIO(),
            event_stream=event_stream,
        )
        emitter = PortableWorkerHeartbeatEmitter(
            bridge,
            owner_id="serialized-owner",
            worker_generation=4,
            interval_seconds=0.005,
        )
        emitter.start()
        output_thread = threading.Thread(
            target=lambda: [bridge.send_hello() for _index in range(10)]
        )
        output_thread.start()
        output_thread.join(timeout=2)
        self.assertFalse(output_thread.is_alive())
        deadline = time.monotonic() + 2
        while (
            event_stream.getvalue().count("\n") < 3
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        emitter.stop()
        stopped_output = event_stream.getvalue()
        time.sleep(0.02)

        frames = [
            json.loads(line)
            for line in stopped_output.splitlines()
        ]
        self.assertGreaterEqual(len(frames), 3)
        self.assertEqual(
            [frame["sequence"] for frame in frames],
            list(range(1, len(frames) + 1)),
        )
        self.assertEqual(
            {frame["kind"] for frame in frames},
            {"HEARTBEAT", "HELLO"},
        )
        self.assertEqual(event_stream.getvalue(), stopped_output)

    def test_abrupt_worker_stderr_is_bounded_and_remains_viewable(self) -> None:
        worker_source = (
            "import json,sys\n"
            "json.loads(sys.stdin.readline())\n"
            "[(print(str(index)+':'+'x'*3000,file=sys.stderr,flush=True))"
            " for index in range(105)]\n"
            "raise SystemExit(19)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)

            def launch_worker(
                launch: PortableSessionLaunch,
            ) -> subprocess.Popen[str]:
                return subprocess.Popen(
                    [sys.executable, "-u", "-c", worker_source],
                    cwd=launch.checkout,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )

            supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
            launch = PortableSessionLaunch(
                session_id="bounded-stderr",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            try:
                supervisor.start_session(launch)
                self._wait_for_status(
                    supervisor,
                    launch.session_id,
                    PortableSessionStatus.INTERRUPTED,
                )
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    interrupted = supervisor.snapshot(launch.session_id)
                    if len(interrupted.diagnostics) >= 100:
                        break
                    time.sleep(0.01)
            finally:
                supervisor.shutdown()

        self.assertEqual(len(interrupted.diagnostics), 100)
        self.assertTrue(
            all(len(line) <= 2_000 for line in interrupted.diagnostics)
        )
        self.assertTrue(
            any(line.startswith("104:") for line in interrupted.diagnostics)
        )
        self.assertEqual(
            interrupted.diagnostics[-1],
            "Worker exited without a terminal result.",
        )

    def test_reused_pid_cannot_make_an_unrelated_process_own_old_lease(
        self,
    ) -> None:
        now = [100.0]
        old_identity = ProcessIdentity(pid=4401, creation_time=5401)
        replacement_identity = ProcessIdentity(pid=4401, creation_time=6401)

        def probe(identity: ProcessIdentity) -> ProcessTreeState:
            self.assertEqual(identity, old_identity)
            self.assertNotEqual(identity, replacement_identity)
            return ProcessTreeState.STOPPED

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(
                root / "portable-sessions.sqlite3",
                clock=lambda: now[0],
                process_probe=probe,
                lease_timeout_seconds=10,
            )
            old_launch = PortableSessionLaunch(
                "pid-old",
                checkout,
                PortableWorkflowOperation.PLANNING,
                (),
            )
            new_launch = PortableSessionLaunch(
                "pid-new",
                checkout,
                PortableWorkflowOperation.PLANNING,
                (),
            )
            catalog.create_session_with_lease(
                old_launch,
                owner_id="pid-old-owner",
                process_identity=old_identity,
                worker_generation=1,
            )
            catalog.create_session(new_launch)
            now[0] = 111.0

            reclaimed = catalog.acquire_session_lease(
                new_launch.session_id,
                owner_id="pid-new-owner",
                process_identity=replacement_identity,
                worker_generation=1,
            )

        self.assertEqual(reclaimed.process_id, old_identity.pid)
        self.assertEqual(
            reclaimed.process_start_fingerprint,
            replacement_identity.creation_time,
        )
        self.assertEqual(reclaimed.owner_id, "pid-new-owner")

    @staticmethod
    def _wait_for_path(path: Path) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        raise AssertionError(f"Timed out waiting for {path.name}.")

    @staticmethod
    def _wait_for_status(
        supervisor: PortableSessionSupervisor,
        session_id: str,
        status: PortableSessionStatus,
    ) -> PortableSessionSnapshot:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = supervisor.snapshot(session_id)
            if snapshot.status is status:
                return snapshot
            time.sleep(0.01)
        raise AssertionError(f"Timed out waiting for {status.value}.")
