"""Keep a workflow step reserved while the session runs authorized test gates."""

from __future__ import annotations

import json
import os
import shlex
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import operator_verification
from .operator_verification import (
    RECEIPT_FILE,
    REQUEST_FILE,
    OperatorVerification,
    read_json,
    source_fingerprint,
    validate_receipt,
    write_json,
)
from .portable_runtime import active_portable_runtime
from .redaction import redact_persisted_evidence
from .verification_process import publish_verification_output, run_verification_command

if TYPE_CHECKING:
    from .codex_runner import CodexRunner, RoleResult
    from .state import LoopStateWriter


STATE_KEY = "operator_verifications"
RUNNING_STAGE = "Running external verification"


def gate_identity(gate: dict[str, Any]) -> str:
    return json.dumps(
        {key: value for key, value in gate.items() if key != "reason"}, sort_keys=True
    )


def shell_command(arguments: list[str]) -> str:
    if os.name == "nt":
        return "& " + " ".join("'" + part.replace("'", "''") + "'" for part in arguments)
    return shlex.join(arguments)


class OperatorHandoff:
    def __init__(
        self,
        runner: CodexRunner,
        state_writer: LoopStateWriter,
        wait_context: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        self.runner = runner
        self.state_writer = state_writer
        self._wait_context = wait_context

    def run_role(self, arguments: dict[str, Any]) -> RoleResult:
        from .codex_runner import RoleResult

        key = json.dumps(
            [
                arguments["issue"].number,
                str(arguments["step_instance_id"]),
                arguments["pass_number"],
            ]
        )
        records = self.state_writer.state.setdefault(STATE_KEY, {})
        record = records.get(key)
        guidance = str(arguments.get("step_guidance") or "")
        accepted: dict[str, str] = {}
        accepted_fingerprint = ""
        while True:
            if record is not None:
                gate = OperatorVerification.parse(record["gate"])
                assert gate is not None
                if Path(
                    record["repository"]
                ).resolve() != self.runner.repo_root.resolve() or record[
                    "source_fingerprint"
                ] != source_fingerprint(self.runner.repo_root):
                    record = self._create_request(key, gate, arguments)
                try:
                    with self._wait_context():
                        evidence = self._wait(record)
                except (ValueError, OSError, KeyError, ET.ParseError) as error:
                    return RoleResult(
                        status="BLOCKED",
                        summary="Automatic test verification did not pass.",
                        fix_list=[redact_persisted_evidence(str(error))],
                    )
                if accepted_fingerprint != record["source_fingerprint"]:
                    accepted.clear()
                accepted_fingerprint = record["source_fingerprint"]
                accepted[gate_identity(record["gate"])] = evidence
                # Keep the accepted request durable until this step finishes. A
                # lifecycle interruption must not discard its verified evidence.
                arguments["step_guidance"] = guidance + "\n\n" + "\n\n".join(accepted.values())
                arguments["attempt_label"] = f"operator-{uuid.uuid4().hex}"
            result = self.runner.run_role(**arguments)
            gate = result.operator_verification
            if result.status != "BLOCKED" or gate is None:
                # Retain evidence through the executor's next checkpoint. If
                # interrupted between return and checkpoint, resume can reuse it.
                return result
            try:
                gate.project(self.runner.repo_root)
                gate_key = gate_identity(gate.to_dict())
                if (
                    gate_key in accepted
                    and record is not None
                    and (record["source_fingerprint"] == source_fingerprint(self.runner.repo_root))
                ):
                    return RoleResult(
                        status="BLOCKED",
                        summary="The worker requested a gate already verified on these inputs.",
                        fix_list=[
                            "Inspect the worker result: accepted verification was supplied "
                            "but the worker did not continue implementation."
                        ],
                    )
                record = self._create_request(key, gate, arguments)
            except (ValueError, OSError) as error:
                return RoleResult(
                    status="BLOCKED",
                    summary="The worker supplied an invalid verification request.",
                    fix_list=[str(error)],
                )

    def _directory(self, record: dict[str, Any]) -> Path:
        identifier = str(uuid.UUID(record["request_id"]))
        return Path(self.runner.log_root) / "operator-verifications" / identifier

    def _create_request(
        self,
        key: str,
        gate: OperatorVerification,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        # Publish the handoff before inspecting the checkout, and stop the
        # dashboard from repainting the already-finished agent as WORKING.
        with self._wait_context():
            runtime = active_portable_runtime()
            if runtime is not None:
                runtime.update_session_status(
                    stage="Preparing external verification",
                    active_issue=arguments["issue"].number,
                )
                runtime.show_screen(
                    f"PREPARING EXTERNAL VERIFICATION — issue {arguments['issue'].number}\n\n"
                    "The agent returned its result. Checking local source files before "
                    "running the authorized tests automatically."
                )
            return self._write_request(key, gate, arguments)

    def _write_request(
        self,
        key: str,
        gate: OperatorVerification,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        gate.project(self.runner.repo_root)
        record = {
            "request_id": str(uuid.uuid4()),
            "repository": str(self.runner.repo_root.resolve()),
            "issue": arguments["issue"].number,
            "step": str(arguments["step_instance_id"]),
            "pass": arguments["pass_number"],
            "gate": gate.to_dict() | {"reason": redact_persisted_evidence(gate.reason)},
            "source_fingerprint": source_fingerprint(self.runner.repo_root),
        }
        directory = self._directory(record)
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / REQUEST_FILE, record)
        self.state_writer.state.setdefault(STATE_KEY, {})[key] = record
        self.state_writer.flush()
        return record

    def _wait(self, record: dict[str, Any]) -> str:
        directory = self._directory(record)
        runtime = active_portable_runtime()
        while True:
            if read_json(directory / REQUEST_FILE) != record:
                raise ValueError("Verification request changed; refusing to execute it.")
            if (directory / RECEIPT_FILE).exists():
                try:
                    evidence = validate_receipt(record, directory)
                except (ValueError, OSError, KeyError, ET.ParseError):
                    pass  # Failed or stale evidence is rerun once in this invocation.
                else:
                    if runtime is not None:
                        runtime.update_session_status(
                            stage="Resuming verified step", active_issue=record["issue"]
                        )
                        runtime.show_screen(evidence)
                    return evidence
            if runtime is not None:
                runtime.update_session_status(stage=RUNNING_STAGE, active_issue=record["issue"])
                runtime.show_screen(redact_persisted_evidence(
                    f"RUNNING EXTERNAL VERIFICATION — issue {record['issue']}\n\n"
                    f"{record['gate']['reason']}\n\n"
                    "Dev Loop is running the authorized tests automatically.\n"
                    f"Required: {record['gate']['expected_tests']} passed, zero skipped.\n"
                    f"Results: {directory}\n\n"
                    "F9 Actions: Pause or Cancel. Esc: hide to Sessions."
                ))
            operator_verification.execute_request(
                directory / REQUEST_FILE,
                run_command=run_verification_command,
                emit=publish_verification_output,
            )
            fingerprint = source_fingerprint(self.runner.repo_root)
            if fingerprint != record["source_fingerprint"]:
                record["source_fingerprint"] = fingerprint
                record["request_id"] = str(uuid.uuid4())
                directory = self._directory(record)
                directory.mkdir(parents=True, exist_ok=True)
                write_json(directory / REQUEST_FILE, record)
                self.state_writer.flush()
                continue
            # A failed run must surface as a blocker, never an endless rerun or
            # a manual command prompt. Success is independently checked here.
            try:
                validate_receipt(record, directory)
            except (ValueError, OSError, KeyError, ET.ParseError) as error:
                raise ValueError(
                    f"Verification failed: {error} Results and verification.log: {directory}"
                ) from error
