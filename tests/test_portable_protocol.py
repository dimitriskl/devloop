from __future__ import annotations

import json
import unittest

from devloop.portable_protocol import (
    MAX_PORTABLE_PROTOCOL_FRAME_BYTES,
    PortableProtocolError,
    PortableProtocolStreamDecoder,
    WorkerMessageKind,
)


class PortableProtocolStreamDecoderTests(unittest.TestCase):
    def test_partial_reads_multiple_frames_and_platform_endings_are_deterministic(
        self,
    ) -> None:
        decoder = PortableProtocolStreamDecoder.for_worker_events("session-a")
        first = _frame(1, "HELLO", {}) + "\r\n"
        second = _frame(2, "ACTIVITY", {"message": "working"}) + "\n"
        encoded = (first + second).encode("utf-8")

        frames = (
            *decoder.feed(encoded[:7]),
            *decoder.feed(encoded[7:31]),
            *decoder.feed(encoded[31:]),
            *decoder.finish(),
        )

        self.assertEqual(
            [(frame.sequence, frame.kind) for frame in frames],
            [(1, WorkerMessageKind.HELLO.value), (2, WorkerMessageKind.ACTIVITY.value)],
        )

    def test_invalid_encoding_and_unterminated_frames_fail_closed(self) -> None:
        with self.assertRaisesRegex(PortableProtocolError, "valid UTF-8"):
            PortableProtocolStreamDecoder.for_worker_events("session-a").feed(
                b"\xff\n"
            )
        decoder = PortableProtocolStreamDecoder.for_worker_events("session-a")
        decoder.feed(_frame(1, "HELLO", {}).encode("utf-8"))
        with self.assertRaisesRegex(PortableProtocolError, "partial frame"):
            decoder.finish()

    def test_oversized_frame_is_rejected_before_a_newline_arrives(self) -> None:
        decoder = PortableProtocolStreamDecoder.for_worker_events("session-a")

        with self.assertRaisesRegex(PortableProtocolError, "exceeds 65536 bytes"):
            decoder.feed(b"x" * (MAX_PORTABLE_PROTOCOL_FRAME_BYTES + 1))

    def test_missing_extra_unknown_and_schema_invalid_fields_fail_closed(self) -> None:
        invalid_values = (
            {
                "version": 1,
                "session_id": "session-a",
                "sequence": 1,
                "kind": "HELLO",
            },
            {
                "version": 1,
                "session_id": "session-a",
                "sequence": 1,
                "kind": "HELLO",
                "payload": {},
                "unexpected": True,
            },
            {
                "version": 1,
                "session_id": "session-a",
                "sequence": 1,
                "kind": "NOT_A_KIND",
                "payload": {},
            },
            {
                "version": 1,
                "session_id": "session-a",
                "sequence": 1,
                "kind": "ACTIVITY",
                "payload": {"message": 4},
            },
            {
                "version": 1,
                "session_id": "session-a",
                "sequence": 1,
                "kind": "COMPLETION",
                "payload": {"exit_code": 0, "provider_payload": "raw"},
            },
        )

        for value in invalid_values:
            with self.subTest(value=value):
                decoder = PortableProtocolStreamDecoder.for_worker_events("session-a")
                with self.assertRaises(PortableProtocolError):
                    decoder.feed(
                        (json.dumps(value, separators=(",", ":")) + "\n").encode(
                            "utf-8"
                        )
                    )

    def test_duplicate_stale_skipped_and_out_of_order_sequences_fail_closed(self) -> None:
        for sequence in (1, 0, 3, 4):
            with self.subTest(sequence=sequence):
                decoder = PortableProtocolStreamDecoder.for_worker_events("session-a")
                decoder.feed((_frame(1, "HELLO", {}) + "\n").encode("utf-8"))
                with self.assertRaises(PortableProtocolError):
                    decoder.feed(
                        (_frame(sequence, "ACTIVITY", {"message": "ignored"}) + "\n").encode(
                            "utf-8"
                        )
                    )

    def test_supervisor_launch_rejects_nested_unredacted_provider_fields(self) -> None:
        decoder = PortableProtocolStreamDecoder.for_supervisor_commands("session-a")
        value = {
            "version": 1,
            "session_id": "session-a",
            "sequence": 1,
            "kind": "START",
            "payload": {
                "operation": "PLANNING",
                "arguments": [],
                "owner_id": "owner-a",
                "worker_generation": 1,
                "partial_work_context": {
                    "activity": [],
                    "diagnostics": [],
                    "provider_payload": {"authorization": "secret"},
                },
            },
        }

        with self.assertRaisesRegex(PortableProtocolError, "partial-work context"):
            decoder.feed(
                (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")
            )


def _frame(sequence: int, kind: str, payload: object) -> str:
    return json.dumps(
        {
            "version": 1,
            "session_id": "session-a",
            "sequence": sequence,
            "kind": kind,
            "payload": payload,
        },
        separators=(",", ":"),
    )


if __name__ == "__main__":
    unittest.main()
