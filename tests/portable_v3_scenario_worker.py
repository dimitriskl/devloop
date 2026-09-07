"""Deterministic backend for the integrated scenario; never invokes an AI CLI."""

from __future__ import annotations

import json
import os
import sys
import time
from enum import Enum
from pathlib import Path

from devloop import portable_worker
from devloop.codex_runner import RoleResult
from devloop.issue_pack import parse_issue_index
from devloop.issue_scheduler import SchedulingPhase
from devloop.portable_runtime import PortableRunContext, active_portable_runtime
from devloop.portable_session_catalog import (
    PortablePlanningSettings,
    PortableSessionCatalog,
    active_portable_catalog_session,
    bind_active_catalog_session_checkout,
)
from devloop.portable_sessions import (
    PortableRecoveryData,
    PortableSessionLaunch,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
    PortableWorktreeLeaseConflict,
    run_portable_plain_session,
)
from devloop.state import LoopStateWriter

THREAD_ID = "11111111-2222-4333-8444-555555555555"
PLANNING_SETTINGS = PortablePlanningSettings(
    backend="CODEX_CLI",
    model="gpt-5.4",
    reasoning_effort="high",
    fast="OFF",
    timeout_seconds=1200,
    checkpoint_seconds=300,
)
OWNERSHIP_MARKER = "scenario-ownership.json"
PARTIAL_BYTES = b"Partial backend edit; reviewer has not finished.\n"


class BackendCommand(str, Enum):
    ACTIVITY = "activity"
    ASK = "ask"
    PARTIAL = "partial"
    CRASH = "crash"
    COMPLETE = "complete"


def write_json(path: Path, value: object) -> None:
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def workflow_paths(checkout: Path) -> tuple[Path, Path, Path]:
    feature = checkout / "prd" / "scenario"
    return (
        feature / "scenario.md",
        feature / "issues" / "README.md",
        feature / "issues" / "0001-scenario.md",
    )


def seed_workflow(checkout: Path) -> None:
    prd, index, issue_path = workflow_paths(checkout)
    index.parent.mkdir(parents=True)
    prd.write_text("# Representative unfinished workflow\n", encoding="utf-8")
    index.write_text("- [Issue 0001](./0001-scenario.md)\n", encoding="utf-8")
    issue_path.write_text("# Fixture work\n\nCompleted: [ ]\n", encoding="utf-8")
    logs = prd.parent / ".loop.logs"
    logs.mkdir()
    (logs / "preserved.log").write_bytes(b"Existing non-secret project evidence\r\n")
    issue = parse_issue_index(index)[0]
    writer = LoopStateWriter(index)
    writer.record_run_start(checkout, prd, [issue.number], False)
    writer.reserve_scheduling_attempt(issue, phase=SchedulingPhase.NORMAL_SCHEDULING, ordinal=1)
    writer.record_issue_start(issue)
    writer.record_role_result(
        issue,
        "coder",
        2,
        RoleResult(status="PASS", summary="Durable coder pass two."),
    )


def _complete_fixture(checkout: Path) -> None:
    _prd, index, issue_path = workflow_paths(checkout)
    issue = parse_issue_index(index)[0]
    writer = LoopStateWriter(index)
    result = RoleResult(status="PASS", summary="Synthetic backend completed fixture work.")
    writer.record_role_result(issue, "reviewer", 2, result)
    writer.record_role_result(issue, "qa", 2, result)
    writer.record_issue_completed(issue, result, result, result)
    issue_path.write_text("# Fixture work\n\nCompleted: [x]\n", encoding="utf-8")


def _run_backend(root: Path, role: str, attempt: int) -> None:
    events = root / "events" / role / str(attempt)
    commands = root / "commands" / role / str(attempt)

    def execute(
        operation: PortableWorkflowOperation,
        arguments: list[str],
        *,
        recovery: PortableRecoveryData | None = None,
    ) -> int:
        # Keep the shipped worker's checkpoint comparison and argument validation.
        if recovery is not None:
            arguments = portable_worker._arguments_for_recovery(operation, arguments, recovery)
        active = active_portable_catalog_session()
        assert active is not None
        catalog, record, _restore = active
        assert record is not None
        checkout = Path.cwd().resolve()
        if role == "alpha":
            if record.planning_thread_id is None:
                catalog.save_planning_settings(record.session_id, PLANNING_SETTINGS)
                catalog.save_planning_thread(record.session_id, THREAD_ID)
            else:
                assert record.planning_thread_id == THREAD_ID
                assert record.planning_settings == PLANNING_SETTINGS
        else:
            prd, index, _issue = workflow_paths(checkout)
            bind_active_catalog_session_checkout(
                checkout,
                prd_path=prd,
                issues_index_path=index,
            )
            cursor = LoopStateWriter(index).resume_issue(parse_issue_index(index)[0])
            assert cursor.next_role.value == "reviewer"
            assert cursor.pass_number == 2
        bridge = active_portable_runtime()
        assert isinstance(bridge, portable_worker.PortableWorkerRuntimeBridge)
        bridge.update_run_context(
            PortableRunContext(
                project_root=str(root / "repository"),
                implementation_branch=f"scenario-{role}",
                implementation_worktree=str(checkout),
                prd_path="" if role == "alpha" else str(workflow_paths(checkout)[0]),
            )
        )
        write_json(
            events / "backend-request.json",
            {
                "role": role,
                "attempt": attempt,
                "checkout": str(checkout),
                "commands": str(commands),
                "recovery": recovery.to_payload() if recovery is not None else None,
                "arguments": arguments,
            },
        )
        bridge.write_output(f"{role}: backend started\n", is_error=False)
        write_json(events / "backend-ready.json", {"commands": str(commands)})
        command_number = 1
        while True:
            if bridge.lifecycle_request is not None:
                # read_line observes the real lifecycle interrupt before requesting input.
                bridge.read_line("Synthetic backend reached its durable boundary")
                raise AssertionError("Lifecycle request should stop the backend")
            command_path = commands / f"{command_number}.json"
            if not command_path.is_file():
                time.sleep(0.01)
                continue
            command = BackendCommand(json.loads(command_path.read_text(encoding="utf-8")))
            write_json(
                events / f"command-received-{command_number}.json",
                {"command": command.value, "path": str(command_path)},
            )
            if command is BackendCommand.ACTIVITY:
                bridge.write_output(f"{role}: continued in background\n", is_error=False)
            elif command is BackendCommand.ASK:
                answer = bridge.read_line(f"{role}: enter this session's answer")
                write_json(events / "answer.json", {"role": role, "answer": answer})
            elif command is BackendCommand.PARTIAL:
                (checkout / "partial.txt").write_bytes(PARTIAL_BYTES)
                print(f"{role}: partial edit diagnostic", file=sys.stderr, flush=True)
                bridge.write_output(f"{role}: partial edit persisted\n", is_error=False)
            elif command is BackendCommand.CRASH:
                print(f"{role}: abrupt synthetic crash 17", file=sys.stderr, flush=True)
                os._exit(17)
            elif command is BackendCommand.COMPLETE:
                assert role != "alpha", "Pre-PRD planning is not project completion"
                _complete_fixture(checkout)
                return 0
            write_json(events / f"command-{command_number}.json", {"command": command.value})
            command_number += 1

    portable_worker._run_operation = execute
    raise SystemExit(
        portable_worker.main(["--session-id", os.environ["DEVLOOP_PORTABLE_SESSION_ID"]])
    )


def _conflict_probe(root: Path, mode: str, checkout: Path) -> None:
    catalog = PortableSessionCatalog(root / "catalog.sqlite3")
    launch = PortableSessionLaunch(
        f"conflict-{mode}",
        checkout,
        PortableWorkflowOperation.PLANNING,
        (),
    )
    invoked = False

    def forbidden_operation() -> int:
        nonlocal invoked
        invoked = True
        raise AssertionError("A conflicting worktree must never invoke its backend")

    if mode == "plain":
        result = run_portable_plain_session(
            launch,
            forbidden_operation,
            catalog=catalog,
            owner_id="scenario-plain-probe",
        )
        print(json.dumps({"result": result, "backend_invoked": invoked}))
        return

    def forbidden_launcher(_launch: PortableSessionLaunch) -> None:
        forbidden_operation()

    supervisor = PortableSessionSupervisor(
        catalog=catalog,
        owner_id="scenario-other-application",
        worker_launcher=forbidden_launcher,  # type: ignore[arg-type]
    )
    try:
        try:
            supervisor.start_session(launch)
        except PortableWorktreeLeaseConflict as error:
            print(
                json.dumps(
                    {
                        "conflict_session": error.session_id,
                        "backend_invoked": invoked,
                    }
                )
            )
        else:
            raise AssertionError("Another application bypassed the worktree lease")
    finally:
        supervisor.shutdown()


if __name__ == "__main__":
    mode, root_text, role = sys.argv[1:4]
    scenario_root = Path(root_text).resolve()
    manifest = json.loads((scenario_root / OWNERSHIP_MARKER).read_text(encoding="utf-8"))
    selected_checkout = Path(manifest["worktrees"][role]).resolve()
    assert selected_checkout.is_relative_to(scenario_root)
    if mode == "worker":
        assert Path.cwd().resolve() == selected_checkout
        _run_backend(scenario_root, role, int(sys.argv[4]))
    else:
        assert mode in {"plain", "application"}
        _conflict_probe(scenario_root, mode, selected_checkout)
