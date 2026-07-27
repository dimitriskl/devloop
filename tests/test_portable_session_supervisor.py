from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from devloop.portable_session_catalog import PortableSessionCatalog
from devloop.portable_sessions import (
    PortableSessionLaunch,
    PortableSessionSnapshot,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
)


class PortableSessionSupervisorTests(unittest.TestCase):
    def test_published_workflow_resumes_with_its_prd_in_the_same_application(
        self,
    ) -> None:
        worker_source = textwrap.dedent(
            """
            import json
            import sys

            session_id = sys.argv[1]
            json.loads(sys.stdin.readline())
            print(json.dumps({
                "version": 1,
                "session_id": session_id,
                "sequence": 1,
                "kind": "HELLO",
                "payload": {},
            }), flush=True)
            print(json.dumps({
                "version": 1,
                "session_id": session_id,
                "sequence": 2,
                "kind": "COMPLETION",
                "payload": {"exit_code": 0},
            }), flush=True)
            """
        )
        workers: list[subprocess.Popen[str]] = []
        launches: list[PortableSessionLaunch] = []

        def launch_worker(
            launch: PortableSessionLaunch,
        ) -> subprocess.Popen[str]:
            launches.append(launch)
            if len(launches) == 1:
                catalog.publish_workflow(
                    launch.session_id,
                    prd_path=prd,
                    issues_index_path=issues,
                    activity_summary="Published workflow ready for delivery",
                )
            worker = subprocess.Popen(
                [sys.executable, "-u", "-c", worker_source, launch.session_id],
                cwd=launch.checkout,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            workers.append(worker)
            return worker

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            prd = checkout / "change.md"
            issues = checkout / "README.md"
            prd.write_text("# Change\n", encoding="utf-8")
            issues.write_text("# Issues\n", encoding="utf-8")
            launch = PortableSessionLaunch(
                session_id="pre-prd-resume-twice",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(checkout)),
            )
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            catalog.create_session(launch)
            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=catalog,
                resume_candidates_loader=lambda: (
                    SimpleNamespace(prd_path=prd.resolve()),
                ),
            )

            supervisor.resume_session(launch.session_id)
            self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.READY,
            )
            resumed = supervisor.resume_session(launch.session_id)
            self.assertEqual(resumed.status, PortableSessionStatus.RUNNING)
            self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.READY,
            )
            supervisor.shutdown()
            returncodes = [worker.wait(timeout=5) for worker in workers]

        self.assertEqual(len(workers), 2)
        self.assertEqual(returncodes, [0, 0])
        self.assertEqual(launches[0].arguments, ("--repo", str(checkout)))
        self.assertEqual(launches[1].arguments, ("--prd", str(prd.resolve())))

    def test_session_projects_context_activity_and_success_from_an_isolated_worker(
        self,
    ) -> None:
        worker_source = textwrap.dedent(
            """
            import json
            import sys

            session_id = sys.argv[1]

            def send(sequence, kind, payload):
                print(json.dumps({
                    "version": 1,
                    "session_id": session_id,
                    "sequence": sequence,
                    "kind": kind,
                    "payload": payload,
                }), flush=True)

            command = json.loads(sys.stdin.readline())
            assert command["kind"] == "START"
            send(1, "HELLO", {})
            send(2, "CONTEXT", {
                "project_root": "C:/code/project",
                "implementation_branch": "feature/session",
                "implementation_worktree": "C:/code/project-worktree",
                "prd_path": "C:/code/project/prd/session.md",
            })
            send(3, "ACTIVITY", {"message": "Planning started"})
            send(4, "COMPLETION", {"exit_code": 0})
            """
        )

        def launch_worker(
            launch: PortableSessionLaunch,
        ) -> subprocess.Popen[str]:
            return subprocess.Popen(
                [sys.executable, "-u", "-c", worker_source, launch.session_id],
                cwd=launch.checkout,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
            launch = PortableSessionLaunch(
                session_id="session-0001",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )

            supervisor.start_session(launch)
            completed = supervisor.wait_for_terminal(
                launch.session_id,
                timeout=5,
            )
            supervisor.shutdown()

        self.assertEqual(completed.status, PortableSessionStatus.COMPLETED)
        self.assertEqual(completed.result, 0)
        self.assertEqual(completed.activity, ("Planning started",))
        self.assertIsNotNone(completed.context)
        assert completed.context is not None
        self.assertEqual(
            completed.context.implementation_worktree,
            "C:/code/project-worktree",
        )

    def test_real_worker_runs_existing_planning_entrypoint_in_child_process(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = PortableSessionSupervisor()
            launch = PortableSessionLaunch(
                session_id="session-planning-help",
                checkout=Path(directory),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--help",),
            )

            supervisor.start_session(launch)
            completed = supervisor.wait_for_terminal(
                launch.session_id,
                timeout=5,
            )
            supervisor.shutdown()

        self.assertEqual(completed.status, PortableSessionStatus.COMPLETED)
        self.assertTrue(
            any("usage:" in message.casefold() for message in completed.activity)
        )

    def test_session_routes_input_only_after_worker_requests_it(self) -> None:
        worker_source = textwrap.dedent(
            """
            import json
            import sys

            session_id = sys.argv[1]

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
                "request_kind": "CHOICE",
                "options": [["start", "Start"], ["cancel", "Cancel"]],
                "default_key": "start",
                "cancel_key": "cancel",
            })
            answer = json.loads(sys.stdin.readline())
            send(2, "ACTIVITY", {"message": "Selected " + answer["payload"]["value"]})
            send(3, "COMPLETION", {"exit_code": 0})
            """
        )

        def launch_worker(
            launch: PortableSessionLaunch,
        ) -> subprocess.Popen[str]:
            return subprocess.Popen(
                [sys.executable, "-u", "-c", worker_source, launch.session_id],
                cwd=launch.checkout,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

        with tempfile.TemporaryDirectory() as directory:
            supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
            launch = PortableSessionLaunch(
                session_id="session-input",
                checkout=Path(directory),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            supervisor.start_session(launch)
            for _ in range(100):
                waiting = supervisor.snapshot(launch.session_id)
                if waiting.status is PortableSessionStatus.WAITING_FOR_INPUT:
                    break
                threading.Event().wait(0.01)
            else:
                self.fail("Worker did not request input.")

            self.assertIsNotNone(waiting.input_request)
            supervisor.provide_input(launch.session_id, "start")
            completed = supervisor.wait_for_terminal(
                launch.session_id,
                timeout=5,
            )
            supervisor.shutdown()

        self.assertEqual(completed.activity, ("Selected start",))

    def test_unknown_protocol_version_fails_only_that_session(self) -> None:
        completed = self._run_invalid_worker_frame(
            '{"version":99,"session_id":"session-invalid","sequence":1,'
            '"kind":"HELLO","payload":{}}'
        )

        self.assertEqual(completed.status, PortableSessionStatus.FAILED)
        self.assertTrue(
            any(
                "Unsupported worker protocol version" in diagnostic
                for diagnostic in completed.diagnostics
            )
        )

    def test_boolean_protocol_versions_are_rejected_as_non_integers(self) -> None:
        for version in (True, False):
            with self.subTest(version=version):
                completed = self._run_invalid_worker_frame(
                    json.dumps(
                        {
                            "version": version,
                            "session_id": "session-invalid",
                            "sequence": 1,
                            "kind": "HELLO",
                            "payload": {},
                        }
                    )
                )

                self.assertEqual(completed.status, PortableSessionStatus.FAILED)
                self.assertEqual(completed.result, 1)
                self.assertIn(
                    "Worker protocol version must be an integer.",
                    completed.diagnostics,
                )

    def test_wrong_worker_session_identity_fails_clearly(self) -> None:
        completed = self._run_invalid_worker_frame(
            '{"version":1,"session_id":"another-session","sequence":1,'
            '"kind":"HELLO","payload":{}}'
        )

        self.assertEqual(completed.status, PortableSessionStatus.FAILED)
        self.assertTrue(
            any(
                "expected 'session-invalid'" in diagnostic
                for diagnostic in completed.diagnostics
            )
        )

    def test_boolean_worker_sequences_are_rejected_as_non_integers(self) -> None:
        for sequence in (True, False):
            with self.subTest(sequence=sequence):
                completed = self._run_invalid_worker_frame(
                    json.dumps(
                        {
                            "version": 1,
                            "session_id": "session-invalid",
                            "sequence": sequence,
                            "kind": "HELLO",
                            "payload": {},
                        }
                    )
                )

                self.assertEqual(completed.status, PortableSessionStatus.FAILED)
                self.assertEqual(completed.result, 1)
                self.assertIn(
                    "Worker frame sequence must be a positive integer.",
                    completed.diagnostics,
                )

    def test_malformed_worker_frame_fails_clearly(self) -> None:
        completed = self._run_invalid_worker_frame("not-json")

        self.assertEqual(completed.status, PortableSessionStatus.FAILED)
        self.assertIn("Worker sent malformed JSON.", completed.diagnostics)

    def test_status_frame_cannot_claim_a_terminal_session_result(self) -> None:
        for status in ("COMPLETED", "FAILED", "CANCELLED"):
            with self.subTest(status=status):
                completed = self._run_invalid_worker_frame(
                    json.dumps(
                        {
                            "version": 1,
                            "session_id": "session-invalid",
                            "sequence": 1,
                            "kind": "STATUS",
                            "payload": {"status": status},
                        }
                    )
                )

                self.assertEqual(completed.status, PortableSessionStatus.FAILED)
                self.assertEqual(completed.result, 1)
                self.assertTrue(
                    any(
                        "terminal session status" in diagnostic
                        for diagnostic in completed.diagnostics
                    )
                )

    def test_boolean_completion_exit_codes_are_rejected_as_non_integers(self) -> None:
        for exit_code in (True, False):
            with self.subTest(exit_code=exit_code):
                completed = self._run_invalid_worker_frame(
                    json.dumps(
                        {
                            "version": 1,
                            "session_id": "session-invalid",
                            "sequence": 1,
                            "kind": "COMPLETION",
                            "payload": {"exit_code": exit_code},
                        }
                    )
                )

                self.assertEqual(completed.status, PortableSessionStatus.FAILED)
                self.assertEqual(completed.result, 1)
                self.assertIn(
                    "Worker completion exit_code must be an integer.",
                    completed.diagnostics,
                )

    def test_worker_standard_error_is_captured_as_session_diagnostics(self) -> None:
        worker_source = textwrap.dedent(
            """
            import json
            import sys

            session_id = sys.argv[1]
            json.loads(sys.stdin.readline())
            print("isolated worker diagnostic", file=sys.stderr, flush=True)
            print(json.dumps({
                "version": 1,
                "session_id": session_id,
                "sequence": 1,
                "kind": "FAILURE",
                "payload": {"message": "worker failed"},
            }), flush=True)
            """
        )

        def launch_worker(
            launch: PortableSessionLaunch,
        ) -> subprocess.Popen[str]:
            return subprocess.Popen(
                [sys.executable, "-u", "-c", worker_source, launch.session_id],
                cwd=launch.checkout,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

        with tempfile.TemporaryDirectory() as directory:
            supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
            launch = PortableSessionLaunch(
                session_id="session-diagnostic",
                checkout=Path(directory),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            supervisor.start_session(launch)
            supervisor.wait_for_terminal(launch.session_id, timeout=5)
            supervisor.shutdown()
            completed = supervisor.snapshot(launch.session_id)

        self.assertEqual(completed.status, PortableSessionStatus.FAILED)
        self.assertEqual(completed.result, 1)
        self.assertIn("isolated worker diagnostic", completed.diagnostics)
        self.assertIn("worker failed", completed.diagnostics)

    def _run_invalid_worker_frame(
        self,
        invalid_frame: str,
    ) -> PortableSessionSnapshot:
        worker_source = textwrap.dedent(
            f"""
            import json
            import sys

            json.loads(sys.stdin.readline())
            print({invalid_frame!r}, flush=True)
            """
        )

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

        with tempfile.TemporaryDirectory() as directory:
            supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
            launch = PortableSessionLaunch(
                session_id="session-invalid",
                checkout=Path(directory),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            supervisor.start_session(launch)
            supervisor.wait_for_terminal(launch.session_id, timeout=5)
            supervisor.shutdown()
            return supervisor.snapshot(launch.session_id)

    def _wait_for_status(
        self,
        supervisor: PortableSessionSupervisor,
        session_id: str,
        expected: PortableSessionStatus,
    ) -> PortableSessionSnapshot:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = supervisor.snapshot(session_id)
            if snapshot.status is expected:
                return snapshot
            time.sleep(0.01)
        self.fail(f"Portable session did not reach {expected.value}.")


if __name__ == "__main__":
    unittest.main()
