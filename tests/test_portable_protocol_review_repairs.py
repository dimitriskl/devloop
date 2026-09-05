from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from unittest import mock

from devloop import portable_sessions
from devloop.portable_protocol import (
    MAX_PORTABLE_PROTOCOL_FRAME_BYTES,
    PortableProtocolError,
    PortableProtocolStreamDecoder,
)
from devloop.portable_session_catalog import PortableSessionCatalog
from devloop.portable_sessions import (
    PortableSessionLaunch,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
)
from devloop.portable_worker import PortableWorkerRuntimeBridge
from devloop.terminal_text import has_unsafe_terminal_controls


class PortableProtocolTransportRepairTests(unittest.TestCase):
    def test_production_launcher_uses_binary_protocol_pipes(self) -> None:
        checkout = Path.cwd().resolve()
        launch = PortableSessionLaunch(
            session_id="binary-production-launch",
            checkout=checkout,
            operation=PortableWorkflowOperation.PLANNING,
            arguments=(),
            argument_base=checkout,
        )
        process = mock.Mock(stdin=mock.Mock(), stdout=mock.Mock(), stderr=mock.Mock())

        with mock.patch.object(
            portable_sessions,
            "launch_process_tree",
            return_value=process,
        ) as launch_process:
            returned = portable_sessions._launch_portable_worker(launch)

        self.assertIs(returned, process)
        kwargs = launch_process.call_args.kwargs
        self.assertNotIn("text", kwargs)
        self.assertNotIn("encoding", kwargs)
        self.assertNotIn("errors", kwargs)

    def test_binary_worker_invalid_utf8_inside_json_fails_closed(self) -> None:
        worker_source = textwrap.dedent(
            r"""
            import json
            import sys

            session_id = sys.argv[1]
            json.loads(sys.stdin.buffer.readline().decode("utf-8"))
            prefix = (
                b'{"version":1,"session_id":"'
                + session_id.encode("ascii")
                + b'","sequence":1,"kind":"ACTIVITY","payload":{"message":"'
            )
            sys.stdout.buffer.write(prefix + b'valid-prefix-\xff-valid-suffix"}}\n')
            sys.stdout.buffer.flush()
            """
        )

        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)

            def launch_worker(launch: PortableSessionLaunch) -> subprocess.Popen[bytes]:
                return subprocess.Popen(
                    [sys.executable, "-u", "-c", worker_source, launch.session_id],
                    cwd=launch.checkout,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

            supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
            launch = PortableSessionLaunch(
                session_id="invalid-production-byte",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            try:
                supervisor.start_session(launch)
                failed = supervisor.wait_for_terminal(launch.session_id, timeout=5)
            finally:
                supervisor.shutdown()

        self.assertEqual(failed.status, PortableSessionStatus.FAILED)
        self.assertEqual(failed.activity, ())
        self.assertTrue(
            any("not valid UTF-8" in item for item in failed.diagnostics),
            failed.diagnostics,
        )


class PortableProtocolFrameLimitRepairTests(unittest.TestCase):
    def test_frame_limit_excludes_lf_and_crlf_terminators_identically(self) -> None:
        encoded = _exact_size_launch_frame(MAX_PORTABLE_PROTOCOL_FRAME_BYTES)
        self.assertEqual(len(encoded), MAX_PORTABLE_PROTOCOL_FRAME_BYTES)

        for ending in (b"\n", b"\r\n"):
            with self.subTest(ending=ending):
                decoder = PortableProtocolStreamDecoder.for_supervisor_commands("limit-session")
                frames = decoder.feed(encoded + ending)
                self.assertEqual(len(frames), 1)

    def test_frame_limit_plus_one_rejects_lf_and_crlf_identically(self) -> None:
        encoded = _exact_size_launch_frame(MAX_PORTABLE_PROTOCOL_FRAME_BYTES + 1)
        self.assertEqual(len(encoded), MAX_PORTABLE_PROTOCOL_FRAME_BYTES + 1)

        for ending in (b"\n", b"\r\n"):
            with self.subTest(ending=ending):
                decoder = PortableProtocolStreamDecoder.for_supervisor_commands("limit-session")
                with self.assertRaisesRegex(PortableProtocolError, "exceeds 65536 bytes"):
                    decoder.feed(encoded + ending)


class PortableCheckpointSummaryRepairTests(unittest.TestCase):
    def test_hostile_checkpoint_summary_is_safe_in_pause_catalog_reload_and_render(self) -> None:
        worker_source = textwrap.dedent(
            r"""
            import json
            import os
            import sys

            from devloop.portable_session_catalog import (
                PortablePlanningSettings,
                active_portable_catalog_session,
            )

            session_id = sys.argv[1]
            start = json.loads(sys.stdin.buffer.readline().decode("utf-8"))
            active = active_portable_catalog_session()
            assert active is not None
            catalog, record, _restore = active
            settings = PortablePlanningSettings(
                backend="CODEX_CLI",
                model="gpt-5.4",
                reasoning_effort="high",
                fast="OFF",
                timeout_seconds=1200,
                checkpoint_seconds=300,
            )
            thread_id = "11111111-2222-4333-8444-555555555555"
            catalog.save_planning_settings(session_id, settings)
            catalog.save_planning_thread(session_id, thread_id)

            def send(sequence, kind, payload):
                frame = json.dumps({
                    "version": 1,
                    "session_id": session_id,
                    "sequence": sequence,
                    "kind": kind,
                    "payload": payload,
                }, separators=(",", ":"))
                sys.stdout.buffer.write((frame + "\n").encode("utf-8"))
                sys.stdout.buffer.flush()

            send(1, "INPUT_REQUEST", {
                "request_id": "pause-request",
                "request_generation": 1,
                "request_kind": "TEXT",
                "prompt": "Pause me",
            })
            command = json.loads(sys.stdin.buffer.readline().decode("utf-8"))
            send(2, "CHECKPOINT", {
                "checkpoint_kind": "PLANNING",
                "planning_thread_id": thread_id,
                "planning_settings": settings.to_dict(),
                "summary": "Authorization: Bearer secret-token\x1b[2J\n forged row",
                **command["payload"],
            })
            """
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            catalog = PortableSessionCatalog(root / "catalog.sqlite3")
            owner_id = "checkpoint-summary-shell"

            def launch_worker(launch: PortableSessionLaunch) -> subprocess.Popen[bytes]:
                environment = os.environ.copy()
                environment["DEVLOOP_PORTABLE_SESSION_CATALOG"] = str(catalog.path)
                environment["DEVLOOP_PORTABLE_SESSION_ID"] = launch.session_id
                environment["DEVLOOP_PORTABLE_SESSION_OWNER_ID"] = owner_id
                return subprocess.Popen(
                    [sys.executable, "-u", "-c", worker_source, launch.session_id],
                    cwd=launch.checkout,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

            supervisor = PortableSessionSupervisor(
                worker_launcher=launch_worker,
                catalog=catalog,
                owner_id=owner_id,
            )
            launch = PortableSessionLaunch(
                session_id="hostile-checkpoint-summary",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            try:
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
                durable = catalog.get_session(launch.session_id)
            finally:
                supervisor.shutdown()

            with closing(sqlite3.connect(catalog.path)) as connection:
                connection.execute(
                    "UPDATE sessions SET activity_summary = ? WHERE session_id = ?",
                    (
                        "Authorization: Bearer catalog-token\x1b[2J\n forged reload",
                        launch.session_id,
                    ),
                )
                connection.commit()
            reloaded_supervisor = PortableSessionSupervisor(catalog=catalog)
            try:
                reloaded = reloaded_supervisor.snapshot(launch.session_id)
            finally:
                reloaded_supervisor.shutdown()

        for value in (paused.activity[-1], durable.activity_summary, reloaded.activity[-1]):
            self.assertFalse(has_unsafe_terminal_controls(value), value)
            self.assertNotIn("-token", value)
            self.assertNotIn("\n", value)
            self.assertIn("[redacted]", value.lower())

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
        raise AssertionError(supervisor.snapshot(session_id))


class PortableNestedSchemaRepairTests(unittest.TestCase):
    def test_planning_settings_are_fully_typed_closed_and_bounded(self) -> None:
        valid = _launch_payload()
        invalid_settings = (
            {**valid["planning_settings"], "backend": "UNKNOWN"},
            {**valid["planning_settings"], "fast": "MAYBE"},
            {**valid["planning_settings"], "timeout_seconds": 0},
            {**valid["planning_settings"], "timeout_seconds": float("inf")},
            {**valid["planning_settings"], "checkpoint_seconds": 1201},
            {**valid["planning_settings"], "model": ""},
            {**valid["planning_settings"], "model": "x" * 201},
            {**valid["planning_settings"], "model": ["gpt-5.4"]},
            {**valid["planning_settings"], "unexpected": True},
        )

        _decode_launch(valid)
        for settings in invalid_settings:
            with self.subTest(settings=settings):
                payload = deepcopy(valid)
                payload["planning_settings"] = settings
                with self.assertRaises(PortableProtocolError):
                    _decode_launch(payload)

    def test_recovery_schema_validates_nullable_fields_enums_paths_and_lists(self) -> None:
        planning = _launch_payload()["recovery"]
        assert isinstance(planning, dict)
        delivery = {
            "checkpoint_kind": "PRD",
            "checkout": str(Path.cwd().resolve()),
            "activity": ["safe"],
            "diagnostics": [],
            "planning_thread_id": None,
            "planning_settings": None,
            "prd_path": str((Path.cwd() / "prd.md").resolve()),
            "issues_index_path": str((Path.cwd() / "issues.md").resolve()),
            "issue_id": "0011",
            "next_role": "reviewer",
            "pass_number": 2,
        }
        _decode_launch(_launch_payload(recovery=planning))
        _decode_launch(_launch_payload(recovery=delivery))

        invalid_recoveries = []
        for key, value in (
            ("checkpoint_kind", "UNKNOWN"),
            ("checkout", "relative/path"),
            ("activity", ["item"] * 21),
            ("activity", ["x" * 2001]),
            ("diagnostics", [4]),
            ("planning_thread_id", "not-a-uuid"),
            ("prd_path", "unexpected.md"),
        ):
            invalid = deepcopy(planning)
            invalid[key] = value
            invalid_recoveries.append(invalid)
        invalid = deepcopy(planning)
        invalid["unexpected"] = True
        invalid_recoveries.append(invalid)
        for key, value in (
            ("next_role", "intruder"),
            ("pass_number", True),
            ("prd_path", None),
            ("planning_thread_id", "11111111-2222-4333-8444-555555555555"),
        ):
            invalid = deepcopy(delivery)
            invalid[key] = value
            invalid_recoveries.append(invalid)

        for recovery in invalid_recoveries:
            with self.subTest(recovery=recovery):
                with self.assertRaises(PortableProtocolError):
                    _decode_launch(_launch_payload(recovery=recovery))


class PortableApprovalBridgeRepairTests(unittest.TestCase):
    def test_real_worker_bridge_round_trips_a_typed_approval_decision(self) -> None:
        worker_source = textwrap.dedent(
            r"""
            import sys

            from devloop.portable_worker import (
                PortableWorkerRuntimeBridge,
                _read_launch_frame,
            )

            session_id = sys.argv[1]
            _read_launch_frame(session_id, sys.stdin.buffer)
            bridge = PortableWorkerRuntimeBridge(
                session_id,
                command_stream=sys.stdin.buffer,
                event_stream=sys.stdout.buffer,
            )
            decision = bridge.request_approval(
                "Allow the bounded action?",
                supported_decisions=("DENY", "APPROVE_ONCE"),
                default_decision="DENY",
                cancel_decision="DENY",
            )
            bridge.write_output("decision=" + decision, is_error=False)
            bridge.send_completion(0)
            """
        )

        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)

            def launch_worker(launch: PortableSessionLaunch) -> subprocess.Popen[bytes]:
                return subprocess.Popen(
                    [sys.executable, "-u", "-c", worker_source, launch.session_id],
                    cwd=launch.checkout,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

            supervisor = PortableSessionSupervisor(worker_launcher=launch_worker)
            launch = PortableSessionLaunch(
                session_id="real-approval-bridge",
                checkout=checkout,
                operation=PortableWorkflowOperation.PLANNING,
                arguments=(),
            )
            try:
                supervisor.start_session(launch)
                waiting = PortableCheckpointSummaryRepairTests._wait_for_status(
                    supervisor,
                    launch.session_id,
                    PortableSessionStatus.WAITING_FOR_INPUT,
                )
                assert waiting.input_request is not None
                self.assertEqual(waiting.input_request.kind.value, "APPROVAL")
                with self.assertRaisesRegex(ValueError, "offered decisions"):
                    supervisor.provide_input(
                        launch.session_id,
                        "APPROVE_FOR_SESSION",
                        request_id=waiting.input_request.request_id,
                        request_generation=waiting.input_request.generation,
                    )
                supervisor.provide_input(
                    launch.session_id,
                    "APPROVE_ONCE",
                    request_id=waiting.input_request.request_id,
                    request_generation=waiting.input_request.generation,
                )
                completed = supervisor.wait_for_terminal(launch.session_id, timeout=5)
            finally:
                supervisor.shutdown()

        self.assertEqual(completed.status, PortableSessionStatus.COMPLETED)
        self.assertIn("decision=APPROVE_ONCE", completed.activity)

    def test_approval_protocol_rejects_open_decisions_and_wrong_response_kind(self) -> None:
        invalid_request = {
            "request_kind": "APPROVAL",
            "request_id": "approval-1",
            "request_generation": 1,
            "prompt": "Approve?",
            "options": [["INJECTED", "Injected"]],
            "default_key": "INJECTED",
            "cancel_key": "INJECTED",
        }
        with self.assertRaises(PortableProtocolError):
            _decode_worker_event("INPUT_REQUEST", invalid_request)

        invalid_response = {
            "decision": "INJECTED",
            "request_id": "approval-1",
            "request_generation": 1,
        }
        with self.assertRaises(PortableProtocolError):
            _decode_supervisor_command("APPROVAL_DECISION", invalid_response, sequence=2)

        mismatches = (
            ("USER_INPUT", "approval-1", 1, {"value": "DENY"}),
            ("APPROVAL_DECISION", "stale-request", 1, {"decision": "DENY"}),
            ("APPROVAL_DECISION", "approval-1", 2, {"decision": "DENY"}),
        )
        for kind, request_id, generation, response in mismatches:
            with self.subTest(kind=kind, request_id=request_id, generation=generation):
                response.update(
                    request_id=request_id,
                    request_generation=generation,
                )
                command = json.dumps(
                    {
                        "version": 1,
                        "session_id": "approval-worker",
                        "sequence": 2,
                        "kind": kind,
                        "payload": response,
                    },
                    separators=(",", ":"),
                )
                bridge = PortableWorkerRuntimeBridge(
                    "approval-worker",
                    command_stream=io.StringIO(command + "\n"),
                    event_stream=io.StringIO(),
                )
                with (
                    mock.patch(
                        "devloop.portable_worker.uuid.uuid4",
                        return_value="approval-1",
                    ),
                    self.assertRaises(PortableProtocolError),
                ):
                    bridge.request_approval(
                        "Approve?",
                        supported_decisions=("DENY", "APPROVE_ONCE"),
                        default_decision="DENY",
                        cancel_decision="DENY",
                    )


def _exact_size_launch_frame(size: int) -> bytes:
    value = {
        "version": 1,
        "session_id": "limit-session",
        "sequence": 1,
        "kind": "START",
        "payload": {
            "operation": "PLANNING",
            "arguments": [],
            "owner_id": "owner",
            "worker_generation": 1,
        },
    }
    while True:
        baseline = json.dumps(value, separators=(",", ":")).encode("utf-8")
        remaining = size - len(baseline)
        if remaining <= 8194:
            value["payload"]["arguments"].append("x" * (remaining - 3))
            break
        value["payload"]["arguments"].append("x" * 8192)
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _planning_settings() -> dict[str, object]:
    return {
        "backend": "CODEX_CLI",
        "model": "gpt-5.4",
        "reasoning_effort": "high",
        "fast": "OFF",
        "timeout_seconds": 1200,
        "checkpoint_seconds": 300,
    }


def _launch_payload(*, recovery: object | None = None) -> dict[str, object]:
    settings = _planning_settings()
    if recovery is None:
        recovery = {
            "checkpoint_kind": "PLANNING",
            "checkout": str(Path.cwd().resolve()),
            "activity": ["safe"],
            "diagnostics": [],
            "planning_thread_id": "11111111-2222-4333-8444-555555555555",
            "planning_settings": settings,
            "prd_path": None,
            "issues_index_path": None,
            "issue_id": None,
            "next_role": None,
            "pass_number": None,
        }
    return {
        "operation": "PLANNING",
        "arguments": [],
        "owner_id": "owner",
        "worker_generation": 1,
        "planning_settings": settings,
        "recovery": recovery,
    }


def _decode_launch(payload: object) -> None:
    frame = json.dumps(
        {
            "version": 1,
            "session_id": "schema-session",
            "sequence": 1,
            "kind": "START",
            "payload": payload,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    decoder = PortableProtocolStreamDecoder.for_supervisor_commands("schema-session")
    decoder.feed(frame + b"\n")


def _decode_worker_event(kind: str, payload: object) -> None:
    _decode_frame(kind, payload, worker_event=True, sequence=1)


def _decode_supervisor_command(kind: str, payload: object, *, sequence: int) -> None:
    _decode_frame(kind, payload, worker_event=False, sequence=sequence)


def _decode_frame(kind: str, payload: object, *, worker_event: bool, sequence: int) -> None:
    frame = json.dumps(
        {
            "version": 1,
            "session_id": "schema-session",
            "sequence": sequence,
            "kind": kind,
            "payload": payload,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    decoder = (
        PortableProtocolStreamDecoder.for_worker_events(
            "schema-session",
            expected_sequence=sequence,
        )
        if worker_event
        else PortableProtocolStreamDecoder.for_supervisor_commands(
            "schema-session",
            expected_sequence=sequence,
        )
    )
    decoder.feed(frame + b"\n")


if __name__ == "__main__":
    unittest.main()
