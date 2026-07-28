from __future__ import annotations

import io
import json
import threading
import unittest
from collections.abc import Mapping
from typing import Any
from unittest import mock

from devloop import cli
from devloop.portable_protocol import PortableProtocolError, PortableProtocolFrame
from devloop.portable_runtime import PortableRuntimeStopped, portable_runtime_session
from devloop.portable_worker import PortableWorkerRuntimeBridge
from devloop.statusui import Stage


class PortableWorkerRuntimeBridgeTests(unittest.TestCase):
    def test_pause_releases_a_worker_waiting_for_input(self) -> None:
        command_stream = io.StringIO(
            json.dumps(
                {
                    "version": 1,
                    "session_id": "session-pause",
                    "sequence": 2,
                    "kind": "PAUSE",
                    "payload": {},
                }
            )
            + "\n"
        )
        bridge = PortableWorkerRuntimeBridge(
            "session-pause",
            command_stream=command_stream,
            event_stream=io.StringIO(),
        )

        with self.assertRaises(PortableRuntimeStopped):
            bridge.read_line("Planning prompt")

        self.assertEqual(bridge.lifecycle_request, "PAUSE")

    def test_active_worker_observes_pause_at_runtime_checkpoint(self) -> None:
        command_stream = io.StringIO(
            json.dumps(
                {
                    "version": 1,
                    "session_id": "session-active-pause",
                    "sequence": 2,
                    "kind": "PAUSE",
                    "payload": {},
                }
            )
            + "\n"
        )
        bridge = PortableWorkerRuntimeBridge(
            "session-active-pause",
            command_stream=command_stream,
            event_stream=io.StringIO(),
        )
        bridge.start_control_reader()
        deadline = threading.Event()
        for _ in range(100):
            if bridge.lifecycle_request == "PAUSE":
                break
            deadline.wait(0.001)

        with self.assertRaises(PortableRuntimeStopped):
            bridge.show_screen("Next durable runtime boundary")

    def test_choice_request_identity_round_trips_through_user_input(self) -> None:
        command_stream = io.StringIO(
            json.dumps(
                {
                    "version": 1,
                    "session_id": "session-choice",
                    "sequence": 2,
                    "kind": "USER_INPUT",
                    "payload": {
                        "value": "accept",
                        "request_id": "fixed-request",
                        "request_generation": 1,
                    },
                }
            )
            + "\n"
        )
        event_stream = io.StringIO()
        bridge = PortableWorkerRuntimeBridge(
            "session-choice",
            command_stream=command_stream,
            event_stream=event_stream,
        )

        with mock.patch(
            "devloop.portable_worker.uuid.uuid4",
            return_value="fixed-request",
        ):
            selected = bridge.choose(
                (("accept", "Accept"), ("deny", "Deny")),
                default_key="deny",
                cancel_key="deny",
                render=lambda _content: None,
            )

        request = json.loads(event_stream.getvalue())
        self.assertEqual(selected, "accept")
        self.assertEqual(request["payload"]["request_id"], "fixed-request")
        self.assertEqual(request["payload"]["request_generation"], 1)

    def test_worker_rejects_user_input_for_another_request(self) -> None:
        command_stream = io.StringIO(
            json.dumps(
                {
                    "version": 1,
                    "session_id": "session-text",
                    "sequence": 2,
                    "kind": "USER_INPUT",
                    "payload": {
                        "value": "stale",
                        "request_id": "request-a",
                        "request_generation": 1,
                    },
                }
            )
            + "\n"
        )
        bridge = PortableWorkerRuntimeBridge(
            "session-text",
            command_stream=command_stream,
            event_stream=io.StringIO(),
        )

        with (
            mock.patch(
                "devloop.portable_worker.uuid.uuid4",
                return_value="request-b",
            ),
            self.assertRaisesRegex(
                PortableProtocolError,
                "does not match the current input request",
            ),
        ):
            bridge.read_line("New value")

    def test_delivery_role_transition_emits_stage_issue_and_pass_status(self) -> None:
        event_stream = io.StringIO()
        bridge = PortableWorkerRuntimeBridge(
            "session-delivery",
            command_stream=io.StringIO(),
            event_stream=event_stream,
        )

        class RecordingDashboard:
            enabled = True
            has_workflow_progress = True

            def begin_role(self, stage: Stage, pass_number: int) -> None:
                self.started = (stage, pass_number)

        dashboard = RecordingDashboard()
        with portable_runtime_session(bridge):
            cli.begin_role_output(
                dashboard,  # type: ignore[arg-type]
                Stage.REVIEW,
                "issue 0004 / pass 3",
                "0004",
                3,
                "Security Review",
            )

        frame = json.loads(event_stream.getvalue())
        self.assertEqual(
            frame,
            {
                "version": 1,
                "session_id": "session-delivery",
                "sequence": 1,
                "kind": "STATUS",
                "payload": {
                    "status": "RUNNING",
                    "stage": "Security Review · pass 3",
                    "active_issue": "0004",
                },
            },
        )
        self.assertEqual(dashboard.started, (Stage.REVIEW, 3))

    def test_concurrent_events_are_written_in_allocated_sequence_order(self) -> None:
        event_stream = io.StringIO()
        bridge = PortableWorkerRuntimeBridge(
            "session-concurrent",
            command_stream=io.StringIO(),
            event_stream=event_stream,
        )
        first_frame_allocated = threading.Event()
        second_frame_allocated = threading.Event()
        release_first_frame = threading.Event()
        real_frame = PortableProtocolFrame

        def construct_frame(
            *,
            version: int,
            session_id: str,
            sequence: int,
            kind: str,
            payload: Mapping[str, Any],
        ) -> PortableProtocolFrame:
            if sequence == 1:
                first_frame_allocated.set()
                release_first_frame.wait(timeout=1)
            elif sequence == 2:
                second_frame_allocated.set()
            return real_frame(
                version=version,
                session_id=session_id,
                sequence=sequence,
                kind=kind,
                payload=payload,
            )

        with mock.patch(
            "devloop.portable_worker.PortableProtocolFrame",
            side_effect=construct_frame,
        ):
            first_emitter = threading.Thread(target=bridge.send_hello)
            second_emitter = threading.Thread(
                target=bridge.send_completion,
                args=(0,),
            )
            first_emitter.start()
            self.assertTrue(first_frame_allocated.wait(timeout=1))
            second_emitter.start()
            second_frame_allocated.wait(timeout=0.2)
            release_first_frame.set()
            first_emitter.join(timeout=1)
            second_emitter.join(timeout=1)

        self.assertFalse(first_emitter.is_alive())
        self.assertFalse(second_emitter.is_alive())
        sequences = [
            json.loads(line)["sequence"]
            for line in event_stream.getvalue().splitlines()
        ]
        self.assertEqual(sequences, [1, 2])


if __name__ == "__main__":
    unittest.main()
