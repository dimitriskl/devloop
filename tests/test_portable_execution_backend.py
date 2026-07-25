from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from devloop import codex_runner, statusui
from devloop.codex_events import parse_codex_event, render_safe_codex_activity
from devloop.issue_pack import Issue
from devloop.portable_execution_backend import (
    ExecutionBackend,
    ExecutionBackendId,
    RunWideBlocker,
    RunWideBlockerKind,
    StepActivityEvent,
    StepActivityKind,
    StepAttemptRequest,
    StepAttemptResult,
    codex_cli,
    registered_execution_backend_ids,
    sole_registered_execution_backend,
    update_checkpoint_for_step_activity,
)
from devloop.portable_workflow import (
    ExecutionBudget,
    FastPreference,
    StepExecutionSettings,
)
from devloop.statusui import Stage
from devloop.templates import BundleContext, Preset

TURN_COMPLETED_LINE = '{"type":"turn.completed","usage":{}}'
COMMAND_STARTED_LINE = (
    '{"type":"item.started","item":{"id":"command-1",'
    '"type":"command_execution","status":"in_progress"}}'
)
COMMAND_COMPLETED_LINE = (
    '{"type":"item.completed","item":{"id":"command-1",'
    '"type":"command_execution","status":"completed"}}'
)
WEB_SEARCH_STARTED_LINE = (
    '{"type":"item.started","item":{"id":"search-1","type":"web_search"}}'
)
WEB_SEARCH_COMPLETED_LINE = (
    '{"type":"item.completed","item":{"id":"search-1","type":"web_search"}}'
)
FILE_CHANGE_STARTED_LINE = (
    '{"type":"item.started","item":{"id":"change-1","type":"file_change"}}'
)
AGENT_MESSAGE_LINE = (
    '{"type":"item.completed","item":{"type":"agent_message",'
    '"text":"Inspecting the acceptance criteria."}}'
)
REASONING_STARTED_LINE = (
    '{"type":"item.started","item":{"type":"reasoning","text":"private"}}'
)
ERROR_LINE = '{"type":"error","message":"You have hit your usage limit"}'
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _activity(line: str):
    return codex_cli.codex_step_activity_event(parse_codex_event(line))


class _FakeStream(io.StringIO):
    def __init__(self, *, tty: bool = True) -> None:
        super().__init__()
        self._tty = tty

    @property
    def encoding(self) -> str:
        return "utf-8"

    def isatty(self) -> bool:
        return self._tty


class _FakeCodexProcess:
    """A `codex exec` stand-in that emits fixed stream lines and then exits."""

    def __init__(self, stdout_lines: tuple[str, ...]) -> None:
        self.stdin = io.StringIO()
        self.stdout = iter(stdout_lines)
        self.stderr: list[str] = []
        self.returncode: int | None = None

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class _FakeCheckpointBudget:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def pause_checkpoint(self) -> None:
        self.calls.append("pause")

    def resume_checkpoint(self) -> None:
        self.calls.append("resume")


class _RecordingExecutionBackend(ExecutionBackend):
    """A backend that records the request instead of reaching any provider."""

    def __init__(self, result: StepAttemptResult) -> None:
        self._result = result
        self.requests: list[StepAttemptRequest] = []

    @property
    def backend_id(self) -> ExecutionBackendId:
        return ExecutionBackendId.CODEX_CLI

    def invoke(self, request: StepAttemptRequest) -> StepAttemptResult:
        self.requests.append(request)
        return self._result

    def discover_model_catalog(self, *, cwd: Path):
        raise AssertionError("A step attempt must not discover a Model Catalog.")

    def authorize_execution_settings(self, authorizations, *, model_catalog) -> None:
        raise AssertionError("A step attempt must not run preflight authorization.")


class ExecutionBackendRegistryTests(unittest.TestCase):
    def test_exactly_one_execution_backend_is_registered(self) -> None:
        self.assertEqual(
            registered_execution_backend_ids(),
            (ExecutionBackendId.CODEX_CLI,),
        )

        backend = sole_registered_execution_backend()

        self.assertIsInstance(backend, ExecutionBackend)
        self.assertIs(backend.backend_id, ExecutionBackendId.CODEX_CLI)


class CodexInvocationTests(unittest.TestCase):
    def test_codex_agent_invocation_for_a_step_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            codex_cli,
            "uses_legacy_approval_flag",
            return_value=False,
        ):
            root = Path(raw)
            command = codex_cli.build_codex_exec_command(
                codex="codex",
                repo_root=root,
                sandbox="workspace-write",
                approval_policy="never",
                schema_path=root / "role-result.schema.json",
                message_path=root / "attempt.last-message.json",
                execution_settings=StepExecutionSettings(
                    ExecutionBackendId.CODEX_CLI,
                    "gpt-5.6-sol",
                    "xhigh",
                    FastPreference.OFF,
                ),
            )

            self.assertEqual(
                command,
                [
                    "codex",
                    "exec",
                    "-C",
                    str(root),
                    "-s",
                    "workspace-write",
                    "-m",
                    "gpt-5.6-sol",
                    "-c",
                    'model_reasoning_effort="xhigh"',
                    "-c",
                    'service_tier="default"',
                    "--disable",
                    "fast_mode",
                    "-c",
                    'approval_policy="never"',
                    "--output-schema",
                    str(root / "role-result.schema.json"),
                    "-o",
                    str(root / "attempt.last-message.json"),
                    "--json",
                    "-",
                ],
            )

    def test_fast_on_selects_the_fast_service_tier_and_legacy_approval_flag(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            codex_cli,
            "uses_legacy_approval_flag",
            return_value=True,
        ):
            root = Path(raw)
            command = codex_cli.build_codex_exec_command(
                codex="codex",
                repo_root=root,
                sandbox="read-only",
                approval_policy="on-request",
                schema_path=root / "schema.json",
                message_path=root / "message.json",
                execution_settings=StepExecutionSettings(
                    ExecutionBackendId.CODEX_CLI,
                    "gpt-5.6-luna",
                    "high",
                    FastPreference.ON,
                ),
            )

        self.assertEqual(
            command,
            [
                "codex",
                "exec",
                "-C",
                str(root),
                "-s",
                "read-only",
                "-m",
                "gpt-5.6-luna",
                "-c",
                'model_reasoning_effort="high"',
                "-c",
                'service_tier="fast"',
                "--enable",
                "fast_mode",
                "-a",
                "on-request",
                "--output-schema",
                str(root / "schema.json"),
                "-o",
                str(root / "message.json"),
                "--json",
                "-",
            ],
        )

    def test_invoke_returns_the_recovered_structured_message(self) -> None:
        final_message = json.dumps({"status": "PASS", "summary": "implemented"})
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": final_message},
                    }
                ),
                TURN_COMPLETED_LINE,
            ]
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            written: dict[Path, str] = {}

            def write_log(path: Path, text: str) -> None:
                written[path] = text
                path.write_text(text, encoding="utf-8")

            request = StepAttemptRequest(
                prompt="Implement the issue.",
                repo_root=root,
                schema_path=root / "schema.json",
                message_path=root / "attempt.last-message.json",
                stdout_path=root / "attempt.stdout.jsonl",
                stderr_path=root / "attempt.stderr.txt",
                write_log=write_log,
                execution_budget=ExecutionBudget(
                    timeout_seconds=60,
                    checkpoint_seconds=30,
                ),
                activity_stage=Stage.DEVELOPMENT,
            )
            with mock.patch.object(
                codex_cli,
                "build_codex_exec_command",
                return_value=["codex"],
            ), mock.patch.object(
                codex_cli,
                "run_codex_exec_with_connection_retries",
                return_value=CompletedProcess(["codex"], 0, stdout, ""),
            ):
                result = codex_cli.CodexCliExecutionBackend().invoke(request)

            self.assertEqual(result.message, final_message)
            self.assertIsNone(result.run_wide_blocker)
            self.assertEqual(result.refusals, ())
            self.assertEqual(written[request.message_path], final_message)

    def test_invoke_reports_a_run_wide_blocker_without_a_role_message(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = StepAttemptRequest(
                prompt="Implement the issue.",
                repo_root=root,
                schema_path=root / "schema.json",
                message_path=root / "attempt.last-message.json",
                stdout_path=root / "attempt.stdout.jsonl",
                stderr_path=root / "attempt.stderr.txt",
                write_log=lambda path, text: path.write_text(text, encoding="utf-8"),
            )
            with mock.patch.object(
                codex_cli,
                "build_codex_exec_command",
                return_value=["codex"],
            ), mock.patch.object(
                codex_cli,
                "run_codex_exec_with_connection_retries",
                return_value=CompletedProcess(["codex"], 1, f"{ERROR_LINE}\n", ""),
            ):
                result = codex_cli.CodexCliExecutionBackend().invoke(request)

            self.assertIsNotNone(result.run_wide_blocker)
            assert result.run_wide_blocker is not None
            self.assertIs(
                result.run_wide_blocker.kind,
                RunWideBlockerKind.USAGE_LIMIT,
            )
            self.assertEqual(result.message, "")
            self.assertFalse(request.message_path.exists())


class NeutralStepActivityTests(unittest.TestCase):
    def test_active_backend_operations_translate_to_tool_activity(self) -> None:
        started = _activity(COMMAND_STARTED_LINE)
        completed = _activity(COMMAND_COMPLETED_LINE)

        assert started is not None and completed is not None
        self.assertIs(started.kind, StepActivityKind.TOOL_STARTED)
        self.assertIs(completed.kind, StepActivityKind.TOOL_COMPLETED)
        self.assertEqual(started.tool_key, "command_execution:command-1")
        self.assertEqual(completed.tool_key, "command_execution:command-1")

    def test_remaining_codex_events_translate_to_their_neutral_kind(self) -> None:
        cases = (
            (AGENT_MESSAGE_LINE, StepActivityKind.MESSAGE),
            (REASONING_STARTED_LINE, StepActivityKind.REASONING),
            (TURN_COMPLETED_LINE, StepActivityKind.TURN_COMPLETED),
            (ERROR_LINE, StepActivityKind.ERROR),
            # A repository file change is not an active backend operation, so it
            # must not pause the inactivity checkpoint.
            (FILE_CHANGE_STARTED_LINE, StepActivityKind.MESSAGE),
        )
        for line, expected_kind in cases:
            with self.subTest(line=line):
                event = _activity(line)

                assert event is not None
                self.assertIs(event.kind, expected_kind)
                if expected_kind is not StepActivityKind.TURN_COMPLETED:
                    self.assertIsNone(event.tool_key)

    def test_neutral_activity_text_matches_the_safe_codex_rendering(self) -> None:
        lines = (
            AGENT_MESSAGE_LINE,
            REASONING_STARTED_LINE,
            COMMAND_STARTED_LINE,
            COMMAND_COMPLETED_LINE,
            FILE_CHANGE_STARTED_LINE,
            ERROR_LINE,
            TURN_COMPLETED_LINE,
            "not json at all",
        )
        for line in lines:
            with self.subTest(line=line):
                payload = parse_codex_event(line)
                event = codex_cli.codex_step_activity_event(payload)

                self.assertEqual(
                    event.activity if event is not None else None,
                    render_safe_codex_activity(payload),
                )

    def test_reasoning_activity_never_exposes_chain_of_thought(self) -> None:
        event = _activity(REASONING_STARTED_LINE)

        assert event is not None
        self.assertEqual(event.activity, "Codex is reasoning about the task.")


class InactivityCheckpointTests(unittest.TestCase):
    def test_nested_tool_activity_pauses_once_and_resumes_once(self) -> None:
        budget = _FakeCheckpointBudget()
        active_tools: set[str] = set()

        for line in (
            COMMAND_STARTED_LINE,
            WEB_SEARCH_STARTED_LINE,
            WEB_SEARCH_COMPLETED_LINE,
            COMMAND_COMPLETED_LINE,
        ):
            update_checkpoint_for_step_activity(budget, _activity(line), active_tools)

        self.assertEqual(budget.calls, ["pause", "resume"])
        self.assertEqual(active_tools, set())

    def test_non_tool_activity_never_touches_the_checkpoint(self) -> None:
        budget = _FakeCheckpointBudget()
        active_tools: set[str] = set()

        for line in (
            AGENT_MESSAGE_LINE,
            REASONING_STARTED_LINE,
            FILE_CHANGE_STARTED_LINE,
            ERROR_LINE,
            TURN_COMPLETED_LINE,
        ):
            update_checkpoint_for_step_activity(budget, _activity(line), active_tools)

        self.assertEqual(budget.calls, [])
        self.assertEqual(active_tools, set())

    def test_an_unmatched_completion_leaves_the_checkpoint_running(self) -> None:
        budget = _FakeCheckpointBudget()

        update_checkpoint_for_step_activity(
            budget,
            _activity(COMMAND_COMPLETED_LINE),
            set(),
        )

        self.assertEqual(budget.calls, [])


class ActivityFeedConsumptionTests(unittest.TestCase):
    def test_the_activity_feed_callback_receives_the_neutral_event(self) -> None:
        """The feed is handed events, not bare text, so it can read the kind."""
        events: list[StepActivityEvent | None] = []
        with mock.patch.object(
            codex_cli.subprocess,
            "Popen",
            return_value=_FakeCodexProcess(
                (
                    f"{AGENT_MESSAGE_LINE}\n",
                    f"{COMMAND_STARTED_LINE}\n",
                    f"{ERROR_LINE}\n",
                    f"{TURN_COMPLETED_LINE}\n",
                )
            ),
        ), redirect_stdout(io.StringIO()) as printed:
            result = codex_cli.run_streaming_codex_command(
                ["codex", "exec", "--json", "-"],
                input_text="Implement the issue.",
                cwd=Path(__file__).parent,
                stage=Stage.DEVELOPMENT,
                activity_callback=events.append,
            )

        self.assertEqual(result.returncode, 0)
        # A live feed replaces Portable Plain Mode printing, exactly as before.
        self.assertEqual(printed.getvalue(), "")
        self.assertEqual(
            [(event.kind, event.activity) for event in events if event is not None],
            [
                (
                    StepActivityKind.MESSAGE,
                    "Codex update: Inspecting the acceptance criteria.",
                ),
                (StepActivityKind.TOOL_STARTED, "Running a repository command."),
                (
                    StepActivityKind.ERROR,
                    "Codex reported an error: You have hit your usage limit",
                ),
                (StepActivityKind.TURN_COMPLETED, None),
            ],
        )

    def test_the_dashboard_activity_feed_renders_the_neutral_event(self) -> None:
        """The Portable Activity Feed accepts a kind Codex never reports."""
        output = _FakeStream()
        dashboard = statusui.IssueDashboard(
            issue_number="0004",
            issue_title="Fail honestly on denied tools",
            position=4,
            total=9,
            stream=output,
            frame_seconds=60,
            terminal_size=lambda **_: os.terminal_size((100, 24)),
        )

        dashboard.begin_role(Stage.DEVELOPMENT, 1)
        dashboard.notify_activity(
            StepActivityEvent(
                kind=StepActivityKind.PERMISSION_DENIED,
                activity="The backend refused a repository command.",
            )
        )
        # An event with nothing to display still counts as backend progress and
        # must never blank the feed, exactly as a bare ``None`` did before.
        dashboard.notify_activity(_activity(TURN_COMPLETED_LINE))
        dashboard.notify_activity(None)
        dashboard.close()

        rendered = ANSI_ESCAPE_PATTERN.sub("", output.getvalue())
        self.assertIn("The backend refused a repository command.", rendered)


class RunWideBlockerOwnershipTests(unittest.TestCase):
    def test_the_blocker_type_lives_in_a_backend_neutral_module(self) -> None:
        self.assertEqual(
            RunWideBlocker.__module__,
            "devloop.portable_execution_backend.blockers",
        )

    def test_codex_classification_stays_a_per_backend_function(self) -> None:
        cases = (
            (
                '{"type":"error","message":"You have hit your usage limit"}\n',
                RunWideBlockerKind.USAGE_LIMIT,
            ),
            (
                '{"type":"turn.failed","error":"unauthorized"}\n',
                RunWideBlockerKind.AUTHENTICATION,
            ),
            (
                '{"type":"turn.failed","error":"HTTP 503 service unavailable"}\n',
                RunWideBlockerKind.SERVICE_UNAVAILABLE,
            ),
        )
        for stdout, expected_kind in cases:
            with self.subTest(stdout=stdout):
                blocker = codex_cli.classify_run_wide_blocker(stdout, "")

                self.assertIsNotNone(blocker)
                assert blocker is not None
                self.assertIs(blocker.kind, expected_kind)

    def test_repository_command_failures_are_not_run_wide(self) -> None:
        stdout = f"{COMMAND_COMPLETED_LINE}\n"

        self.assertIsNone(codex_cli.classify_run_wide_blocker(stdout, ""))


def _role_runner(root: Path, backend: ExecutionBackend) -> codex_runner.CodexRunner:
    repository_root = Path(__file__).parents[1]
    runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
    runner.bundle = BundleContext(
        root=repository_root,
        prompts=repository_root / "prompts",
        schemas=repository_root / "schemas",
    )
    runner.repo_root = root
    runner.prd_path = root / "prd.md"
    runner.issues_index = root / "README.md"
    runner.preset = Preset(
        name="test",
        required_docs=[],
        roles={"coder": {"skills": [], "agents": []}},
    )
    runner.log_root = root / ".loop.logs"
    runner.use_self_improvement_wiki = False
    runner.execution_backend = backend
    runner.ensure_log_root()
    return runner


class RoleRunnerBackendDispatchTests(unittest.TestCase):
    def _runner(
        self,
        root: Path,
        backend: ExecutionBackend,
    ) -> codex_runner.CodexRunner:
        return _role_runner(root, backend)

    def test_run_role_reaches_the_provider_only_through_the_interface(self) -> None:
        final_message = json.dumps(
            {"status": "PASS", "summary": "Implemented the issue."}
        )
        backend = _RecordingExecutionBackend(
            StepAttemptResult(
                process=CompletedProcess(
                    ["backend"],
                    0,
                    "stdout-from-backend\n",
                    "stderr-from-backend\n",
                ),
                message=final_message,
            )
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Backend dispatch\n", encoding="utf-8")
            runner = self._runner(root, backend)
            budget = ExecutionBudget(timeout_seconds=120, checkpoint_seconds=60)
            settings = StepExecutionSettings(
                ExecutionBackendId.CODEX_CLI,
                "gpt-5.6-luna",
                "high",
                FastPreference.OFF,
            )

            result = runner.run_role(
                role="coder",
                issue=Issue("0001", "Backend dispatch", issue_path, False),
                pass_number=1,
                execution_settings=settings,
                execution_budget=budget,
                progress="1/2",
            )

            self.assertEqual(result.status, "PASS")
            self.assertEqual(result.summary, "Implemented the issue.")
            self.assertEqual(len(backend.requests), 1)
            request = backend.requests[0]
            self.assertIn("Implement the issue", request.prompt)
            self.assertEqual(request.repo_root, root)
            self.assertIs(request.execution_settings, settings)
            self.assertIs(request.execution_budget, budget)
            self.assertIs(request.activity_stage, Stage.DEVELOPMENT)
            self.assertEqual(request.activity_context, "1/2 p1")
            self.assertEqual(
                request.schema_path,
                runner.bundle.schemas / "role-result.schema.json",
            )
            for path in (
                request.stdout_path,
                request.stderr_path,
                request.message_path,
            ):
                self.assertEqual(path.parent, runner.log_root.resolve())
            self.assertEqual(
                request.stdout_path.read_text(encoding="utf-8"),
                "stdout-from-backend\n",
            )
            self.assertEqual(
                request.stderr_path.read_text(encoding="utf-8"),
                "stderr-from-backend\n",
            )

    def test_run_role_raises_the_backend_reported_run_wide_blocker(self) -> None:
        backend = _RecordingExecutionBackend(
            StepAttemptResult(
                process=CompletedProcess(["backend"], 1, "", ""),
                run_wide_blocker=RunWideBlocker(
                    kind=RunWideBlockerKind.USAGE_LIMIT,
                    summary="Codex usage is exhausted.",
                ),
            )
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Blocked run\n", encoding="utf-8")
            runner = self._runner(root, backend)

            with self.assertRaises(codex_runner.RunWideBlockerError) as raised:
                runner.run_role(
                    role="coder",
                    issue=Issue("0001", "Blocked run", issue_path, False),
                    pass_number=1,
                )

        self.assertIs(raised.exception.blocker.kind, RunWideBlockerKind.USAGE_LIMIT)


class RunWideBlockerPolicyTests(unittest.TestCase):
    """Each caller keeps its own answer to a Run-Wide Blocker at the interface."""

    STRUCTURED_MESSAGE = json.dumps(
        {"status": "PASS", "summary": "Recorded one durable lesson."}
    )
    # An unrelated provider diagnostic that merely matches a blocker pattern.
    BLOCKER_MATCHING_STDERR = (
        "warning: background telemetry probe reported rate limit exceeded\n"
    )

    def _completed_attempt(self) -> CompletedProcess[str]:
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": self.STRUCTURED_MESSAGE,
                        },
                    }
                ),
                TURN_COMPLETED_LINE,
                "",
            ]
        )
        return CompletedProcess(
            ["codex"],
            0,
            stdout,
            self.BLOCKER_MATCHING_STDERR,
        )

    def _runner(self, root: Path) -> codex_runner.CodexRunner:
        return _role_runner(root, codex_cli.CodexCliExecutionBackend())

    def test_self_improvement_compiler_recovers_its_message_despite_blocker_text(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = self._runner(root)
            wiki_root = root / "wiki"
            wiki_root.mkdir()

            with mock.patch.object(
                codex_cli,
                "build_codex_exec_command",
                return_value=["codex"],
            ), mock.patch.object(
                codex_cli,
                "run_codex_exec_with_connection_retries",
                return_value=self._completed_attempt(),
            ):
                result = runner.run_self_improvement_compiler(
                    state_path=root / "README.loop.state.json",
                    board_path=root / "README.loop.md",
                    wiki_root=wiki_root,
                    max_lessons=3,
                )

            self.assertEqual(result.status, "PASS")
            self.assertEqual(result.summary, "Recorded one durable lesson.")
            self.assertEqual(result.raw_message, self.STRUCTURED_MESSAGE)
            self.assertEqual(
                (runner.log_root / "self-improvement-compiler.last-message.json")
                .read_text(encoding="utf-8"),
                self.STRUCTURED_MESSAGE,
            )

    def test_role_attempt_still_blocks_and_recovers_no_message(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = self._runner(root)
            issue_path = root / "0001.md"
            issue_path.write_text("# Blocked run\n", encoding="utf-8")

            with mock.patch.object(
                codex_cli,
                "build_codex_exec_command",
                return_value=["codex"],
            ), mock.patch.object(
                codex_cli,
                "run_codex_exec_with_connection_retries",
                return_value=self._completed_attempt(),
            ), self.assertRaises(codex_runner.RunWideBlockerError) as raised:
                runner.run_role(
                    role="coder",
                    issue=Issue("0001", "Blocked run", issue_path, False),
                    pass_number=1,
                )

            self.assertIs(
                raised.exception.blocker.kind,
                RunWideBlockerKind.USAGE_LIMIT,
            )
            # `state.recover_role_passes` scans for these, so a blocked role
            # attempt must leave none behind.
            self.assertEqual(list(runner.log_root.glob("*.last-message.json")), [])


if __name__ == "__main__":
    unittest.main()
