from __future__ import annotations

import json
import os
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


class PortableSessionSupervisorTests(unittest.TestCase):
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

            supervisor.provide_input(launch.session_id, "start")
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

            supervisor.provide_input(launch.session_id, "continue")
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
            same_app.provide_input(launch.session_id, "continue")
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
            restarted.provide_input(launch.session_id, "continue")
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
            supervisor.provide_input(launch.session_id, "yes")
            supervisor.wait_for_terminal(launch.session_id, timeout=5)
            supervisor.shutdown()

        self.assertEqual(
            waiting.progress,
            PortableSessionProgress(
                stage="development",
                completed_issues=2,
                total_issues=5,
                active_issue="0003",
            ),
        )
        self.assertGreater(waiting.updated_at, 0)

    def test_resume_candidate_projects_persisted_progress_before_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            prd_path = checkout / "change.md"
            candidate = SimpleNamespace(
                candidate_id="candidate-progress",
                checkout=checkout,
                prd_path=prd_path,
                completed_issues=4,
                pending_issues=1,
                total_issues=5,
                active_issue="0005",
                active_status="qa",
                updated_at=1_720_000_000.0,
            )

            supervisor = PortableSessionSupervisor(resume_candidates=(candidate,))
            snapshot = supervisor.snapshot(candidate.candidate_id)
            supervisor.shutdown()

        self.assertEqual(
            snapshot.progress,
            PortableSessionProgress(
                stage="qa",
                completed_issues=4,
                total_issues=5,
                active_issue="0005",
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
                active_status="development",
                updated_at=10.0,
            )
            advanced = SimpleNamespace(
                candidate_id=initial.candidate_id,
                checkout=checkout,
                prd_path=prd_path,
                completed_issues=2,
                total_issues=3,
                active_issue="0003",
                active_status="review",
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
            supervisor.provide_input(initial.candidate_id, "yes")
            supervisor.wait_for_terminal(initial.candidate_id, timeout=5)
            supervisor.shutdown()

        self.assertEqual(
            waiting.progress,
            PortableSessionProgress(
                stage="review",
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
                supervisor.provide_input(snapshot.session_id, "stale input")

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
            supervisor.provide_input(session_id, "stale input")
        with self.assertRaisesRegex(ValueError, "Unknown portable session"):
            supervisor.provide_input("unknown-session", "input")

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

            supervisor.provide_input(failing.session_id, "alpha-only")
            failed = supervisor.wait_for_terminal(failing.session_id, timeout=5)
            still_waiting = supervisor.snapshot(continuing.session_id)
            supervisor.provide_input(continuing.session_id, "beta-only")
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
        self.fail(f"Portable session did not reach {expected.value}.")


if __name__ == "__main__":
    unittest.main()
