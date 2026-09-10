"""Keep a workflow step reserved while the session runs authorized test gates."""

from __future__ import annotations

import json
import os
import shlex
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
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
from .portable_protocol import MAX_PORTABLE_PROTOCOL_TEXT_CHARACTERS
from .portable_runtime import active_portable_runtime
from .redaction import redact_persisted_evidence
from .verification_feedback import VerificationFailed, raise_for_failed_verification
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


def strengthens_failed_gate(
    gate: OperatorVerification, failed: OperatorVerification
) -> bool:
    """Whether ``gate`` demands strictly more of the same command than ``failed``.

    ``expected_tests`` is the worker's prediction of how many tests its filter
    matches, and a filter matching more than predicted fails the gate. Raising
    that prediction on an otherwise identical gate cannot be a way around the
    failure: the very same command must now prove more tests passed. Every
    other difference can weaken the gate — a narrower filter, another project,
    a lower count — so only this one is a correction rather than a bypass.

    Without it a miscount is unrecoverable. The worker cannot lower ``executed``
    to meet a wrong expectation without deleting real tests, which the step
    contract forbids, so the issue deadlocks on bookkeeping while its tests pass.
    """
    return gate.expected_tests > failed.expected_tests and gate_identity(
        gate.to_dict() | {"expected_tests": failed.expected_tests}
    ) == gate_identity(failed.to_dict())


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
        *,
        resume_output: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.runner = runner
        self.state_writer = state_writer
        self._wait_context = wait_context
        self._resume_output = resume_output

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
        accepted: dict[str, str] = {}
        accepted_fingerprint = ""
        failed_gate: OperatorVerification | None = None
        failed_fingerprint = ""
        failure_evidence = ""
        completion: RoleResult | None = None
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
                if accepted_fingerprint != record["source_fingerprint"]:
                    accepted.clear()
                try:
                    with self._wait_context():
                        evidence = self._wait(record)
                except VerificationFailed as error:
                    failed_gate = gate
                    failed_fingerprint = record["source_fingerprint"]
                    failure_evidence = str(error)
                    completion = None
                    runtime = active_portable_runtime()
                    if runtime is not None:
                        runtime.update_session_status(
                            stage="Returning failed verification to workflow",
                            active_issue=arguments["issue"].number,
                        )
                        suffix = "\n[Preview shortened. Full evidence is supplied to the workflow.]"
                        preview = failure_evidence
                        if len(preview) > MAX_PORTABLE_PROTOCOL_TEXT_CHARACTERS:
                            preview = preview[:MAX_PORTABLE_PROTOCOL_TEXT_CHARACTERS - len(suffix)]
                            preview += suffix
                        runtime.show_screen(preview)
                except (ValueError, OSError, KeyError, ET.ParseError) as error:
                    return RoleResult(
                        status="BLOCKED",
                        summary="Automatic test verification did not pass.",
                        fix_list=[redact_persisted_evidence(str(error))],
                    )
                else:
                    accepted_fingerprint = record["source_fingerprint"]
                    accepted[gate_identity(record["gate"])] = evidence
                    failed_gate = None
                    failure_evidence = ""
                    if completion is not None:
                        return replace(
                            completion,
                            verification_commands=[*completion.verification_commands, evidence],
                        )
                # Keep the accepted request durable until this step finishes. A
                # lifecycle interruption must not discard its verified evidence.
                arguments["verification_evidence"] = (
                    "\n\n".join(accepted.values()) + "\n\n" + failure_evidence
                )
                arguments["attempt_label"] = f"operator-{uuid.uuid4().hex}"
                if self._resume_output is not None:
                    self._resume_output(arguments)
            result = self.runner.run_role(**arguments)
            gate = result.operator_verification
            if failed_gate is not None:
                current_fingerprint = source_fingerprint(self.runner.repo_root)
                needs_verification = result.status == "PASS" or gate is not None
                corrects_expectation = gate is not None and strengthens_failed_gate(
                    gate, failed_gate
                )
                if needs_verification and not corrects_expectation and (
                    current_fingerprint == failed_fingerprint
                    or (gate is not None and gate_identity(gate.to_dict()) !=
                        gate_identity(failed_gate.to_dict()))
                ):
                    return replace(
                        result,
                        status="FAIL",
                        summary="The failed verification gate still needs repair.",
                        fix_list=[failure_evidence],
                        operator_verification=None,
                    )
                if result.status == "PASS":
                    completion = result
                    record = self._create_request(key, failed_gate, arguments)
                    continue
                if gate is None:
                    # In review/QA this follows the existing FAIL -> Development
                    # transition with the actual test evidence as rework input.
                    return replace(result, fix_list=[*result.fix_list, failure_evidence])
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
                    raise_for_failed_verification(record, directory)
                    evidence = validate_receipt(record, directory)
                except VerificationFailed:
                    raise
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
                raise_for_failed_verification(record, directory)
                validate_receipt(record, directory)
            except VerificationFailed:
                raise
            except (ValueError, OSError, KeyError, ET.ParseError) as error:
                raise ValueError(
                    f"Verification failed: {error} Results and verification.log: {directory}"
                ) from error
