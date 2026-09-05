from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

from devloop.portable_protocol import PortableProtocolError, PortableProtocolStreamDecoder
from devloop.portable_session_catalog import PortableSessionCatalog
from devloop.portable_sessions import (
    PortableSessionLaunch,
    PortableSessionStatus,
    PortableWorkflowOperation,
)


class PortableCatalogReadConsistencyTests(unittest.TestCase):
    def test_catalog_open_uses_one_snapshot_while_session_revisions_advance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            database = root / "catalog.sqlite3"
            catalog = PortableSessionCatalog(database)
            catalog.create_session(
                PortableSessionLaunch(
                    session_id="concurrent-validation",
                    checkout=checkout,
                    operation=PortableWorkflowOperation.PLANNING,
                    arguments=(),
                )
            )
            started = threading.Event()
            stop = threading.Event()
            writer_errors: list[BaseException] = []

            def advance_revisions() -> None:
                started.set()
                revision = 0
                while not stop.is_set():
                    try:
                        catalog.update_session_status(
                            "concurrent-validation",
                            PortableSessionStatus.READY,
                            activity_summary=f"revision {revision}",
                        )
                    except BaseException as error:
                        writer_errors.append(error)
                        return
                    revision += 1

            writer = threading.Thread(target=advance_revisions)
            writer.start()
            self.assertTrue(started.wait(timeout=2))
            try:
                for _attempt in range(200):
                    reopened = PortableSessionCatalog(database)
                    self.assertEqual(
                        reopened.get_session("concurrent-validation").status,
                        PortableSessionStatus.READY,
                    )
            finally:
                stop.set()
                writer.join(timeout=5)

        self.assertFalse(writer.is_alive())
        self.assertEqual(writer_errors, [])


class PortableNestedEnumValidationTests(unittest.TestCase):
    def test_malformed_nested_enum_values_raise_only_protocol_errors(self) -> None:
        malformed_values = ({"bad": "value"}, ["bad"], None, True)
        for field_name in ("backend", "fast"):
            for malformed in malformed_values:
                with self.subTest(field=field_name, malformed=malformed):
                    payload = _launch_payload()
                    settings = _planning_settings()
                    settings[field_name] = malformed
                    payload["planning_settings"] = settings
                    self._assert_rejected(payload)
        for field_name in ("checkpoint_kind", "next_role"):
            for malformed in malformed_values:
                with self.subTest(field=field_name, malformed=malformed):
                    payload = _launch_payload()
                    recovery = _prd_recovery()
                    recovery[field_name] = malformed
                    payload["recovery"] = recovery
                    self._assert_rejected(payload)

    def _assert_rejected(self, payload: dict[str, object]) -> None:
        frame = json.dumps(
            {
                "version": 1,
                "session_id": "nested-enum",
                "sequence": 1,
                "kind": "START",
                "payload": payload,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        decoder = PortableProtocolStreamDecoder.for_supervisor_commands(
            "nested-enum"
        )
        with self.assertRaises(PortableProtocolError):
            decoder.feed(frame + b"\n")


class PortableApprovalSubsetValidationTests(unittest.TestCase):
    def test_real_worker_rejects_a_valid_decision_that_was_not_offered(self) -> None:
        worker_source = textwrap.dedent(
            r"""
            import sys

            from devloop.portable_protocol import PortableProtocolError
            from devloop.portable_worker import PortableWorkerRuntimeBridge

            bridge = PortableWorkerRuntimeBridge(
                "subset-approval",
                command_stream=sys.stdin.buffer,
                event_stream=sys.stdout.buffer,
            )
            try:
                bridge.request_approval(
                    "Allow the bounded action?",
                    supported_decisions=("DENY", "APPROVE_ONCE"),
                    default_decision="DENY",
                    cancel_decision="DENY",
                )
            except PortableProtocolError as error:
                if "offered" in str(error):
                    raise SystemExit(23) from error
                raise
            raise SystemExit(0)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", worker_source],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert process.stdout is not None
            request = json.loads(process.stdout.readline())
            self.assertEqual(
                [option[0] for option in request["payload"]["options"]],
                ["DENY", "APPROVE_ONCE"],
            )
            response = {
                "version": 1,
                "session_id": "subset-approval",
                "sequence": 2,
                "kind": "APPROVAL_DECISION",
                "payload": {
                    "decision": "APPROVE_FOR_SESSION",
                    "request_id": request["payload"]["request_id"],
                    "request_generation": request["payload"]["request_generation"],
                },
            }
            assert process.stdin is not None
            process.stdin.write(
                json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
            )
            process.stdin.flush()
            self.assertEqual(process.wait(timeout=5), 23)
            self.assertEqual(process.stdout.read(), b"")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def _launch_payload() -> dict[str, object]:
    return {
        "operation": "PLANNING",
        "arguments": [],
        "owner_id": "owner",
        "worker_generation": 1,
    }


def _planning_settings() -> dict[str, object]:
    return {
        "backend": "CODEX_CLI",
        "model": "gpt-5.4",
        "reasoning_effort": "high",
        "fast": "OFF",
        "timeout_seconds": 1200,
        "checkpoint_seconds": 300,
    }


def _prd_recovery() -> dict[str, object]:
    checkout = str(Path.cwd().resolve())
    return {
        "checkpoint_kind": "PRD",
        "checkout": checkout,
        "activity": [],
        "diagnostics": [],
        "planning_thread_id": None,
        "planning_settings": None,
        "prd_path": str(Path(checkout) / "prd.md"),
        "issues_index_path": str(Path(checkout) / "issues" / "README.md"),
        "issue_id": "0001",
        "next_role": "coder",
        "pass_number": 1,
    }


if __name__ == "__main__":
    unittest.main()
