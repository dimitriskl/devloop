from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from devloop import operator_handoff
from devloop import operator_verification as verification
from devloop.codex_runner import RoleResult
from devloop.issue_pack import Issue
from devloop.operator_handoff import OperatorHandoff
from devloop.operator_verification import OperatorVerification, VerificationKind
from devloop.portable_runtime import PortableRuntimeStopped
from devloop.portable_workflow import (
    IssueStatus,
    PortableWorkflowExecutor,
    StepOutcome,
    default_portable_component_catalog,
    default_portable_workflow,
)
from devloop.state import LoopStateWriter
from devloop.verification_feedback import VerificationFailed, raise_for_failed_verification


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "tests.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"/>')
    (tmp_path / "Tests.cs").write_text("class Tests {}")
    return tmp_path


@pytest.fixture
def gate() -> OperatorVerification:
    return OperatorVerification(
        VerificationKind.DOTNET_TEST,
        "tests.csproj",
        "ClassName=Tests",
        2,
        "The authorized SQL tests cannot execute in this worker.",
    )


def report(path: Path, *, skipped: bool = False, total: int = 2) -> None:
    passed = 0 if skipped else total
    outcome = "NotExecuted" if skipped else "Passed"
    names = ("one", "two", *(f"case{index}" for index in range(2, total)))[:total]
    results = "".join(
        f'<UnitTestResult testId="{name}" outcome="{outcome}"/>' for name in names
    )
    path.write_text(
        '<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">'
        f'<ResultSummary outcome="Completed"><Counters total="{total}" executed="{passed}" '
        f'passed="{passed}" failed="0" notExecuted="{total - passed}"/></ResultSummary>'
        f"<Results>{results}</Results></TestRun>"
    )


def receipt(record: dict, directory: Path, *, skipped: bool = False, total: int = 2) -> None:
    trx = directory / verification.REPORT_FILE
    report(trx, skipped=skipped, total=total)
    verification.write_json(
        directory / verification.RECEIPT_FILE,
        {
            "request_id": record["request_id"],
            "source_fingerprint": record["source_fingerprint"],
            "exit_code": 0,
            "report_path": trx.name,
            "report_sha256": hashlib.sha256(trx.read_bytes()).hexdigest(),
        },
    )


def failed_receipt(record: dict, directory: Path) -> None:
    receipt(record, directory)
    path = directory / verification.REPORT_FILE
    path.write_text(path.read_text().replace(
        'executed="2" passed="2" failed="0"', 'executed="2" passed="0" failed="2"'
    ).replace('outcome="Passed"/>', 'outcome="Failed"><Output><ErrorInfo>'
              '<Message>Moq.MockException: TableExistsAsync has no setup; '
              'Password=secret-value</Message>'
              '</ErrorInfo></Output></UnitTestResult>'))
    result_path = directory / verification.RECEIPT_FILE
    result = verification.read_json(result_path)
    result.update(exit_code=1, report_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    verification.write_json(result_path, result)


def test_saved_test_failure_reaches_coder_before_retest_and_repair_is_verified(
    repository: Path, gate: OperatorVerification, monkeypatch
) -> None:
    handoff, runner, writer, arguments = setup_handoff(repository, gate)
    key = json.dumps(["0003", "development", 1])
    record = handoff._create_request(key, gate, arguments)
    failed_receipt(record, handoff._directory(record))
    executions = []

    def repair(**kwargs):
        assert not executions
        assert "TableExistsAsync" in kwargs["verification_evidence"]
        assert "secret-value" not in kwargs["verification_evidence"]
        (repository / "Tests.cs").write_text("class Repaired {}")
        return RoleResult(status="PASS", summary="Repaired missing mock setup")

    def execute(request_path, **kwargs):
        executions.append(request_path)
        receipt(verification.read_json(request_path), request_path.parent)
        return 0

    runner.run_role.side_effect = repair
    monkeypatch.setattr(verification, "execute_request", execute)
    result = OperatorHandoff(runner, writer).run_role(arguments)
    assert result.status == "PASS"
    assert result.summary == "Repaired missing mock setup"
    assert "2 passed, zero skipped" in result.verification_commands[-1]
    assert runner.run_role.call_count == 1
    assert len(executions) == 1


def test_claimed_repair_that_still_fails_returns_to_worker_instead_of_passing(
    repository: Path, gate: OperatorVerification, monkeypatch
) -> None:
    handoff, runner, _, arguments = setup_handoff(repository, gate)
    record = handoff._create_request(json.dumps(["0003", "development", 1]), gate, arguments)
    failed_receipt(record, handoff._directory(record))

    def worker(**kwargs):
        if runner.run_role.call_count == 1:
            (repository / "Tests.cs").write_text("class StillBroken {}")
            return RoleResult(status="PASS")
        assert "TableExistsAsync" in kwargs["verification_evidence"]
        return RoleResult(status="FAIL", summary="Repair incomplete")

    def execute(request_path, **kwargs):
        failed_receipt(verification.read_json(request_path), request_path.parent)
        return 1

    runner.run_role.side_effect = worker
    execution = Mock(side_effect=execute)
    monkeypatch.setattr(verification, "execute_request", execution)
    result = handoff.run_role(arguments)
    assert result.status == "FAIL"
    assert result.summary == "Repair incomplete"
    assert execution.call_count == 1
    assert runner.run_role.call_count == 2


@pytest.mark.parametrize("response", ["PASS", "same-gate", "different-gate"])
def test_worker_cannot_bypass_failed_gate_or_repeat_it_without_changes(
    repository: Path, gate: OperatorVerification, monkeypatch, response: str
) -> None:
    handoff, runner, _, arguments = setup_handoff(repository, gate)
    requested_gate = gate
    if response == "different-gate":
        requested_gate = OperatorVerification.parse(gate.to_dict() | {"test_filter": "Other"})

    def worker(**kwargs):
        if runner.run_role.call_count == 1:
            return RoleResult(status="BLOCKED", operator_verification=gate)
        if response == "different-gate":
            (repository / "Tests.cs").write_text("class Changed {}")
        return RoleResult(
            status="PASS" if response == "PASS" else "BLOCKED",
            operator_verification=None if response == "PASS" else requested_gate,
        )

    def execute(request_path, **kwargs):
        failed_receipt(verification.read_json(request_path), request_path.parent)
        return 1

    execution = Mock(side_effect=execute)
    runner.run_role.side_effect = worker
    monkeypatch.setattr(verification, "execute_request", execution)
    assert handoff.run_role(arguments).status == "FAIL"
    assert runner.run_role.call_count == 2
    assert execution.call_count == 1


def test_worker_may_raise_an_undercounted_expectation_on_an_otherwise_equal_gate(
    repository: Path, gate: OperatorVerification, monkeypatch
) -> None:
    """A filter matching more tests than declared must stay recoverable.

    Raising ``expected_tests`` on a gate that is otherwise identical strengthens
    what the gate demands, so unlike a weaker filter it is no way around the
    failure. Discarding it instead deadlocks the issue: the worker cannot make
    ``executed`` match a wrong expectation without deleting real tests.
    """
    corrected = OperatorVerification.parse(gate.to_dict() | {"expected_tests": 3})
    handoff, runner, _, arguments = setup_handoff(repository, gate)

    def worker(**kwargs):
        if runner.run_role.call_count == 1:
            return RoleResult(status="BLOCKED", operator_verification=gate)
        if runner.run_role.call_count == 2:
            return RoleResult(status="BLOCKED", operator_verification=corrected)
        return RoleResult(status="PASS", summary="Implementation completed")

    def execute(request_path, **kwargs):
        # The filter matches three tests however many the worker declared.
        receipt(verification.read_json(request_path), request_path.parent, total=3)
        return 0

    runner.run_role.side_effect = worker
    execution = Mock(side_effect=execute)
    monkeypatch.setattr(verification, "execute_request", execution)
    result = handoff.run_role(arguments)
    assert result.status == "PASS"
    assert "3 passed, zero skipped" in runner.run_role.call_args.kwargs["verification_evidence"]
    assert runner.run_role.call_count == 3
    assert execution.call_count == 2


def test_worker_cannot_lower_a_failed_expectation_to_match_fewer_tests(
    repository: Path, gate: OperatorVerification, monkeypatch
) -> None:
    """Lowering ``expected_tests`` weakens the gate, so it stays a bypass."""
    weakened = OperatorVerification.parse(gate.to_dict() | {"expected_tests": 1})
    handoff, runner, _, arguments = setup_handoff(repository, gate)

    def worker(**kwargs):
        if runner.run_role.call_count == 1:
            return RoleResult(status="BLOCKED", operator_verification=gate)
        return RoleResult(status="BLOCKED", operator_verification=weakened)

    def execute(request_path, **kwargs):
        receipt(verification.read_json(request_path), request_path.parent, total=1)
        return 0

    runner.run_role.side_effect = worker
    execution = Mock(side_effect=execute)
    monkeypatch.setattr(verification, "execute_request", execution)
    assert handoff.run_role(arguments).status == "FAIL"
    assert runner.run_role.call_count == 2
    assert execution.call_count == 1


def test_pause_during_repair_replays_failure_without_reexecuting_tests(
    repository: Path, gate: OperatorVerification, monkeypatch
) -> None:
    handoff, runner, writer, arguments = setup_handoff(repository, gate)
    record = handoff._create_request(json.dumps(["0003", "development", 1]), gate, arguments)
    failed_receipt(record, handoff._directory(record))
    execute = Mock(side_effect=AssertionError("Must give the saved failure to the worker"))
    monkeypatch.setattr(verification, "execute_request", execute)
    runner.run_role.side_effect = PortableRuntimeStopped("Paused during repair")
    with pytest.raises(PortableRuntimeStopped):
        handoff.run_role(arguments.copy())
    runner.run_role.side_effect = [RoleResult(status="FAIL", summary="Needs source repair")]
    result = OperatorHandoff(runner, writer).run_role(arguments.copy())
    assert result.status == "FAIL"
    assert "TableExistsAsync" in runner.run_role.call_args.kwargs["verification_evidence"]
    assert "TableExistsAsync" in result.fix_list[-1]
    assert not execute.called


@pytest.mark.parametrize("failed_role", ["reviewer", "qa"])
def test_failed_review_gate_routes_evidence_to_development_and_retests(
    repository: Path, gate: OperatorVerification, monkeypatch, failed_role: str
) -> None:
    handoff, runner, _, _ = setup_handoff(repository, gate)
    issue = Issue("0003", "SQL implementation", repository / "issue.md", False)
    issue.path.write_text("# SQL implementation\n")
    repairing = False
    executions = []

    def worker(**arguments):
        nonlocal repairing
        if arguments["role"] == failed_role and arguments["pass_number"] == 1:
            if "Automatic verification FAILED" in (arguments.get("verification_evidence") or ""):
                return RoleResult(status="FAIL", summary="Test repair required")
            return RoleResult(status="BLOCKED", operator_verification=gate)
        if arguments["role"] == "coder" and arguments["pass_number"] == 2 and not repairing:
            assert "TableExistsAsync" in "\n".join(arguments["fix_list"])
            repairing = True
            (repository / "Tests.cs").write_text("class Repaired {}")
            return RoleResult(status="BLOCKED", operator_verification=gate)
        return RoleResult(status="PASS")

    def execute(request_path, **kwargs):
        record = verification.read_json(request_path)
        executions.append(record)
        if repairing:
            receipt(record, request_path.parent)
            return 0
        failed_receipt(record, request_path.parent)
        return 1

    class Adapter:
        def run_role(self, **arguments):
            return handoff.run_role(arguments)

    runner.run_role.side_effect = worker
    monkeypatch.setattr(verification, "execute_request", execute)
    execution = PortableWorkflowExecutor(
        default_portable_workflow(), default_portable_component_catalog(), Adapter()
    ).run(issue, pass_number=1, max_passes=2)
    assert execution.issue_status is IssueStatus.COMPLETED
    assert len(executions) == 2
    assert not any(attempt.outcome is StepOutcome.BLOCKED for attempt in execution.attempts)
    assert any(attempt.outcome is StepOutcome.CHANGES_REQUESTED for attempt in execution.attempts)


@pytest.mark.parametrize("change", ["request", "hash", "source", "escape"])
def test_failed_evidence_must_match_request_and_current_sources(
    repository: Path, gate: OperatorVerification, change: str
) -> None:
    handoff, _, _, arguments = setup_handoff(repository, gate)
    record = handoff._create_request("key", gate, arguments)
    directory = handoff._directory(record)
    failed_receipt(record, directory)
    path = directory / verification.RECEIPT_FILE
    result = verification.read_json(path)
    if change == "request":
        result["request_id"] = "wrong"
    elif change == "hash":
        result["report_sha256"] = "wrong"
    elif change == "source":
        (repository / "Tests.cs").write_text("class Changed {}")
    else:
        result["report_path"] = "../outside.trx"
    verification.write_json(path, result)
    with pytest.raises(ValueError) as raised:
        raise_for_failed_verification(record, directory)
    assert not isinstance(raised.value, VerificationFailed)


def test_build_failure_without_trx_is_returned_as_repair_feedback(
    repository: Path, gate: OperatorVerification
) -> None:
    handoff, _, _, arguments = setup_handoff(repository, gate)
    record = handoff._create_request("key", gate, arguments)
    directory = handoff._directory(record)

    def build_failure(command, checkout, output):
        (output / "verification.log").write_text("error CS1002: ; expected; Password=hidden")
        return 1

    assert verification.execute_request(
        directory / verification.REQUEST_FILE, run_command=build_failure
    ) == 1
    with pytest.raises(VerificationFailed, match="CS1002") as raised:
        raise_for_failed_verification(record, directory)
    assert "hidden" not in str(raised.value)


def setup_handoff(repository: Path, gate: OperatorVerification) -> tuple:
    runner = SimpleNamespace(
        repo_root=repository,
        log_root=repository / ".loop.logs",
        run_role=Mock(
            side_effect=[
                RoleResult(status="BLOCKED", operator_verification=gate),
                RoleResult(status="PASS", summary="Implementation completed"),
            ]
        ),
    )
    writer = SimpleNamespace(state={}, flush=Mock())
    handoff = OperatorHandoff(runner, writer)
    arguments = {
        "issue": SimpleNamespace(number="0003"),
        "step_instance_id": "development",
        "pass_number": 1,
        "step_attempt_id": "stable-attempt",
        "step_guidance": "Keep scope.",
    }
    return handoff, runner, writer, arguments


def test_authorized_gate_executes_without_operator_interaction(
    repository: Path, gate: OperatorVerification, monkeypatch
) -> None:
    handoff, runner, _, arguments = setup_handoff(repository, gate)
    runtime = Mock()
    runtime.wait_for_retry.side_effect = AssertionError("Must execute, not wait for the operator")
    monkeypatch.setattr(operator_handoff, "active_portable_runtime", lambda: runtime)

    def execute(request_path, **kwargs):
        record = verification.read_json(request_path)
        receipt(record, request_path.parent)
        return 0

    execute_mock = Mock(side_effect=execute)
    monkeypatch.setattr(verification, "execute_request", execute_mock)
    assert handoff.run_role(arguments).status == "PASS"
    assert execute_mock.call_count == 1
    assert runner.run_role.call_count == 2
    assert "2 passed, zero skipped" in runner.run_role.call_args.kwargs["verification_evidence"]
    assert not runtime.choose.called


@pytest.mark.parametrize("failure", ["skipped", "exit", "missing", "launch"])
def test_automatic_failure_returns_blocker_without_prompt_or_repeated_execution(
    repository: Path, gate: OperatorVerification, monkeypatch, failure: str
) -> None:
    handoff, runner, _, arguments = setup_handoff(repository, gate)

    def execute(request_path, **kwargs):
        if failure == "launch":
            raise OSError("Cannot launch dotnet")
        if failure != "missing":
            record = verification.read_json(request_path)
            receipt(record, request_path.parent, skipped=failure == "skipped")
            if failure == "exit":
                path = request_path.parent / verification.RECEIPT_FILE
                verification.write_json(path, verification.read_json(path) | {"exit_code": 1})
        return 1

    execution = Mock(side_effect=execute)
    monkeypatch.setattr(verification, "execute_request", execution)
    runtime = Mock()
    monkeypatch.setattr(operator_handoff, "active_portable_runtime", lambda: runtime)
    result = handoff.run_role(arguments)
    repairable = failure in {"skipped", "exit"}
    assert result.status == ("FAIL" if repairable else "BLOCKED")
    assert result.fix_list
    assert execution.call_count == 1
    assert runner.run_role.call_count == (2 if repairable else 1)
    assert not runtime.wait_for_retry.called
    assert not runtime.choose.called


def test_changed_request_cannot_launch_tests(
    repository: Path, gate: OperatorVerification, monkeypatch
) -> None:
    handoff, _, _, arguments = setup_handoff(repository, gate)
    record = handoff._create_request("key", gate, arguments)
    path = handoff._directory(record) / verification.REQUEST_FILE
    verification.write_json(path, record | {"repository": str(repository.parent)})
    execute = Mock()
    monkeypatch.setattr(verification, "execute_request", execute)
    with pytest.raises(ValueError, match="request changed"):
        handoff._wait(record)
    assert not execute.called


def test_pending_request_from_previous_version_runs_automatically(
    repository: Path, gate: OperatorVerification, monkeypatch
) -> None:
    handoff, runner, writer, arguments = setup_handoff(repository, gate)
    key = json.dumps(["0003", "development", 1])
    record = handoff._create_request(key, gate, arguments)
    runner.run_role.side_effect = [RoleResult(status="PASS")]

    def execute(request_path, **kwargs):
        assert verification.read_json(request_path) == record
        assert runner.run_role.call_count == 0
        receipt(record, request_path.parent)
        return 0

    monkeypatch.setattr(verification, "execute_request", execute)
    assert OperatorHandoff(runner, writer).run_role(arguments).status == "PASS"


def test_handoff_uses_supervised_execution_and_real_receipt_validation(
    repository: Path, gate: OperatorVerification, monkeypatch
) -> None:
    handoff, runner, writer, arguments = setup_handoff(repository, gate)

    def run(command, checkout, output):
        assert checkout == repository
        assert command[:2] == ["dotnet", "test"]
        assert command[2] == str(repository / "tests.csproj")
        assert command[command.index("--filter") + 1] == gate.test_filter
        assert "--no-restore" in command
        assert "--no-build" not in command
        report(output / verification.REPORT_FILE)
        return 0

    execute = Mock(side_effect=run)
    monkeypatch.setattr(operator_handoff, "run_verification_command", execute)
    assert handoff.run_role(arguments).status == "PASS"
    record = next(iter(writer.state[operator_handoff.STATE_KEY].values()))
    assert "2 passed, zero skipped" in verification.validate_receipt(
        record, handoff._directory(record)
    )
    assert execute.call_count == 1
    assert runner.run_role.call_count == 2


def test_legacy_results_and_typed_gate_round_trip(gate: OperatorVerification) -> None:
    assert RoleResult.from_message('{"status":"PASS"}').operator_verification is None
    parsed = RoleResult.from_message(
        json.dumps({"status": "BLOCKED", "operator_verification": gate.to_dict()})
    )
    assert parsed.operator_verification == gate
    malformed = gate.to_dict() | {"expected_tests": True}
    result = RoleResult.from_message(
        json.dumps({"status": "BLOCKED", "operator_verification": malformed})
    )
    assert result.operator_verification is None
    assert "Invalid operator verification" in result.summary


@pytest.mark.parametrize("project", ["../outside.csproj", "missing.csproj", "Tests.cs"])
def test_gate_requires_existing_confined_project(
    repository: Path, gate: OperatorVerification, project: str
) -> None:
    candidate = OperatorVerification.parse(gate.to_dict() | {"project_path": project})
    with pytest.raises(ValueError):
        candidate.project(repository)


def test_skipped_tests_are_rejected_even_with_successful_exit(
    repository: Path, gate: OperatorVerification
) -> None:
    handoff, _, _, arguments = setup_handoff(repository, gate)
    record = handoff._create_request("key", gate, arguments)
    directory = handoff._directory(record)
    receipt(record, directory, skipped=True)
    with pytest.raises(ValueError, match="zero skips"):
        verification.validate_receipt(record, directory)
    receipt(record, directory)
    assert "2 passed, zero skipped" in verification.validate_receipt(record, directory)


@pytest.mark.parametrize("change", ["request", "hash", "source", "count", "escape"])
def test_mismatched_evidence_is_rejected(
    repository: Path, gate: OperatorVerification, change: str
) -> None:
    handoff, _, _, arguments = setup_handoff(repository, gate)
    record = handoff._create_request("key", gate, arguments)
    directory = handoff._directory(record)
    receipt(record, directory)
    result_path = directory / verification.RECEIPT_FILE
    result = verification.read_json(result_path)
    if change == "request":
        result["request_id"] = "another-request"
    elif change == "hash":
        result["report_sha256"] = "bad"
    elif change == "source":
        (repository / "Tests.cs").write_text("class Changed {}")
    elif change == "count":
        record["gate"]["expected_tests"] = 3
    else:
        result["report_path"] = "../outside.trx"
    verification.write_json(result_path, result)
    with pytest.raises(ValueError):
        verification.validate_receipt(record, directory)


def test_execution_replaces_screen_then_resumes_same_step(
    repository: Path, gate: OperatorVerification, monkeypatch
) -> None:
    handoff, runner, writer, arguments = setup_handoff(repository, gate)
    runtime = Mock()
    def execute(request_path, **kwargs):
        receipt(verification.read_json(request_path), request_path.parent)
        return 0

    monkeypatch.setattr(verification, "execute_request", execute)
    monkeypatch.setattr(operator_handoff, "active_portable_runtime", lambda: runtime)
    result = handoff.run_role(arguments)
    assert result.status == "PASS"
    assert runtime.show_screen.call_count == 3  # preparation, running, accepted evidence
    first, second = runner.run_role.call_args_list
    assert first.kwargs["step_attempt_id"] == second.kwargs["step_attempt_id"] == "stable-attempt"
    assert first.kwargs["pass_number"] == second.kwargs["pass_number"] == 1
    assert "Keep scope." in second.kwargs["step_guidance"]
    assert "2 passed, zero skipped" in second.kwargs["verification_evidence"]
    assert len(writer.state[operator_handoff.STATE_KEY]) == 1


def test_cancel_persists_request_and_resume_waits_before_launching_model(
    repository: Path, gate: OperatorVerification, monkeypatch
) -> None:
    handoff, runner, writer, arguments = setup_handoff(repository, gate)
    runtime = Mock()
    execute = Mock(side_effect=PortableRuntimeStopped("Paused"))
    monkeypatch.setattr(verification, "execute_request", execute)
    monkeypatch.setattr(operator_handoff, "active_portable_runtime", lambda: runtime)
    with pytest.raises(PortableRuntimeStopped):
        handoff.run_role(arguments)
    record = next(iter(writer.state[operator_handoff.STATE_KEY].values()))
    assert runner.run_role.call_count == 1
    # A new coordinator recovering the durable state must wait before any model invocation.
    resumed = OperatorHandoff(runner, writer)
    with pytest.raises(PortableRuntimeStopped):
        resumed.run_role(arguments)
    assert runner.run_role.call_count == 1
    receipt(record, handoff._directory(record))
    assert resumed.run_role(arguments).status == "PASS"
    assert runner.run_role.call_count == 2
    assert execute.call_count == 2  # Resume reused accepted evidence without a third launch.


def test_source_edit_refreshes_command_without_consuming_a_step(
    repository: Path, gate: OperatorVerification, monkeypatch
) -> None:
    handoff, runner, writer, arguments = setup_handoff(repository, gate)
    runtime = Mock()
    identifiers = []

    def execute(request_path, **kwargs):
        record = verification.read_json(request_path)
        identifiers.append(record["request_id"])
        if len(identifiers) == 1:
            (repository / "Tests.cs").write_text("class Fixed {}")
        else:
            receipt(record, request_path.parent)
        return 0

    monkeypatch.setattr(verification, "execute_request", execute)
    monkeypatch.setattr(operator_handoff, "active_portable_runtime", lambda: runtime)
    assert handoff.run_role(arguments).status == "PASS"
    assert identifiers[0] != identifiers[1]
    assert runner.run_role.call_count == 2


def test_operator_command_builds_and_records_only_real_dotnet_result(
    repository: Path, gate: OperatorVerification, monkeypatch
) -> None:
    handoff, _, _, arguments = setup_handoff(repository, gate)
    record = handoff._create_request("key", gate, arguments)
    directory = handoff._directory(record)
    original_run = subprocess.run
    observed = []

    def fake_dotnet(command, **kwargs):
        if command[0] == "git":
            return original_run(command, **kwargs)
        observed.append(command)
        report(Path(command[-1]) / verification.REPORT_FILE)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_dotnet)
    assert verification.execute_request(directory / verification.REQUEST_FILE) == 0
    assert len(observed) == 1
    assert observed[0][:2] == ["dotnet", "test"]
    assert "--no-build" not in observed[0]
    assert "2 passed, zero skipped" in verification.validate_receipt(record, directory)


def test_new_source_invalidates_but_generated_results_do_not(repository: Path) -> None:
    before = verification.source_fingerprint(repository)
    (repository / "TestResults").mkdir()
    (repository / "TestResults" / "result.json").write_text("{}")
    assert verification.source_fingerprint(repository) == before
    (repository / "New.cs").write_text("class New {}")
    assert verification.source_fingerprint(repository) != before


def test_powershell_command_quotes_metacharacters_as_literals(monkeypatch) -> None:
    monkeypatch.setattr(operator_handoff.os, "name", "nt")
    command = operator_handoff.shell_command(
        ["C:/path with space/python.exe", "D:/it's/$(command).json"]
    )
    assert command == "& 'C:/path with space/python.exe' 'D:/it''s/$(command).json'"


def test_actual_workflow_checkpoint_resumes_without_recording_blocked_attempt(
    repository: Path,
    gate: OperatorVerification,
    monkeypatch,
) -> None:
    handoff, runner, writer, _ = setup_handoff(repository, gate)
    runner.run_role.side_effect = [RoleResult(status="BLOCKED", operator_verification=gate)] + [
        RoleResult(status="PASS") for _ in range(8)
    ]
    issue = Issue("0003", "SQL implementation", repository / "issue.md", False)
    issue.path.write_text("# SQL implementation\n")
    runtime = Mock()
    monkeypatch.setattr(
        verification, "execute_request", Mock(side_effect=PortableRuntimeStopped("Paused"))
    )
    monkeypatch.setattr(operator_handoff, "active_portable_runtime", lambda: runtime)

    class Adapter:
        def run_role(self, **arguments):
            return handoff.run_role(arguments)

    workflow = default_portable_workflow()
    catalog = default_portable_component_catalog()
    executor = PortableWorkflowExecutor(workflow, catalog, Adapter())
    checkpoints = []
    with pytest.raises(PortableRuntimeStopped):
        executor.run(issue, pass_number=1, checkpoint=checkpoints.append)
    checkpoint = checkpoints[-1]
    assert len(checkpoint.attempts) == 0
    record = next(iter(writer.state[operator_handoff.STATE_KEY].values()))
    receipt(record, handoff._directory(record))
    execution = executor.run(issue, pass_number=1, recovery=checkpoint)
    assert execution.issue_status is IssueStatus.COMPLETED
    assert all(attempt.outcome is StepOutcome.SUCCEEDED for attempt in execution.attempts)
    assert all(attempt.pass_number == 1 for attempt in execution.attempts)


def test_ignored_local_settings_invalidate_verification(repository: Path) -> None:
    (repository / ".gitignore").write_text("appsettings.local.json\n")
    before = verification.source_fingerprint(repository)
    (repository / "appsettings.local.json").write_text('{"Connection": "local-test"}')
    after = verification.source_fingerprint(repository)
    assert before != after
    (repository / "appsettings.local.json").write_text('{"Connection": "changed-test"}')
    assert after != verification.source_fingerprint(repository)


def test_dtd_report_is_rejected(repository: Path) -> None:
    path = repository / "malicious.trx"
    path.write_bytes('<!DOCTYPE TestRun [<!ENTITY x "expansion">]><TestRun/>'.encode("utf-16"))
    with pytest.raises(ValueError, match="DTD"):
        verification.validate_report(path, 1)


@pytest.mark.skipif(os.name != "nt", reason="Git for Windows probes the inherited input pipe")
def test_source_check_does_not_wait_for_worker_control_pipe(repository: Path, monkeypatch) -> None:
    """Replay the live worker: its control reader holds an open synchronous pipe."""
    read_fd, write_fd = os.pipe()
    control_input = os.fdopen(read_fd, "rb")
    reader = threading.Thread(target=control_input.read, daemon=True)
    reader.start()
    released = threading.Event()
    release_lock = threading.Lock()

    def release_control_pipe():
        with release_lock:
            if not released.is_set():
                released.set()
                os.close(write_fd)

    real_run = subprocess.run

    def run_with_worker_stdin(command, **kwargs):
        # Model inherited stdin without changing pytest's standard handle.
        # Explicit child stdin takes precedence, as it does in the worker.
        kwargs.setdefault("stdin", control_input)
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run_with_worker_stdin)
    watchdog = threading.Timer(5, release_control_pipe)
    watchdog.start()
    try:
        verification.source_fingerprint(repository)
        assert not released.is_set(), "Git waited for the worker control pipe to close"
    finally:
        watchdog.cancel()
        release_control_pipe()
        reader.join(timeout=2)
        control_input.close()


def test_git_timeout_returns_actionable_result_instead_of_leaving_step_running(
    repository: Path,
    gate: OperatorVerification,
    monkeypatch,
) -> None:
    handoff, runner, _, arguments = setup_handoff(repository, gate)

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    result = handoff.run_role(arguments)
    assert result.status == "BLOCKED"
    assert "Git source inspection timed out after 30 seconds" in result.fix_list[0]
    assert runner.run_role.call_count == 1


def test_preparation_screen_is_published_before_inspecting_source(
    repository: Path,
    gate: OperatorVerification,
    monkeypatch,
) -> None:
    handoff, _, _, arguments = setup_handoff(repository, gate)
    runtime = Mock()
    monkeypatch.setattr(operator_handoff, "active_portable_runtime", lambda: runtime)

    def fingerprint(_repository):
        assert runtime.update_session_status.call_args.kwargs["stage"] == (
            "Preparing external verification"
        )
        assert "PREPARING EXTERNAL VERIFICATION" in runtime.show_screen.call_args.args[0]
        return "test-fingerprint"

    monkeypatch.setattr(operator_handoff, "source_fingerprint", fingerprint)
    handoff._create_request("key", gate, arguments)


def test_request_survives_real_state_reload_without_invalidating_itself(
    repository: Path,
    gate: OperatorVerification,
) -> None:
    handoff, runner, _, arguments = setup_handoff(repository, gate)
    index = repository / "README.md"
    index.write_text("# Issues\n")
    writer = LoopStateWriter(index)
    writer.prd_state_path = repository / "devloop.status.json"
    handoff = OperatorHandoff(runner, writer)
    record = handoff._create_request("key", gate, arguments)
    assert verification.source_fingerprint(repository) == record["source_fingerprint"]
    recovered = LoopStateWriter(index)
    assert recovered.state[operator_handoff.STATE_KEY]["key"] == record
    receipt(record, handoff._directory(record))
    assert "2 passed, zero skipped" in verification.validate_receipt(
        record, handoff._directory(record)
    )
