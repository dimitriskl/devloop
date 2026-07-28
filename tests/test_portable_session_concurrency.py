from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from devloop import cli
from devloop.portable_session_catalog import PortableSessionCatalog
from devloop.portable_sessions import (
    PortableSessionLaunch,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
    run_portable_plain_session,
)


class PortableSessionConcurrencyTests(unittest.TestCase):
    def test_new_session_launch_failure_persists_failed_and_releases_capacity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            owner_id = "new-launch-failure-shell"
            launch = PortableSessionLaunch(
                session_id="new-launch-failure",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            supervisor = PortableSessionSupervisor(
                worker_launcher=lambda _launch: (_ for _ in ()).throw(
                    OSError("worker executable is unavailable")
                ),
                catalog=catalog,
                owner_id=owner_id,
            )
            try:
                with self.assertRaisesRegex(
                    OSError,
                    "worker executable is unavailable",
                ):
                    supervisor.start_session(launch)

                snapshot = supervisor.snapshot(launch.session_id)
                durable = catalog.get_session(launch.session_id)

                self.assertEqual(snapshot.status, PortableSessionStatus.FAILED)
                self.assertEqual(durable.status, PortableSessionStatus.FAILED)
                self.assertFalse(
                    catalog.owns_execution_capacity(
                        launch.session_id,
                        owner_id=owner_id,
                    )
                )
                self.assertIsNone(catalog.get_worktree_lease(checkout))
            finally:
                supervisor.shutdown()

    def test_resumed_session_launch_failure_releases_capacity_and_worktree_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            owner_id = "resume-launch-failure-shell"
            launch = PortableSessionLaunch(
                session_id="resume-launch-failure",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            catalog.create_session(launch)
            catalog.update_session_status(
                launch.session_id,
                PortableSessionStatus.PAUSED,
            )
            supervisor = PortableSessionSupervisor(
                worker_launcher=lambda _launch: (_ for _ in ()).throw(
                    OSError("worker executable is unavailable")
                ),
                catalog=catalog,
                owner_id=owner_id,
            )
            try:
                with self.assertRaisesRegex(
                    OSError,
                    "worker executable is unavailable",
                ):
                    supervisor.resume_session(launch.session_id)

                snapshot = supervisor.snapshot(launch.session_id)
                durable = catalog.get_session(launch.session_id)

                self.assertEqual(snapshot.status, PortableSessionStatus.FAILED)
                self.assertEqual(durable.status, PortableSessionStatus.FAILED)
                self.assertFalse(
                    catalog.owns_execution_capacity(
                        launch.session_id,
                        owner_id=owner_id,
                    )
                )
                self.assertIsNone(catalog.get_worktree_lease(checkout))
            finally:
                supervisor.shutdown()

    def test_queued_launch_failure_releases_capacity_to_next_session(
        self,
    ) -> None:
        worker_source = "import sys,time\nsys.stdin.readline()\ntime.sleep(30)\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            catalog.set_concurrency_limit(1)
            owner_id = "queued-launch-failure-shell"
            launched: list[str] = []

            def launch_worker(
                launch: PortableSessionLaunch,
            ) -> subprocess.Popen[str]:
                launched.append(launch.session_id)
                if launch.session_id == "queued-launch-failure":
                    raise OSError("queued worker executable is unavailable")
                return subprocess.Popen(
                    [sys.executable, "-u", "-c", worker_source],
                    cwd=launch.checkout,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    **({} if os.name == "nt" else {"start_new_session": True}),
                )

            launches = []
            for session_id in (
                "capacity-holder",
                "queued-launch-failure",
                "next-after-launch-failure",
            ):
                checkout = root / session_id
                checkout.mkdir()
                launches.append(
                    PortableSessionLaunch(
                        session_id=session_id,
                        checkout=checkout,
                        operation=PortableWorkflowOperation.PLANNING,
                        arguments=(),
                    )
                )

            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=catalog,
                owner_id=owner_id,
            )
            try:
                supervisor.start_session(launches[0])
                self.assertEqual(
                    supervisor.start_session(launches[1]).status,
                    PortableSessionStatus.QUEUED,
                )
                self.assertEqual(
                    supervisor.start_session(launches[2]).status,
                    PortableSessionStatus.QUEUED,
                )

                supervisor.force_stop_session(launches[0].session_id)
                self._wait_for_status(
                    supervisor,
                    launches[2].session_id,
                    PortableSessionStatus.RUNNING,
                )

                failed = catalog.get_session(launches[1].session_id)
                self.assertEqual(failed.status, PortableSessionStatus.FAILED)
                self.assertFalse(
                    catalog.owns_execution_capacity(
                        launches[1].session_id,
                        owner_id=owner_id,
                    )
                )
                self.assertTrue(
                    catalog.owns_execution_capacity(
                        launches[2].session_id,
                        owner_id=owner_id,
                    )
                )
                self.assertIsNone(
                    catalog.get_worktree_lease(launches[1].checkout)
                )
                self.assertEqual(
                    launched,
                    [
                        launches[0].session_id,
                        launches[1].session_id,
                        launches[2].session_id,
                    ],
                )

                supervisor.force_stop_session(launches[2].session_id)
                self.assertFalse(
                    catalog.owns_execution_capacity(
                        launches[2].session_id,
                        owner_id=owner_id,
                    )
                )
            finally:
                supervisor.shutdown()

    def test_checkpoint_failure_reaps_dead_worker_and_releases_capacity(self) -> None:
        worker_source = (
            "import json,sys\n"
            "session_id=sys.argv[1]\n"
            "json.loads(sys.stdin.readline())\n"
            "if session_id == 'checkpoint-failure':\n"
            " print(json.dumps({'version':1,'session_id':session_id,"
            "'sequence':1,'kind':'ACTIVITY','payload':{"
            "'message':'Checkpoint failure worker started'}}),flush=True)\n"
            " command=json.loads(sys.stdin.readline())\n"
            " assert command['kind']=='PAUSE',command\n"
            " print(json.dumps({'version':1,'session_id':session_id,"
            "'sequence':2,'kind':'CHECKPOINT_FAILURE','payload':{"
            "'message':'Durable checkpoint could not be captured',"
            "'action':command['payload']['action'],"
            "'worker_generation':command['payload']['worker_generation'],"
            "'request_id':command['payload']['request_id']}}),flush=True)\n"
            "else:\n"
            " print(json.dumps({'version':1,'session_id':session_id,"
            "'sequence':1,'kind':'COMPLETION','payload':{'exit_code':0}}),"
            "flush=True)\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            catalog.set_concurrency_limit(1)
            owner_id = "checkpoint-failure-shell"

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
                    **(
                        {}
                        if os.name == "nt"
                        else {"start_new_session": True}
                    ),
                )

            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=catalog,
                owner_id=owner_id,
            )
            launches = []
            for session_id in ("checkpoint-failure", "after-checkpoint-failure"):
                checkout = root / session_id
                checkout.mkdir()
                launches.append(
                    PortableSessionLaunch(
                        session_id=session_id,
                        checkout=checkout,
                        operation=PortableWorkflowOperation.PLANNING,
                        arguments=(),
                    )
                )
            try:
                supervisor.start_session(launches[0])
                self._wait_for_status(
                    supervisor,
                    launches[0].session_id,
                    PortableSessionStatus.RUNNING,
                )
                queued = supervisor.start_session(launches[1])
                self.assertEqual(queued.status, PortableSessionStatus.QUEUED)

                supervisor.pause_session(launches[0].session_id)
                interrupted = self._wait_for_status(
                    supervisor,
                    launches[0].session_id,
                    PortableSessionStatus.INTERRUPTED,
                )
                resumed = self._wait_for_status(
                    supervisor,
                    launches[1].session_id,
                    PortableSessionStatus.READY,
                )

                self.assertIn(
                    "Durable checkpoint could not be captured",
                    interrupted.diagnostics,
                )
                self.assertEqual(resumed.result, 0)
                self.assertFalse(
                    catalog.owns_execution_capacity(
                        launches[0].session_id,
                        owner_id=owner_id,
                    )
                )
                self.assertIsNone(
                    catalog.get_worktree_lease(launches[0].checkout)
                )
            finally:
                supervisor.shutdown()

    def test_cooperative_pause_releases_capacity_and_retains_checkout(self) -> None:
        worker_source = textwrap.dedent(
            """
            import sys
            from pathlib import Path

            from devloop import portable_worker
            from devloop.portable_runtime import active_portable_runtime
            from devloop.portable_session_catalog import (
                bind_active_catalog_session_checkout,
            )

            prd_path = Path(sys.argv[2]).resolve()
            issues_index = Path(sys.argv[3]).resolve()

            def wait_for_pause(_operation, _arguments):
                bind_active_catalog_session_checkout(
                    Path.cwd(),
                    prd_path=prd_path,
                    issues_index_path=issues_index,
                )
                bridge = active_portable_runtime()
                assert bridge is not None
                bridge.read_line("Durable PRD checkpoint ready")
                return 0

            portable_worker._run_operation = wait_for_pause
            raise SystemExit(portable_worker.main(["--session-id", sys.argv[1]]))
            """
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            prd_directory = checkout / "prd" / "capacity"
            issues_directory = prd_directory / "issues"
            issues_directory.mkdir(parents=True)
            prd = prd_directory / "capacity.md"
            issues = issues_directory / "README.md"
            issue = issues_directory / "0001-capacity.md"
            prd.write_text("# Capacity\n", encoding="utf-8")
            issues.write_text("- [Capacity](./0001-capacity.md)\n", encoding="utf-8")
            issue.write_text("# Capacity\n\nCompleted: [ ]\n", encoding="utf-8")
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            owner_id = "pause-capacity-shell"

            def launch_worker(
                launch: PortableSessionLaunch,
            ) -> subprocess.Popen[str]:
                environment = os.environ.copy()
                environment["DEVLOOP_UI_MODE"] = "application"
                environment["DEVLOOP_PORTABLE_SESSION_CATALOG"] = str(catalog.path)
                environment["DEVLOOP_PORTABLE_SESSION_OWNER_ID"] = owner_id
                environment["DEVLOOP_PORTABLE_SESSION_ID"] = launch.session_id
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
                        worker_source,
                        launch.session_id,
                        str(prd),
                        str(issues),
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
                session_id="pause-capacity",
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
            self.assertFalse(
                catalog.owns_execution_capacity(
                    launch.session_id,
                    owner_id=owner_id,
                )
            )

            supervisor.pause_session(launch.session_id)
            self._wait_for_status(
                supervisor,
                launch.session_id,
                PortableSessionStatus.PAUSED,
            )
            deadline = time.monotonic() + 5
            while (
                time.monotonic() < deadline
                and catalog.get_worktree_lease(checkout) is not None
            ):
                time.sleep(0.01)
            record = catalog.get_session(launch.session_id)
            supervisor.shutdown()
            owns_capacity_after_pause = catalog.owns_execution_capacity(
                launch.session_id,
                owner_id=owner_id,
            )
            lease_after_pause = catalog.get_worktree_lease(checkout)

        self.assertFalse(owns_capacity_after_pause)
        self.assertIsNone(lease_after_pause)
        self.assertEqual(record.status, PortableSessionStatus.PAUSED)
        self.assertEqual(record.checkout, checkout.resolve())
        self.assertEqual(record.prd_path, prd.resolve())
        self.assertEqual(record.issues_index_path, issues.resolve())

    def test_worker_status_cannot_release_capacity_with_inactive_lifecycle_states(
        self,
    ) -> None:
        worker_source = (
            "import json,sys,time\n"
            "from pathlib import Path\n"
            "session_id,status,root=sys.argv[1:]\n"
            "root=Path(root)\n"
            "json.loads(sys.stdin.readline())\n"
            "if session_id == 'malicious-session':\n"
            " print(json.dumps({'version':1,'session_id':session_id,'sequence':1,"
            "'kind':'STATUS','payload':{'status':status,'stage':'still-live'}}),"
            "flush=True)\n"
            " while not (root / 'release-malicious').exists(): time.sleep(0.01)\n"
            " sequence=2\n"
            "else:\n"
            " (root / 'queued-worker-launched').touch()\n"
            " sequence=1\n"
            "print(json.dumps({'version':1,'session_id':session_id,"
            "'sequence':sequence,'kind':'COMPLETION','payload':{'exit_code':0}}),"
            "flush=True)\n"
        )
        for malicious_status in ("READY", "PAUSED", "WAITING_FOR_INPUT"):
            with (
                self.subTest(status=malicious_status),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
                catalog.set_concurrency_limit(1)
                processes: list[subprocess.Popen[str]] = []

                def launch_worker(
                    launch: PortableSessionLaunch,
                ) -> subprocess.Popen[str]:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-u",
                            "-c",
                            worker_source,
                            launch.session_id,
                            malicious_status,
                            str(root),
                        ],
                        cwd=launch.checkout,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                    )
                    processes.append(process)
                    return process

                supervisor = PortableSessionSupervisor(
                    worker_launcher=launch_worker,
                    catalog=catalog,
                    owner_id=f"malicious-{malicious_status.lower()}",
                )
                launches = []
                for session_id in ("malicious-session", "queued-session"):
                    checkout = root / session_id
                    checkout.mkdir()
                    launches.append(
                        PortableSessionLaunch(
                            session_id=session_id,
                            checkout=checkout,
                            operation=PortableWorkflowOperation.PLANNING,
                            arguments=(),
                        )
                    )
                try:
                    supervisor.start_session(launches[0])
                    queued = supervisor.start_session(launches[1])

                    self.assertEqual(queued.status, PortableSessionStatus.QUEUED)
                    time.sleep(0.4)
                    self.assertFalse((root / "queued-worker-launched").exists())
                    self.assertIsNone(processes[0].poll())
                    self.assertEqual(
                        supervisor.snapshot("malicious-session").status,
                        PortableSessionStatus.RUNNING,
                    )
                finally:
                    (root / "release-malicious").touch()
                    supervisor.shutdown()

    def test_separate_processes_cannot_exceed_one_machine_slot(self) -> None:
        worker_source = (
            "import sys,time\n"
            "from pathlib import Path\n"
            "from devloop.portable_session_catalog import PortableSessionCatalog\n"
            "database,session_id,owner_id,start,result=sys.argv[1:]\n"
            "while not Path(start).exists(): time.sleep(0.005)\n"
            "granted=PortableSessionCatalog(Path(database))."
            "request_execution_capacity(session_id,owner_id=owner_id)\n"
            "Path(result).write_text(str(granted),encoding='utf-8')\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            catalog.set_concurrency_limit(1)
            launches = []
            owners = ("process-a", "process-b")
            for index, owner in enumerate(owners):
                checkout = root / f"process-checkout-{index}"
                checkout.mkdir()
                launch = PortableSessionLaunch(
                    session_id=f"process-session-{index}",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
                launches.append(launch)
                catalog.create_session_with_lease(
                    launch,
                    owner_id=owner,
                    process_id=200 + index,
                )
            start = root / "start"
            results = (root / "result-a", root / "result-b")
            subprocess_environment = os.environ.copy()
            source_root = str(Path(__file__).resolve().parents[1] / "src")
            existing_python_path = subprocess_environment.get("PYTHONPATH")
            subprocess_environment["PYTHONPATH"] = os.pathsep.join(
                path
                for path in (source_root, existing_python_path)
                if path
            )
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        worker_source,
                        str(catalog.path),
                        launches[index].session_id,
                        owners[index],
                        str(start),
                        str(results[index]),
                    ],
                    cwd=root,
                    env=subprocess_environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                for index in range(2)
            ]
            start.touch()
            outputs = [process.communicate(timeout=10) for process in processes]

            self.assertEqual([process.returncode for process in processes], [0, 0])
            self.assertEqual([stderr for _stdout, stderr in outputs], ["", ""])
            self.assertEqual(
                sorted(result.read_text(encoding="utf-8") for result in results),
                ["False", "True"],
            )

    def test_new_catalog_defaults_to_two_and_updates_limit_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "portable-sessions.sqlite3"
            catalog = PortableSessionCatalog(database)

            self.assertEqual(catalog.get_concurrency_limit(), 2)

            catalog.set_concurrency_limit(3)

            self.assertEqual(
                PortableSessionCatalog(database).get_concurrency_limit(),
                3,
            )

            for invalid in (True, 0, -1, 65, 1.5, "4"):
                with self.subTest(invalid=invalid):
                    with self.assertRaisesRegex(
                        ValueError,
                        "integer from 1 through 64",
                    ):
                        catalog.set_concurrency_limit(invalid)  # type: ignore[arg-type]

            self.assertEqual(catalog.get_concurrency_limit(), 3)

    def test_capacity_is_machine_wide_fair_and_released_by_inactive_statuses(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            owner_ids = tuple(f"shell-{index}" for index in range(4))
            session_ids = tuple(f"session-{index}" for index in range(4))
            for index, session_id in enumerate(session_ids):
                checkout = root / f"checkout-{index}"
                checkout.mkdir()
                catalog.create_session_with_lease(
                    PortableSessionLaunch(
                        session_id=session_id,
                        checkout=checkout,
                        operation=PortableWorkflowOperation.PLANNING,
                        arguments=(),
                    ),
                    owner_id=owner_ids[index],
                    process_id=100 + index,
                )

            other_process = PortableSessionCatalog(catalog.path)
            self.assertTrue(
                catalog.request_execution_capacity(
                    session_ids[0],
                    owner_id=owner_ids[0],
                    process_id=100,
                )
            )
            self.assertTrue(
                other_process.request_execution_capacity(
                    session_ids[1],
                    owner_id=owner_ids[1],
                    process_id=101,
                )
            )
            self.assertFalse(
                catalog.request_execution_capacity(
                    session_ids[2],
                    owner_id=owner_ids[2],
                    process_id=102,
                )
            )
            self.assertFalse(
                other_process.request_execution_capacity(
                    session_ids[3],
                    owner_id=owner_ids[3],
                    process_id=103,
                )
            )
            self.assertEqual(
                catalog.get_session(session_ids[2]).status,
                PortableSessionStatus.QUEUED,
            )

            catalog.release_execution_capacity(
                session_ids[0],
                owner_id=owner_ids[0],
                status=PortableSessionStatus.PAUSED,
            )
            self.assertFalse(
                other_process.request_execution_capacity(
                    session_ids[3],
                    owner_id=owner_ids[3],
                    process_id=103,
                )
            )
            self.assertTrue(
                catalog.request_execution_capacity(
                    session_ids[2],
                    owner_id=owner_ids[2],
                    process_id=102,
                )
            )

            other_process.set_concurrency_limit(1)
            other_process.release_execution_capacity(
                session_ids[1],
                owner_id=owner_ids[1],
                status=PortableSessionStatus.WAITING_FOR_INPUT,
            )
            self.assertFalse(
                other_process.request_execution_capacity(
                    session_ids[3],
                    owner_id=owner_ids[3],
                    process_id=103,
                )
            )
            catalog.release_execution_capacity(
                session_ids[2],
                owner_id=owner_ids[2],
                status=PortableSessionStatus.COMPLETED,
            )
            self.assertTrue(
                other_process.request_execution_capacity(
                    session_ids[3],
                    owner_id=owner_ids[3],
                    process_id=103,
                )
            )

    def test_cancelled_queued_session_never_launches_after_capacity_returns(
        self,
    ) -> None:
        worker_source = (
            "import json,socket,sys\n"
            "session_id=sys.argv[1]\n"
            "gate_port=int(sys.argv[2])\n"
            "json.loads(sys.stdin.readline())\n"
            "if session_id == 'active-session':\n"
            " with socket.create_connection(('127.0.0.1',gate_port)) as gate:\n"
            "  gate.recv(1)\n"
            "print(json.dumps({'version':1,'session_id':session_id,"
            "'sequence':1,'kind':'COMPLETION','payload':{'exit_code':0}}),"
            "flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            catalog.set_concurrency_limit(1)
            with socket.create_server(("127.0.0.1", 0)) as active_gate:
                active_gate.settimeout(5)
                gate_port = active_gate.getsockname()[1]
                launched: list[str] = []

                def launch_worker(
                    launch: PortableSessionLaunch,
                ) -> subprocess.Popen[str]:
                    launched.append(launch.session_id)
                    return subprocess.Popen(
                        [
                            sys.executable,
                            "-u",
                            "-c",
                            worker_source,
                            launch.session_id,
                            str(gate_port),
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
                    owner_id="queued-cancel-shell",
                )
                launches = []
                for session_id in ("active-session", "cancelled-before-launch"):
                    checkout = root / session_id
                    checkout.mkdir()
                    prd = checkout / "change.md"
                    issues = checkout / "README.md"
                    prd.write_text("# Change\n", encoding="utf-8")
                    issues.write_text("# Issues\n", encoding="utf-8")
                    launches.append(
                        PortableSessionLaunch(
                            session_id=session_id,
                            checkout=checkout,
                            operation=PortableWorkflowOperation.DELIVERY,
                            arguments=("--prd", str(prd), "--issues", str(issues)),
                        )
                    )
                connection = None
                try:
                    supervisor.start_session(launches[0])
                    connection, _address = active_gate.accept()
                    queued = supervisor.start_session(launches[1])
                    self.assertEqual(queued.status, PortableSessionStatus.QUEUED)

                    cancelled = supervisor.cancel_session(launches[1].session_id)
                    connection.sendall(b"x")
                    connection.close()
                    connection = None
                    active = supervisor.wait_for_terminal(
                        launches[0].session_id,
                        timeout=5,
                    )

                    self.assertEqual(active.status, PortableSessionStatus.COMPLETED)
                    self.assertEqual(
                        cancelled.status,
                        PortableSessionStatus.CANCELLED,
                    )
                    self.assertEqual(
                        catalog.get_session(launches[1].session_id).status,
                        PortableSessionStatus.CANCELLED,
                    )
                    self.assertEqual(launched, ["active-session"])
                    self.assertIsNone(
                        catalog.get_worktree_lease(launches[1].checkout)
                    )
                finally:
                    if connection is not None:
                        connection.sendall(b"x")
                        connection.close()
                    supervisor.shutdown()

    def test_requeued_input_cannot_starve_an_older_capacity_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            catalog.set_concurrency_limit(1)
            sessions = (
                ("active-session", "active-shell", 201),
                ("older-session", "older-shell", 202),
                ("input-session", "input-shell", 203),
            )
            for session_id, owner_id, process_id in sessions:
                checkout = root / session_id
                checkout.mkdir()
                catalog.create_session_with_lease(
                    PortableSessionLaunch(
                        session_id=session_id,
                        checkout=checkout,
                        operation=PortableWorkflowOperation.PLANNING,
                        arguments=(),
                    ),
                    owner_id=owner_id,
                    process_id=process_id,
                )

            self.assertTrue(
                catalog.request_execution_capacity(
                    "active-session",
                    owner_id="active-shell",
                    process_id=201,
                )
            )
            self.assertFalse(
                catalog.request_execution_capacity(
                    "older-session",
                    owner_id="older-shell",
                    process_id=202,
                )
            )
            catalog.enqueue_execution_capacity(
                "input-session",
                owner_id="input-shell",
                process_id=203,
            )

            catalog.release_execution_capacity(
                "active-session",
                owner_id="active-shell",
                status=PortableSessionStatus.COMPLETED,
            )
            self.assertFalse(
                catalog.request_execution_capacity(
                    "input-session",
                    owner_id="input-shell",
                    process_id=203,
                )
            )
            self.assertTrue(
                catalog.request_execution_capacity(
                    "older-session",
                    owner_id="older-shell",
                    process_id=202,
                )
            )
            catalog.release_execution_capacity(
                "older-session",
                owner_id="older-shell",
                status=PortableSessionStatus.COMPLETED,
            )
            self.assertTrue(
                catalog.request_execution_capacity(
                    "input-session",
                    owner_id="input-shell",
                    process_id=203,
                )
            )

    def test_supervisor_queues_third_worker_and_starts_it_after_release(self) -> None:
        worker_source = (
            "import json,sys,time\n"
            "from pathlib import Path\n"
            "session_id=sys.argv[1]\n"
            "marker=Path(sys.argv[2]) / session_id\n"
            "json.loads(sys.stdin.readline())\n"
            "while not marker.exists(): time.sleep(0.01)\n"
            "print(json.dumps({'version':1,'session_id':session_id,"
            "'sequence':1,'kind':'COMPLETION','payload':{'exit_code':0}}),"
            "flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launched: list[str] = []

            def launch_worker(
                launch: PortableSessionLaunch,
            ) -> subprocess.Popen[str]:
                launched.append(launch.session_id)
                return subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        worker_source,
                        launch.session_id,
                        str(root),
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
                owner_id="single-shell",
            )
            launches = []
            for index in range(3):
                checkout = root / f"worktree-{index}"
                checkout.mkdir()
                prd = checkout / "change.md"
                issues = checkout / "README.md"
                prd.write_text("# Change\n", encoding="utf-8")
                issues.write_text("# Issues\n", encoding="utf-8")
                launches.append(
                    PortableSessionLaunch(
                        session_id=f"session-{index}",
                        checkout=checkout,
                        operation=PortableWorkflowOperation.DELIVERY,
                        arguments=("--prd", str(prd), "--issues", str(issues)),
                    )
                )

            try:
                first = supervisor.start_session(launches[0])
                second = supervisor.start_session(launches[1])
                third = supervisor.start_session(launches[2])

                self.assertEqual(first.status, PortableSessionStatus.RUNNING)
                self.assertEqual(second.status, PortableSessionStatus.RUNNING)
                self.assertEqual(third.status, PortableSessionStatus.QUEUED)
                self.assertEqual(launched, ["session-0", "session-1"])

                (root / "session-0").touch()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and "session-2" not in launched:
                    time.sleep(0.01)

                self.assertEqual(launched, ["session-0", "session-1", "session-2"])
                self.assertIn(
                    supervisor.snapshot("session-2").status,
                    {PortableSessionStatus.RUNNING, PortableSessionStatus.COMPLETED},
                )

                (root / "session-1").touch()
                (root / "session-2").touch()
                supervisor.wait_for_terminal("session-1", timeout=5)
                supervisor.wait_for_terminal("session-2", timeout=5)
            finally:
                for launch in launches:
                    (root / launch.session_id).touch()
                supervisor.shutdown()

    def test_worker_crash_releases_capacity_to_queued_session(self) -> None:
        worker_source = (
            "import json,sys\n"
            "session_id=sys.argv[1]\n"
            "json.loads(sys.stdin.readline())\n"
            "if session_id == 'crashing-session': raise SystemExit(17)\n"
            "print(json.dumps({'version':1,'session_id':session_id,"
            "'sequence':1,'kind':'COMPLETION','payload':{'exit_code':0}}),"
            "flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            catalog.set_concurrency_limit(1)

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
                owner_id="crash-shell",
            )
            launches = []
            for session_id in ("crashing-session", "queued-after-crash"):
                checkout = root / session_id
                checkout.mkdir()
                prd = checkout / "change.md"
                issues = checkout / "README.md"
                prd.write_text("# Change\n", encoding="utf-8")
                issues.write_text("# Issues\n", encoding="utf-8")
                launches.append(
                    PortableSessionLaunch(
                        session_id=session_id,
                        checkout=checkout,
                        operation=PortableWorkflowOperation.DELIVERY,
                        arguments=("--prd", str(prd), "--issues", str(issues)),
                    )
                )
            try:
                supervisor.start_session(launches[0])
                queued = supervisor.start_session(launches[1])
                self.assertEqual(queued.status, PortableSessionStatus.QUEUED)

                completed = supervisor.wait_for_terminal(
                    "queued-after-crash",
                    timeout=5,
                )

                self.assertEqual(completed.status, PortableSessionStatus.COMPLETED)
                self.assertEqual(
                    supervisor.snapshot("crashing-session").status,
                    PortableSessionStatus.FAILED,
                )
            finally:
                supervisor.shutdown()

    def test_restart_keeps_previously_queued_session_passive_until_resume(
        self,
    ) -> None:
        worker_source = (
            "import json,socket,sys\n"
            "session_id=sys.argv[1]\n"
            "gate_port=int(sys.argv[2])\n"
            "json.loads(sys.stdin.readline())\n"
            "if session_id == 'active-session':\n"
            " with socket.create_connection(('127.0.0.1',gate_port)) as gate:\n"
            "  gate.recv(1)\n"
            "print(json.dumps({'version':1,'session_id':session_id,"
            "'sequence':1,'kind':'COMPLETION','payload':{'exit_code':0}}),"
            "flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            catalog.set_concurrency_limit(1)
            with socket.create_server(("127.0.0.1", 0)) as active_gate:
                active_gate.settimeout(5)
                gate_port = active_gate.getsockname()[1]
                first_launches: list[str] = []

                def launch_first_worker(
                    launch: PortableSessionLaunch,
                ) -> subprocess.Popen[str]:
                    first_launches.append(launch.session_id)
                    return subprocess.Popen(
                        [
                            sys.executable,
                            "-u",
                            "-c",
                            worker_source,
                            launch.session_id,
                            str(gate_port),
                        ],
                        cwd=launch.checkout,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                    )

                first = PortableSessionSupervisor(
                    worker_launcher=launch_first_worker,
                    catalog=catalog,
                    owner_id="before-restart-shell",
                )
                launches = []
                for session_id in ("active-session", "passive-after-restart"):
                    checkout = root / session_id
                    checkout.mkdir()
                    prd = checkout / "change.md"
                    issues = checkout / "README.md"
                    prd.write_text("# Change\n", encoding="utf-8")
                    issues.write_text("# Issues\n", encoding="utf-8")
                    launches.append(
                        PortableSessionLaunch(
                            session_id=session_id,
                            checkout=checkout,
                            operation=PortableWorkflowOperation.DELIVERY,
                            arguments=("--prd", str(prd), "--issues", str(issues)),
                        )
                    )
                connection = None
                try:
                    first.start_session(launches[0])
                    connection, _address = active_gate.accept()
                    queued = first.start_session(launches[1])
                    self.assertEqual(queued.status, PortableSessionStatus.QUEUED)
                    paused = first.pause_session(launches[1].session_id)
                    self.assertEqual(paused.status, PortableSessionStatus.PAUSED)
                    connection.sendall(b"x")
                    connection.close()
                    connection = None
                    first.wait_for_terminal(launches[0].session_id, timeout=5)
                finally:
                    if connection is not None:
                        connection.sendall(b"x")
                        connection.close()
                    first.shutdown()

                restarted_launches: list[str] = []

                def launch_restarted_worker(
                    launch: PortableSessionLaunch,
                ) -> subprocess.Popen[str]:
                    restarted_launches.append(launch.session_id)
                    return subprocess.Popen(
                        [
                            sys.executable,
                            "-u",
                            "-c",
                            worker_source,
                            launch.session_id,
                            str(gate_port),
                        ],
                        cwd=launch.checkout,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                    )

                restarted = PortableSessionSupervisor(
                    worker_launcher=launch_restarted_worker,
                    catalog=PortableSessionCatalog(catalog.path),
                    owner_id="after-restart-shell",
                )
                try:
                    self.assertEqual(
                        restarted.snapshot(launches[1].session_id).status,
                        PortableSessionStatus.PAUSED,
                    )
                    self.assertEqual(restarted_launches, [])

                    resumed = restarted.resume_session(launches[1].session_id)
                    completed = restarted.wait_for_terminal(
                        launches[1].session_id,
                        timeout=5,
                    )

                    self.assertEqual(
                        resumed.status,
                        PortableSessionStatus.RUNNING,
                    )
                    self.assertEqual(
                        completed.status,
                        PortableSessionStatus.COMPLETED,
                    )
                    self.assertEqual(
                        restarted_launches,
                        ["passive-after-restart"],
                    )
                finally:
                    restarted.shutdown()

    def test_stale_catalog_read_cannot_restore_running_after_crash(self) -> None:
        worker_source = (
            "import json,socket,sys\n"
            "session_id=sys.argv[1]\n"
            "crash_port=int(sys.argv[2])\n"
            "json.loads(sys.stdin.readline())\n"
            "if session_id == 'crashing-session':\n"
            " with socket.create_connection(('127.0.0.1',crash_port)) as gate:\n"
            "  gate.recv(1)\n"
            " raise SystemExit(17)\n"
            "print(json.dumps({'version':1,'session_id':session_id,"
            "'sequence':1,'kind':'COMPLETION','payload':{'exit_code':0}}),"
            "flush=True)\n"
        )

        class BarrierCatalog(PortableSessionCatalog):
            def __init__(self, path: Path) -> None:
                super().__init__(path)
                self.arm_stale_read = threading.Event()
                self.stale_read_captured = threading.Event()
                self.release_stale_read = threading.Event()
                self.stale_read_returned = threading.Event()
                self.allow_fresh_read = threading.Event()
                self.crash_ownership_released = threading.Event()

            def list_sessions(self):
                records = super().list_sessions()
                scheduler_read = threading.current_thread().name.startswith(
                    "portable-session-capacity-"
                )
                statuses = {
                    record.session_id: record.status for record in records
                }
                if (
                    scheduler_read
                    and self.arm_stale_read.is_set()
                    and not self.stale_read_captured.is_set()
                    and statuses.get("crashing-session")
                    is PortableSessionStatus.RUNNING
                    and statuses.get("queued-after-crash")
                    is PortableSessionStatus.QUEUED
                ):
                    self.stale_read_captured.set()
                    if not self.release_stale_read.wait(timeout=5):
                        raise AssertionError("Stale catalog read was not released.")
                    self.stale_read_returned.set()
                elif scheduler_read and self.stale_read_returned.is_set():
                    if not self.allow_fresh_read.wait(timeout=5):
                        raise AssertionError("Fresh catalog read was not released.")
                return records

            def release_worktree_lease(
                self,
                session_id: str,
                *,
                owner_id: str,
            ) -> bool:
                released = super().release_worktree_lease(
                    session_id,
                    owner_id=owner_id,
                )
                if session_id == "crashing-session" and released:
                    self.crash_ownership_released.set()
                return released

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = BarrierCatalog(root / "portable-sessions.sqlite3")
            catalog.set_concurrency_limit(1)
            with socket.create_server(("127.0.0.1", 0)) as crash_gate:
                crash_gate.settimeout(5)
                crash_port = crash_gate.getsockname()[1]

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
                            str(crash_port),
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
                    owner_id="crash-order-shell",
                )
                launches = []
                for session_id in ("crashing-session", "queued-after-crash"):
                    checkout = root / session_id
                    checkout.mkdir()
                    prd = checkout / "change.md"
                    issues = checkout / "README.md"
                    prd.write_text("# Change\n", encoding="utf-8")
                    issues.write_text("# Issues\n", encoding="utf-8")
                    launches.append(
                        PortableSessionLaunch(
                            session_id=session_id,
                            checkout=checkout,
                            operation=PortableWorkflowOperation.DELIVERY,
                            arguments=("--prd", str(prd), "--issues", str(issues)),
                        )
                    )
                try:
                    supervisor.start_session(launches[0])
                    queued = supervisor.start_session(launches[1])
                    self.assertEqual(queued.status, PortableSessionStatus.QUEUED)

                    catalog.arm_stale_read.set()
                    self.assertTrue(catalog.stale_read_captured.wait(timeout=5))
                    connection, _address = crash_gate.accept()
                    with connection:
                        connection.sendall(b"x")
                    self.assertTrue(
                        catalog.crash_ownership_released.wait(timeout=5)
                    )
                    self.assertEqual(
                        catalog.get_session("crashing-session").status,
                        PortableSessionStatus.FAILED,
                    )

                    catalog.release_stale_read.set()
                    completed = supervisor.wait_for_terminal(
                        "queued-after-crash",
                        timeout=5,
                    )

                    self.assertEqual(
                        completed.status,
                        PortableSessionStatus.COMPLETED,
                    )
                    self.assertEqual(
                        supervisor.snapshot("crashing-session").status,
                        PortableSessionStatus.FAILED,
                    )
                finally:
                    catalog.release_stale_read.set()
                    catalog.allow_fresh_read.set()
                    supervisor.shutdown()

    def test_waiting_session_requeues_input_until_capacity_returns(self) -> None:
        worker_source = (
            "import json,sys,time\n"
            "from pathlib import Path\n"
            "session_id=sys.argv[1]\n"
            "marker=Path(sys.argv[2]) / session_id\n"
            "json.loads(sys.stdin.readline())\n"
            "if session_id == 'session-input':\n"
            " print(json.dumps({'version':1,'session_id':session_id,"
            "'sequence':1,'kind':'INPUT_REQUEST','payload':{"
            "'request_kind':'TEXT','prompt':'Continue','options':[],"
            "'default_key':'','cancel_key':None,'request_id':'input-1',"
            "'request_generation':1}}),flush=True)\n"
            " answer=json.loads(sys.stdin.readline())['payload']['value']\n"
            " assert answer == 'approved'\n"
            "else:\n"
            " while not marker.exists(): time.sleep(0.01)\n"
            "print(json.dumps({'version':1,'session_id':session_id,"
            "'sequence':2 if session_id == 'session-input' else 1,"
            "'kind':'COMPLETION','payload':{'exit_code':0}}),flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            catalog.set_concurrency_limit(1)

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
                        str(root),
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
                owner_id="input-shell",
            )
            launches = []
            for session_id in ("session-input", "session-busy"):
                checkout = root / session_id
                checkout.mkdir()
                prd = checkout / "change.md"
                issues = checkout / "README.md"
                prd.write_text("# Change\n", encoding="utf-8")
                issues.write_text("# Issues\n", encoding="utf-8")
                launches.append(
                    PortableSessionLaunch(
                        session_id=session_id,
                        checkout=checkout,
                        operation=PortableWorkflowOperation.DELIVERY,
                        arguments=("--prd", str(prd), "--issues", str(issues)),
                    )
                )
            try:
                supervisor.start_session(launches[0])
                waiting = self._wait_for_status(
                    supervisor,
                    "session-input",
                    PortableSessionStatus.WAITING_FOR_INPUT,
                )
                supervisor.start_session(launches[1])
                request = waiting.input_request
                assert request is not None

                queued = supervisor.provide_input(
                    "session-input",
                    "approved",
                    request_id=request.request_id,
                    request_generation=request.generation,
                )

                self.assertEqual(queued.status, PortableSessionStatus.QUEUED)
                self.assertEqual(
                    supervisor.snapshot("session-busy").status,
                    PortableSessionStatus.RUNNING,
                )

                (root / "session-busy").touch()
                completed = supervisor.wait_for_terminal(
                    "session-input",
                    timeout=5,
                )
                self.assertEqual(completed.status, PortableSessionStatus.COMPLETED)
            finally:
                (root / "session-busy").touch()
                supervisor.shutdown()

    def test_second_supervisor_observes_queued_catalog_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launched_processes: list[subprocess.Popen[str]] = []

            def launch_worker(
                launch: PortableSessionLaunch,
            ) -> subprocess.Popen[str]:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        "import json,sys,time; json.loads(sys.stdin.readline()); "
                        "time.sleep(60)",
                    ],
                    cwd=launch.checkout,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                launched_processes.append(process)
                return process

            active = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=catalog,
                owner_id="active-shell",
            )
            observer = PortableSessionSupervisor(
                worker_launcher=lambda _launch: self.fail("Observer launched a worker"),
                catalog=PortableSessionCatalog(catalog.path),
                owner_id="observer-shell",
            )
            try:
                for index in range(3):
                    checkout = root / f"observed-{index}"
                    checkout.mkdir()
                    active.start_session(
                        PortableSessionLaunch(
                            session_id=f"observed-session-{index}",
                            checkout=checkout,
                            operation=PortableWorkflowOperation.PLANNING,
                            arguments=(),
                        )
                    )

                deadline = time.monotonic() + 5
                observed = {}
                while time.monotonic() < deadline:
                    observed = {
                        snapshot.session_id: snapshot
                        for snapshot in observer.list_sessions()
                    }
                    if (
                        observed.get("observed-session-2") is not None
                        and observed["observed-session-2"].status
                        is PortableSessionStatus.QUEUED
                    ):
                        break
                    time.sleep(0.01)

                self.assertEqual(
                    observed["observed-session-2"].status,
                    PortableSessionStatus.QUEUED,
                )
            finally:
                active.shutdown()
                observer.shutdown()

    def test_former_owner_observes_later_updates_from_another_supervisor(self) -> None:
        completing_worker = (
            "import json,sys\n"
            "session_id=sys.argv[1]\n"
            "json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'version':1,'session_id':session_id,'sequence':1,"
            "'kind':'COMPLETION','payload':{'exit_code':0}}),flush=True)\n"
        )
        resumed_worker = (
            "import json,sys,time\n"
            "from pathlib import Path\n"
            "session_id,release=sys.argv[1:]\n"
            "json.loads(sys.stdin.readline())\n"
            "while not Path(release).exists(): time.sleep(0.01)\n"
            "print(json.dumps({'version':1,'session_id':session_id,'sequence':1,"
            "'kind':'COMPLETION','payload':{'exit_code':0}}),flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            checkout = root / "former-owner"
            checkout.mkdir()
            prd = checkout / "change.md"
            issues = checkout / "README.md"
            prd.write_text("# Change\n", encoding="utf-8")
            issues.write_text("# Issues\n", encoding="utf-8")
            launch = PortableSessionLaunch(
                session_id="former-owner-session",
                checkout=checkout,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=("--prd", str(prd), "--issues", str(issues)),
            )
            release = root / "release-resumed"

            def start_process(source: str, *arguments: str) -> subprocess.Popen[str]:
                return subprocess.Popen(
                    [sys.executable, "-u", "-c", source, *arguments],
                    cwd=checkout,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )

            former_owner = PortableSessionSupervisor(
                worker_launcher=lambda item: start_process(
                    completing_worker,
                    item.session_id,
                ),
                catalog=catalog,
                owner_id="former-owner-shell",
            )
            later_owner: PortableSessionSupervisor | None = None
            try:
                former_owner.start_session(launch)
                completed = former_owner.wait_for_terminal(
                    launch.session_id,
                    timeout=5,
                )
                self.assertEqual(completed.status, PortableSessionStatus.COMPLETED)
                deadline = time.monotonic() + 5
                while (
                    time.monotonic() < deadline
                    and catalog.get_worktree_lease(checkout) is not None
                ):
                    time.sleep(0.01)
                self.assertIsNone(catalog.get_worktree_lease(checkout))

                later_owner = PortableSessionSupervisor(
                    worker_launcher=lambda item: start_process(
                        resumed_worker,
                        item.session_id,
                        str(release),
                    ),
                    catalog=PortableSessionCatalog(catalog.path),
                    owner_id="later-owner-shell",
                )
                resumed = later_owner.resume_session(launch.session_id)
                self.assertEqual(resumed.status, PortableSessionStatus.RUNNING)

                observed = self._wait_for_status(
                    former_owner,
                    launch.session_id,
                    PortableSessionStatus.RUNNING,
                )
                self.assertEqual(observed.status, PortableSessionStatus.RUNNING)
            finally:
                release.touch()
                if later_owner is not None:
                    later_owner.shutdown()
                former_owner.shutdown()

    def test_plain_mode_waits_for_the_same_machine_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            catalog.set_concurrency_limit(1)
            busy_checkout = root / "busy"
            plain_checkout = root / "plain"
            busy_checkout.mkdir()
            plain_checkout.mkdir()
            busy = PortableSessionLaunch(
                session_id="busy-session",
                checkout=busy_checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            plain = PortableSessionLaunch(
                session_id="plain-session",
                checkout=plain_checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=("--repo", str(plain_checkout)),
            )
            catalog.create_session_with_lease(busy, owner_id="busy-shell")
            self.assertTrue(
                catalog.request_execution_capacity(
                    busy.session_id,
                    owner_id="busy-shell",
                )
            )
            notices: list[str] = []
            operation_started = threading.Event()
            result: list[int] = []

            def operation() -> int:
                operation_started.set()
                return 7

            thread = threading.Thread(
                target=lambda: result.append(
                    run_portable_plain_session(
                        plain,
                        operation,
                        catalog=PortableSessionCatalog(catalog.path),
                        owner_id="plain-process",
                        queue_notice=notices.append,
                        poll_interval=0.01,
                    )
                )
            )
            thread.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not notices:
                time.sleep(0.01)

            self.assertEqual(notices, ["Portable session plain-session [QUEUED]"])
            self.assertFalse(operation_started.is_set())

            catalog.release_execution_capacity(
                busy.session_id,
                owner_id="busy-shell",
                status=PortableSessionStatus.PAUSED,
            )
            thread.join(timeout=5)

            self.assertFalse(thread.is_alive())
            self.assertTrue(operation_started.is_set())
            self.assertEqual(result, [7])
            self.assertEqual(
                catalog.get_session(plain.session_id).status,
                PortableSessionStatus.FAILED,
            )

    def _wait_for_status(
        self,
        supervisor: PortableSessionSupervisor,
        session_id: str,
        expected: PortableSessionStatus,
    ):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = supervisor.snapshot(session_id)
            if snapshot.status is expected:
                return snapshot
            time.sleep(0.01)
        self.fail(f"Portable session did not reach {expected.value}.")


if __name__ == "__main__":
    unittest.main()
