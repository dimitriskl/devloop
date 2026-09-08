from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from threading import Event, Thread
from unittest import mock

from devloop.cli import execute_dependency_schedule
from devloop.codex_runner import CodexRunner, RoleResult, RunWideBlockerError
from devloop.issue_pack import Issue
from devloop.issue_scheduler import IssueDependencyGraph, SchedulingPhase
from devloop.portable_execution_backend import RunWideBlocker, RunWideBlockerKind, codex_cli
from devloop.portable_execution_backend.claude_code import claude_run_wide_blocker
from devloop.portable_protocol import PortableProtocolError, SupervisorMessageKind
from devloop.portable_runtime import PortableRuntimeBridge, PortableRuntimeStopped
from devloop.portable_worker import PortableWorkerRuntimeBridge
from devloop.state import LoopStateWriter
from devloop.templates import BundleContext
from devloop.usage_limit_retry import retry_usage_limited_run


class UsageLimitRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.issue = Issue('0002', 'Current issue', root / '0002.md', False)
        self.writer = LoopStateWriter(root / 'README.md')
        self.writer.issue_state(self.issue).update(
            status='IN_PROGRESS', current_pass=2, current_step_instance_id='development'
        )
        self.writer.reserve_scheduling_attempt(
            self.issue, phase=SchedulingPhase.NORMAL_SCHEDULING, ordinal=1
        )
        self.now = 1000.0
        self.messages: list[str] = []

    def wait(self, seconds: float) -> None:
        self.now += seconds

    def run_retry(self, operation, **kwargs):
        return retry_usage_limited_run(
            operation, self.writer, self.messages.append,
            clock=lambda: self.now, wait=kwargs.get('wait', self.wait)
        )

    def blocker(self, reset_at=None):
        return RunWideBlockerError(
            RunWideBlocker(RunWideBlockerKind.USAGE_LIMIT, 'Usage exhausted.', reset_at)
        )

    def test_waits_for_reset_then_five_minutes_even_if_retry_reports_another_reset(self):
        calls = []

        def operation():
            calls.append(self.now)
            if len(calls) == 1:
                raise self.blocker(1010)
            if len(calls) == 2:
                raise self.blocker(5000)
            return 'done'

        self.assertEqual(self.run_retry(operation), 'done')
        self.assertEqual(calls, [1000, 1010, 1310])
        self.assertIn('00:10', self.messages[0])
        self.assertIsNone(self.writer.run_pause())
        self.assertEqual(self.writer.issue_state(self.issue)['current_pass'], 2)
        self.assertEqual(self.writer.additional_passes(), {})

    def test_codex_backend_retries_usage_limits_until_role_succeeds(self) -> None:
        root = self.issue.path.parent
        runner = CodexRunner.__new__(CodexRunner)
        runner.bundle = BundleContext(root=root, prompts=root, schemas=root)
        runner.repo_root = root
        runner.prd_path = root / "prd.md"
        runner.issues_index = self.writer.issues_index
        runner.log_root = root / ".loop.logs"
        runner.execution_backend = codex_cli.CodexCliExecutionBackend()
        runner.ensure_log_root()
        partial_file = root / "partial-work.txt"
        partial_file.write_text("Work before the limit.", encoding="utf-8")
        attempts: list[float] = []

        def transport(**_arguments: object) -> CompletedProcess[str]:
            attempts.append(self.now)
            self.assertEqual(self.writer.issue_state(self.issue)["current_pass"], 2)
            if len(attempts) < 3:
                # Exercise both error envelopes accepted by the real Codex adapter.
                event = (
                    {"type": "error", "message": "You have hit your usage limit."}
                    if len(attempts) == 1 else
                    {"type": "turn.failed", "error": {"message": "Usage limit exceeded."}}
                )
                return CompletedProcess(["codex"], 1, json.dumps(event), "")
            event = {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps({"status": "PASS"})},
            }
            return CompletedProcess(["codex"], 0, json.dumps(event), "")

        with (
            mock.patch.object(runner, "build_prompt", return_value="Continue the issue."),
            mock.patch.object(codex_cli, "build_codex_exec_command", return_value=["codex"]),
            mock.patch.object(codex_cli, "run_codex_exec_with_connection_retries", transport),
        ):
            result = self.run_retry(lambda: runner.run_role("coder", self.issue, pass_number=2))

        self.assertEqual(result.status, "PASS")
        self.assertEqual(attempts, [1000, 1300, 1600])
        self.assertIn("05:00", self.messages[0])
        self.assertIsNone(self.writer.run_pause())
        self.assertEqual(self.writer.additional_passes(), {})
        self.assertEqual(self.writer.active_scheduling_attempt()["ordinal"], 1)
        self.assertEqual(partial_file.read_text(encoding="utf-8"), "Work before the limit.")

    def test_later_issue_uses_its_own_reset_after_previous_issue_recovers(self):
        calls = []

        def operation():
            calls.append(self.now)
            if len(calls) == 1:
                raise self.blocker(1010)
            if len(calls) == 2:
                self.writer.clear_run_pause()
                raise self.blocker(1500)
            return 'done'

        self.run_retry(operation)
        self.assertEqual(calls, [1000, 1010, 1500])

    def test_missing_expired_and_invalid_resets_use_five_minutes(self):
        for reset in (None, 999, True, 'tomorrow', float('nan'), float('inf'), 10**1000):
            with self.subTest(reset=str(reset)[:20]):
                self.now = 1000
                operation = mock.Mock(side_effect=[self.blocker(reset), 'done'])
                self.assertEqual(self.run_retry(operation), 'done')
                self.assertEqual(self.now, 1300)

    def test_other_blockers_do_not_retry(self):
        for kind in RunWideBlockerKind:
            if kind is RunWideBlockerKind.USAGE_LIMIT:
                continue
            operation = mock.Mock(side_effect=RunWideBlockerError(RunWideBlocker(kind, 'Stop')))
            with self.assertRaises(RunWideBlockerError):
                self.run_retry(operation)
            operation.assert_called_once()
            self.assertEqual(self.now, 1000)

    def test_pause_preserves_deadline_and_resume_waits_remaining_time(self):
        operation = mock.Mock(side_effect=self.blocker(1010))
        with self.assertRaises(PortableRuntimeStopped):
            self.run_retry(operation, wait=mock.Mock(side_effect=PortableRuntimeStopped()))
        self.writer = LoopStateWriter(self.writer.issues_index)
        self.assertEqual(self.writer.run_pause()['retry_at'], 1010)
        self.assertEqual(self.writer.run_pause()['pass'], 2)
        self.now = 1004
        observed = []
        self.run_retry(lambda: observed.append(self.now))
        self.assertEqual(observed, [1010])

    def test_real_scheduler_retries_reserved_issue_before_dependent_work(self):
        other = Issue('0003', 'Dependent', self.issue.path.parent / '0003.md', False, ('0002',))
        issues = [self.issue, other]
        calls = []

        def execute(issue, phase, ordinal):
            calls.append((issue.number, phase, ordinal))
            if len(calls) == 1:
                raise self.blocker(1002)
            self.writer.issue_state(issue)['status'] = 'COMPLETED'
            self.writer.flush()
            return RoleResult(status='PASS')

        def schedule():
            return execute_dependency_schedule(
                issues=issues, graph=IssueDependencyGraph(issues),
                state_writer=self.writer, execute_issue=execute
            )

        self.assertTrue(self.run_retry(schedule).completed)
        self.assertEqual([call[0] for call in calls], ['0002', '0002', '0003'])
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(self.writer.additional_passes(), {})


class ProviderResetTests(unittest.TestCase):
    def test_reads_real_rejected_event_shape_and_ignores_allowed_windows(self):
        # Shape and timestamp verified against the 2026-09-07 eConnectorV2 log.
        events = [
            {'type': 'rate_limit_event', 'rate_limit_info': {
                'status': 'allowed_warning', 'resetsAt': 1789164000,
                'overageStatus': 'rejected'}},
            {'type': 'rate_limit_event', 'rate_limit_info': {
                'status': 'rejected', 'resetsAt': 1788796800,
                'rateLimitType': 'five_hour', 'overageStatus': 'rejected',
                'overageDisabledReason': 'org_level_disabled', 'isUsingOverage': False}},
        ]
        blocker = claude_run_wide_blocker(None, stdout='\n'.join(map(json.dumps, events)))
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker.reset_at, 1788796800)

    def test_invalid_reset_does_not_hide_usage_limit(self):
        for reset in (None, True, '1788796800', -1, float('inf'), 10**1000):
            event = {'type': 'rate_limit_event', 'rate_limit_info': {
                'status': 'rejected', 'resetsAt': reset}}
            blocker = claude_run_wide_blocker(None, stdout=json.dumps(event))
            self.assertIs(blocker.kind, RunWideBlockerKind.USAGE_LIMIT)
            self.assertIsNone(blocker.reset_at)


class RetryLifecycleTests(unittest.TestCase):
    def test_application_stop_interrupts_wait(self):
        bridge = PortableRuntimeBridge()
        bridge.request_stop()
        with self.assertRaises(PortableRuntimeStopped):
            bridge.wait_for_retry(300)

    def test_every_worker_lifecycle_command_interrupts_wait(self):
        for kind in (SupervisorMessageKind.PAUSE, SupervisorMessageKind.CANCEL,
                     SupervisorMessageKind.FORCE_STOP, SupervisorMessageKind.SHUTDOWN):
            bridge = PortableWorkerRuntimeBridge(
                'session', command_stream=io.BytesIO(), event_stream=io.BytesIO()
            )
            frame = mock.Mock(kind=kind.value)
            with mock.patch('devloop.portable_worker._read_command_frame', return_value=frame):
                bridge._read_commands(None)
            with self.assertRaises(PortableRuntimeStopped):
                bridge.wait_for_retry(300)

    def test_stop_wakes_a_wait_already_in_progress(self):
        bridge = PortableWorkerRuntimeBridge(
            'session', command_stream=io.BytesIO(), event_stream=io.BytesIO()
        )
        entered = Event()
        stopped = Event()

        def waiting():
            entered.set()
            try:
                bridge.wait_for_retry(0.5)
            except PortableRuntimeStopped:
                stopped.set()

        thread = Thread(target=waiting)
        thread.start()
        self.assertTrue(entered.wait(1))
        bridge.request_stop()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(stopped.is_set())

    def test_closed_supervisor_stream_prevents_retry(self):
        bridge = PortableWorkerRuntimeBridge(
            'session', command_stream=io.BytesIO(), event_stream=io.BytesIO()
        )
        bridge._read_commands(None)
        with self.assertRaises(PortableProtocolError):
            bridge.wait_for_retry(300)
