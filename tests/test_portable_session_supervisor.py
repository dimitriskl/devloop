from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devloop import cli, interactive_runner
from devloop.portable_session_catalog import PortableSessionCatalog
from devloop.portable_sessions import (
    PortableSessionInputKind,
    PortableSessionInputRequest,
    PortableSessionLaunch,
    PortableSessionProgress,
    PortableSessionSnapshot,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
)
from devloop.subprocess_utils import (
    ProcessTerminationResult,
    process_tree_creation_kwargs,
    register_process_tree,
    terminate_process,
)


class PortableSessionSupervisorTests(unittest.TestCase):
    def test_pause_uses_exact_durable_pre_prd_thread_and_settings(self) -> None:
        worker_source = textwrap.dedent(
            """
            import sys

            from devloop import portable_worker
            from devloop.portable_runtime import active_portable_runtime
            from devloop.portable_session_catalog import (
                PortablePlanningSettings,
                active_portable_catalog_session,
            )

            thread_id = "11111111-2222-4333-8444-555555555555"
            settings = PortablePlanningSettings(
                backend="CODEX_CLI",
                model="gpt-5.4",
                reasoning_effort="high",
                fast="OFF",
                timeout_seconds=1200,
                checkpoint_seconds=300,
            )

            def wait_for_pause(_operation, _arguments):
                active = active_portable_catalog_session()
                assert active is not None
                catalog, record, _restore = active
                assert record is not None
                catalog.save_planning_settings(record.session_id, settings)
                catalog.save_planning_thread(record.session_id, thread_id)
                bridge = active_portable_runtime()
                assert bridge is not None
                bridge.read_line("Planning checkpoint ready")
                return 0

            portable_worker._run_operation = wait_for_pause
            raise SystemExit(portable_worker.main(["--session-id", sys.argv[1]]))
            """
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            owner_id = "real-planning-pause-shell"

            def launch_worker(launch: PortableSessionLaunch) -> subprocess.Popen[str]:
                environment = self._worker_environment(
                    catalog,
                    owner_id,
                )
                environment["DEVLOOP_PORTABLE_SESSION_ID"] = launch.session_id
                return subprocess.Popen(
                    [sys.executable, "-u", "-c", worker_source, launch.session_id],
                    cwd=launch.checkout,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )

            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=catalog,
                owner_id=owner_id,
            )
            launch = PortableSessionLaunch(
                session_id="real-planning-pause",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            supervisor.start_session(launch)
            self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.WAITING_FOR_INPUT,
            )
            supervisor.pause_session(launch.session_id)
            paused = self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.PAUSED,
            )
            record = catalog.get_session(launch.session_id)
            cancelled = supervisor.cancel_session(launch.session_id)
            supervisor.shutdown()

        self.assertEqual(
            record.planning_thread_id,
            "11111111-2222-4333-8444-555555555555",
        )
        self.assertIsNotNone(record.planning_settings)
        self.assertIn(record.planning_thread_id, paused.activity[-1])
        self.assertEqual(cancelled.status, PortableSessionStatus.CANCELLED)

    def test_pause_uses_exact_durable_prd_role_and_pass_cursor(self) -> None:
        worker_source = textwrap.dedent(
            """
            import sys
            from pathlib import Path

            from devloop import portable_worker
            from devloop.codex_runner import RoleResult
            from devloop.issue_pack import parse_issue_index
            from devloop.issue_scheduler import SchedulingPhase
            from devloop.portable_runtime import active_portable_runtime
            from devloop.portable_session_catalog import (
                bind_active_catalog_session_checkout,
            )
            from devloop.state import LoopStateWriter

            prd_path = Path(sys.argv[2]).resolve()
            issues_index = Path(sys.argv[3]).resolve()

            def wait_for_pause(_operation, _arguments):
                bind_active_catalog_session_checkout(
                    Path.cwd(),
                    prd_path=prd_path,
                    issues_index_path=issues_index,
                )
                issue = parse_issue_index(issues_index)[0]
                writer = LoopStateWriter(issues_index)
                writer.record_run_start(Path.cwd(), prd_path, [issue.number], False)
                writer.reserve_scheduling_attempt(
                    issue,
                    phase=SchedulingPhase.NORMAL_SCHEDULING,
                    ordinal=1,
                )
                writer.record_issue_start(issue)
                writer.record_role_result(
                    issue,
                    "coder",
                    2,
                    RoleResult(status="PASS", summary="Coder pass two persisted."),
                )
                bridge = active_portable_runtime()
                assert bridge is not None
                bridge.read_line("PRD checkpoint ready")
                return 0

            portable_worker._run_operation = wait_for_pause
            raise SystemExit(portable_worker.main(["--session-id", sys.argv[1]]))
            """
        )
        resumed_worker_source = textwrap.dedent(
            """
            import json
            import sys
            from pathlib import Path

            from devloop.issue_pack import parse_issue_index
            from devloop.state import LoopStateWriter

            session_id = sys.argv[1]
            issues_index = Path(sys.argv[3]).resolve()
            json.loads(sys.stdin.readline())
            issue = parse_issue_index(issues_index)[0]
            cursor = LoopStateWriter(issues_index).resume_issue(issue)
            assert cursor.next_role.value == "reviewer"
            assert cursor.pass_number == 2
            for sequence, kind, payload in (
                (1, "ACTIVITY", {
                    "message": "Resumed exact reviewer pass 2 cursor",
                }),
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
        worker_sources = iter((worker_source, resumed_worker_source))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            issues_directory = checkout / "prd" / "cursor" / "issues"
            issues_directory.mkdir(parents=True)
            prd = issues_directory.parent / "cursor.md"
            index = issues_directory / "README.md"
            issue = issues_directory / "0001-cursor.md"
            prd.write_text("# Cursor\n", encoding="utf-8")
            index.write_text("- [Cursor](./0001-cursor.md)\n", encoding="utf-8")
            issue.write_text("# Cursor\n\nCompleted: [ ]\n", encoding="utf-8")
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            owner_id = "real-prd-pause-shell"

            def launch_worker(launch: PortableSessionLaunch) -> subprocess.Popen[str]:
                environment = self._worker_environment(catalog, owner_id)
                environment["DEVLOOP_PORTABLE_SESSION_ID"] = launch.session_id
                return subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        next(worker_sources),
                        launch.session_id,
                        str(prd),
                        str(index),
                    ],
                    cwd=launch.checkout,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )

            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=catalog,
                owner_id=owner_id,
            )
            launch = PortableSessionLaunch(
                session_id="real-prd-pause",
                checkout=checkout,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=("--prd", str(prd), "--issues", str(index)),
            )
            supervisor.start_session(launch)
            self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.WAITING_FOR_INPUT,
            )
            supervisor.pause_session(launch.session_id)
            paused = self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.PAUSED,
            )
            supervisor.resume_session(launch.session_id)
            completed = supervisor.wait_for_terminal(
                launch.session_id,
                timeout=5,
            )
            supervisor.shutdown()

        self.assertIn("issue 0001", paused.activity[-1])
        self.assertIn("reviewer pass 2", paused.activity[-1])
        self.assertIn("Resumed exact reviewer pass 2 cursor", completed.activity)

    def test_pause_rejects_a_marker_without_authoritative_evidence(self) -> None:
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
                "request_id": "planning-choice",
                "request_generation": 1,
                "request_kind": "TEXT",
                "prompt": "What should we build?",
            })
            command = json.loads(sys.stdin.readline())
            assert command["kind"] == "PAUSE", command
            send(2, "CHECKPOINT", {
                "summary": "Planning thread and settings are durable",
            })
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
                session_id="pause-waiting",
                checkout=Path(directory),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            supervisor.start_session(launch)
            self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.WAITING_FOR_INPUT,
            )

            pausing = supervisor.pause_session(launch.session_id)
            interrupted = self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.INTERRUPTED,
            )
            supervisor.shutdown()

        self.assertEqual(pausing.status, PortableSessionStatus.PAUSING)
        self.assertEqual(interrupted.status, PortableSessionStatus.INTERRUPTED)
        self.assertIsNone(interrupted.input_request)
        self.assertIn(
            "authoritative session catalog",
            interrupted.diagnostics[-1],
        )

    def test_pause_without_durable_state_is_interrupted(self) -> None:
        worker_source = textwrap.dedent(
            """
            import sys
            import time

            from devloop import portable_worker
            from devloop.portable_runtime import active_portable_runtime

            def active_operation(_operation, _arguments):
                bridge = active_portable_runtime()
                assert bridge is not None
                while True:
                    bridge.show_screen("Active operation boundary")
                    time.sleep(0.01)

            portable_worker._run_operation = active_operation
            raise SystemExit(
                portable_worker.main(["--session-id", sys.argv[1]])
            )
            """
        )

        def launch_worker(
            launch: PortableSessionLaunch,
        ) -> subprocess.Popen[str]:
            environment = os.environ.copy()
            source_path = str(Path(cli.__file__).resolve().parents[1])
            environment["PYTHONPATH"] = source_path
            return subprocess.Popen(
                [sys.executable, "-u", "-c", worker_source, launch.session_id],
                cwd=launch.checkout,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

        with tempfile.TemporaryDirectory() as directory:
            supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
            launch = PortableSessionLaunch(
                session_id="pause-active",
                checkout=Path(directory),
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=(),
            )
            supervisor.start_session(launch)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if "Active operation boundary" in supervisor.snapshot(
                    launch.session_id
                ).activity:
                    break
                time.sleep(0.01)
            else:
                self.fail("Active worker did not publish a runtime boundary.")

            supervisor.pause_session(launch.session_id)
            interrupted = self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.INTERRUPTED,
            )
            supervisor.shutdown()

        self.assertEqual(interrupted.status, PortableSessionStatus.INTERRUPTED)
        self.assertIn(
            "Portable Session Catalog is unavailable",
            interrupted.diagnostics[-1],
        )

    def test_force_stop_preserves_partial_work_and_diagnostics(self) -> None:
        worker_source = textwrap.dedent(
            """
            import json
            import sys
            from pathlib import Path

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
            Path("partial-work.txt").write_text("kept\\n", encoding="utf-8")
            print("partial command diagnostic", file=sys.stderr, flush=True)
            send(1, "ACTIVITY", {"message": "Last durable issue checkpoint"})
            sys.stdin.readline()
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
                session_id="force-stop-active",
                checkout=checkout,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=(),
            )
            supervisor.start_session(launch)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = supervisor.snapshot(launch.session_id)
                if (
                    current.activity
                    and current.diagnostics
                    and (checkout / "partial-work.txt").exists()
                ):
                    break
                time.sleep(0.01)
            else:
                self.fail("Worker did not publish partial-work evidence.")

            stopped = supervisor.force_stop_session(launch.session_id)
            supervisor.shutdown()
            partial_work = (checkout / "partial-work.txt").read_text(
                encoding="utf-8"
            )

        self.assertEqual(stopped.status, PortableSessionStatus.INTERRUPTED)
        self.assertEqual(stopped.result, 130)
        self.assertEqual(partial_work, "kept\n")
        self.assertIn("Last durable issue checkpoint", stopped.activity)
        self.assertIn("partial command diagnostic", stopped.diagnostics)

    def test_explicit_cancel_records_cancelled_and_stops_worker(self) -> None:
        worker_source = textwrap.dedent(
            """
            import sys

            from devloop import portable_worker
            from devloop.portable_runtime import active_portable_runtime

            def wait_for_cancel(_operation, _arguments):
                bridge = active_portable_runtime()
                assert bridge is not None
                bridge.read_line("Wait for explicit cancellation")
                return 0

            portable_worker._run_operation = wait_for_cancel
            raise SystemExit(portable_worker.main(["--session-id", sys.argv[1]]))
            """
        )

        def launch_worker(
            launch: PortableSessionLaunch,
        ) -> subprocess.Popen[str]:
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(cli.__file__).resolve().parents[1])
            return subprocess.Popen(
                [sys.executable, "-u", "-c", worker_source, launch.session_id],
                cwd=launch.checkout,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

        with tempfile.TemporaryDirectory() as directory:
            supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
            launch = PortableSessionLaunch(
                session_id="cancel-active",
                checkout=Path(directory),
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            supervisor.start_session(launch)
            self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.WAITING_FOR_INPUT,
            )

            cancelled = supervisor.cancel_session(launch.session_id)
            terminal = supervisor.wait_for_terminal(launch.session_id, timeout=1)
            supervisor.shutdown()

        self.assertEqual(cancelled.status, PortableSessionStatus.CANCELLED)
        self.assertEqual(terminal.status, PortableSessionStatus.CANCELLED)
        self.assertEqual(cancelled.result, 130)

    def test_interrupted_session_can_be_cancelled_without_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            launch = PortableSessionLaunch(
                session_id="cancel-interrupted-metadata",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            catalog.create_session(launch)
            catalog.update_session_status(
                launch.session_id,
                PortableSessionStatus.INTERRUPTED,
                activity_summary="Retained interrupted checkpoint",
            )
            supervisor = PortableSessionSupervisor(catalog=catalog)

            cancelled = supervisor.cancel_session(launch.session_id)
            persisted_status = catalog.get_session(launch.session_id).status
            supervisor.shutdown()

        self.assertEqual(cancelled.status, PortableSessionStatus.CANCELLED)
        self.assertEqual(persisted_status, PortableSessionStatus.CANCELLED)

    def test_ambiguous_force_stop_retains_capacity_lease_and_worker_ownership(
        self,
    ) -> None:
        worker_source = textwrap.dedent(
            """
            import json
            import sys
            import time

            json.loads(sys.stdin.readline())
            while True:
                time.sleep(0.05)
            """
        )
        processes: list[subprocess.Popen[str]] = []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            owner_id = "ambiguous-force-shell"

            def launch_worker(launch: PortableSessionLaunch) -> subprocess.Popen[str]:
                process = subprocess.Popen(
                    [sys.executable, "-u", "-c", worker_source],
                    cwd=launch.checkout,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    **process_tree_creation_kwargs(),
                )
                register_process_tree(process)
                processes.append(process)
                return process

            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=catalog,
                owner_id=owner_id,
            )
            launch = PortableSessionLaunch(
                session_id="ambiguous-force-stop",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            supervisor.start_session(launch)
            with mock.patch(
                "devloop.portable_sessions.terminate_process",
                return_value=ProcessTerminationResult(
                    tree_terminated=False,
                    detail="Injected ambiguous termination timeout.",
                ),
            ):
                interrupted = supervisor.force_stop_session(launch.session_id)

            self.assertEqual(
                interrupted.status,
                PortableSessionStatus.INTERRUPTED,
            )
            self.assertTrue(
                catalog.owns_execution_capacity(
                    launch.session_id,
                    owner_id=owner_id,
                )
            )
            self.assertIsNotNone(catalog.get_worktree_lease(checkout))
            self.assertIsNone(processes[0].poll())

            cleanup = terminate_process(processes[0])
            self.assertTrue(cleanup.tree_terminated, cleanup.detail)

        self.assertIsNotNone(processes[0].poll())

    def test_force_and_cancel_confirm_real_child_and_grandchild_trees_dead(
        self,
    ) -> None:
        grandchild_source = (
            "import os,time;"
            "from pathlib import Path;"
            "Path('grandchild.pid').write_text(str(os.getpid()),encoding='utf-8');"
            "time.sleep(60)"
        )
        child_source = (
            "import os,subprocess,sys,time;"
            "from pathlib import Path;"
            f"p=subprocess.Popen([sys.executable,'-u','-c',{grandchild_source!r}]);"
            "Path('child.pid').write_text(str(os.getpid()),encoding='utf-8');"
            "p.wait()"
        )
        worker_source = textwrap.dedent(
            f"""
            import subprocess
            import sys
            import time
            from pathlib import Path

            from devloop import portable_worker
            from devloop.portable_runtime import active_portable_runtime
            from devloop.subprocess_utils import (
                process_tree_creation_kwargs,
                register_process_tree,
                unregister_process_tree,
            )

            child_source = {child_source!r}

            def run_backend(_operation, _arguments):
                process = subprocess.Popen(
                    [sys.executable, "-u", "-c", child_source],
                    stdin=subprocess.DEVNULL,
                    **process_tree_creation_kwargs(),
                )
                register_process_tree(process)
                try:
                    while not Path("grandchild.pid").is_file():
                        time.sleep(0.01)
                    process.wait()
                finally:
                    unregister_process_tree(process)
                bridge = active_portable_runtime()
                assert bridge is not None
                bridge.show_screen("Backend tree exited")
                return 0

            portable_worker._run_operation = run_backend
            raise SystemExit(portable_worker.main(["--session-id", sys.argv[1]]))
            """
        )

        for action in ("force", "cancel"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                checkout = root / "checkout"
                checkout.mkdir()
                catalog = PortableSessionCatalog(root / "catalog.sqlite3")
                owner_id = f"{action}-tree-shell"

                def launch_worker(
                    launch: PortableSessionLaunch,
                ) -> subprocess.Popen[str]:
                    environment = self._worker_environment(catalog, owner_id)
                    environment["DEVLOOP_PORTABLE_SESSION_ID"] = launch.session_id
                    return subprocess.Popen(
                        [
                            sys.executable,
                            "-u",
                            "-c",
                            worker_source,
                            launch.session_id,
                        ],
                        cwd=launch.checkout,
                        env=environment,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        **process_tree_creation_kwargs(),
                    )

                supervisor = PortableSessionSupervisor(
                    worker_launcher=launch_worker,
                    catalog=catalog,
                    owner_id=owner_id,
                )
                launch = PortableSessionLaunch(
                    session_id=f"{action}-real-tree",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
                supervisor.start_session(launch)
                try:
                    child_pid = self._wait_for_pid(checkout / "child.pid")
                except AssertionError as error:
                    self.fail(
                        f"{error}; snapshot={supervisor.snapshot(launch.session_id)!r}"
                    )
                grandchild_pid = self._wait_for_pid(checkout / "grandchild.pid")

                stopped = (
                    supervisor.force_stop_session(launch.session_id)
                    if action == "force"
                    else supervisor.cancel_session(launch.session_id)
                )
                supervisor.shutdown()

                self.assertEqual(
                    stopped.status,
                    (
                        PortableSessionStatus.INTERRUPTED
                        if action == "force"
                        else PortableSessionStatus.CANCELLED
                    ),
                    stopped.diagnostics,
                )
                self.assertTrue(self._wait_for_process_dead(child_pid))
                self.assertTrue(self._wait_for_process_dead(grandchild_pid))

    def test_shutdown_cooperatively_pauses_all_live_sessions(self) -> None:
        worker_source = textwrap.dedent(
            """
            import sys
            import time

            from devloop import portable_worker
            from devloop.portable_runtime import active_portable_runtime
            from devloop.portable_session_catalog import (
                PortablePlanningSettings,
                active_portable_catalog_session,
            )

            def run_until_pause(_operation, _arguments):
                active = active_portable_catalog_session()
                assert active is not None
                catalog, record, _restore = active
                assert record is not None
                catalog.save_planning_settings(
                    record.session_id,
                    PortablePlanningSettings(
                        backend="CODEX_CLI",
                        model="gpt-5.4",
                        reasoning_effort="high",
                        fast="OFF",
                        timeout_seconds=1200,
                        checkpoint_seconds=300,
                    ),
                )
                catalog.save_planning_thread(
                    record.session_id,
                    "11111111-2222-4333-8444-" + (
                        "555555555555"
                        if record.session_id.endswith("waiting")
                        else "666666666666"
                    ),
                )
                bridge = active_portable_runtime()
                assert bridge is not None
                if record.session_id.endswith("waiting"):
                    bridge.read_line("Planning input")
                else:
                    while True:
                        bridge.show_screen("Active planning work")
                        time.sleep(0.01)
                return 0

            portable_worker._run_operation = run_until_pause
            raise SystemExit(portable_worker.main(["--session-id", sys.argv[1]]))
            """
        )
        processes: list[subprocess.Popen[str]] = []
        catalog: PortableSessionCatalog
        owner_id = "aggregate-exit-shell"

        def launch_worker(
            launch: PortableSessionLaunch,
        ) -> subprocess.Popen[str]:
            environment = self._worker_environment(catalog, owner_id)
            environment["DEVLOOP_PORTABLE_SESSION_ID"] = launch.session_id
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", worker_source, launch.session_id],
                cwd=launch.checkout,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            processes.append(process)
            return process

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active_checkout = root / "active"
            waiting_checkout = root / "waiting"
            active_checkout.mkdir()
            waiting_checkout.mkdir()
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=catalog,
                owner_id=owner_id,
            )
            launches = (
                PortableSessionLaunch(
                    session_id="exit-active",
                    checkout=active_checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                ),
                PortableSessionLaunch(
                    session_id="exit-waiting",
                    checkout=waiting_checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                ),
            )
            for launch in launches:
                supervisor.start_session(launch)
            self._wait_for_status(
                supervisor,
                "exit-waiting",
                PortableSessionStatus.WAITING_FOR_INPUT,
            )

            supervisor.shutdown()
            snapshots = tuple(
                supervisor.snapshot(launch.session_id) for launch in launches
            )

        self.assertEqual(
            tuple(snapshot.status for snapshot in snapshots),
            (PortableSessionStatus.PAUSED, PortableSessionStatus.PAUSED),
        )
        self.assertTrue(all(process.poll() is not None for process in processes))

    def test_default_planning_handoff_confirms_worktree_only_after_start_transfer(
        self,
    ) -> None:
        worker_bootstrap = textwrap.dedent(
            """
            import sys
            from pathlib import Path
            from types import SimpleNamespace

            from devloop import cli

            cli.resolve_run_workflow_with_repair = lambda *_args, **_kwargs: object()
            def complete_after_confirmed_transfer(**_kwargs):
                cli.read_prompt("Hold the confirmed implementation-worktree lease")
                return cli.DependencyScheduleResult(completed=True)

            cli.execute_dependency_schedule = complete_after_confirmed_transfer
            cli.offer_merge_followup = lambda **_kwargs: None
            cli.choose_run_review_action = (
                lambda *_args, **_kwargs: cli.RunReviewAction.EXIT
            )
            cli.resolve_self_improvement_wiki_path = (
                lambda *_args, **_kwargs: Path(sys.argv[2])
            )
            cli.ensure_self_improvement_wiki = lambda *_args, **_kwargs: None
            cli.write_self_improvement_context = (
                lambda *_args, **_kwargs: Path(sys.argv[2]) / "context.json"
            )
            cli.CodexRunner.run_self_improvement_compiler = (
                lambda *_args, **_kwargs: SimpleNamespace(
                    status="PASS",
                    summary="Skipped by planning handoff regression.",
                    changed_files=[],
                    findings=[],
                    residual_risks=[],
                )
            )

            from devloop.portable_worker import main

            raise SystemExit(main(["--session-id", sys.argv[1]]))
            """
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            subprocess.run(
                ["git", "init"],
                cwd=source,
                check=True,
                capture_output=True,
                text=True,
            )
            (source / "baseline.txt").write_text("baseline\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "."],
                cwd=source,
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
                    "baseline",
                ],
                cwd=source,
                check=True,
                capture_output=True,
                text=True,
            )
            issues = source / "prd" / "handoff-transfer" / "issues"
            issues.mkdir(parents=True)
            prd = issues.parent / "handoff-transfer.md"
            index = issues / "README.md"
            issue = issues / "0001-transfer.md"
            prd.write_text(
                "# Handoff Transfer\n\n"
                "## Target Product\n\n"
                "Product: devloop-plan + devloop\n",
                encoding="utf-8",
            )
            index.write_text(
                "- [Transfer planning delivery](./0001-transfer.md)\n",
                encoding="utf-8",
            )
            issue.write_text(
                "# Transfer planning delivery\n\n"
                "## Target Product\n\n"
                "Product: devloop-plan + devloop\n\n"
                "Completed: [ ]\n",
                encoding="utf-8",
            )

            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            owner_id = "planning-handoff-shell"

            def launch_planning_worker(
                selected: PortableSessionLaunch,
            ) -> subprocess.Popen[str]:
                environment = os.environ.copy()
                environment["APPDATA"] = str(root / "config")
                environment["DEVLOOP_UI_MODE"] = "application"
                environment["DEVLOOP_PORTABLE_SESSION_ID"] = selected.session_id
                environment["DEVLOOP_PORTABLE_SESSION_CATALOG"] = str(catalog.path)
                environment["DEVLOOP_PORTABLE_SESSION_OWNER_ID"] = owner_id
                source_path = str(Path(cli.__file__).resolve().parents[1])
                existing_python_path = environment.get("PYTHONPATH")
                environment["PYTHONPATH"] = (
                    source_path
                    if not existing_python_path
                    else os.pathsep.join((source_path, existing_python_path))
                )
                return subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        worker_bootstrap,
                        selected.session_id,
                        str(root / "wiki"),
                    ],
                    cwd=selected.checkout,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )

            launch = PortableSessionLaunch(
                session_id="default-planning-handoff",
                checkout=source,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--prd", str(prd)),
            )
            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_planning_worker,
                catalog=catalog,
                owner_id=owner_id,
            )
            supervisor.start_session(launch)

            waiting = self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.WAITING_FOR_INPUT,
            )
            proposed_worktree = root / "source-handoff-transfer-dev"
            self.assertFalse(proposed_worktree.exists())
            self.assertEqual(waiting.checkout, source.resolve())
            assert waiting.context is not None
            self.assertEqual(
                Path(waiting.context.implementation_worktree),
                source.resolve(),
            )
            self.assertIsNone(catalog.get_worktree_lease(proposed_worktree))

            self._provide_current_input(supervisor, launch.session_id, "start")
            transferred_snapshot = self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.WAITING_FOR_INPUT,
            )
            transferred = catalog.get_session(launch.session_id)
            implementation_prd = (
                proposed_worktree
                / "prd"
                / "handoff-transfer"
                / "handoff-transfer.md"
            ).resolve()
            implementation_index = (
                proposed_worktree
                / "prd"
                / "handoff-transfer"
                / "issues"
                / "README.md"
            ).resolve()
            self.assertEqual(
                transferred_snapshot.checkout,
                proposed_worktree.resolve(),
            )
            assert transferred_snapshot.context is not None
            self.assertEqual(
                Path(transferred_snapshot.context.implementation_worktree),
                proposed_worktree.resolve(),
            )
            self.assertEqual(transferred.checkout, proposed_worktree.resolve())
            self.assertEqual(transferred.prd_path, implementation_prd)
            self.assertEqual(transferred.issues_index_path, implementation_index)
            self.assertIsNone(catalog.get_worktree_lease(source))
            transferred_lease = catalog.get_worktree_lease(proposed_worktree)
            assert transferred_lease is not None
            self.assertEqual(transferred_lease.session_id, launch.session_id)

            self._provide_current_input(supervisor, launch.session_id, "continue")
            completed = supervisor.wait_for_terminal(launch.session_id, timeout=10)
            supervisor.shutdown()
            self.assertEqual(
                completed.status,
                PortableSessionStatus.COMPLETED,
                completed.diagnostics,
            )
            self.assertIsNone(catalog.get_worktree_lease(proposed_worktree))

    def test_planning_delivery_transfer_resumes_only_from_implementation_worktree(
        self,
    ) -> None:
        waiting_worker_source = textwrap.dedent(
            """
            import json
            import sys

            session_id = sys.argv[1]
            json.loads(sys.stdin.readline())
            for sequence, kind, payload in (
                (1, "HELLO", {}),
                (2, "INPUT_REQUEST", {
                    "request_kind": "TEXT",
                    "prompt": "Hold the implementation-worktree lease",
                    "options": [],
                    "default_key": "",
                    "cancel_key": None,
                }),
            ):
                print(json.dumps({
                    "version": 1,
                    "session_id": session_id,
                    "sequence": sequence,
                    "kind": kind,
                    "payload": payload,
                }), flush=True)
            json.loads(sys.stdin.readline())
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
            source = root / "source"
            implementation = root / "implementation"
            source.mkdir()
            subprocess.run(
                ["git", "init"],
                cwd=source,
                check=True,
                capture_output=True,
                text=True,
            )
            (source / "baseline.txt").write_text("baseline\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "."],
                cwd=source,
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
                    "baseline",
                ],
                cwd=source,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    "feature/pointer-transfer",
                    str(implementation),
                ],
                cwd=source,
                check=True,
                capture_output=True,
                text=True,
            )
            source_issues = source / "prd" / "pointer-transfer" / "issues"
            source_issues.mkdir(parents=True)
            source_prd = source_issues.parent / "pointer-transfer.md"
            source_index = source_issues / "README.md"
            source_issue = source_issues / "0001-transfer.md"
            source_prd.write_text(
                "# Pointer Transfer\n\n"
                "## Target Product\n\n"
                "Product: devloop-plan + devloop\n",
                encoding="utf-8",
            )
            source_index.write_text(
                "- [Transfer pointers](./0001-transfer.md)\n",
                encoding="utf-8",
            )
            source_issue.write_text(
                "# Transfer pointers\n\n"
                "## Target Product\n\n"
                "Product: devloop-plan + devloop\n",
                encoding="utf-8",
            )
            implementation_prd = (
                implementation / "prd" / "pointer-transfer" / "pointer-transfer.md"
            )
            implementation_index = (
                implementation / "prd" / "pointer-transfer" / "issues" / "README.md"
            )
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            owner_id = "same-shell"
            launch = PortableSessionLaunch(
                session_id="planning-delivery-transfer",
                checkout=source,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(source)),
            )
            catalog.create_session_with_lease(launch, owner_id=owner_id)
            self.assertTrue(
                catalog.request_execution_capacity(
                    launch.session_id,
                    owner_id=owner_id,
                )
            )
            catalog.publish_workflow(
                launch.session_id,
                prd_path=source_prd,
                issues_index_path=source_index,
                activity_summary="Planning published in source checkout",
            )
            observed_launches: list[PortableSessionLaunch] = []

            def launch_waiting_worker(
                selected: PortableSessionLaunch,
            ) -> subprocess.Popen[str]:
                observed_launches.append(selected)
                return subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        waiting_worker_source,
                        selected.session_id,
                    ],
                    cwd=selected.checkout,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )

            candidate = SimpleNamespace(
                candidate_id="implementation-candidate",
                checkout=implementation.resolve(),
                prd_path=implementation_prd.resolve(),
            )
            same_app = PortableSessionSupervisor(
                worker_launcher=launch_waiting_worker,
                catalog=catalog,
                resume_candidates_loader=lambda: (candidate,),
                owner_id=owner_id,
            )
            self.assertEqual(
                same_app.snapshot(launch.session_id).checkout,
                source.resolve(),
            )

            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "DEVLOOP_PORTABLE_SESSION_CATALOG": str(catalog.path),
                        "DEVLOOP_PORTABLE_SESSION_ID": launch.session_id,
                        "DEVLOOP_PORTABLE_SESSION_OWNER_ID": owner_id,
                    },
                    clear=False,
                ),
                mock.patch.object(
                    cli,
                    "resolve_run_workflow_with_repair",
                    return_value=object(),
                ),
                mock.patch.object(
                    cli,
                    "execute_dependency_schedule",
                    return_value=SimpleNamespace(completed=True),
                ),
                mock.patch.object(cli, "offer_merge_followup"),
            ):
                delivery_result = cli.main(
                    [
                        "--prd",
                        str(source_prd),
                        "--issues",
                        str(source_index),
                        "--all",
                        "--create-worktree",
                        "--worktree-path",
                        str(implementation),
                        "--branch-name",
                        "feature/pointer-transfer",
                        "--non-interactive",
                        "--plain",
                        "--no-self-improvement-wiki",
                    ]
                )

            transferred = catalog.get_session(launch.session_id)
            self.assertEqual(delivery_result, 0)
            self.assertTrue(implementation_prd.is_file())
            self.assertTrue(implementation_index.is_file())
            self.assertEqual(transferred.checkout, implementation.resolve())
            self.assertEqual(transferred.prd_path, implementation_prd.resolve())
            self.assertEqual(
                transferred.issues_index_path,
                implementation_index.resolve(),
            )
            self.assertIsNone(catalog.get_worktree_lease(source))
            transferred_lease = catalog.get_worktree_lease(implementation)
            assert transferred_lease is not None
            self.assertEqual(transferred_lease.session_id, launch.session_id)

            same_app.resume_session(launch.session_id)
            waiting = self._wait_for_status(
                same_app,
                launch.session_id,
                PortableSessionStatus.WAITING_FOR_INPUT,
            )
            self.assertEqual(waiting.checkout, implementation.resolve())
            self.assertEqual(
                observed_launches[-1].arguments,
                ("--prd", str(implementation_prd.resolve())),
            )
            self.assertIsNone(catalog.get_worktree_lease(source))
            self.assertIsNotNone(catalog.get_worktree_lease(implementation))
            self._provide_current_input(same_app, launch.session_id, "continue")
            self._wait_for_status(
                same_app,
                launch.session_id,
                PortableSessionStatus.READY,
            )
            same_app.shutdown()

            restart_candidates = catalog.discover_resume_candidates(
                interactive_runner.find_resume_candidates
            )
            restarted = PortableSessionSupervisor(
                worker_launcher=launch_waiting_worker,
                catalog=PortableSessionCatalog(catalog.path),
                resume_candidates=restart_candidates,
                resume_candidates_loader=lambda: restart_candidates,
                owner_id="restarted-shell",
            )
            restarted.resume_session(launch.session_id)
            restarted_waiting = self._wait_for_status(
                restarted,
                launch.session_id,
                PortableSessionStatus.WAITING_FOR_INPUT,
            )
            restarted_catalog = PortableSessionCatalog(catalog.path)
            self.assertEqual(
                restarted_waiting.checkout,
                implementation.resolve(),
            )
            self.assertEqual(
                observed_launches[-1].arguments,
                ("--prd", str(implementation_prd.resolve())),
            )
            self.assertIsNone(restarted_catalog.get_worktree_lease(source))
            restarted_lease = restarted_catalog.get_worktree_lease(implementation)
            assert restarted_lease is not None
            self.assertEqual(restarted_lease.session_id, launch.session_id)
            self._provide_current_input(restarted, launch.session_id, "continue")
            self._wait_for_status(
                restarted,
                launch.session_id,
                PortableSessionStatus.READY,
            )
            restarted.shutdown()

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
            candidate = SimpleNamespace(
                candidate_id="published-workflow-candidate",
                checkout=checkout.resolve(),
                prd_path=prd.resolve(),
            )
            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=catalog,
                resume_candidates=(candidate,),
                resume_candidates_loader=lambda: (candidate,),
            )
            self.assertEqual(len(supervisor.list_sessions()), 2)

            supervisor.resume_session(launch.session_id)
            published = self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.READY,
            )
            self.assertEqual(published.prd_path, prd.resolve())
            self.assertEqual(
                [snapshot.session_id for snapshot in supervisor.list_sessions()],
                [launch.session_id],
            )
            self.assertEqual(
                [record.session_id for record in catalog.list_sessions()],
                [launch.session_id],
            )
            with self.assertRaisesRegex(ValueError, "Unknown portable session"):
                supervisor.snapshot(candidate.candidate_id)
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

    def test_status_projects_live_progress_for_sessions_monitoring(self) -> None:
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
            send(1, "STATUS", {
                "status": "RUNNING",
                "stage": "development",
                "completed_issues": 2,
                "total_issues": 5,
                "active_issue": "0003",
            })
            send(2, "STATUS", {
                "status": "RUNNING",
                "stage": "Security Review · pass 2",
            })
            send(3, "INPUT_REQUEST", {
                "request_kind": "TEXT",
                "prompt": "Continue",
            })
            json.loads(sys.stdin.readline())
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
            supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
            launch = PortableSessionLaunch(
                session_id="session-progress",
                checkout=Path(directory),
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=(),
            )

            supervisor.start_session(launch)
            waiting = self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.WAITING_FOR_INPUT,
            )
            self._provide_current_input(supervisor, launch.session_id, "yes")
            supervisor.wait_for_terminal(launch.session_id, timeout=5)
            supervisor.shutdown()

        self.assertEqual(
            waiting.progress,
            PortableSessionProgress(
                stage="Security Review · pass 2",
                completed_issues=2,
                total_issues=5,
                active_issue="0003",
            ),
        )
        self.assertGreater(waiting.updated_at, 0)

    def test_real_resume_candidate_keeps_issue_status_separate_from_workflow_stage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            feature = checkout / "prd" / "candidate-progress"
            issues = feature / "issues"
            issues.mkdir(parents=True)
            prd_path = feature / "candidate-progress.md"
            issues_index = issues / "README.md"
            issue_path = issues / "0001-progress.md"
            prd_path.write_text("# Candidate progress\n", encoding="utf-8")
            issues_index.write_text(
                "[Issue 0001](./0001-progress.md)\n",
                encoding="utf-8",
            )
            issue_path.write_text("# Issue\n\nCompleted: [ ]\n", encoding="utf-8")
            step_id = "e7f9d3a2-1b64-48c5-9d20-6a7b8c9d0e02"
            issues_index.with_name("README.loop.state.json").write_text(
                json.dumps(
                    {
                        "issues": {
                            "0001": {
                                "status": "IN_PROGRESS",
                                "current_step_instance_id": step_id,
                            }
                        },
                        "resolved_workflow": {
                            "steps": [
                                {
                                    "instance_id": step_id,
                                    "display_name": "Security Review",
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            catalog = PortableSessionCatalog(checkout / "portable-sessions.sqlite3")
            catalog.create_session(
                PortableSessionLaunch(
                    session_id="catalog-registration",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
            )
            candidate = catalog.discover_resume_candidates(
                interactive_runner.find_resume_candidates
            )[0]

            supervisor = PortableSessionSupervisor(
                catalog=catalog,
                resume_candidates=(candidate,),
            )
            snapshot = supervisor.snapshot(candidate.candidate_id)
            supervisor.shutdown()

        self.assertEqual(candidate.active_status, "IN_PROGRESS")
        self.assertEqual(
            snapshot.progress,
            PortableSessionProgress(
                stage="Security Review",
                completed_issues=0,
                total_issues=1,
                active_issue="0001",
            ),
        )
        self.assertEqual(snapshot.updated_at, candidate.updated_at)

    def test_worker_activity_refreshes_authoritative_issue_progress(self) -> None:
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
            send(1, "ACTIVITY", {"message": "Issue checkpoint advanced"})
            send(2, "INPUT_REQUEST", {
                "request_kind": "TEXT",
                "prompt": "Continue",
            })
            json.loads(sys.stdin.readline())
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
            checkout = Path(directory)
            prd_path = checkout / "change.md"
            initial = SimpleNamespace(
                candidate_id="candidate-live-progress",
                checkout=checkout,
                prd_path=prd_path,
                completed_issues=1,
                total_issues=3,
                active_issue="0002",
                active_status="IN_PROGRESS",
                active_stage="Development",
                updated_at=10.0,
            )
            advanced = SimpleNamespace(
                candidate_id=initial.candidate_id,
                checkout=checkout,
                prd_path=prd_path,
                completed_issues=2,
                total_issues=3,
                active_issue="0003",
                active_status="IN_PROGRESS",
                active_stage="Security Review",
                updated_at=20.0,
            )
            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                resume_candidates=(initial,),
                resume_candidates_loader=lambda: (advanced,),
            )

            supervisor.resume_session(initial.candidate_id)
            waiting = self._wait_for_status(
                supervisor,
                initial.candidate_id,
                PortableSessionStatus.WAITING_FOR_INPUT,
            )
            self._provide_current_input(supervisor, initial.candidate_id, "yes")
            supervisor.wait_for_terminal(initial.candidate_id, timeout=5)
            supervisor.shutdown()

        self.assertEqual(
            waiting.progress,
            PortableSessionProgress(
                stage="Security Review",
                completed_issues=2,
                total_issues=3,
                active_issue="0003",
            ),
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
        self.assertEqual(completed.progress.stage, "analysis")

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
            self._provide_current_input(supervisor, launch.session_id, "start")
            completed = supervisor.wait_for_terminal(
                launch.session_id,
                timeout=5,
            )
            supervisor.shutdown()

        self.assertEqual(completed.activity, ("Selected start",))

    def test_worker_terminal_frames_clear_a_pending_input_request(self) -> None:
        worker_source = textwrap.dedent(
            """
            import json
            import sys

            session_id = sys.argv[1]
            terminal_kind = sys.argv[2]

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
                "request_kind": "TEXT",
                "prompt": "Pending value",
            })
            terminal_payload = (
                {"exit_code": 0}
                if terminal_kind == "COMPLETION"
                else {"message": "worker failed"}
            )
            send(2, terminal_kind, terminal_payload)
            """
        )

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
                    launch.arguments[0],
                ],
                cwd=launch.checkout,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

        with tempfile.TemporaryDirectory() as directory:
            supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
            terminal_snapshots = []
            for terminal_kind in ("COMPLETION", "FAILURE"):
                launch = PortableSessionLaunch(
                    session_id=f"session-{terminal_kind.casefold()}-while-waiting",
                    checkout=Path(directory),
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(terminal_kind,),
                )
                supervisor.start_session(launch)
                terminal_snapshots.append(
                    supervisor.wait_for_terminal(launch.session_id, timeout=5)
                )
            supervisor.shutdown()

        self.assertEqual(
            [snapshot.status for snapshot in terminal_snapshots],
            [PortableSessionStatus.COMPLETED, PortableSessionStatus.FAILED],
        )
        self.assertTrue(
            all(snapshot.input_request is None for snapshot in terminal_snapshots)
        )
        for snapshot in terminal_snapshots:
            with self.assertRaisesRegex(
                ValueError,
                "terminal and cannot accept input",
            ):
                supervisor.provide_input(
                    snapshot.session_id,
                    "stale input",
                    request_id="stale-request",
                    request_generation=1,
                )

    def test_provide_input_rejects_a_stale_non_running_request_clearly(self) -> None:
        supervisor = PortableSessionSupervisor()
        session_id = "session-stale-input"
        supervisor._snapshots[session_id] = PortableSessionSnapshot(
            session_id=session_id,
            checkout=Path.cwd(),
            status=PortableSessionStatus.WAITING_FOR_INPUT,
            input_request=PortableSessionInputRequest(
                kind=PortableSessionInputKind.TEXT,
                prompt="No worker owns this request",
            ),
        )

        with self.assertRaisesRegex(
            ValueError,
            "not running and cannot accept input",
        ):
            supervisor.provide_input(
                session_id,
                "stale input",
                request_id="stale-request",
                request_generation=1,
            )
        with self.assertRaisesRegex(ValueError, "Unknown portable session"):
            supervisor.provide_input(
                "unknown-session",
                "input",
                request_id="unknown-request",
                request_generation=1,
            )

    def test_provide_input_reconciles_a_broken_worker_pipe_atomically(self) -> None:
        class BlockingOutput:
            def __init__(self) -> None:
                self._lines: queue.Queue[str | None] = queue.Queue()

            def __iter__(self) -> BlockingOutput:
                return self

            def __next__(self) -> str:
                line = self._lines.get(timeout=5)
                if line is None:
                    raise StopIteration
                return line

            def send(self, line: str) -> None:
                self._lines.put(line)

            def close(self) -> None:
                self._lines.put(None)

        class BrokenPipeInput:
            def __init__(self, process: FakeWorkerProcess) -> None:
                self._process = process
                self.lines: list[str] = []
                self.flush_count = 0

            def write(self, value: str) -> int:
                self.lines.append(value)
                return len(value)

            def flush(self) -> None:
                self.flush_count += 1
                if self.flush_count == 2:
                    self._process.return_code = 17
                    raise BrokenPipeError("worker closed stdin")

            def close(self) -> None:
                return None

        class FakeWorkerProcess:
            def __init__(self) -> None:
                self.return_code: int | None = None
                self.stdout = BlockingOutput()
                self.stderr = BlockingOutput()
                self.stdin = BrokenPipeInput(self)

            def poll(self) -> int | None:
                return self.return_code

            def terminate(self) -> None:
                self.return_code = -15

            def wait(self, timeout: float | None = None) -> int:
                if self.return_code is None:
                    raise subprocess.TimeoutExpired("fake-worker", timeout)
                return self.return_code

        stale_frames = (
            ("STATUS", {"status": "RUNNING", "stage": "stale status"}),
            (
                "INPUT_REQUEST",
                {
                    "request_kind": "TEXT",
                    "prompt": "Stale buffered request",
                },
            ),
            ("COMPLETION", {"exit_code": 0}),
        )
        for stale_kind, stale_payload in stale_frames:
            with self.subTest(stale_kind=stale_kind):
                process = FakeWorkerProcess()
                supervisor = PortableSessionSupervisor(
                    worker_launcher=lambda _launch: process
                )
                launch = PortableSessionLaunch(
                    session_id=f"session-broken-input-pipe-{stale_kind.casefold()}",
                    checkout=Path.cwd(),
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
                supervisor.start_session(launch)
                process.stdout.send(
                    json.dumps(
                        {
                            "version": 1,
                            "session_id": launch.session_id,
                            "sequence": 1,
                            "kind": "INPUT_REQUEST",
                            "payload": {
                                "request_kind": "TEXT",
                                "prompt": "Value before worker exit",
                            },
                        }
                    )
                    + "\n"
                )
                self._wait_for_status(
                    supervisor,
                    launch.session_id,
                    PortableSessionStatus.WAITING_FOR_INPUT,
                )
                running = supervisor._running[launch.session_id]

                with self.assertRaisesRegex(
                    ValueError,
                    "worker input channel closed before input could be sent",
                ):
                    self._provide_current_input(
                        supervisor,
                        launch.session_id,
                        "too late",
                    )

                failed = supervisor.snapshot(launch.session_id)
                self.assertEqual(failed.status, PortableSessionStatus.FAILED)
                self.assertIsNone(failed.input_request)
                self.assertEqual(running.next_supervisor_sequence, 2)
                self.assertNotIn(launch.session_id, supervisor._running)

                process.stdout.send(
                    json.dumps(
                        {
                            "version": 1,
                            "session_id": launch.session_id,
                            "sequence": 2,
                            "kind": stale_kind,
                            "payload": stale_payload,
                        }
                    )
                    + "\n"
                )
                if stale_kind != "COMPLETION":
                    process.stdout.send(
                        json.dumps(
                            {
                                "version": 1,
                                "session_id": launch.session_id,
                                "sequence": 3,
                                "kind": "COMPLETION",
                                "payload": {"exit_code": 0},
                            }
                        )
                        + "\n"
                    )
                process.stdout.close()
                process.stderr.close()
                supervisor.shutdown()

                unchanged = supervisor.snapshot(launch.session_id)
                self.assertEqual(unchanged.status, PortableSessionStatus.FAILED)
                self.assertEqual(unchanged.result, 1)
                self.assertIsNone(unchanged.input_request)
                self.assertEqual(unchanged.progress.stage, "")

    def test_replacement_worker_ignores_buffered_frames_from_retired_generation(
        self,
    ) -> None:
        class BlockingOutput:
            def __init__(self) -> None:
                self._lines: queue.Queue[str | None] = queue.Queue()

            def __iter__(self) -> BlockingOutput:
                return self

            def __next__(self) -> str:
                line = self._lines.get(timeout=5)
                if line is None:
                    raise StopIteration
                return line

            def send(self, line: str) -> None:
                self._lines.put(line)

            def close(self) -> None:
                self._lines.put(None)

        class WorkerInput:
            def __init__(
                self,
                process: FakeWorkerProcess,
                *,
                fail_on_second_flush: bool,
            ) -> None:
                self._process = process
                self._fail_on_second_flush = fail_on_second_flush
                self.flush_count = 0

            def write(self, value: str) -> int:
                return len(value)

            def flush(self) -> None:
                self.flush_count += 1
                if self._fail_on_second_flush and self.flush_count == 2:
                    self._process.return_code = 17
                    raise BrokenPipeError("retired worker closed stdin")

            def close(self) -> None:
                return None

        class FakeWorkerProcess:
            def __init__(self, *, fail_on_second_flush: bool) -> None:
                self.return_code: int | None = None
                self.stdout = BlockingOutput()
                self.stderr = BlockingOutput()
                self.stdin = WorkerInput(
                    self,
                    fail_on_second_flush=fail_on_second_flush,
                )

            def poll(self) -> int | None:
                return self.return_code

            def terminate(self) -> None:
                self.return_code = -15

            def wait(self, timeout: float | None = None) -> int:
                if self.return_code is None:
                    raise subprocess.TimeoutExpired("fake-worker", timeout)
                return self.return_code

        retired_process = FakeWorkerProcess(fail_on_second_flush=True)
        replacement_process = FakeWorkerProcess(fail_on_second_flush=False)
        processes = iter((retired_process, replacement_process))
        supervisor = PortableSessionSupervisor(
            worker_launcher=lambda _launch: next(processes)
        )
        launch = PortableSessionLaunch(
            session_id="session-worker-generation",
            checkout=Path.cwd(),
            operation=PortableWorkflowOperation.PLANNING,
            arguments=(),
        )
        supervisor.start_session(launch)
        retired_stdout_thread = supervisor._threads[-2]
        retired_process.stdout.send(
            json.dumps(
                {
                    "version": 1,
                    "session_id": launch.session_id,
                    "sequence": 1,
                    "kind": "INPUT_REQUEST",
                    "payload": {
                        "request_kind": "TEXT",
                        "prompt": "Retired worker request",
                    },
                }
            )
            + "\n"
        )
        self._wait_for_status(
            supervisor,
            launch.session_id,
            PortableSessionStatus.WAITING_FOR_INPUT,
        )
        with self.assertRaisesRegex(
            ValueError,
            "worker input channel closed before input could be sent",
        ):
            self._provide_current_input(supervisor, launch.session_id, "too late")

        resumed = supervisor.resume_session(launch.session_id)
        self.assertEqual(resumed.status, PortableSessionStatus.RUNNING)
        self.assertIs(
            supervisor._running[launch.session_id].process,
            replacement_process,
        )

        retired_process.stdout.send(
            json.dumps(
                {
                    "version": 1,
                    "session_id": launch.session_id,
                    "sequence": 2,
                    "kind": "STATUS",
                    "payload": {
                        "status": "RUNNING",
                        "stage": "retired generation",
                    },
                }
            )
            + "\n"
        )
        retired_process.stdout.send(
            json.dumps(
                {
                    "version": 1,
                    "session_id": launch.session_id,
                    "sequence": 3,
                    "kind": "COMPLETION",
                    "payload": {"exit_code": 0},
                }
            )
            + "\n"
        )
        retired_stdout_thread.join(timeout=1)
        self.assertFalse(retired_stdout_thread.is_alive())

        after_retired_frames = supervisor.snapshot(launch.session_id)
        self.assertEqual(
            after_retired_frames.status,
            PortableSessionStatus.RUNNING,
        )
        self.assertEqual(after_retired_frames.progress.stage, "")
        self.assertIs(
            supervisor._running[launch.session_id].process,
            replacement_process,
        )

        replacement_process.stdout.send(
            json.dumps(
                {
                    "version": 1,
                    "session_id": launch.session_id,
                    "sequence": 1,
                    "kind": "INPUT_REQUEST",
                    "payload": {
                        "request_kind": "TEXT",
                        "prompt": "Replacement worker request",
                    },
                }
            )
            + "\n"
        )
        waiting = self._wait_for_status(
            supervisor,
            launch.session_id,
            PortableSessionStatus.WAITING_FOR_INPUT,
        )
        self.assertIsNotNone(waiting.input_request)
        assert waiting.input_request is not None
        self.assertEqual(waiting.input_request.prompt, "Replacement worker request")

        replacement_process.stdout.send(
            json.dumps(
                {
                    "version": 1,
                    "session_id": launch.session_id,
                    "sequence": 2,
                    "kind": "COMPLETION",
                    "payload": {"exit_code": 0},
                }
            )
            + "\n"
        )
        completed = supervisor.wait_for_terminal(launch.session_id, timeout=1)
        supervisor.shutdown()
        self.assertEqual(completed.status, PortableSessionStatus.COMPLETED)

    def test_retired_reader_cannot_release_replacement_workers_lease(self) -> None:
        class BlockingOutput:
            def __init__(self) -> None:
                self._lines: queue.Queue[str | None] = queue.Queue()

            def __iter__(self) -> BlockingOutput:
                return self

            def __next__(self) -> str:
                line = self._lines.get(timeout=5)
                if line is None:
                    raise StopIteration
                return line

            def send(self, line: str) -> None:
                self._lines.put(line)

            def close(self) -> None:
                self._lines.put(None)

        class WorkerInput:
            def __init__(
                self,
                process: FakeWorkerProcess,
                *,
                fail_on_second_flush: bool,
            ) -> None:
                self._process = process
                self._fail_on_second_flush = fail_on_second_flush
                self.flush_count = 0

            def write(self, value: str) -> int:
                return len(value)

            def flush(self) -> None:
                self.flush_count += 1
                if self._fail_on_second_flush and self.flush_count == 2:
                    self._process.return_code = 17
                    raise BrokenPipeError("retired worker closed stdin")

            def close(self) -> None:
                return None

        class FakeWorkerProcess:
            def __init__(self, *, fail_on_second_flush: bool) -> None:
                self.return_code: int | None = None
                self.stdout = BlockingOutput()
                self.stderr = BlockingOutput()
                self.stdin = WorkerInput(
                    self,
                    fail_on_second_flush=fail_on_second_flush,
                )

            def poll(self) -> int | None:
                return self.return_code

            def terminate(self) -> None:
                self.return_code = -15

            def wait(self, timeout: float | None = None) -> int:
                if self.return_code is None:
                    raise subprocess.TimeoutExpired("fake-worker", timeout)
                return self.return_code

        class BlockingReleaseCatalog(PortableSessionCatalog):
            def __init__(self, path: Path) -> None:
                super().__init__(path)
                self.release_entered = threading.Event()
                self.allow_release = threading.Event()
                self.block_next_release = True

            def release_worktree_lease(
                self,
                session_id: str,
                *,
                owner_id: str,
            ) -> bool:
                if self.block_next_release:
                    self.block_next_release = False
                    self.release_entered.set()
                    if not self.allow_release.wait(timeout=2):
                        raise TimeoutError("Test did not allow retired lease release.")
                return super().release_worktree_lease(
                    session_id,
                    owner_id=owner_id,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = BlockingReleaseCatalog(root / "catalog.sqlite3")
            retired_process = FakeWorkerProcess(fail_on_second_flush=True)
            replacement_process = FakeWorkerProcess(fail_on_second_flush=False)
            processes = iter((retired_process, replacement_process))
            supervisor = PortableSessionSupervisor(
                worker_launcher=lambda _launch: next(processes),
                catalog=catalog,
                owner_id="same-shell",
            )
            launch = PortableSessionLaunch(
                session_id="session-lease-generation",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            supervisor.start_session(launch)
            retired_process.stdout.send(
                json.dumps(
                    {
                        "version": 1,
                        "session_id": launch.session_id,
                        "sequence": 1,
                        "kind": "INPUT_REQUEST",
                        "payload": {
                            "request_kind": "TEXT",
                            "prompt": "Retired worker request",
                        },
                    }
                )
                + "\n"
            )
            self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.WAITING_FOR_INPUT,
            )
            with self.assertRaisesRegex(
                ValueError,
                "worker input channel closed before input could be sent",
            ):
                self._provide_current_input(
                    supervisor,
                    launch.session_id,
                    "too late",
                )

            retired_process.stdout.close()
            self.assertTrue(catalog.release_entered.wait(timeout=1))
            resumed: list[PortableSessionSnapshot] = []
            resume_thread = threading.Thread(
                target=lambda: resumed.append(
                    supervisor.resume_session(launch.session_id)
                )
            )
            resume_thread.start()
            time.sleep(0.05)
            catalog.allow_release.set()
            resume_thread.join(timeout=1)

            self.assertFalse(resume_thread.is_alive())
            self.assertEqual(resumed[0].status, PortableSessionStatus.RUNNING)
            replacement_lease = catalog.get_worktree_lease(checkout)
            self.assertIsNotNone(replacement_lease)
            assert replacement_lease is not None
            self.assertEqual(replacement_lease.session_id, launch.session_id)
            self.assertIs(
                supervisor._running[launch.session_id].process,
                replacement_process,
            )
            replacement_process.return_code = 0
            replacement_process.stdout.close()
            replacement_process.stderr.close()
            supervisor.shutdown()

    def test_request_identity_rejects_queued_stale_input_for_every_kind(self) -> None:
        worker_source = textwrap.dedent(
            """
            import json
            import sys

            session_id = sys.argv[1]
            request_kind = sys.argv[2]

            def send(sequence, kind, payload):
                print(json.dumps({
                    "version": 1,
                    "session_id": session_id,
                    "sequence": sequence,
                    "kind": kind,
                    "payload": payload,
                }), flush=True)

            json.loads(sys.stdin.readline())
            common = {
                "request_kind": request_kind,
                "options": [["accept", "Accept"], ["deny", "Deny"]],
                "default_key": "deny",
                "cancel_key": "deny",
            }
            send(1, "INPUT_REQUEST", {
                **common,
                "prompt": "Request A",
                "request_id": "request-a",
                "request_generation": 1,
            })
            first = json.loads(sys.stdin.readline())
            assert first["payload"]["request_id"] == "request-a"
            assert first["payload"]["request_generation"] == 1
            send(2, "INPUT_REQUEST", {
                **common,
                "prompt": "Request B",
                "request_id": "request-b",
                "request_generation": 2,
            })
            second = json.loads(sys.stdin.readline())
            assert second["payload"]["request_id"] == "request-b"
            assert second["payload"]["request_generation"] == 2
            send(3, "COMPLETION", {"exit_code": 0})
            """
        )

        for request_kind in ("CHOICE", "APPROVAL", "TEXT"):
            with self.subTest(request_kind=request_kind):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    worker = root / "worker.py"
                    worker.write_text(worker_source, encoding="utf-8")
                    launch = PortableSessionLaunch(
                        session_id=f"request-identity-{request_kind.casefold()}",
                        checkout=root,
                        operation=PortableWorkflowOperation.PLANNING,
                        arguments=(),
                    )

                    def launch_worker(
                        selected: PortableSessionLaunch,
                    ) -> subprocess.Popen[str]:
                        return subprocess.Popen(
                            [
                                sys.executable,
                                "-u",
                                str(worker),
                                selected.session_id,
                                request_kind,
                            ],
                            cwd=selected.checkout,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            encoding="utf-8",
                        )

                    supervisor = PortableSessionSupervisor(
                        worker_launcher=launch_worker
                    )
                    supervisor.start_session(launch)
                    request_a_snapshot = self._wait_for_status(
                        supervisor,
                        launch.session_id,
                        PortableSessionStatus.WAITING_FOR_INPUT,
                    )
                    request_a = request_a_snapshot.input_request
                    assert request_a is not None
                    self.assertEqual(request_a.request_id, "request-a")
                    self.assertEqual(request_a.generation, 1)
                    supervisor.provide_input(
                        launch.session_id,
                        "accept",
                        request_id=request_a.request_id,
                        request_generation=request_a.generation,
                    )
                    request_b_snapshot = self._wait_for_input_prompt(
                        supervisor,
                        launch.session_id,
                        "Request B",
                    )
                    request_b = request_b_snapshot.input_request
                    assert request_b is not None

                    with self.assertRaisesRegex(
                        ValueError,
                        "no longer the current input request",
                    ):
                        supervisor.provide_input(
                            launch.session_id,
                            "queued duplicate",
                            request_id=request_a.request_id,
                            request_generation=request_a.generation,
                        )

                    unchanged = supervisor.snapshot(launch.session_id)
                    self.assertEqual(
                        unchanged.status,
                        PortableSessionStatus.WAITING_FOR_INPUT,
                    )
                    self.assertEqual(unchanged.input_request, request_b)
                    supervisor.provide_input(
                        launch.session_id,
                        "deny",
                        request_id=request_b.request_id,
                        request_generation=request_b.generation,
                    )
                    completed = supervisor.wait_for_terminal(
                        launch.session_id,
                        timeout=5,
                    )
                    supervisor.shutdown()
                    self.assertEqual(
                        completed.status,
                        PortableSessionStatus.COMPLETED,
                        completed.diagnostics,
                    )

    def test_two_workers_route_interleaved_input_and_isolate_one_failure(self) -> None:
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
                "request_kind": "TEXT",
                "prompt": "Value for " + session_id,
            })
            answer = json.loads(sys.stdin.readline())["payload"]["value"]
            if session_id == "session-failing":
                send(2, "FAILURE", {"message": "failed after " + answer})
            else:
                send(2, "ACTIVITY", {"message": "continued with " + answer})
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
            root = Path(directory)
            checkout_a = root / "alpha"
            checkout_b = root / "beta"
            checkout_a.mkdir()
            checkout_b.mkdir()
            supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
            failing = PortableSessionLaunch(
                session_id="session-failing",
                checkout=checkout_a,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            continuing = PortableSessionLaunch(
                session_id="session-continuing",
                checkout=checkout_b,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=(),
            )

            supervisor.start_session(failing)
            supervisor.start_session(continuing)
            self._wait_for_status(
                supervisor,
                failing.session_id,
                PortableSessionStatus.WAITING_FOR_INPUT,
            )
            self._wait_for_status(
                supervisor,
                continuing.session_id,
                PortableSessionStatus.WAITING_FOR_INPUT,
            )

            self._provide_current_input(supervisor, failing.session_id, "alpha-only")
            failed = supervisor.wait_for_terminal(failing.session_id, timeout=5)
            still_waiting = supervisor.snapshot(continuing.session_id)
            self._provide_current_input(
                supervisor,
                continuing.session_id,
                "beta-only",
            )
            completed = supervisor.wait_for_terminal(
                continuing.session_id,
                timeout=5,
            )
            supervisor.shutdown()

        self.assertEqual(failed.status, PortableSessionStatus.FAILED)
        self.assertIn("failed after alpha-only", failed.diagnostics)
        self.assertEqual(
            still_waiting.status,
            PortableSessionStatus.WAITING_FOR_INPUT,
        )
        self.assertEqual(completed.status, PortableSessionStatus.COMPLETED)
        self.assertEqual(completed.activity, ("continued with beta-only",))

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
        self.fail(
            f"Portable session did not reach {expected.value}: "
            f"{supervisor.snapshot(session_id)!r}"
        )

    def _worker_environment(
        self,
        catalog: PortableSessionCatalog,
        owner_id: str,
    ) -> dict[str, str]:
        environment = os.environ.copy()
        environment["DEVLOOP_UI_MODE"] = "application"
        environment["DEVLOOP_PORTABLE_SESSION_CATALOG"] = str(catalog.path)
        environment["DEVLOOP_PORTABLE_SESSION_OWNER_ID"] = owner_id
        source_path = str(Path(cli.__file__).resolve().parents[1])
        existing_python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            source_path
            if not existing_python_path
            else os.pathsep.join((source_path, existing_python_path))
        )
        return environment

    def _wait_for_pid(self, path: Path) -> int:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.is_file():
                try:
                    return int(path.read_text(encoding="utf-8"))
                except ValueError:
                    pass
            time.sleep(0.01)
        self.fail(f"Process identity was not written: {path}")

    def _process_is_alive(self, process_id: int) -> bool:
        if os.name == "nt":
            result = subprocess.run(
                [
                    "tasklist",
                    "/FI",
                    f"PID eq {process_id}",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            return f'"{process_id}"' in result.stdout
        try:
            os.kill(process_id, 0)
        except OSError:
            return False
        return True

    def _wait_for_process_dead(self, process_id: int) -> bool:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not self._process_is_alive(process_id):
                return True
            time.sleep(0.01)
        return False

    def _wait_for_input_prompt(
        self,
        supervisor: PortableSessionSupervisor,
        session_id: str,
        prompt: str,
    ) -> PortableSessionSnapshot:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = supervisor.snapshot(session_id)
            if (
                snapshot.input_request is not None
                and snapshot.input_request.prompt == prompt
            ):
                return snapshot
            time.sleep(0.01)
        self.fail(f"Portable session did not request {prompt!r}.")

    def _provide_current_input(
        self,
        supervisor: PortableSessionSupervisor,
        session_id: str,
        value: str,
    ) -> PortableSessionSnapshot:
        request = supervisor.snapshot(session_id).input_request
        assert request is not None
        return supervisor.provide_input(
            session_id,
            value,
            request_id=request.request_id,
            request_generation=request.generation,
        )


if __name__ == "__main__":
    unittest.main()
