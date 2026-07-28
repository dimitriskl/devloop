from __future__ import annotations

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
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
    run_portable_plain_session,
)


class PortableSessionConcurrencyTests(unittest.TestCase):
    def test_cooperative_pause_releases_capacity_and_retains_checkout(self) -> None:
        worker_source = (
            "import json,sys\n"
            "session_id=sys.argv[1]\n"
            "json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'version':1,'session_id':session_id,"
            "'sequence':1,'kind':'ACTIVITY','payload':{"
            "'message':'Capacity is active'}}),flush=True)\n"
            "command=json.loads(sys.stdin.readline())\n"
            "assert command['kind']=='PAUSE',command\n"
            "print(json.dumps({'version':1,'session_id':session_id,"
            "'sequence':2,'kind':'CHECKPOINT','payload':{"
            "'summary':'Capacity-safe checkpoint'}}),flush=True)\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            owner_id = "pause-capacity-shell"

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
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if supervisor.snapshot(launch.session_id).activity:
                    break
                time.sleep(0.01)
            else:
                self.fail("Active worker did not publish activity.")
            self.assertTrue(
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
