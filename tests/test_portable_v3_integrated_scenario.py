"""One connected three-worktree demo. Native wrapper/profile gates remain separate."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest
from portable_test_support import isolated_environment, require_fixture_path
from portable_v3_scenario_worker import (
    OWNERSHIP_MARKER,
    PARTIAL_BYTES,
    PLANNING_SETTINGS,
    THREAD_ID,
    BackendCommand,
    seed_workflow,
    workflow_paths,
    write_json,
)
from textual.pilot import Pilot
from textual.widgets import Input, OptionList, Static

from devloop.issue_pack import parse_issue_index
from devloop.portable_project_adoption import adopt_v021_configuration
from devloop.portable_runtime import PortableRuntimeBridge
from devloop.portable_session_catalog import PortableSessionCatalog
from devloop.portable_sessions import (
    PortableSessionLaunch,
    PortableSessionStatus,
    PortableSessionSupervisor,
    PortableWorkflowOperation,
)
from devloop.portable_ui.app import (
    SESSION_HISTORY_ID,
    SESSIONS_TAB_ID,
    PortableApplicationShell,
)
from devloop.state import LoopStateWriter
from devloop.subprocess_utils import (
    ProcessTerminationResult,
    launch_process_tree,
    terminate_process,
)

ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("portable_v3_scenario_worker.py")
ROLES = ("alpha", "beta", "gamma")
WAIT_SECONDS = 12.0


class RecordingBellShell(PortableApplicationShell):
    """Observe the optional notification without ringing the operator's terminal."""

    bell_count = 0

    def bell(self) -> None:
        self.bell_count += 1


class ScenarioCleanupError(RuntimeError):
    def __init__(self, failures: list[str]) -> None:
        self.failures = failures
        super().__init__("Scenario cleanup failed:\n" + "\n".join(failures))


class Scenario:
    def __init__(self, root: Path) -> None:
        self.root = require_fixture_path(root)
        self.repository = root / "repository"
        self.worktrees = {role: root / f"worktree-{role}" for role in ROLES}
        self.catalog = PortableSessionCatalog(root / "catalog.sqlite3")
        self.session_ids = {role: f"scenario-{role}" for role in ROLES}
        self.attempts = dict.fromkeys(ROLES, 0)
        self.command_numbers = dict.fromkeys(ROLES, 0)
        self.processes: list[subprocess.Popen[str]] = []
        self.launch_order: list[str] = []
        self.supervisors: list[PortableSessionSupervisor] = []
        self.owner_id = "scenario-shell-first"
        self.supervisor: PortableSessionSupervisor
        self.evidence: dict[str, object] = {
            "scope": "real Git/catalog/supervisor/worker protocol; fake backend; headless UI",
            "native_wrappers": "NOT RUN",
            "historical_installation": "NOT RUN",
            "status": "RUNNING",
            "phases": [],
            "source_sha256": {
                str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (Path(__file__).resolve(), WORKER)
            },
        }

    def record(self, phase: str, **facts: object) -> None:
        phases = self.evidence["phases"]
        assert isinstance(phases, list)
        phases.append({"phase": phase, **facts})
        write_json(self.root / "evidence.json", self.evidence)

    def git(self, checkout: Path, *arguments: str) -> str:
        require_fixture_path(checkout)
        result = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-C", str(checkout), *arguments],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        return result.stdout.strip()

    def project_hashes(self) -> dict[str, str]:
        return {
            f"{role}/{path.relative_to(checkout).as_posix()}": hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for role, checkout in self.worktrees.items()
            for path in sorted(checkout.rglob("*"))
            if path.is_file()
        }

    def prepare(self) -> None:
        self.repository.mkdir()
        self.git(self.repository, "init")
        self.git(self.repository, "config", "user.name", "Scenario Fixture")
        self.git(self.repository, "config", "user.email", "scenario@example.invalid")
        (self.repository / "README.md").write_text("# Disposable scenario repository\n")
        self.git(self.repository, "add", "README.md")
        self.git(self.repository, "commit", "-m", "Create scenario fixture")
        for role, checkout in self.worktrees.items():
            require_fixture_path(checkout, allow_existing=False)
            self.git(self.repository, "worktree", "add", "-b", f"scenario-{role}", str(checkout))
            assert (checkout / ".git").is_file()
            assert Path(self.git(checkout, "rev-parse", "--show-toplevel")).resolve() == checkout
        common_dirs = {
            Path(
                self.git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
            ).resolve()
            for checkout in self.worktrees.values()
        }
        assert common_dirs == {self.repository / ".git"}
        write_json(
            self.root / OWNERSHIP_MARKER,
            {
                "worktrees": {role: str(path) for role, path in self.worktrees.items()},
            },
        )
        seed_workflow(self.worktrees["beta"])
        configuration = self.root / "devloop-plan-v021-shaped.json"
        write_json(
            configuration,
            {
                "target_repo": str(self.worktrees["alpha"]),
                "target_repo_confirmed": True,
                "selection": {"reasoning": "high"},
            },
        )
        before = self.project_hashes()
        configuration_bytes = configuration.read_bytes()
        worktree_listing = self.git(self.repository, "worktree", "list", "--porcelain")
        branches = self.git(self.repository, "branch", "--format=%(refname)")
        first = adopt_v021_configuration(self.catalog, configuration)
        sessions = self.catalog.list_sessions()
        assert len(sessions) == 1
        self.session_ids["beta"] = sessions[0].session_id
        second = adopt_v021_configuration(self.catalog, configuration)
        assert self.catalog.list_sessions() == sessions
        assert len(self.catalog.list_adoption_receipts()) == 1
        assert self.catalog.list_adoption_receipts()[0].source_version == "0.2.1"
        assert configuration.read_bytes() == configuration_bytes
        assert self.project_hashes() == before
        assert self.git(self.repository, "worktree", "list", "--porcelain") == worktree_listing
        assert self.git(self.repository, "branch", "--format=%(refname)") == branches
        self.record(
            "representative-v021-shaped-adoption",
            project_sha256=before,
            first=first.render(),
            repeated=second.render(),
            project_bytes_unchanged=True,
            configuration_sha256=hashlib.sha256(configuration_bytes).hexdigest(),
            provenance="Synthetic configuration/project; no old installer or old runner executed",
        )
        seed_workflow(self.worktrees["gamma"])
        self.restart_supervisor()

    def restart_supervisor(self) -> None:
        self.catalog = PortableSessionCatalog(self.root / "catalog.sqlite3")
        self.supervisor = PortableSessionSupervisor(
            catalog=self.catalog,
            owner_id=self.owner_id,
            worker_launcher=self.launch_worker,
        )
        self.supervisors.append(self.supervisor)

    def launch(self, role: str, *, session_id: str | None = None) -> PortableSessionLaunch:
        checkout = self.worktrees[role]
        prd, index, _issue = workflow_paths(checkout)
        return PortableSessionLaunch(
            session_id or self.session_ids[role],
            checkout,
            PortableWorkflowOperation.PLANNING
            if role == "alpha"
            else PortableWorkflowOperation.DELIVERY,
            () if role == "alpha" else ("--prd", str(prd), "--issues", str(index)),
        )

    def launch_worker(self, launch: PortableSessionLaunch) -> subprocess.Popen[str]:
        role = next(
            role for role, session_id in self.session_ids.items() if session_id == launch.session_id
        )
        self.attempts[role] += 1
        self.command_numbers[role] = 0
        attempt = self.attempts[role]
        for kind in ("events", "commands"):
            (self.root / kind / role / str(attempt)).mkdir(parents=True)
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": str(ROOT / "src"),
                "DEVLOOP_UI_MODE": "application",
                "DEVLOOP_PORTABLE_SESSION_CATALOG": str(self.catalog.path),
                "DEVLOOP_PORTABLE_SESSION_OWNER_ID": self.owner_id,
                "DEVLOOP_PORTABLE_SESSION_ID": launch.session_id,
            }
        )
        process = launch_process_tree(
            [sys.executable, "-u", str(WORKER), "worker", str(self.root), role, str(attempt)],
            cwd=launch.checkout,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.processes.append(process)
        self.launch_order.append(role)
        return process

    def command(self, role: str, command: BackendCommand) -> None:
        self.command_numbers[role] += 1
        write_json(
            self.root
            / "commands"
            / role
            / str(self.attempts[role])
            / f"{self.command_numbers[role]}.json",
            command.value,
        )

    def event_path(self, role: str, name: str) -> Path:
        return self.root / "events" / role / str(self.attempts[role]) / f"{name}.json"

    def event(self, role: str, name: str) -> dict[str, object]:
        return json.loads(self.event_path(role, name).read_text(encoding="utf-8"))

    def status(self, role: str) -> PortableSessionStatus:
        return self.supervisor.snapshot(self.session_ids[role]).status

    def cursor(self, role: str) -> tuple[str, int]:
        _prd, index, _issue = workflow_paths(self.worktrees[role])
        cursor = LoopStateWriter(index).resume_issue(parse_issue_index(index)[0])
        return cursor.next_role.value, cursor.pass_number

    def state_bytes(self, role: str) -> bytes:
        _prd, index, _issue = workflow_paths(self.worktrees[role])
        return index.with_name(f"{index.stem}.loop.state.json").read_bytes()

    def owns_capacity(self, role: str) -> bool:
        return self.catalog.owns_execution_capacity(
            self.session_ids[role],
            owner_id=self.owner_id,
        )

    def shell(self) -> RecordingBellShell:
        return RecordingBellShell(
            PortableRuntimeBridge(),
            session_supervisor=self.supervisor,
            session_launch=self.launch("alpha", session_id="unused-shell-launch"),
        )

    def close(self) -> None:
        failures: list[str] = []
        for index, supervisor in reversed(list(enumerate(self.supervisors))):
            try:
                supervisor.shutdown()
            except BaseException as error:
                failures.append(f"supervisor {index} shutdown: {type(error).__name__}: {error}")
        for index, process in enumerate(self.processes):
            try:
                try:
                    result = terminate_process(process)
                    if not result.tree_terminated:
                        failures.append(f"worker {index} termination unconfirmed: {result.detail}")
                except BaseException as error:
                    failures.append(f"worker {index} terminate: {type(error).__name__}: {error}")
                finally:
                    try:
                        process.wait(timeout=10)
                    except BaseException as error:
                        failures.append(f"worker {index} wait: {type(error).__name__}: {error}")
            finally:
                for name in ("stdin", "stdout", "stderr"):
                    try:
                        stream = getattr(process, name)
                        if stream is not None:
                            stream.close()
                    except BaseException as error:
                        failures.append(f"worker {index} {name}: {type(error).__name__}: {error}")
        if failures:
            raise ScenarioCleanupError(failures)

    @contextmanager
    def running(self) -> Iterator[None]:
        original_error: BaseException | None = None
        try:
            yield
        except BaseException as error:
            original_error = error
            raise
        finally:
            try:
                self.finalize(original_error)
            except BaseException as error:
                if original_error is None:
                    raise
                # Keep the causal scenario exception; details also remain in evidence.json.
                print(f"Scenario finalization also failed: {error}", file=sys.stderr)

    def finalize(self, original_error: BaseException | None) -> None:
        cleanup_error: ScenarioCleanupError | None = None
        try:
            self.close()
        except ScenarioCleanupError as error:
            cleanup_error = error
        self.evidence["cleanup_failures"] = cleanup_error.failures if cleanup_error else []
        if original_error is not None or cleanup_error is not None:
            self.evidence["status"] = "FAILED (inspect pytest traceback and cleanup_failures)"
            write_json(self.root / "evidence.json", self.evidence)
        else:
            self.evidence["status"] = "PASS (scoped scenario only)"
            self.record(
                "all-owned-workers-reaped",
                workers=len(self.processes),
                launch_order=self.launch_order,
            )
        print(f"Integrated three-worktree evidence: {self.root / 'evidence.json'}")
        if cleanup_error is not None:
            raise cleanup_error


async def wait_until(predicate: Callable[[], bool], description: str) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not predicate():
        assert time.monotonic() < deadline, f"Timed out waiting for {description}"
        await asyncio.sleep(0.02)


async def wait_status(scenario: Scenario, role: str, status: PortableSessionStatus) -> None:
    try:
        await wait_until(lambda: scenario.status(role) is status, f"{role} {status.value}")
    except AssertionError:
        snapshots = {}
        for observed_role, session_id in scenario.session_ids.items():
            snapshot = scenario.supervisor.snapshot(session_id)
            snapshots[observed_role] = {
                "session_id": session_id,
                "status": snapshot.status.value,
                "activity": snapshot.activity,
                "diagnostics": snapshot.diagnostics,
                "result": snapshot.result,
                "input_request": snapshot.input_request is not None,
                "checkout": str(snapshot.checkout),
            }
        scenario.evidence["timeout_before_ui_unmount"] = {
            "waiting_for": f"{role} {status.value}",
            "snapshots": snapshots,
            "workers": [
                {"pid": process.pid, "returncode": process.poll()} for process in scenario.processes
            ],
        }
        write_json(scenario.root / "evidence.json", scenario.evidence)
        raise


async def wait_started(scenario: Scenario, role: str) -> None:
    await wait_until(
        lambda: scenario.event_path(role, "backend-request").is_file(),
        f"{role} backend request",
    )


async def choose(app: RecordingBellShell, pilot: Pilot[int], option_id: str) -> None:
    menu = app.query_one("#portable-navigation", OptionList)
    indexes = [i for i in range(menu.option_count) if menu.get_option_at_index(i).id == option_id]
    assert len(indexes) == 1, (
        option_id,
        [menu.get_option_at_index(i).id for i in range(menu.option_count)],
    )
    menu.highlighted = indexes[0]
    menu.focus()
    await pilot.press("enter")
    await pilot.pause()


def text(app: RecordingBellShell, widget: str) -> str:
    return str(app.query_one(widget, Static).content)


def background_input_received(app: RecordingBellShell, checkout: Path) -> bool:
    status = PortableSessionStatus.WAITING_FOR_INPUT.value
    expected_tab = f"{checkout.name} [{status}] [INPUT!]"
    return expected_tab in text(app, "#portable-tabs").split(" | ") and app.bell_count == 1


async def open_session(
    scenario: Scenario, app: RecordingBellShell, pilot: Pilot[int], role: str
) -> None:
    if text(app, "#portable-header") != "Dev Loop > Sessions":
        await choose(app, pilot, SESSIONS_TAB_ID)
    await choose(app, pilot, scenario.session_ids[role])
    assert text(app, "#portable-header") == f"Dev Loop > {scenario.worktrees[role].name}"


async def pause_session(scenario: Scenario, role: str) -> None:
    scenario.supervisor.pause_session(scenario.session_ids[role])
    await wait_status(scenario, role, PortableSessionStatus.PAUSED)
    assert scenario.catalog.get_worktree_lease(scenario.worktrees[role]) is None
    assert not scenario.owns_capacity(role)


async def prove_conflicts(scenario: Scenario) -> None:
    launches = list(scenario.launch_order)
    existing = scenario.supervisor.start_session(
        scenario.launch("alpha", session_id="duplicate-tab")
    )
    assert existing.session_id == scenario.session_ids["alpha"]
    assert scenario.launch_order == launches
    results = {}
    for mode in ("application", "plain"):
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(WORKER), mode, str(scenario.root), "alpha"],
            cwd=scenario.root,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        assert "\x1b" not in result.stdout + result.stderr
        observed = json.loads(result.stdout)
        assert observed["backend_invoked"] is False
        if mode == "plain":
            assert observed["result"] == 73
            assert scenario.session_ids["alpha"] in result.stderr
        else:
            assert observed["conflict_session"] == scenario.session_ids["alpha"]
        results[mode] = observed
    assert scenario.launch_order == launches
    scenario.record(
        "same-worktree-exclusion", same_shell_focused=existing.session_id, processes=results
    )


async def first_application(scenario: Scenario) -> None:
    app = scenario.shell()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        assert text(app, "#portable-tabs") == "Sessions"
        assert scenario.launch_order == []
        scenario.supervisor.start_session(scenario.launch("alpha"))
        scenario.supervisor.resume_session(scenario.session_ids["beta"])
        third = scenario.supervisor.start_session(scenario.launch("gamma"))
        assert third.status is PortableSessionStatus.QUEUED
        assert scenario.catalog.get_concurrency_limit() == 2
        for role in ("alpha", "beta"):
            await wait_started(scenario, role)
            await wait_status(scenario, role, PortableSessionStatus.RUNNING)
        assert scenario.launch_order == ["alpha", "beta"]
        assert {role for role in ROLES if scenario.owns_capacity(role)} == {"alpha", "beta"}
        await pilot.pause()
        await open_session(scenario, app, pilot, "gamma")
        assert "[QUEUED]" in text(app, "#portable-tabs")
        scenario.record(
            "two-running-one-queued",
            launch_order=list(scenario.launch_order),
            tabs=text(app, "#portable-tabs"),
        )
        await prove_conflicts(scenario)
        await open_session(scenario, app, pilot, "alpha")
        await open_session(scenario, app, pilot, "beta")
        beta_header = text(app, "#portable-header")
        scenario.command("alpha", BackendCommand.ACTIVITY)
        await wait_until(
            lambda: "worktree-alpha [RUNNING] *" in text(app, "#portable-tabs"),
            "alpha background unread marker",
        )
        assert text(app, "#portable-header") == beta_header
        assert "scenario-beta" in text(app, "#portable-run-context")
        assert "scenario-alpha" not in text(app, "#portable-run-context")
        scenario.command("beta", BackendCommand.ASK)
        await wait_status(scenario, "beta", PortableSessionStatus.WAITING_FOR_INPUT)
        await wait_until(lambda: app.query_one("#portable-input", Input).display, "beta input")
        beta_input = app.query_one("#portable-input", Input)
        beta_input.value = "beta-only-answer"
        await wait_started(scenario, "gamma")
        assert not scenario.owns_capacity("beta")
        assert scenario.owns_capacity("gamma")
        scenario.command("alpha", BackendCommand.ASK)
        await wait_status(scenario, "alpha", PortableSessionStatus.WAITING_FOR_INPUT)
        assert not scenario.owns_capacity("alpha")
        await wait_until(
            lambda: background_input_received(app, scenario.worktrees["alpha"]),
            "alpha tab input attention and bell callback",
        )
        assert text(app, "#portable-header") == beta_header
        assert scenario.launch_order == ["alpha", "beta", "gamma"]
        assert app.bell_count == 1
        assert app.focused is beta_input
        assert beta_input.value == "beta-only-answer"
        await pilot.press("enter")
        await wait_until(lambda: scenario.event_path("beta", "answer").is_file(), "beta answer")
        assert scenario.event("beta", "answer")["answer"] == "beta-only-answer"
        assert not scenario.event_path("alpha", "answer").exists()
        await open_session(scenario, app, pilot, "alpha")
        app.query_one("#portable-input", Input).value = "alpha-only-answer"
        await pilot.press("enter")
        await wait_status(scenario, "alpha", PortableSessionStatus.QUEUED)
        before_pause = scenario.cursor("beta")
        before_pause_bytes = scenario.state_bytes("beta")
        await pause_session(scenario, "beta")
        await wait_until(
            lambda: scenario.event_path("alpha", "answer").is_file(), "queued alpha input"
        )
        assert scenario.event("alpha", "answer")["answer"] == "alpha-only-answer"
        assert scenario.cursor("beta") == before_pause == ("reviewer", 2)
        assert scenario.state_bytes("beta") == before_pause_bytes
        scenario.record(
            "input-isolation-and-pause-capacity",
            answers={role: scenario.event(role, "answer") for role in ("alpha", "beta")},
            beta_cursor=list(before_pause),
            notification_count=app.bell_count,
        )
        await open_session(scenario, app, pilot, "gamma")
        await pilot.press("escape")
        await pilot.pause()
        assert "worktree-gamma" not in text(app, "#portable-tabs")
        scenario.command("gamma", BackendCommand.ACTIVITY)
        await wait_until(
            lambda: "gamma: continued" in text(app, "#portable-detail"), "hidden progress"
        )
        assert scenario.status("gamma") is PortableSessionStatus.RUNNING
        await open_session(scenario, app, pilot, "gamma")
        assert "gamma: continued" in text(app, "#portable-detail")
        scenario.record(
            "tab-context-unread-notification-hide-reopen",
            header=beta_header,
            reopened_tabs=text(app, "#portable-tabs"),
        )
        for role in ("alpha", "gamma"):
            await pause_session(scenario, role)
        record = scenario.catalog.get_session(scenario.session_ids["alpha"])
        assert record.prd_path is None
        assert record.planning_thread_id == THREAD_ID
        assert record.planning_settings == PLANNING_SETTINGS
        scenario.record(
            "application-paused", statuses={role: scenario.status(role).value for role in ROLES}
        )


async def second_application(scenario: Scenario) -> None:
    assert all(process.poll() is not None for process in scenario.processes)
    prior_launches = list(scenario.launch_order)
    scenario.owner_id = "scenario-shell-restarted"
    scenario.restart_supervisor()
    app = scenario.shell()
    async with app.run_test(size=(120, 34)) as pilot:
        await pilot.pause()
        assert text(app, "#portable-tabs") == "Sessions"
        assert scenario.launch_order == prior_launches
        assert all(scenario.status(role) is PortableSessionStatus.PAUSED for role in ROLES)
        for role in ROLES:
            scenario.supervisor.resume_session(scenario.session_ids[role])
        for role in ("alpha", "beta"):
            await wait_started(scenario, role)
        planning = scenario.event("alpha", "backend-request")["recovery"]
        assert isinstance(planning, dict)
        assert planning["planning_thread_id"] == THREAD_ID
        assert planning["planning_settings"] == PLANNING_SETTINGS.to_dict()
        delivery = scenario.event("beta", "backend-request")["recovery"]
        assert isinstance(delivery, dict)
        assert (delivery["issue_id"], delivery["next_role"], delivery["pass_number"]) == (
            "0001",
            "reviewer",
            2,
        )
        assert scenario.status("gamma") is PortableSessionStatus.QUEUED
        scenario.record("passive-restart-exact-resume", planning=planning, delivery=delivery)
        before_force_stop = scenario.state_bytes("beta")
        scenario.command("beta", BackendCommand.PARTIAL)
        await wait_until(
            lambda: (scenario.worktrees["beta"] / "partial.txt").exists(), "partial edit"
        )
        await wait_until(
            lambda: any(
                "partial edit diagnostic" in line
                for line in scenario.supervisor.snapshot(scenario.session_ids["beta"]).diagnostics
            ),
            "partial edit diagnostics",
        )
        stopped = await asyncio.to_thread(
            scenario.supervisor.force_stop_session, scenario.session_ids["beta"]
        )
        assert stopped.status is PortableSessionStatus.INTERRUPTED
        assert stopped.result != 0
        assert (scenario.worktrees["beta"] / "partial.txt").read_bytes() == PARTIAL_BYTES
        assert scenario.cursor("beta") == ("reviewer", 2)
        assert scenario.state_bytes("beta") == before_force_stop
        assert not parse_issue_index(workflow_paths(scenario.worktrees["beta"])[1])[0].completed
        await wait_status(scenario, "gamma", PortableSessionStatus.RUNNING)
        await wait_started(scenario, "gamma")
        assert scenario.status("alpha") is PortableSessionStatus.RUNNING
        launches_before_crash = list(scenario.launch_order)
        scenario.command("gamma", BackendCommand.CRASH)
        await wait_status(scenario, "gamma", PortableSessionStatus.INTERRUPTED)
        await wait_until(
            lambda: any(
                "abrupt synthetic crash 17" in line
                for line in scenario.supervisor.snapshot(scenario.session_ids["gamma"]).diagnostics
            ),
            "crash diagnostics",
        )
        scenario.command("alpha", BackendCommand.ACTIVITY)
        await wait_until(
            lambda: scenario.event_path("alpha", "command-1").is_file(), "healthy sibling"
        )
        assert scenario.launch_order == launches_before_crash
        assert scenario.status("alpha") is PortableSessionStatus.RUNNING
        scenario.record(
            "force-stop-and-crash-isolation",
            stopped_result=stopped.result,
            partial_sha256=hashlib.sha256(PARTIAL_BYTES).hexdigest(),
            beta_cursor=list(scenario.cursor("beta")),
            automatic_replay=False,
        )
        scenario.supervisor.resume_session(scenario.session_ids["beta"])
        await wait_started(scenario, "beta")
        recovered = scenario.event("beta", "backend-request")["recovery"]
        assert isinstance(recovered, dict)
        assert (recovered["next_role"], recovered["pass_number"]) == ("reviewer", 2)
        assert any("partial edit diagnostic" in line for line in recovered["diagnostics"])
        assert scenario.cursor("beta") == ("reviewer", 2)
        scenario.command("beta", BackendCommand.COMPLETE)
        await wait_status(scenario, "beta", PortableSessionStatus.COMPLETED)
        scenario.supervisor.resume_session(scenario.session_ids["gamma"])
        await wait_started(scenario, "gamma")
        assert scenario.cursor("gamma") == ("reviewer", 2)
        crash_recovery = scenario.event("gamma", "backend-request")["recovery"]
        assert isinstance(crash_recovery, dict)
        assert any("abrupt synthetic crash 17" in line for line in crash_recovery["diagnostics"])
        scenario.command("gamma", BackendCommand.COMPLETE)
        await wait_status(scenario, "gamma", PortableSessionStatus.COMPLETED)
        await pause_session(scenario, "alpha")
        await pilot.pause()
        await choose(app, pilot, SESSION_HISTORY_ID)
        assert "History" in text(app, "#portable-header")
        history = scenario.supervisor.list_history_sessions()
        assert {item.session_id for item in history} == {
            scenario.session_ids["beta"],
            scenario.session_ids["gamma"],
        }
        scenario.record(
            "explicit-recovery-and-completed-history",
            history=[item.session_id for item in history],
            force_stop_recovery=recovered,
        )


def relink_and_forget(scenario: Scenario) -> None:
    scenario.owner_id = "scenario-history-shell"
    scenario.restart_supervisor()
    before_move = scenario.project_hashes()
    old = scenario.worktrees["gamma"]
    moved = require_fixture_path(scenario.root / "worktree-gamma-moved", allow_existing=False)
    # Both fully resolved targets are private fixture leaves. Git updates its worktree metadata.
    scenario.git(scenario.repository, "worktree", "move", str(old), str(moved))
    scenario.worktrees["gamma"] = moved
    assert scenario.project_hashes() == before_move
    unavailable = scenario.catalog.mark_missing_worktrees_unavailable()
    assert scenario.session_ids["gamma"] in unavailable
    scenario.supervisor.list_sessions()
    assert scenario.status("gamma") is PortableSessionStatus.UNAVAILABLE
    with pytest.raises(ValueError, match="PRD and Issue Index"):
        scenario.supervisor.relink_session(
            scenario.session_ids["gamma"], scenario.worktrees["alpha"]
        )
    assert scenario.catalog.get_session(scenario.session_ids["gamma"]).checkout == old
    linked = scenario.supervisor.relink_session(scenario.session_ids["gamma"], moved)
    assert linked.status is PortableSessionStatus.COMPLETED
    assert linked.checkout == moved
    assert scenario.project_hashes() == before_move
    branches = scenario.git(scenario.repository, "branch", "--format=%(refname)")
    listing = scenario.git(scenario.repository, "worktree", "list", "--porcelain")
    scenario.supervisor.forget_session(scenario.session_ids["beta"])
    assert scenario.project_hashes() == before_move
    assert scenario.git(scenario.repository, "branch", "--format=%(refname)") == branches
    assert scenario.git(scenario.repository, "worktree", "list", "--porcelain") == listing
    assert scenario.session_ids["beta"] not in {
        item.session_id for item in scenario.catalog.list_sessions()
    }
    assert {item.session_id for item in scenario.supervisor.list_history_sessions()} == {
        scenario.session_ids["gamma"]
    }
    scenario.record(
        "unavailable-invalid-valid-relink-metadata-forget",
        project_sha256=before_move,
        project_bytes_unchanged=True,
        relinked_checkout=str(moved),
    )


def test_three_worktrees_share_one_catalog_across_the_complete_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = require_fixture_path(tmp_path)
    environment = isolated_environment(root / "environment")
    for key in tuple(os.environ):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DEVLOOP_SESSION_ATTENTION_BELL", "1")
    scenario = Scenario(root)
    with scenario.running():
        scenario.prepare()
        asyncio.run(first_application(scenario))
        asyncio.run(second_application(scenario))
        relink_and_forget(scenario)


@pytest.fixture
def cleanup_scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scenario:
    """Fault injection only: no real supervisor threads, workers or subprocesses."""
    scenario = Scenario(require_fixture_path(tmp_path))
    scenario.supervisors = [mock.Mock(spec=PortableSessionSupervisor) for _ in range(2)]
    for _ in range(2):
        process = mock.Mock(spec=subprocess.Popen)
        process.poll.return_value = None
        process.wait.return_value = 0
        for name in ("stdin", "stdout", "stderr"):
            setattr(process, name, mock.Mock())
        scenario.processes.append(process)
    for target, name in (
        (subprocess, "run"),
        (subprocess, "Popen"),
        (os, "system"),
        (os, "_exit"),
        (sys.modules[__name__], "launch_process_tree"),
    ):
        monkeypatch.setattr(
            target, name, mock.Mock(side_effect=AssertionError(f"blocked real process: {name}"))
        )
    return scenario


@pytest.mark.parametrize(
    "failure", ("supervisor", "terminate", "wait", "stdin", "stdout", "stderr")
)
def test_cleanup_attempts_every_owned_resource_after_a_failure(
    cleanup_scenario: Scenario, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    scenario = cleanup_scenario
    error = RuntimeError(f"{failure} failed")
    terminated = ProcessTerminationResult(tree_terminated=True, detail="fixture stopped")
    terminate = mock.Mock(return_value=terminated)
    monkeypatch.setattr(sys.modules[__name__], "terminate_process", terminate)
    if failure == "supervisor":
        scenario.supervisors[-1].shutdown.side_effect = error
    elif failure == "terminate":
        terminate.side_effect = [error, terminated]
    elif failure == "wait":
        scenario.processes[0].wait.side_effect = error
    else:
        getattr(scenario.processes[0], failure).close.side_effect = error
    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        scenario.close()
    for supervisor in scenario.supervisors:
        supervisor.shutdown.assert_called_once_with()
    assert terminate.call_args_list == [mock.call(process) for process in scenario.processes]
    for process in scenario.processes:
        process.wait.assert_called_once_with(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close.assert_called_once_with()


def test_cleanup_rejects_unconfirmed_termination_even_when_wait_returns(
    cleanup_scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = cleanup_scenario
    for process in scenario.processes:
        process.poll.return_value = 0
    monkeypatch.setattr(
        sys.modules[__name__],
        "terminate_process",
        mock.Mock(return_value=ProcessTerminationResult(tree_terminated=False, detail="unknown")),
    )
    with pytest.raises(RuntimeError, match="termination unconfirmed"):
        scenario.close()
    for process in scenario.processes:
        process.wait.assert_called_once_with(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close.assert_called_once_with()


def test_cleanup_reports_every_failure_after_visiting_all_resources(
    cleanup_scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = cleanup_scenario
    for supervisor in scenario.supervisors:
        supervisor.shutdown.side_effect = RuntimeError("shutdown failed")
    terminate = mock.Mock(side_effect=RuntimeError("terminate failed"))
    monkeypatch.setattr(sys.modules[__name__], "terminate_process", terminate)
    for process in scenario.processes:
        process.wait.side_effect = RuntimeError("wait failed")
        for name in ("stdin", "stdout", "stderr"):
            getattr(process, name).close.side_effect = RuntimeError(f"{name} failed")
    with pytest.raises(ScenarioCleanupError) as raised:
        scenario.close()
    assert len(raised.value.failures) == 12
    for index in range(2):
        assert (
            f"supervisor {index} shutdown: RuntimeError: shutdown failed" in raised.value.failures
        )
        for operation in ("terminate", "wait", "stdin", "stdout", "stderr"):
            assert (
                f"worker {index} {operation}: RuntimeError: {operation} failed"
                in raised.value.failures
            )


@pytest.mark.parametrize(
    "original_error", (RuntimeError("causal scenario failure"), KeyboardInterrupt())
)
def test_cleanup_preserves_original_scenario_exception_and_records_failure(
    cleanup_scenario: Scenario, monkeypatch: pytest.MonkeyPatch, original_error: BaseException
) -> None:
    scenario = cleanup_scenario
    scenario.supervisors[-1].shutdown.side_effect = RuntimeError("shutdown failed")
    monkeypatch.setattr(
        sys.modules[__name__],
        "terminate_process",
        mock.Mock(return_value=ProcessTerminationResult(tree_terminated=True, detail="stopped")),
    )
    with pytest.raises(type(original_error)) as raised, scenario.running():
        scenario.record("fixture-body-before-failure")
        raise original_error
    assert raised.value is original_error
    evidence = json.loads((scenario.root / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"].startswith("FAILED")
    assert len(evidence["cleanup_failures"]) == 1
    assert all(phase["phase"] != "all-owned-workers-reaped" for phase in evidence["phases"])
    for process in scenario.processes:
        process.wait.assert_called_once_with(timeout=10)


def test_cleanup_failure_after_successful_body_cannot_publish_pass(
    cleanup_scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = cleanup_scenario
    scenario.processes[0].wait.side_effect = subprocess.TimeoutExpired("fixture worker", 10)
    monkeypatch.setattr(
        sys.modules[__name__],
        "terminate_process",
        mock.Mock(return_value=ProcessTerminationResult(tree_terminated=True, detail="stopped")),
    )
    with pytest.raises(ScenarioCleanupError), scenario.running():
        scenario.record("fixture-body-completed")
    evidence = json.loads((scenario.root / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"].startswith("FAILED")
    assert len(evidence["cleanup_failures"]) == 1
    assert all(phase["phase"] != "all-owned-workers-reaped" for phase in evidence["phases"])


def test_cleanup_publishes_pass_only_after_all_workers_are_confirmed_stopped(
    cleanup_scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = cleanup_scenario

    def terminate(_process: subprocess.Popen[str]) -> ProcessTerminationResult:
        evidence = json.loads((scenario.root / "evidence.json").read_text(encoding="utf-8"))
        assert evidence["status"] == "RUNNING"
        return ProcessTerminationResult(tree_terminated=True, detail="stopped")

    monkeypatch.setattr(sys.modules[__name__], "terminate_process", terminate)
    with scenario.running():
        scenario.record("fixture-body-completed")
    evidence = json.loads((scenario.root / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "PASS (scoped scenario only)"
    assert evidence["cleanup_failures"] == []
    assert evidence["phases"][-1]["phase"] == "all-owned-workers-reaped"
    for process in scenario.processes:
        process.wait.assert_called_once_with(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close.assert_called_once_with()


@pytest.mark.parametrize(
    ("tabs", "bells", "expected"),
    (
        ("worktree-beta [WAITING_FOR_INPUT] [INPUT!]", 0, False),
        ("worktree-beta [WAITING_FOR_INPUT] [INPUT!]", 1, False),
        ("worktree-alpha [WAITING_FOR_INPUT] [INPUT!]", 0, False),
        ("worktree-alpha [RUNNING] * | worktree-beta [WAITING_FOR_INPUT] [INPUT!]", 1, False),
        ("worktree-alpha [WAITING_FOR_INPUT] [INPUT!]", 1, True),
    ),
)
def test_background_attention_requires_alpha_tab_and_expected_bell(
    monkeypatch: pytest.MonkeyPatch, tabs: str, bells: int, expected: bool
) -> None:
    app = mock.Mock(spec=RecordingBellShell)
    app.bell_count = bells
    monkeypatch.setattr(sys.modules[__name__], "text", lambda _app, _widget: tabs)
    assert background_input_received(app, Path("worktree-alpha")) is expected
