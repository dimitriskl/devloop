from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from devloop.portable_session_catalog import PortableSessionCatalog
from devloop.portable_sessions import (
    PortableSessionLaunch,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
)


class PortableSessionProtocolSecurityTests(unittest.TestCase):
    def test_cross_session_hostility_is_isolated_and_approval_is_typed(self) -> None:
        worker_source = textwrap.dedent(
            r"""
            import json
            import sys
            from pathlib import Path

            session_id, mode = sys.argv[1:3]
            json.loads(sys.stdin.readline())
            if mode == "hostile":
                print("Authorization: Bearer secret-token\x1b[31m", file=sys.stderr, flush=True)
                print(json.dumps({
                    "version": 1,
                    "session_id": "session-victim",
                    "sequence": 1,
                    "kind": "HELLO",
                    "payload": {},
                }), flush=True)
                raise SystemExit(7)
            checkout = str(Path.cwd())
            print(json.dumps({
                "version": 1,
                "session_id": session_id,
                "sequence": 1,
                "kind": "CONTEXT",
                "payload": {
                    "project_root": checkout + "\x1b[31m",
                    "implementation_branch": "main\x1b]0;spoofed\x07",
                    "implementation_worktree": checkout,
                    "prd_path": checkout + "/plan.md\x1b[2J",
                },
            }), flush=True)
            print(json.dumps({
                "version": 1,
                "session_id": session_id,
                "sequence": 2,
                "kind": "INPUT_REQUEST",
                "payload": {
                    "request_id": "approval-1",
                    "request_generation": 1,
                    "request_kind": "APPROVAL",
                    "prompt": "Approve?\x1b[2Jspoofed",
                    "options": [["DENY", "Deny"], ["APPROVE_ONCE", "Approve once"]],
                    "default_key": "DENY",
                    "cancel_key": "DENY",
                },
            }), flush=True)
            decision = json.loads(sys.stdin.readline())
            Path("decision.json").write_text(json.dumps(decision), encoding="utf-8")
            print(json.dumps({
                "version": 1,
                "session_id": session_id,
                "sequence": 3,
                "kind": "COMPLETION",
                "payload": {"exit_code": 0},
            }), flush=True)
            """
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attacker_checkout = root / "attacker"
            victim_checkout = root / "victim"
            attacker_checkout.mkdir()
            victim_checkout.mkdir()
            worker = root / "worker.py"
            worker.write_text(worker_source, encoding="utf-8")
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")

            def launch_worker(launch: PortableSessionLaunch) -> subprocess.Popen[str]:
                return subprocess.Popen(
                    [sys.executable, "-u", str(worker), launch.session_id, launch.arguments[0]],
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
                owner_id="protocol-security-shell",
            )
            attacker = PortableSessionLaunch(
                session_id="session-attacker",
                checkout=attacker_checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("hostile",),
            )
            victim = PortableSessionLaunch(
                session_id="session-victim",
                checkout=victim_checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("valid",),
            )
            try:
                supervisor.start_session(attacker)
                supervisor.start_session(victim)
                waiting = self._wait_for_status(
                    supervisor,
                    victim.session_id,
                    PortableSessionStatus.WAITING_FOR_INPUT,
                )
                failed = supervisor.wait_for_terminal(attacker.session_id, timeout=5)

                self.assertEqual(failed.status, PortableSessionStatus.FAILED)
                self.assertTrue(
                    any("expected 'session-attacker'" in item for item in failed.diagnostics)
                )
                self.assertNotIn("secret-token", "\n".join(failed.diagnostics))
                self.assertNotIn("\x1b", "\n".join(failed.diagnostics))
                self.assertEqual(waiting.status, PortableSessionStatus.WAITING_FOR_INPUT)
                durable_victim = catalog.get_session(victim.session_id)
                self.assertEqual(
                    durable_victim.status,
                    PortableSessionStatus.WAITING_FOR_INPUT,
                )
                self.assertEqual(durable_victim.activity_summary, "")
                assert waiting.input_request is not None
                self.assertNotIn("\x1b", waiting.input_request.prompt)
                self.assertIn("spoofed", waiting.input_request.prompt)
                assert waiting.context is not None
                self.assertNotIn("\x1b", waiting.context.project_root)
                self.assertNotIn("\x1b", waiting.context.implementation_branch)
                self.assertNotIn("\x1b", waiting.context.prd_path)

                with self.assertRaisesRegex(ValueError, "offered decisions"):
                    supervisor.provide_input(
                        victim.session_id,
                        "INJECTED_DECISION",
                        request_id=waiting.input_request.request_id,
                        request_generation=waiting.input_request.generation,
                    )
                self.assertEqual(
                    supervisor.snapshot(victim.session_id).status,
                    PortableSessionStatus.WAITING_FOR_INPUT,
                )

                supervisor.provide_input(
                    victim.session_id,
                    "APPROVE_ONCE",
                    request_id=waiting.input_request.request_id,
                    request_generation=waiting.input_request.generation,
                )
                completed = self._wait_for_status(
                    supervisor,
                    victim.session_id,
                    PortableSessionStatus.READY,
                )
                decision = json.loads(
                    (victim_checkout / "decision.json").read_text(encoding="utf-8")
                )
            finally:
                supervisor.shutdown()

        self.assertEqual(completed.status, PortableSessionStatus.READY)
        self.assertEqual(decision["kind"], "APPROVAL_DECISION")
        self.assertEqual(decision["payload"]["decision"], "APPROVE_ONCE")
        self.assertNotIn("value", decision["payload"])

    def test_concurrent_hostile_workers_fail_without_affecting_valid_siblings(self) -> None:
        worker_source = textwrap.dedent(
            r"""
            import json
            import sys

            session_id, mode = sys.argv[1:3]
            json.loads(sys.stdin.readline())
            frame = {
                "version": 1,
                "session_id": session_id,
                "sequence": 1,
                "kind": "HELLO",
                "payload": {},
            }
            if mode == "valid":
                print(json.dumps(frame), flush=True)
                frame.update(sequence=2, kind="ACTIVITY", payload={"message": session_id})
                print(json.dumps(frame), flush=True)
                frame.update(sequence=3, kind="COMPLETION", payload={"exit_code": 0})
                print(json.dumps(frame), flush=True)
            elif mode == "extra":
                frame["unexpected"] = True
                print(json.dumps(frame), flush=True)
            elif mode == "unknown":
                frame["kind"] = "HOSTILE"
                print(json.dumps(frame), flush=True)
            elif mode == "skipped":
                frame["sequence"] = 2
                print(json.dumps(frame), flush=True)
            elif mode == "invalid-utf8":
                sys.stdout.buffer.write(b"\xff\n")
                sys.stdout.buffer.flush()
            elif mode == "oversized":
                sys.stdout.write("x" * 70000)
                sys.stdout.flush()
            """
        )
        modes = ("valid", "valid", "extra", "unknown", "skipped", "invalid-utf8", "oversized")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / "worker.py"
            worker.write_text(worker_source, encoding="utf-8")
            processes: list[subprocess.Popen[str]] = []

            def launch_worker(launch: PortableSessionLaunch) -> subprocess.Popen[str]:
                process = subprocess.Popen(
                    [sys.executable, "-u", str(worker), launch.session_id, launch.arguments[0]],
                    cwd=launch.checkout,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                processes.append(process)
                return process

            supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
            launches = []
            try:
                for index, mode in enumerate(modes):
                    checkout = root / f"checkout-{index}"
                    checkout.mkdir()
                    launch = PortableSessionLaunch(
                        session_id=f"stress-{index}",
                        checkout=checkout,
                        operation=PortableWorkflowOperation.PLANNING,
                        arguments=(mode,),
                    )
                    launches.append(launch)
                    supervisor.start_session(launch)
                snapshots = {
                    launch.session_id: supervisor.wait_for_terminal(
                        launch.session_id,
                        timeout=10,
                    )
                    for launch in launches
                }
            finally:
                supervisor.shutdown()

        for index, mode in enumerate(modes):
            snapshot = snapshots[f"stress-{index}"]
            if mode == "valid":
                self.assertEqual(snapshot.status, PortableSessionStatus.COMPLETED)
                self.assertEqual(snapshot.activity, (snapshot.session_id,))
            else:
                self.assertEqual(snapshot.status, PortableSessionStatus.FAILED)
                self.assertEqual(snapshot.activity, ())
        self.assertTrue(all(process.poll() is not None for process in processes))

    def test_oversized_stderr_is_discarded_without_retaining_split_secrets(self) -> None:
        worker_source = textwrap.dedent(
            """
            import json
            import sys

            session_id = sys.argv[1]
            json.loads(sys.stdin.readline())
            sys.stderr.write(("Authorization: Bearer secret-value " * 10000))
            sys.stderr.flush()
            print(json.dumps({
                "version": 1,
                "session_id": session_id,
                "sequence": 1,
                "kind": "COMPLETION",
                "payload": {"exit_code": 0},
            }), flush=True)
            """
        )

        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)

            def launch_worker(launch: PortableSessionLaunch) -> subprocess.Popen[str]:
                return subprocess.Popen(
                    [sys.executable, "-u", "-c", worker_source, launch.session_id],
                    cwd=launch.checkout,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )

            supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
            launch = PortableSessionLaunch(
                session_id="oversized-diagnostic",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            try:
                supervisor.start_session(launch)
                completed = supervisor.wait_for_terminal(launch.session_id, timeout=10)
            finally:
                supervisor.shutdown()

        self.assertEqual(completed.status, PortableSessionStatus.COMPLETED)
        self.assertEqual(len(completed.diagnostics), 1)
        self.assertIn("size limit", completed.diagnostics[0])
        self.assertNotIn("secret-value", completed.diagnostics[0])

    @staticmethod
    def _wait_for_status(
        supervisor: PortableSessionSupervisor,
        session_id: str,
        status: PortableSessionStatus,
    ):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = supervisor.snapshot(session_id)
            if snapshot.status is status:
                return snapshot
            time.sleep(0.01)
        snapshot = supervisor.snapshot(session_id)
        raise AssertionError(
            f"Session did not reach {status.value}: {snapshot!r}"
        )


if __name__ == "__main__":
    unittest.main()
