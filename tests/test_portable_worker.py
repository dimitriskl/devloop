from __future__ import annotations

import io
import json
import threading
import unittest
from collections.abc import Mapping
from typing import Any
from unittest import mock

from devloop.portable_protocol import PortableProtocolFrame
from devloop.portable_worker import PortableWorkerRuntimeBridge


class PortableWorkerRuntimeBridgeTests(unittest.TestCase):
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
