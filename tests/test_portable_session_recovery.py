from __future__ import annotations

import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from devloop import interactive_runner, subprocess_utils
from devloop.codex_runner import RoleResult
from devloop.issue_pack import Issue
from devloop.issue_scheduler import SchedulingPhase
from devloop.portable_session_catalog import (
    PortablePlanningSettings,
    PortableSessionCatalog,
)
from devloop.portable_sessions import (
    PortableCheckpointKind,
    PortablePartialWorkContext,
    PortableRecoveryData,
    PortableSessionLaunch,
    PortableSessionSnapshot,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
    PortableWorktreeLeaseConflict,
)
from devloop.portable_worker import (
    PortableWorkerHeartbeatEmitter,
    PortableWorkerRuntimeBridge,
    _run_operation,
)
from devloop.state import LoopStateWriter
from devloop.subprocess_utils import (
    ProcessIdentity,
    ProcessTreeIdentity,
    ProcessTreeKind,
    ProcessTreeState,
    capture_process_identity,
    process_identity_state,
    process_tree_creation_kwargs,
    terminate_process,
)


_APPLICATION_DEATH_WORKER_SOURCE = (
    "import json,os,subprocess,sys,time\n"
    "from pathlib import Path\n"
    "from devloop.subprocess_utils import capture_process_identity\n"
    "def publish(path,payload):\n"
    " temporary=path.with_name(f'.{path.name}.{os.getpid()}.tmp')\n"
    " temporary.write_text(json.dumps(payload),encoding='utf-8')\n"
    " os.replace(temporary,path)\n"
    "control=Path(sys.argv[1]);start=Path(sys.argv[2]);ready=Path(sys.argv[3])\n"
    "root=capture_process_identity()\n"
    "publish(control,{'worker':[root.pid,root.creation_time]})\n"
    "while not start.exists():time.sleep(.01)\n"
    "child=subprocess.Popen([sys.executable,'-u','-c','import time;time.sleep(60)'])\n"
    "descendant=capture_process_identity(child.pid)\n"
    "publish(ready,{'worker':[root.pid,root.creation_time],"
    "'descendant':[descendant.pid,descendant.creation_time]})\n"
    "time.sleep(60)\n"
)

_APPLICATION_DEATH_APP_SOURCE = (
    "import os,sys,time\n"
    "from pathlib import Path\n"
    "from devloop.portable_session_catalog import PortablePlanningSettings,"
    "PortableSessionCatalog\n"
    "from devloop.portable_sessions import PortableSessionLaunch,"
    "PortableSessionStatus,PortableWorkflowOperation\n"
    "from devloop.subprocess_utils import capture_process_identity,"
    "capture_process_tree_identity,launch_process_tree\n"
    "root=Path(sys.argv[1]);checkout=Path(sys.argv[2])\n"
    "session_id=sys.argv[3];owner_id=sys.argv[4]\n"
    "ready=Path(sys.argv[5]);control=Path(sys.argv[6]);start=Path(sys.argv[7])\n"
    "exit_now=None if sys.argv[8]=='-' else Path(sys.argv[8])\n"
    "owner=capture_process_identity()\n"
    "catalog=PortableSessionCatalog(root/'portable-sessions.sqlite3')\n"
    "settings=PortablePlanningSettings(backend='CODEX_CLI',model='gpt-5.4',"
    "reasoning_effort='high',fast='OFF',timeout_seconds=1200,"
    "checkpoint_seconds=300)\n"
    "catalog.create_session_with_lease(PortableSessionLaunch(session_id,checkout,"
    "PortableWorkflowOperation.PLANNING,()),owner_id=owner_id,"
    "process_identity=owner,planning_settings=settings)\n"
    "catalog.save_planning_thread(session_id,"
    "'11111111-2222-4333-8444-555555555555')\n"
    "catalog.update_session_status(session_id,PortableSessionStatus.RUNNING,"
    "activity_summary='Durable planning checkpoint')\n"
    "worker=launch_process_tree([sys.executable,'-u','-c',sys.argv[9],"
    "str(control),str(start),str(ready)],cwd=checkout)\n"
    "catalog.bind_worktree_lease_worker(session_id,owner_id=owner_id,"
    "owner_process_identity=owner,"
    "process_tree_identity=capture_process_tree_identity(worker),"
    "worker_generation=1)\n"
    "while not ready.exists():time.sleep(.01)\n"
    "while exit_now is not None and not exit_now.exists():time.sleep(.01)\n"
    "os._exit(23)\n"
)

_CATALOG_LEASE_OWNER_SOURCE = (
    "import json,os,sys,time\n"
    "from pathlib import Path\n"
    "from devloop.portable_session_catalog import PortableSessionCatalog\n"
    "from devloop.portable_sessions import PortableSessionLaunch,PortableWorkflowOperation\n"
    "from devloop.subprocess_utils import capture_process_identity\n"
    "def publish(path,payload):\n"
    " temporary=path.with_name(f'.{path.name}.{os.getpid()}.tmp')\n"
    " temporary.write_text(json.dumps(payload),encoding='utf-8')\n"
    " os.replace(temporary,path)\n"
    "catalog=PortableSessionCatalog(Path(sys.argv[1]),lease_timeout_seconds=float(sys.argv[7]))\n"
    "checkout=Path(sys.argv[2]);session_id=sys.argv[3];owner_id=sys.argv[4]\n"
    "ready=Path(sys.argv[5]);stop=Path(sys.argv[6]);identity=capture_process_identity()\n"
    "generation=None if sys.argv[8]=='-' else int(sys.argv[8])\n"
    "catalog.create_session_with_lease(PortableSessionLaunch(session_id,checkout,"
    "PortableWorkflowOperation.PLANNING,()),owner_id=owner_id,"
    "process_identity=identity,worker_generation=generation)\n"
    "publish(ready,{'pid':identity.pid,'creation_time':identity.creation_time})\n"
    "while not stop.exists():time.sleep(.01)\n"
)

_CATALOG_LEASE_CONTENDER_SOURCE = (
    "import json,os,sys,time\n"
    "from pathlib import Path\n"
    "from devloop.portable_session_catalog import PortableSessionCatalog\n"
    "from devloop.portable_sessions import PortableWorktreeLeaseConflict\n"
    "from devloop.subprocess_utils import capture_process_identity\n"
    "def publish(path,payload):\n"
    " temporary=path.with_name(f'.{path.name}.{os.getpid()}.tmp')\n"
    " temporary.write_text(json.dumps(payload),encoding='utf-8')\n"
    " os.replace(temporary,path)\n"
    "catalog=PortableSessionCatalog(Path(sys.argv[1]),lease_timeout_seconds=float(sys.argv[6]))\n"
    "session_id=sys.argv[2];owner_id=sys.argv[3];result=Path(sys.argv[4]);gate=sys.argv[5]\n"
    "while gate!='-' and not Path(gate).exists():time.sleep(.005)\n"
    "identity=capture_process_identity()\n"
    "try:\n"
    " lease=catalog.acquire_session_lease(session_id,owner_id=owner_id,"
    "process_identity=identity)\n"
    " payload={'outcome':'acquired','pid':identity.pid,'creation_time':"
    "identity.creation_time,'session_id':lease.session_id}\n"
    "except PortableWorktreeLeaseConflict as error:\n"
    " payload={'outcome':'blocked','pid':identity.pid,'creation_time':"
    "identity.creation_time,'detail':str(error)}\n"
    "publish(result,payload)\n"
)

_PROCESS_IDENTITY_HOLDER_SOURCE = (
    "import json,os,sys,time\n"
    "from pathlib import Path\n"
    "from devloop.subprocess_utils import capture_process_identity\n"
    "ready=Path(sys.argv[1]);stop=None if sys.argv[2]=='-' else Path(sys.argv[2])\n"
    "identity=capture_process_identity();temporary=ready.with_name(f'.{ready.name}.{os.getpid()}.tmp')\n"
    "temporary.write_text(json.dumps({'pid':identity.pid,'creation_time':"
    "identity.creation_time}),encoding='utf-8');os.replace(temporary,ready)\n"
    "while stop is not None and not stop.exists():time.sleep(.01)\n"
)


class PortableSessionRecoveryTests(unittest.TestCase):
    def test_restarted_supervisor_explicitly_recovers_original_running_session(
        self,
    ) -> None:
        worker_source = (
            "import json,sys\n"
            "start=json.loads(sys.stdin.readline())\n"
            "open('resume-frame.json','w',encoding='utf-8').write(json.dumps(start))\n"
            "session_id=start['session_id']\n"
            "print(json.dumps({'version':1,'session_id':session_id,'sequence':1,"
            "'kind':'HELLO','payload':{}}),flush=True)\n"
            "print(json.dumps({'version':1,'session_id':session_id,'sequence':2,"
            "'kind':'COMPLETION','payload':{'exit_code':0}}),flush=True)\n"
        )
        now = [100.0]
        original_owner = ProcessIdentity(pid=4101, creation_time=5101)
        original_worker = ProcessIdentity(pid=4201, creation_time=5201)
        original_tree = ProcessTreeIdentity(
            root=original_worker,
            kind=ProcessTreeKind.ROOT_PROCESS,
            tree_id=original_worker.pid,
        )
        probed: list[tuple[ProcessTreeIdentity, ProcessIdentity]] = []

        def confirmed_dead_probe(
            tree: ProcessTreeIdentity,
            owner: ProcessIdentity,
        ) -> ProcessTreeState:
            probed.append((tree, owner))
            return ProcessTreeState.STOPPED

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(
                root / "portable-sessions.sqlite3",
                clock=lambda: now[0],
                process_tree_probe=confirmed_dead_probe,
                lease_timeout_seconds=10,
            )
            launch = PortableSessionLaunch(
                "original-session",
                checkout,
                PortableWorkflowOperation.PLANNING,
                (),
            )
            settings = PortablePlanningSettings(
                backend="CODEX_CLI",
                model="gpt-5.4",
                reasoning_effort="high",
                fast="OFF",
                timeout_seconds=1200,
                checkpoint_seconds=300,
            )
            catalog.create_session_with_lease(
                launch,
                owner_id="crashed-application",
                process_identity=original_owner,
                planning_settings=settings,
            )
            catalog.bind_worktree_lease_worker(
                launch.session_id,
                owner_id="crashed-application",
                owner_process_identity=original_owner,
                process_tree_identity=original_tree,
                worker_generation=7,
            )
            catalog.save_planning_thread(
                launch.session_id,
                "11111111-2222-4333-8444-555555555555",
            )
            catalog.update_session_status(
                launch.session_id,
                PortableSessionStatus.RUNNING,
                activity_summary="Durable planning checkpoint",
            )
            now[0] = 111.0
            launches: list[str] = []

            def launch_worker(
                selected: PortableSessionLaunch,
            ) -> subprocess.Popen[str]:
                launches.append(selected.session_id)
                return subprocess.Popen(
                    [sys.executable, "-u", "-c", worker_source],
                    cwd=selected.checkout,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )

            restarted = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=catalog,
                owner_id="restarted-application",
            )
            try:
                discovered = restarted.snapshot(launch.session_id)
                self.assertEqual(discovered.status, PortableSessionStatus.RUNNING)
                self.assertTrue(discovered.recovery_available)
                self.assertEqual(launches, [])

                restarted.resume_session(launch.session_id)
                completed = self._wait_for_status(
                    restarted,
                    launch.session_id,
                    PortableSessionStatus.READY,
                )
                frame = json.loads(
                    (checkout / "resume-frame.json").read_text(encoding="utf-8")
                )
            finally:
                restarted.shutdown()

        self.assertEqual(launches, [launch.session_id])
        self.assertEqual(completed.status, PortableSessionStatus.READY)
        self.assertEqual(probed, [(original_tree, original_owner)])
        self.assertEqual(frame["session_id"], launch.session_id)
        self.assertEqual(frame["kind"], "RESUME")
        self.assertEqual(
            frame["payload"]["recovery"]["planning_thread_id"],
            "11111111-2222-4333-8444-555555555555",
        )

    def test_recovery_hint_revalidates_a_renewed_lease_at_resume_time(self) -> None:
        now = [100.0]
        owner = ProcessIdentity(pid=4301, creation_time=5301)
        worker = ProcessIdentity(pid=4401, creation_time=5401)
        tree = ProcessTreeIdentity(
            root=worker,
            kind=ProcessTreeKind.ROOT_PROCESS,
            tree_id=worker.pid,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(
                root / "portable-sessions.sqlite3",
                clock=lambda: now[0],
                process_tree_probe=lambda _tree, _owner: ProcessTreeState.STOPPED,
                lease_timeout_seconds=10,
            )
            launch = PortableSessionLaunch(
                "renewed-before-resume",
                checkout,
                PortableWorkflowOperation.PLANNING,
                (),
            )
            catalog.create_session_with_lease(
                launch,
                owner_id="live-application",
                process_identity=owner,
            )
            catalog.bind_worktree_lease_worker(
                launch.session_id,
                owner_id="live-application",
                owner_process_identity=owner,
                process_tree_identity=tree,
                worker_generation=3,
            )
            catalog.update_session_status(
                launch.session_id,
                PortableSessionStatus.RUNNING,
            )
            now[0] = 111.0
            launches: list[str] = []
            restarted = PortableSessionSupervisor(
                worker_launcher=lambda selected: launches.append(selected.session_id),
                catalog=catalog,
                owner_id="contending-application",
            )
            try:
                self.assertTrue(
                    restarted.snapshot(launch.session_id).recovery_available
                )
                catalog.renew_worktree_lease(
                    launch.session_id,
                    owner_id="live-application",
                    process_identity=worker,
                    worker_generation=3,
                )

                with self.assertRaisesRegex(
                    PortableWorktreeLeaseConflict,
                    "renewable heartbeat has not expired",
                ):
                    restarted.resume_session(launch.session_id)
                retained = catalog.get_worktree_lease(checkout)
                record = catalog.get_session(launch.session_id)
            finally:
                restarted.shutdown()

        assert retained is not None
        self.assertEqual(launches, [])
        self.assertEqual(retained.session_id, launch.session_id)
        self.assertEqual(retained.owner_id, "live-application")
        self.assertEqual(record.status, PortableSessionStatus.RUNNING)

    def test_explicit_recovery_keeps_ambiguous_original_owner_inspectable(
        self,
    ) -> None:
        now = [100.0]
        owner = ProcessIdentity(pid=4501, creation_time=5501)
        worker = ProcessIdentity(pid=4601, creation_time=5601)
        tree = ProcessTreeIdentity(
            root=worker,
            kind=ProcessTreeKind.ROOT_PROCESS,
            tree_id=worker.pid,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(
                root / "portable-sessions.sqlite3",
                clock=lambda: now[0],
                process_tree_probe=lambda _tree, _owner: ProcessTreeState.UNKNOWN,
                lease_timeout_seconds=10,
            )
            launch = PortableSessionLaunch(
                "ambiguous-original",
                checkout,
                PortableWorkflowOperation.PLANNING,
                (),
            )
            catalog.create_session_with_lease(
                launch,
                owner_id="ambiguous-application",
                process_identity=owner,
            )
            catalog.bind_worktree_lease_worker(
                launch.session_id,
                owner_id="ambiguous-application",
                owner_process_identity=owner,
                process_tree_identity=tree,
                worker_generation=9,
            )
            catalog.update_session_status(
                launch.session_id,
                PortableSessionStatus.RUNNING,
            )
            now[0] = 111.0
            restarted = PortableSessionSupervisor(
                catalog=catalog,
                owner_id="inspection-application",
            )
            try:
                with self.assertRaisesRegex(
                    PortableWorktreeLeaseConflict,
                    "worker process tree liveness is ambiguous",
                ):
                    restarted.resume_session(launch.session_id)
                retained = catalog.get_worktree_lease(checkout)
                record = catalog.get_session(launch.session_id)
            finally:
                restarted.shutdown()

        self.assertEqual(retained.session_id, launch.session_id)
        self.assertEqual(retained.owner_id, "ambiguous-application")
        self.assertEqual(retained.process_tree_identity, tree)
        self.assertEqual(record.status, PortableSessionStatus.RUNNING)

    def test_interrupted_delivery_without_loop_state_cannot_resume_as_scheduler(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            prd = checkout / "change.md"
            issues = checkout / "README.md"
            prd.write_text("# Change\n", encoding="utf-8")
            issues.write_text("# Issues\n", encoding="utf-8")
            launch = PortableSessionLaunch(
                "missing-loop-state",
                checkout,
                PortableWorkflowOperation.DELIVERY,
                ("--prd", str(prd), "--issues", str(issues)),
            )
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            catalog.create_session(launch)
            catalog.publish_workflow(
                launch.session_id,
                prd_path=prd,
                issues_index_path=issues,
                activity_summary="Published delivery",
            )
            catalog.update_session_status(
                launch.session_id,
                PortableSessionStatus.INTERRUPTED,
            )
            launches: list[PortableSessionLaunch] = []

            def unexpected_launch(selected: PortableSessionLaunch) -> subprocess.Popen[str]:
                launches.append(selected)
                raise AssertionError("Recovery launched without durable loop state.")

            supervisor = PortableSessionSupervisor(
                worker_launcher=unexpected_launch,
                catalog=catalog,
                owner_id="missing-state-shell",
            )
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "durable loop state.*missing",
                ):
                    supervisor.resume_session(launch.session_id)
            finally:
                supervisor.shutdown()

        self.assertEqual(launches, [])

    def test_delivery_recovery_rejects_corrupt_empty_and_default_loop_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            issues = root / "README.md"
            issues.write_text("# Issues\n", encoding="utf-8")
            state_path = issues.with_name("README.loop.state.json")
            default_state = LoopStateWriter(issues).state
            cases = (
                ("empty", "", "not valid JSON"),
                ("corrupt", "{", "not valid JSON"),
                ("empty-object", "{}", "no verified run-start checkpoint"),
                (
                    "generated-default",
                    json.dumps(default_state),
                    "no verified run-start checkpoint",
                ),
            )

            for label, content, expected in cases:
                with self.subTest(label=label):
                    state_path.write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, expected):
                        LoopStateWriter(issues).durable_scheduling_checkpoint(
                            repo_root=root,
                            prd_path=root / "change.md",
                        )

    def test_posix_application_death_helpers_compile_against_current_bind_shape(
        self,
    ) -> None:
        compile(_APPLICATION_DEATH_WORKER_SOURCE, "worker-helper", "exec")
        compile(_APPLICATION_DEATH_APP_SOURCE, "app-helper", "exec")
        compile(_CATALOG_LEASE_OWNER_SOURCE, "catalog-owner-helper", "exec")
        compile(_CATALOG_LEASE_CONTENDER_SOURCE, "catalog-contender-helper", "exec")
        compile(_PROCESS_IDENTITY_HOLDER_SOURCE, "identity-holder-helper", "exec")
        self.assertIn("owner_process_identity=owner", _APPLICATION_DEATH_APP_SOURCE)
        self.assertIn(
            "process_tree_identity=capture_process_tree_identity(worker)",
            _APPLICATION_DEATH_APP_SOURCE,
        )
        self.assertIn("os.replace(temporary,path)", _APPLICATION_DEATH_WORKER_SOURCE)

    def test_readiness_waits_for_complete_json_after_delayed_partial_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "delayed-ready.json"
            partial_written = threading.Event()

            def publish_delayed() -> None:
                ready.write_text("{", encoding="utf-8")
                partial_written.set()
                time.sleep(0.05)
                ready.write_text(
                    json.dumps({"worker": [4101, 5101]}),
                    encoding="utf-8",
                )

            publisher = threading.Thread(target=publish_delayed)
            publisher.start()
            self.assertTrue(partial_written.wait(timeout=1))
            try:
                parsed = self._wait_for_json_object(ready)
            finally:
                publisher.join(timeout=1)

        self.assertEqual(parsed, {"worker": [4101, 5101]})

    def test_posix_cleanup_targets_captured_worker_process_group(self) -> None:
        worker = ProcessIdentity(pid=4101, creation_time=5101)
        with mock.patch(
            f"{__name__}.process_identity_state",
            return_value=ProcessTreeState.RUNNING,
        ), mock.patch(f"{__name__}.os.killpg", create=True) as kill_group, mock.patch(
            f"{__name__}.signal.SIGKILL",
            9,
            create=True,
        ):
            self._terminate_worker_process_tree(
                worker,
                None,
                platform_name="posix",
            )

        kill_group.assert_called_once_with(worker.pid, 9)

    def test_partial_work_context_is_bounded_before_prompt_use(self) -> None:
        context = PortablePartialWorkContext.from_sequences(
            activity=(f"activity-{index}-" + ("a" * 1_000) for index in range(20)),
            diagnostics=(
                f"diagnostic-{index}-" + ("d" * 1_000) for index in range(20)
            ),
        )

        self.assertEqual(len(context.activity), 10)
        self.assertEqual(len(context.diagnostics), 10)
        self.assertTrue(all(len(item) <= 500 for item in context.activity))
        self.assertTrue(all(len(item) <= 500 for item in context.diagnostics))
        self.assertLessEqual(len(context.to_prompt()), 10_500)

    def test_recovery_worker_dispatches_typed_context_to_both_executors(self) -> None:
        planning = PortableRecoveryData(
            checkpoint_kind=PortableCheckpointKind.PLANNING,
            checkout=Path.cwd(),
            activity=("planning partial",),
            diagnostics=("planning diagnostic",),
            planning_thread_id="0198c0de-aaaa-bbbb-cccc-444455556666",
            planning_settings={},
        )
        delivery = PortableRecoveryData(
            checkpoint_kind=PortableCheckpointKind.PRD,
            checkout=Path.cwd(),
            activity=("delivery partial",),
            diagnostics=("delivery diagnostic",),
            prd_path=Path("change.md"),
            issues_index_path=Path("README.md"),
            issue_id="0001",
            next_role="coder",
            pass_number=1,
        )
        with mock.patch(
            "devloop.portable_worker._arguments_for_recovery",
            side_effect=lambda _operation, arguments, _recovery: list(arguments),
        ), mock.patch(
            "devloop.interactive_runner.main",
            return_value=0,
        ) as planning_main, mock.patch(
            "devloop.cli.main",
            return_value=0,
        ) as delivery_main:
            _run_operation(PortableWorkflowOperation.PLANNING, [], recovery=planning)
            _run_operation(PortableWorkflowOperation.DELIVERY, [], recovery=delivery)

        planning_context = planning_main.call_args.kwargs["partial_work_context"]
        delivery_context = delivery_main.call_args.kwargs["partial_work_context"]
        self.assertIsInstance(planning_context, PortablePartialWorkContext)
        self.assertIsInstance(delivery_context, PortablePartialWorkContext)
        self.assertEqual(planning_context.diagnostics, ("planning diagnostic",))
        self.assertEqual(delivery_context.diagnostics, ("delivery diagnostic",))

    def test_planning_prompt_consumes_sanitized_partial_work_context(self) -> None:
        context = PortablePartialWorkContext.from_sequences(
            activity=("Edited worker.py\x1b[31m",),
            diagnostics=("Bearer recovery-secret\nfailed after checkpoint",),
        )

        prompt = interactive_runner.build_planning_prompt(
            repo_root=Path("repo"),
            bundle_root=Path("bundle"),
            goal="recover planning",
            skill_paths=[],
            wiki_index=Path("wiki.md"),
            partial_work_context=context,
        )

        self.assertIn("PRESERVED PARTIAL-WORK CONTEXT", prompt)
        self.assertIn("Edited worker.py", prompt)
        self.assertIn("failed after checkpoint", prompt)
        self.assertIn("non-authoritative", prompt)
        self.assertIn("durable recovery checkpoint", prompt)
        self.assertNotIn("\x1b", prompt)
        self.assertNotIn("recovery-secret", prompt)

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

    def test_real_process_stale_lease_blocks_live_owner_then_has_one_reclaimer(
        self,
    ) -> None:
        lease_timeout = 0.1
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "portable-sessions.sqlite3"
            catalog = PortableSessionCatalog(
                database,
                lease_timeout_seconds=lease_timeout,
            )
            owner_ready = root / "owner-ready.json"
            owner_stop = root / "owner-stop"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            owner = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    _CATALOG_LEASE_OWNER_SOURCE,
                    str(database),
                    str(checkout),
                    "real-stale-owner",
                    "real-stale-shell",
                    str(owner_ready),
                    str(owner_stop),
                    str(lease_timeout),
                    "-",
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            contenders: list[subprocess.Popen[str]] = []
            try:
                owner_payload = self._wait_for_json_object(owner_ready)
                owner_identity = ProcessIdentity(
                    pid=int(owner_payload["pid"]),
                    creation_time=int(owner_payload["creation_time"]),
                )
                self.assertEqual(
                    process_identity_state(owner_identity),
                    ProcessTreeState.RUNNING,
                )
                catalog.create_session(
                    PortableSessionLaunch(
                        "blocked-while-owner-live",
                        checkout,
                        PortableWorkflowOperation.PLANNING,
                        (),
                    )
                )
                time.sleep(lease_timeout + 0.05)
                live_result = root / "live-contender.json"
                live_contender = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        _CATALOG_LEASE_CONTENDER_SOURCE,
                        str(database),
                        "blocked-while-owner-live",
                        "live-contender",
                        str(live_result),
                        "-",
                        str(lease_timeout),
                    ],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                contenders.append(live_contender)
                self.assertEqual(live_contender.wait(timeout=5), 0)
                blocked = self._wait_for_json_object(live_result)
                self.assertEqual(blocked["outcome"], "blocked")
                self.assertIn("exact owner process is still running", blocked["detail"])

                owner_stop.touch()
                self.assertEqual(owner.wait(timeout=5), 0)
                self._wait_for_identity_stopped(owner_identity)

                gate = root / "reclaim-gate"
                result_paths: list[Path] = []
                for index in range(2):
                    session_id = f"real-reclaimer-{index}"
                    catalog.create_session(
                        PortableSessionLaunch(
                            session_id,
                            checkout,
                            PortableWorkflowOperation.PLANNING,
                            (),
                        )
                    )
                    result_path = root / f"reclaimer-{index}.json"
                    result_paths.append(result_path)
                    contenders.append(
                        subprocess.Popen(
                            [
                                sys.executable,
                                "-u",
                                "-c",
                                _CATALOG_LEASE_CONTENDER_SOURCE,
                                str(database),
                                session_id,
                                f"reclaimer-{index}",
                                str(result_path),
                                str(gate),
                                str(lease_timeout),
                            ],
                            env=environment,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            encoding="utf-8",
                        )
                    )
                gate.touch()
                for contender in contenders[1:]:
                    self.assertEqual(contender.wait(timeout=5), 0)
                results = [self._wait_for_json_object(path) for path in result_paths]
                self.assertEqual(
                    [result["outcome"] for result in results].count("acquired"),
                    1,
                )
                self.assertEqual(
                    [result["outcome"] for result in results].count("blocked"),
                    1,
                )
            finally:
                owner_stop.touch()
                if owner.poll() is None:
                    owner.kill()
                owner.wait(timeout=5)
                for contender in contenders:
                    if contender.poll() is None:
                        contender.kill()
                    contender.wait(timeout=5)
                    if contender.stdout is not None:
                        contender.stdout.close()
                    if contender.stderr is not None:
                        contender.stderr.close()
                if owner.stdout is not None:
                    owner.stdout.close()
                if owner.stderr is not None:
                    owner.stderr.close()

        self.assertEqual(process_identity_state(owner_identity), ProcessTreeState.STOPPED)
        for result in [blocked, *results]:
            identity = ProcessIdentity(
                pid=int(result["pid"]),
                creation_time=int(result["creation_time"]),
            )
            self.assertEqual(process_identity_state(identity), ProcessTreeState.STOPPED)

    def test_dead_supervisor_does_not_reclaim_a_bound_running_worker_tree(
        self,
    ) -> None:
        now = [100.0]
        owner_identity = ProcessIdentity(pid=4201, creation_time=5201)
        worker_identity = ProcessIdentity(pid=4202, creation_time=5202)
        tree_identity = ProcessTreeIdentity(
            root=worker_identity,
            kind=ProcessTreeKind.ROOT_PROCESS,
            tree_id=worker_identity.pid,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(
                root / "portable-sessions.sqlite3",
                clock=lambda: now[0],
                process_probe=lambda _identity: ProcessTreeState.STOPPED,
                process_tree_probe=(
                    lambda identity, owner: (
                        ProcessTreeState.RUNNING
                        if identity == tree_identity and owner == owner_identity
                        else ProcessTreeState.UNKNOWN
                    )
                ),
                lease_timeout_seconds=30.0,
            )
            first = PortableSessionLaunch(
                "dead-supervisor-live-worker",
                checkout,
                PortableWorkflowOperation.PLANNING,
                (),
            )
            replacement = PortableSessionLaunch(
                "blocked-by-running-worker",
                checkout,
                PortableWorkflowOperation.PLANNING,
                (),
            )
            catalog.create_session_with_lease(
                first,
                owner_id="old-shell",
                process_identity=owner_identity,
            )
            catalog.bind_worktree_lease_worker(
                first.session_id,
                owner_id="old-shell",
                owner_process_identity=owner_identity,
                process_tree_identity=tree_identity,
                worker_generation=1,
            )
            catalog.create_session(replacement)
            now[0] = 131.0

            with self.assertRaises(PortableWorktreeLeaseConflict):
                catalog.acquire_session_lease(
                    replacement.session_id,
                    owner_id="new-shell",
                    process_identity=ProcessIdentity(pid=4203, creation_time=5203),
                )

            retained = catalog.get_worktree_lease(checkout)

        assert retained is not None
        self.assertEqual(retained.worker_process_id, worker_identity.pid)
        self.assertEqual(
            retained.worker_process_start_fingerprint,
            worker_identity.creation_time,
        )
        self.assertEqual(retained.process_tree_kind, ProcessTreeKind.ROOT_PROCESS)
        self.assertEqual(retained.process_tree_id, worker_identity.pid)

    def test_bind_rejects_worker_tree_id_that_does_not_match_root_pid(self) -> None:
        owner_identity = ProcessIdentity(pid=4211, creation_time=5211)
        worker_identity = ProcessIdentity(pid=4212, creation_time=5212)
        mismatched_tree = ProcessTreeIdentity(
            root=worker_identity,
            kind=ProcessTreeKind.POSIX_PROCESS_GROUP,
            tree_id=worker_identity.pid + 1,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                "mismatched-worker-tree",
                checkout,
                PortableWorkflowOperation.PLANNING,
                (),
            )
            catalog.create_session_with_lease(
                launch,
                owner_id="tree-validation-shell",
                process_identity=owner_identity,
            )

            with self.assertRaisesRegex(
                ValueError,
                "tree identity must match its root process",
            ):
                catalog.bind_worktree_lease_worker(
                    launch.session_id,
                    owner_id="tree-validation-shell",
                    owner_process_identity=owner_identity,
                    process_tree_identity=mismatched_tree,
                    worker_generation=1,
                )

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

    def test_real_process_root_fallback_lease_stays_ambiguous_after_owner_exit(
        self,
    ) -> None:
        lease_timeout = 0.1
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "portable-sessions.sqlite3"
            catalog = PortableSessionCatalog(
                database,
                lease_timeout_seconds=lease_timeout,
            )
            ready = root / "root-owner.json"
            stop = root / "root-owner-stop"
            result_path = root / "root-contender.json"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            owner = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    _CATALOG_LEASE_OWNER_SOURCE,
                    str(database),
                    str(checkout),
                    "real-root-owner",
                    "real-root-shell",
                    str(ready),
                    str(stop),
                    str(lease_timeout),
                    "1",
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            contender: subprocess.Popen[str] | None = None
            try:
                owner_payload = self._wait_for_json_object(ready)
                owner_identity = ProcessIdentity(
                    pid=int(owner_payload["pid"]),
                    creation_time=int(owner_payload["creation_time"]),
                )
                root_fallback = catalog.get_worktree_lease(checkout)
                assert root_fallback is not None
                self.assertEqual(
                    root_fallback.process_tree_kind,
                    ProcessTreeKind.ROOT_PROCESS,
                )
                self.assertEqual(root_fallback.worker_process_identity, owner_identity)
                catalog.create_session(
                    PortableSessionLaunch(
                        "blocked-by-root-fallback",
                        checkout,
                        PortableWorkflowOperation.PLANNING,
                        (),
                    )
                )
                stop.touch()
                self.assertEqual(owner.wait(timeout=5), 0)
                self._wait_for_identity_stopped(owner_identity)
                time.sleep(lease_timeout + 0.05)
                contender = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        _CATALOG_LEASE_CONTENDER_SOURCE,
                        str(database),
                        "blocked-by-root-fallback",
                        "root-contender",
                        str(result_path),
                        "-",
                        str(lease_timeout),
                    ],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                self.assertEqual(contender.wait(timeout=5), 0)
                result = self._wait_for_json_object(result_path)
                retained = catalog.get_worktree_lease(checkout)
            finally:
                stop.touch()
                if owner.poll() is None:
                    owner.kill()
                owner.wait(timeout=5)
                if contender is not None:
                    if contender.poll() is None:
                        contender.kill()
                    contender.wait(timeout=5)
                    if contender.stdout is not None:
                        contender.stdout.close()
                    if contender.stderr is not None:
                        contender.stderr.close()
                if owner.stdout is not None:
                    owner.stdout.close()
                if owner.stderr is not None:
                    owner.stderr.close()

        assert retained is not None
        self.assertEqual(result["outcome"], "blocked")
        self.assertIn("worker process tree liveness is ambiguous", result["detail"])
        self.assertNotIn("force", result["detail"].lower())
        self.assertEqual(retained.session_id, "real-root-owner")
        contender_identity = ProcessIdentity(
            pid=int(result["pid"]),
            creation_time=int(result["creation_time"]),
        )
        self.assertEqual(process_identity_state(owner_identity), ProcessTreeState.STOPPED)
        self.assertEqual(
            process_identity_state(contender_identity),
            ProcessTreeState.STOPPED,
        )

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
            LoopStateWriter(issues).record_run_start(
                checkout,
                prd,
                ["0001"],
                dry_run=False,
            )
            launch = PortableSessionLaunch(
                session_id="explicit-recovery",
                checkout=checkout,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=("--prd", str(prd), "--issues", str(issues)),
            )
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            catalog.create_session(launch)
            catalog.publish_workflow(
                launch.session_id,
                prd_path=prd,
                issues_index_path=issues,
                activity_summary="Durable scheduler checkpoint",
            )
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
            try:
                supervisor.resume_session(launch.session_id)
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
        self.assertEqual(recovery["payload"]["recovery"]["issue_id"], None)
        self.assertEqual(recovery["payload"]["recovery"]["next_role"], "scheduler")
        self.assertEqual(recovery["payload"]["recovery"]["pass_number"], 1)
        self.assertIn(
            "partial diagnostic",
            recovery["payload"]["partial_work_context"]["diagnostics"],
        )
        self.assertEqual(completed.status, PortableSessionStatus.COMPLETED)

    def test_explicit_interrupted_recovery_invokes_delivery_at_durable_cursor(
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
        sitecustomize_source = (
            "import json,os\n"
            "from pathlib import Path\n"
            "import devloop.cli\n"
            "from devloop.issue_pack import parse_issue_index\n"
            "from devloop.state import LoopStateWriter\n"
            "def recovered_delivery(arguments,*,partial_work_context=None):\n"
            " arguments=list(arguments)\n"
            " issues=Path(arguments[arguments.index('--issues')+1])\n"
            " issue=parse_issue_index(issues)[0]\n"
            " cursor=LoopStateWriter(issues).resume_issue(issue)\n"
            " Path(os.environ['RECOVERY_RESULT']).write_text(json.dumps({"
            "'arguments':arguments,'next_role':cursor.next_role.value,"
            "'pass_number':cursor.pass_number,'partial_prompt':"
            "partial_work_context.to_prompt()}),encoding='utf-8')\n"
            " return 0\n"
            "devloop.cli.main=recovered_delivery\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            prd = checkout / "change.md"
            issue_path = checkout / "0001-example.md"
            issues = checkout / "README.md"
            prd.write_text("# Change\n", encoding="utf-8")
            issue_path.write_text("# Issue 0001\n", encoding="utf-8")
            issues.write_text(
                "[Issue 0001](./0001-example.md)\n",
                encoding="utf-8",
            )
            issue = Issue("0001", "Issue 0001", issue_path, completed=False)
            writer = LoopStateWriter(issues)
            writer.record_run_start(checkout, prd, [issue.number], dry_run=False)
            writer.record_issue_start(issue)
            writer.reserve_scheduling_attempt(
                issue,
                phase=SchedulingPhase.NORMAL_SCHEDULING,
                ordinal=1,
            )
            writer.record_role_result(
                issue,
                "coder",
                1,
                RoleResult(status="PASS", changed_files=["partial.txt"]),
            )
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                session_id="exact-recovery",
                checkout=checkout,
                operation=PortableWorkflowOperation.DELIVERY,
                arguments=("--prd", str(prd), "--issues", str(issues)),
            )
            catalog.create_session(launch)
            catalog.publish_workflow(
                launch.session_id,
                prd_path=prd,
                issues_index_path=issues,
                activity_summary="Durable reviewer checkpoint",
            )
            launch_count = 0
            result_path = checkout / "recovered-delivery.json"
            hook_root = root / "hook"
            hook_root.mkdir()
            (hook_root / "sitecustomize.py").write_text(
                sitecustomize_source,
                encoding="utf-8",
            )

            def launch_worker(
                selected: PortableSessionLaunch,
            ) -> subprocess.Popen[str]:
                nonlocal launch_count
                launch_count += 1
                if launch_count == 1:
                    command = [
                        sys.executable,
                        "-u",
                        "-c",
                        crashing_source,
                    ]
                    environment = None
                else:
                    command = [
                        sys.executable,
                        "-u",
                        "-m",
                        "devloop.portable_worker",
                        "--session-id",
                        selected.session_id,
                    ]
                    environment = dict(os.environ)
                    environment["PYTHONPATH"] = os.pathsep.join(
                        (
                            str(hook_root),
                            str(Path(__file__).resolve().parents[1] / "src"),
                        )
                    )
                    environment["RECOVERY_RESULT"] = str(result_path)
                    environment["DEVLOOP_PORTABLE_SESSION_CATALOG"] = str(
                        catalog.path
                    )
                    environment["DEVLOOP_PORTABLE_SESSION_ID"] = selected.session_id
                    environment["DEVLOOP_PORTABLE_SESSION_OWNER_ID"] = (
                        "exact-recovery-shell"
                    )
                return subprocess.Popen(
                    command,
                    cwd=selected.checkout,
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
                owner_id="exact-recovery-shell",
            )
            try:
                supervisor.resume_session(launch.session_id)
                self._wait_for_status(
                    supervisor,
                    launch.session_id,
                    PortableSessionStatus.INTERRUPTED,
                )
                time.sleep(0.1)
                self.assertEqual(launch_count, 1)
                self.assertFalse(result_path.exists())

                supervisor.resume_session(launch.session_id)
                completed = supervisor.wait_for_terminal(
                    launch.session_id,
                    timeout=5,
                )
                self.assertTrue(result_path.exists(), completed)
                recovered = json.loads(result_path.read_text(encoding="utf-8"))
                partial = (checkout / "partial.txt").read_text(encoding="utf-8")
            finally:
                supervisor.shutdown()

        self.assertEqual(completed.status, PortableSessionStatus.COMPLETED)
        self.assertEqual(launch_count, 2)
        self.assertEqual(recovered["next_role"], "reviewer")
        self.assertEqual(recovered["pass_number"], 1)
        self.assertEqual(
            recovered["arguments"][-2:],
            ["--start-issue", "0001"],
        )
        self.assertIn("partial diagnostic", recovered["partial_prompt"])
        self.assertIn("non-authoritative", recovered["partial_prompt"])
        self.assertEqual(partial, "preserved")

    def test_worker_revalidates_loop_state_deleted_after_supervisor_checkpoint(
        self,
    ) -> None:
        sitecustomize_source = (
            "import os\n"
            "from pathlib import Path\n"
            "import devloop.cli\n"
            "def forbidden_delivery(*args,**kwargs):\n"
            " Path(os.environ['DELIVERY_CALLED']).touch()\n"
            " return 0\n"
            "devloop.cli.main=forbidden_delivery\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            prd = checkout / "change.md"
            issues = checkout / "README.md"
            prd.write_text("# Change\n", encoding="utf-8")
            issues.write_text("# Issues\n", encoding="utf-8")
            writer = LoopStateWriter(issues)
            writer.record_run_start(checkout, prd, ["0001"], dry_run=False)
            catalog = PortableSessionCatalog(root / "portable-sessions.sqlite3")
            launch = PortableSessionLaunch(
                "deleted-before-worker",
                checkout,
                PortableWorkflowOperation.DELIVERY,
                ("--prd", str(prd), "--issues", str(issues)),
            )
            catalog.create_session(launch)
            catalog.publish_workflow(
                launch.session_id,
                prd_path=prd,
                issues_index_path=issues,
                activity_summary="Durable scheduler checkpoint",
            )
            catalog.update_session_status(
                launch.session_id,
                PortableSessionStatus.INTERRUPTED,
            )
            hook_root = root / "hook"
            hook_root.mkdir()
            (hook_root / "sitecustomize.py").write_text(
                sitecustomize_source,
                encoding="utf-8",
            )
            delivery_called = root / "delivery-called"

            def launch_after_deleting_state(
                selected: PortableSessionLaunch,
            ) -> subprocess.Popen[str]:
                writer.state_path.unlink()
                environment = dict(os.environ)
                environment["PYTHONPATH"] = os.pathsep.join(
                    (
                        str(hook_root),
                        str(Path(__file__).resolve().parents[1] / "src"),
                    )
                )
                environment["DELIVERY_CALLED"] = str(delivery_called)
                environment["DEVLOOP_PORTABLE_SESSION_CATALOG"] = str(catalog.path)
                environment["DEVLOOP_PORTABLE_SESSION_ID"] = selected.session_id
                environment["DEVLOOP_PORTABLE_SESSION_OWNER_ID"] = "drift-shell"
                return subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-m",
                        "devloop.portable_worker",
                        "--session-id",
                        selected.session_id,
                    ],
                    cwd=selected.checkout,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )

            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_after_deleting_state,
                catalog=catalog,
                owner_id="drift-shell",
            )
            try:
                supervisor.resume_session(launch.session_id)
                failed = supervisor.wait_for_terminal(launch.session_id, timeout=5)
            finally:
                supervisor.shutdown()

        self.assertEqual(failed.status, PortableSessionStatus.FAILED)
        self.assertFalse(delivery_called.exists())
        self.assertTrue(
            any("durable loop state" in line and "missing" in line for line in failed.diagnostics)
        )

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
            "process_identity=capture_process_identity())\n"
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

    @unittest.skipUnless(os.name == "nt", "Windows Job Object contract")
    def test_windows_application_death_stops_worker_tree_before_reclaim(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            ready = root / "worker-ready.json"
            control = root / "worker-control.json"
            start = root / "start-worker"
            exit_now = root / "exit-now"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(
                Path(__file__).resolve().parents[1] / "src"
            )
            app = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    _APPLICATION_DEATH_APP_SOURCE,
                    str(root),
                    str(checkout),
                    "windows-dead-tree",
                    "windows-dead-app",
                    str(ready),
                    str(control),
                    str(start),
                    str(exit_now),
                    _APPLICATION_DEATH_WORKER_SOURCE,
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **process_tree_creation_kwargs(),
            )
            worker: ProcessIdentity | None = None
            descendant: ProcessIdentity | None = None
            try:
                control_payload = self._wait_for_json_object(control)
                worker = self._identity_from_payload(control_payload, "worker")
                self.assertEqual(
                    process_identity_state(worker),
                    ProcessTreeState.RUNNING,
                )
                start.touch()
                identities = self._wait_for_json_object(ready)
                self.assertEqual(
                    self._identity_from_payload(identities, "worker"),
                    worker,
                )
                descendant = self._identity_from_payload(identities, "descendant")
                before = PortableSessionCatalog(
                    root / "portable-sessions.sqlite3"
                ).get_worktree_lease(checkout)
                assert before is not None

                self.assertEqual(
                    before.process_tree_kind,
                    ProcessTreeKind.WINDOWS_KILL_ON_CLOSE_JOB,
                )
                self.assertEqual(process_identity_state(worker), ProcessTreeState.RUNNING)
                self.assertEqual(
                    process_identity_state(descendant),
                    ProcessTreeState.RUNNING,
                )
                exit_now.touch()
                self.assertEqual(app.wait(timeout=5), 23)
                self._wait_for_identity_stopped(worker)
                self._wait_for_identity_stopped(descendant)
                time.sleep(0.06)
                catalog = PortableSessionCatalog(
                    root / "portable-sessions.sqlite3",
                    lease_timeout_seconds=0.05,
                )
                replacement = PortableSessionLaunch(
                    "after-windows-tree-death",
                    checkout,
                    PortableWorkflowOperation.PLANNING,
                    (),
                )
                catalog.create_session(replacement)
                reclaimed = catalog.acquire_session_lease(
                    replacement.session_id,
                    owner_id="replacement-windows-app",
                    process_identity=capture_process_identity(),
                )
            finally:
                self._cleanup_application_death_processes(
                    app,
                    worker,
                    descendant,
                )
                if app.stdout is not None:
                    app.stdout.close()
                if app.stderr is not None:
                    app.stderr.close()

        self.assertEqual(reclaimed.session_id, replacement.session_id)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object contract")
    def test_application_restart_explicitly_resumes_original_session(self) -> None:
        resume_worker_source = (
            "import json,sys,time\n"
            "start=json.loads(sys.stdin.readline())\n"
            "open('restart-resume-frame.json','w',encoding='utf-8').write("
            "json.dumps(start))\n"
            "session_id=start['session_id']\n"
            "print(json.dumps({'version':1,'session_id':session_id,'sequence':1,"
            "'kind':'HELLO','payload':{}}),flush=True)\n"
            "print(json.dumps({'version':1,'session_id':session_id,'sequence':2,"
            "'kind':'COMPLETION','payload':{'exit_code':0}}),flush=True)\n"
            "time.sleep(.2)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            ready = root / "worker-ready.json"
            control = root / "worker-control.json"
            start = root / "start-worker"
            exit_now = root / "exit-now"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(
                Path(__file__).resolve().parents[1] / "src"
            )
            app = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    _APPLICATION_DEATH_APP_SOURCE,
                    str(root),
                    str(checkout),
                    "restart-original",
                    "original-application",
                    str(ready),
                    str(control),
                    str(start),
                    str(exit_now),
                    _APPLICATION_DEATH_WORKER_SOURCE,
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **process_tree_creation_kwargs(),
            )
            worker: ProcessIdentity | None = None
            descendant: ProcessIdentity | None = None
            restarted: PortableSessionSupervisor | None = None
            try:
                control_payload = self._wait_for_json_object(control)
                worker = self._identity_from_payload(control_payload, "worker")
                start.touch()
                identities = self._wait_for_json_object(ready)
                descendant = self._identity_from_payload(identities, "descendant")
                exit_now.touch()
                self.assertEqual(app.wait(timeout=5), 23)
                self._wait_for_identity_stopped(worker)
                self._wait_for_identity_stopped(descendant)
                time.sleep(0.06)
                catalog = PortableSessionCatalog(
                    root / "portable-sessions.sqlite3",
                    lease_timeout_seconds=0.05,
                )
                launches: list[str] = []

                def launch_worker(
                    selected: PortableSessionLaunch,
                ) -> subprocess.Popen[str]:
                    launches.append(selected.session_id)
                    return subprocess.Popen(
                        [sys.executable, "-u", "-c", resume_worker_source],
                        cwd=selected.checkout,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                    )

                restarted = PortableSessionSupervisor(
                    worker_launcher=launch_worker,
                    catalog=catalog,
                    owner_id="restarted-application",
                )
                discovered = restarted.snapshot("restart-original")
                self.assertEqual(discovered.status, PortableSessionStatus.RUNNING)
                self.assertTrue(discovered.recovery_available)
                self.assertEqual(launches, [])

                restarted.resume_session("restart-original")
                resumed = self._wait_for_status(
                    restarted,
                    "restart-original",
                    PortableSessionStatus.READY,
                )
                frame = json.loads(
                    (checkout / "restart-resume-frame.json").read_text(
                        encoding="utf-8"
                    )
                )
            finally:
                if restarted is not None:
                    restarted.shutdown()
                self._cleanup_application_death_processes(
                    app,
                    worker,
                    descendant,
                )
                if app.stdout is not None:
                    app.stdout.close()
                if app.stderr is not None:
                    app.stderr.close()

        self.assertEqual(launches, ["restart-original"])
        self.assertEqual(resumed.session_id, "restart-original")
        self.assertEqual(frame["session_id"], "restart-original")
        self.assertEqual(frame["kind"], "RESUME")
        self.assertEqual(
            frame["payload"]["recovery"]["planning_thread_id"],
            "11111111-2222-4333-8444-555555555555",
        )

    def test_version_five_bound_lease_without_worker_tree_remains_ambiguous(
        self,
    ) -> None:
        now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            path = root / "portable-sessions.sqlite3"
            catalog = PortableSessionCatalog(path, clock=lambda: now[0])
            old = PortableSessionLaunch(
                "legacy-v5-owner",
                checkout,
                PortableWorkflowOperation.PLANNING,
                (),
            )
            catalog.create_session_with_lease(
                old,
                owner_id="legacy-v5-shell",
                process_identity=ProcessIdentity(pid=4501, creation_time=5501),
                worker_generation=1,
            )
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    UPDATE worktree_leases
                    SET worker_process_id = NULL,
                        worker_process_start_fingerprint = NULL,
                        process_tree_kind = NULL,
                        process_tree_id = NULL
                    """
                )
                connection.execute("PRAGMA user_version = 5")
                connection.commit()
            finally:
                connection.close()
            migrated = PortableSessionCatalog(
                path,
                clock=lambda: now[0],
                process_probe=lambda _identity: ProcessTreeState.STOPPED,
                lease_timeout_seconds=10,
            )
            replacement = PortableSessionLaunch(
                "blocked-by-legacy-v5",
                checkout,
                PortableWorkflowOperation.PLANNING,
                (),
            )
            migrated.create_session(replacement)
            now[0] = 111.0

            with self.assertRaisesRegex(
                PortableWorktreeLeaseConflict,
                "exact worker process-tree identity is unavailable",
            ):
                migrated.acquire_session_lease(
                    replacement.session_id,
                    owner_id="replacement-shell",
                    process_identity=ProcessIdentity(pid=4502, creation_time=5502),
                )
            connection = sqlite3.connect(path)
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            finally:
                connection.close()

            self.assertEqual(version, 8)

    @unittest.skipUnless(os.name == "posix", "POSIX process-group contract")
    def test_application_death_does_not_reclaim_while_worker_tree_is_alive(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            ready = root / "worker-ready.json"
            control = root / "worker-control.json"
            start = root / "start-worker"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(
                Path(__file__).resolve().parents[1] / "src"
            )
            app = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    _APPLICATION_DEATH_APP_SOURCE,
                    str(root),
                    str(checkout),
                    "dead-tree",
                    "dead-tree-app",
                    str(ready),
                    str(control),
                    str(start),
                    "-",
                    _APPLICATION_DEATH_WORKER_SOURCE,
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **process_tree_creation_kwargs(),
            )
            worker: ProcessIdentity | None = None
            descendant: ProcessIdentity | None = None
            try:
                control_payload = self._wait_for_json_object(control)
                worker = self._identity_from_payload(control_payload, "worker")
                self.assertEqual(
                    process_identity_state(worker),
                    ProcessTreeState.RUNNING,
                )
                start.touch()
                identities = self._wait_for_json_object(ready)
                self.assertEqual(
                    self._identity_from_payload(identities, "worker"),
                    worker,
                )
                descendant = self._identity_from_payload(identities, "descendant")
                self.assertNotEqual(worker.pid, descendant.pid)
                bound = PortableSessionCatalog(
                    root / "portable-sessions.sqlite3"
                ).get_worktree_lease(checkout)
                assert bound is not None
                self.assertEqual(
                    bound.process_tree_kind,
                    ProcessTreeKind.POSIX_PROCESS_GROUP,
                )
                self.assertEqual(bound.worker_process_id, worker.pid)
                self.assertEqual(bound.process_tree_id, worker.pid)
                self.assertEqual(app.wait(timeout=5), 23)
                time.sleep(0.06)
                catalog = PortableSessionCatalog(
                    root / "portable-sessions.sqlite3",
                    lease_timeout_seconds=0.05,
                )
                replacement = PortableSessionLaunch(
                    "blocked-by-live-tree",
                    checkout,
                    PortableWorkflowOperation.PLANNING,
                    (),
                )
                catalog.create_session(replacement)
                with self.assertRaises(PortableWorktreeLeaseConflict):
                    catalog.acquire_session_lease(
                        replacement.session_id,
                        owner_id="replacement-app",
                        process_identity=capture_process_identity(),
                    )
            finally:
                self._cleanup_application_death_processes(
                    app,
                    worker,
                    descendant,
                )
                if app.stdout is not None:
                    app.stdout.close()
                if app.stderr is not None:
                    app.stderr.close()

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

    def test_real_process_incarnation_mismatch_simulates_exact_pid_reuse_condition(
        self,
    ) -> None:
        lease_timeout = 0.1
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "portable-sessions.sqlite3"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            old_ready = root / "old-identity.json"
            old_process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    _PROCESS_IDENTITY_HOLDER_SOURCE,
                    str(old_ready),
                    "-",
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            replacement: subprocess.Popen[str] | None = None
            contender: subprocess.Popen[str] | None = None
            try:
                old_payload = self._wait_for_json_object(old_ready)
                self.assertEqual(old_process.wait(timeout=5), 0)
                old_identity = ProcessIdentity(
                    pid=int(old_payload["pid"]),
                    creation_time=int(old_payload["creation_time"]),
                )
                self._wait_for_identity_stopped(old_identity)

                replacement_ready = root / "replacement-identity.json"
                replacement_stop = root / "replacement-stop"
                replacement = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        _PROCESS_IDENTITY_HOLDER_SOURCE,
                        str(replacement_ready),
                        str(replacement_stop),
                    ],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                replacement_payload = self._wait_for_json_object(replacement_ready)
                replacement_identity = ProcessIdentity(
                    pid=int(replacement_payload["pid"]),
                    creation_time=int(replacement_payload["creation_time"]),
                )
                self.assertNotEqual(
                    replacement_identity.creation_time,
                    old_identity.creation_time,
                )
                self.assertEqual(
                    process_identity_state(replacement_identity),
                    ProcessTreeState.RUNNING,
                )

                # A reused PID is represented durably by the replacement's live PID
                # paired with the prior process incarnation's authentic start token.
                stale_reused_pid_identity = ProcessIdentity(
                    pid=replacement_identity.pid,
                    creation_time=old_identity.creation_time,
                )
                self.assertEqual(
                    process_identity_state(stale_reused_pid_identity),
                    ProcessTreeState.STOPPED,
                )
                catalog = PortableSessionCatalog(
                    database,
                    lease_timeout_seconds=lease_timeout,
                )
                catalog.create_session_with_lease(
                    PortableSessionLaunch(
                        "stale-reused-pid-record",
                        checkout,
                        PortableWorkflowOperation.PLANNING,
                        (),
                    ),
                    owner_id="stale-incarnation",
                    process_identity=stale_reused_pid_identity,
                )
                catalog.create_session(
                    PortableSessionLaunch(
                        "real-incarnation-contender",
                        checkout,
                        PortableWorkflowOperation.PLANNING,
                        (),
                    )
                )
                time.sleep(lease_timeout + 0.05)
                result_path = root / "incarnation-contender.json"
                contender = subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        _CATALOG_LEASE_CONTENDER_SOURCE,
                        str(database),
                        "real-incarnation-contender",
                        "real-incarnation-contender",
                        str(result_path),
                        "-",
                        str(lease_timeout),
                    ],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                self.assertEqual(contender.wait(timeout=5), 0)
                result = self._wait_for_json_object(result_path)
                self.assertEqual(
                    process_identity_state(replacement_identity),
                    ProcessTreeState.RUNNING,
                )
                replacement_stop.touch()
            finally:
                if old_process.poll() is None:
                    old_process.kill()
                old_process.wait(timeout=5)
                if replacement is not None:
                    replacement_stop = root / "replacement-stop"
                    replacement_stop.touch()
                    if replacement.poll() is None:
                        replacement.wait(timeout=5)
                    if replacement.stdout is not None:
                        replacement.stdout.close()
                    if replacement.stderr is not None:
                        replacement.stderr.close()
                if contender is not None:
                    if contender.poll() is None:
                        contender.kill()
                    contender.wait(timeout=5)
                    if contender.stdout is not None:
                        contender.stdout.close()
                    if contender.stderr is not None:
                        contender.stderr.close()
                if old_process.stdout is not None:
                    old_process.stdout.close()
                if old_process.stderr is not None:
                    old_process.stderr.close()

        self.assertEqual(result["outcome"], "acquired")
        self.assertEqual(result["session_id"], "real-incarnation-contender")
        self.assertEqual(process_identity_state(old_identity), ProcessTreeState.STOPPED)
        self.assertEqual(
            process_identity_state(replacement_identity),
            ProcessTreeState.STOPPED,
        )
        contender_identity = ProcessIdentity(
            pid=int(result["pid"]),
            creation_time=int(result["creation_time"]),
        )
        self.assertEqual(
            process_identity_state(contender_identity),
            ProcessTreeState.STOPPED,
        )

    @staticmethod
    def _wait_for_path(path: Path) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        raise AssertionError(f"Timed out waiting for {path.name}.")

    @staticmethod
    def _wait_for_json_object(path: Path) -> dict[str, object]:
        deadline = time.monotonic() + 5
        last_error: OSError | json.JSONDecodeError | None = None
        while time.monotonic() < deadline:
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                last_error = error
                time.sleep(0.01)
                continue
            if isinstance(parsed, dict):
                return parsed
            raise AssertionError(f"Readiness payload in {path.name} is not an object.")
        raise AssertionError(
            f"Timed out waiting for complete JSON in {path.name}: {last_error}"
        )

    @staticmethod
    def _identity_from_payload(
        payload: dict[str, object],
        key: str,
    ) -> ProcessIdentity:
        value = payload.get(key)
        if (
            not isinstance(value, list)
            or len(value) != 2
            or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
            or any(item <= 0 for item in value)
        ):
            raise AssertionError(f"Readiness {key} identity is invalid: {value!r}")
        return ProcessIdentity(pid=value[0], creation_time=value[1])

    @staticmethod
    def _wait_for_identity_stopped(identity: ProcessIdentity) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process_identity_state(identity) is ProcessTreeState.STOPPED:
                return
            time.sleep(0.01)
        raise AssertionError(
            f"Process {identity.pid} did not reach a confirmed stopped state."
        )

    @staticmethod
    def _terminate_worker_process_tree(
        worker: ProcessIdentity,
        descendant: ProcessIdentity | None,
        *,
        platform_name: str | None = None,
    ) -> None:
        if process_identity_state(worker) is ProcessTreeState.STOPPED:
            return
        selected_platform = os.name if platform_name is None else platform_name
        if selected_platform == "posix":
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return
        if selected_platform == "nt":
            retained = {worker}
            if descendant is not None:
                retained.add(descendant)
            subprocess_utils._terminate_windows_process_tree(
                worker.pid,
                retained_identities=retained,
            )
            return
        raise AssertionError(f"Unsupported process-test platform: {selected_platform}")

    @classmethod
    def _cleanup_application_death_processes(
        cls,
        app: subprocess.Popen[str],
        worker: ProcessIdentity | None,
        descendant: ProcessIdentity | None,
    ) -> None:
        app_termination_state = ProcessTreeState.STOPPED
        if app.poll() is None:
            app_termination_state = terminate_process(app).state
        else:
            app.wait(timeout=5)
        if worker is not None:
            cls._terminate_worker_process_tree(worker, descendant)
            cls._wait_for_identity_stopped(worker)
        if descendant is not None:
            cls._wait_for_identity_stopped(descendant)
        if app_termination_state is not ProcessTreeState.STOPPED:
            raise AssertionError("Application process-tree cleanup was not confirmed.")

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
