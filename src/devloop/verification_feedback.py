"""Bind failed test evidence to its request before giving it to the workflow."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .operator_verification import (
    MAX_REPORT_BYTES,
    RECEIPT_FILE,
    TRX_NAMESPACE,
    read_json,
    read_report,
    source_fingerprint,
    validate_report,
)
from .redaction import redact_persisted_evidence
from .terminal_text import sanitize_terminal_text
from .verification_process import VERIFICATION_LOG

MAX_FEEDBACK_CHARACTERS = 16000


class VerificationFailed(ValueError):
    """An executed gate needs repair, rather than an unchanged automatic retry."""


def raise_for_failed_verification(request: dict[str, Any], directory: Path) -> None:
    receipt = read_json(directory / RECEIPT_FILE)
    for key in ("request_id", "source_fingerprint"):
        if receipt.get(key) != request[key]:
            raise ValueError(f"Verification receipt has a different {key}.")
    if source_fingerprint(Path(request["repository"])) != request["source_fingerprint"]:
        raise ValueError("Source inputs changed; a fresh verification request is required.")
    exit_code = receipt.get("exit_code")
    if type(exit_code) is not int or exit_code < 0:
        raise ValueError("Verification receipt has no valid completed process exit code.")
    report = (directory / str(receipt["report_path"])).resolve()
    if not report.is_relative_to(directory.resolve()):
        raise ValueError("TRX report escapes the verification directory.")
    details = ""
    if report.is_file():
        if report.stat().st_size > MAX_REPORT_BYTES:
            raise ValueError("TRX exceeds the size limit.")
        if hashlib.sha256(report.read_bytes()).hexdigest() != receipt.get("report_sha256"):
            raise ValueError("TRX report changed after the test command finished.")
        root = read_report(report)
        try:
            validate_report(report, request["gate"]["expected_tests"])
        except ValueError as error:
            details = str(error)
        if not details and exit_code == 0:
            return
        ns = TRX_NAMESPACE
        counters = root.find(f"{ns}ResultSummary/{ns}Counters")
        if counters is not None:
            details += "\nTest counters: " + str(counters.attrib)
        for result in root.findall(f"{ns}Results/{ns}UnitTestResult"):
            if result.get("outcome") != "Passed":
                details += (
                    f"\n{result.get('testName', result.get('testId', 'Test'))}: "
                    f"{result.get('outcome')}\n"
                    + "\n".join(result.itertext())
                )
    elif exit_code == 0 or receipt.get("report_sha256"):
        raise ValueError("Verification did not produce the recorded TRX report.")
    else:
        # Build failures can exit before a test adapter writes a TRX.
        log = (report.parent / VERIFICATION_LOG).resolve()
        if not log.is_relative_to(directory.resolve()):
            raise ValueError("Verification log escapes the verification directory.")
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - MAX_FEEDBACK_CHARACTERS))
            details = stream.read().decode("utf-8", errors="replace")
    feedback = (
        f"Automatic verification FAILED for {request['gate']['project_path']}; "
        f"filter {request['gate']['test_filter']}; process exit {exit_code}.\n"
        f"TRX: {report}\nLog: {report.parent / VERIFICATION_LOG}\n"
        f"{details[:MAX_FEEDBACK_CHARACTERS]}\n"
        "This is failed test evidence, not a request for operator authorization. "
        "Development: inspect and repair the failure, then request fresh verification. "
        "Review/QA: report FAIL with the concrete findings so normal workflow rework "
        "routes them to Development; do not edit code in a review role. "
        "Do not weaken assertions or claim PASS while this gate is failing."
    )
    raise VerificationFailed(sanitize_terminal_text(
        redact_persisted_evidence(feedback), preserve_newlines=True
    ))
