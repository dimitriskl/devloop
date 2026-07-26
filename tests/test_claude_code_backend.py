"""Claude Code Backend behaviour, driven from recorded provider output.

Every test in this module reads a committed fixture captured from a real run of
the installed Claude CLI. Nothing here spawns a provider executable.
"""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from devloop import codex_runner
from devloop.codex_runner import RoleResult
from devloop.issue_pack import Issue
from devloop.portable_execution_backend import (
    ClaudeCodeExecutionBackend,
    CodexCliExecutionBackend,
    ExecutionBackend,
    ExecutionBackendId,
    StepActivityEvent,
    StepActivityKind,
    StepAttemptProvenance,
    StepAttemptRequest,
    StepAttemptResult,
    claude_code,
    resolve_execution_backend,
    update_checkpoint_for_step_activity,
)
from devloop.portable_workflow import (
    DEVELOPMENT_STEP_ID,
    ExecutionBudget,
    FastPreference,
    StepExecutionSettings,
    StepRuntimeState,
    StepRuntimeStatus,
    default_portable_component_catalog,
    default_portable_workflow,
)
from devloop.statusui import IssueDashboard, Stage, project_workflow_progress
from devloop.step_configuration import (
    MODEL_MISMATCH_LABEL,
    STEP_GUIDANCE_PRECEDENCE,
)
from devloop.subprocess_utils import EXECUTION_BUDGET_EXPIRY_RETURNCODE
from devloop.templates import BundleContext, Preset

REPOSITORY_ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "claude_code"
ROLE_RESULT_SCHEMA = REPOSITORY_ROOT / "schemas" / "role-result.schema.json"
ATTEMPT_SESSION_ID = "0c266e16-aa2a-468c-8906-00525782d4f7"
CLAUDE_SETTINGS = StepExecutionSettings(
    ExecutionBackendId.CLAUDE_CODE,
    "claude-sonnet-5",
    "high",
    FastPreference.OFF,
)
CODEX_SETTINGS = StepExecutionSettings(
    ExecutionBackendId.CODEX_CLI,
    "gpt-5.6-luna",
    "high",
    FastPreference.OFF,
)


def _fixture_text(name: str) -> str:
    return (FIXTURE_ROOT / name).read_text(encoding="utf-8")


def _fixture_events(name: str) -> tuple[dict[str, object], ...]:
    return tuple(
        payload
        for payload in (
            claude_code.parse_claude_event(line)
            for line in _fixture_text(name).splitlines()
        )
        if payload is not None
    )


def _fixture_json(name: str) -> dict[str, object]:
    return json.loads(_fixture_text(name))


def _session_init_event(name: str) -> dict[str, object]:
    for payload in _fixture_events(name):
        if payload.get("subtype") == claude_code.ClaudeSystemSubtype.INIT.value:
            return payload
    raise AssertionError(f"{name} carries no session-initialisation event.")


def _event_shape(payload: dict[str, object]) -> str:
    """A stable label for one recorded event shape, for mapping assertions."""
    event_type = payload.get("type")
    if event_type == claude_code.ClaudeEventType.SYSTEM.value:
        return f"system/{payload.get('subtype')}"
    if event_type in {
        claude_code.ClaudeEventType.ASSISTANT.value,
        claude_code.ClaudeEventType.USER.value,
    }:
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = content if isinstance(content, list) else []
        block_types = "+".join(
            str(block.get("type")) for block in blocks if isinstance(block, dict)
        )
        return f"{event_type}/{block_types}"
    return str(event_type)


class _FakeClaudeProcess:
    """A `claude -p` stand-in that emits recorded stream lines and then exits."""

    def __init__(self, stdout_lines: tuple[str, ...]) -> None:
        self.stdin = io.StringIO()
        self.stdout = iter(stdout_lines)
        self.stderr: tuple[str, ...] = ()
        self.returncode: int | None = None
        self.supplied_prompt = ""

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


class _PacedClaudeProcess:
    """A `claude -p` stand-in whose stream is paced, so a budget can really expire.

    The stream stays open after its recorded lines are exhausted and closes only
    once the process has been signalled, which is what lets a test observe the
    Execution Budget terminating a genuinely unfinished attempt.
    """

    IDLE_POLL_SECONDS = 0.02

    def __init__(
        self,
        stdout_lines: tuple[str, ...] = (),
        *,
        line_delay: float = 0.0,
    ) -> None:
        self.stdin = _CapturingStdin()
        self.stderr: tuple[str, ...] = ()
        self.returncode: int | None = None
        self.signals: list[str] = []
        self._stdout_lines = stdout_lines
        self._line_delay = line_delay
        self.stdout = self._paced_stream()

    def _paced_stream(self):
        for line in self._stdout_lines:
            if self.returncode is not None:
                return
            time.sleep(self._line_delay)
            yield line
        while self.returncode is None:
            time.sleep(self.IDLE_POLL_SECONDS)

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.signals.append("terminate")
        self.returncode = -15

    def kill(self) -> None:
        self.signals.append("kill")
        self.returncode = -9


class _CapturingStdin(io.StringIO):
    """Standard input that survives being closed, so a test can read it back."""

    def __init__(self) -> None:
        super().__init__()
        self.supplied = ""

    def close(self) -> None:
        self.supplied = self.getvalue()
        super().close()


class _FakeCheckpointBudget:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def pause_checkpoint(self) -> None:
        self.calls.append("pause")

    def resume_checkpoint(self) -> None:
        self.calls.append("resume")


class _FrozenClock:
    """A fixed attempt timestamp, so two rendered prompts are comparable."""

    @staticmethod
    def now() -> datetime:
        return datetime(2026, 7, 25, 12, 0, 0)


class _RecordingExecutionBackend(ExecutionBackend):
    """A backend that records the request instead of reaching any provider."""

    def __init__(self, backend_id: ExecutionBackendId, message: str) -> None:
        self._backend_id = backend_id
        self._message = message
        self.requests: list[StepAttemptRequest] = []

    @property
    def backend_id(self) -> ExecutionBackendId:
        return self._backend_id

    def invoke(self, request: StepAttemptRequest) -> StepAttemptResult:
        self.requests.append(request)
        return StepAttemptResult(
            process=CompletedProcess(["backend"], 0, "", ""),
            message=self._message,
        )

    def discover_model_catalog(self, *, cwd: Path):
        raise AssertionError("A step attempt must not discover a Model Catalog.")

    def authorize_execution_settings(
        self,
        authorizations,
        *,
        model_catalog,
        cwd: Path,
    ) -> None:
        raise AssertionError("A step attempt must not run preflight authorization.")


class _RefusingExecutionBackend(ExecutionBackend):
    """A backend that fails the test if a Workflow Step attempt reaches it."""

    def __init__(self, backend_id: ExecutionBackendId) -> None:
        self._backend_id = backend_id

    @property
    def backend_id(self) -> ExecutionBackendId:
        return self._backend_id

    def invoke(self, request: StepAttemptRequest) -> StepAttemptResult:
        raise AssertionError(
            f"The {self._backend_id.value} backend must not run this attempt."
        )

    def discover_model_catalog(self, *, cwd: Path):
        raise AssertionError("A step attempt must not discover a Model Catalog.")

    def authorize_execution_settings(
        self,
        authorizations,
        *,
        model_catalog,
        cwd: Path,
    ) -> None:
        raise AssertionError("A step attempt must not run preflight authorization.")


def _role_runner(root: Path, backend: ExecutionBackend) -> codex_runner.CodexRunner:
    runner = codex_runner.CodexRunner.__new__(codex_runner.CodexRunner)
    runner.bundle = BundleContext(
        root=REPOSITORY_ROOT,
        prompts=REPOSITORY_ROOT / "prompts",
        schemas=REPOSITORY_ROOT / "schemas",
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
    runner.dry_run = False
    runner.ensure_log_root()
    return runner


def _issue(root: Path, title: str = "Claude-backed attempt") -> Issue:
    issue_path = root / "0003.md"
    issue_path.write_text(f"# {title}\n", encoding="utf-8")
    return Issue("0003", title, issue_path, False)


class ClaudeInvocationTests(unittest.TestCase):
    def _command(self, settings: StepExecutionSettings | None = CLAUDE_SETTINGS):
        return claude_code.build_claude_command(
            "claude",
            schema_path=ROLE_RESULT_SCHEMA,
            session_id=ATTEMPT_SESSION_ID,
            execution_settings=settings,
        )

    def test_the_claude_invocation_is_the_decided_argument_list(self) -> None:
        self.assertEqual(
            self._command(),
            [
                "claude",
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--model",
                "claude-sonnet-5",
                "--effort",
                "high",
                "--json-schema",
                claude_code.claude_json_schema_argument(ROLE_RESULT_SCHEMA),
                "--permission-mode",
                "bypassPermissions",
                "--setting-sources",
                "project,local",
                "--strict-mcp-config",
                "--session-id",
                ATTEMPT_SESSION_ID,
            ],
        )

    def test_the_prompt_is_never_a_positional_argument(self) -> None:
        """A variadic option would silently consume a positional prompt."""
        prompt = "Implement the issue."
        command = self._command()

        self.assertNotIn(prompt, command)
        self.assertIn("-p", command)
        # Nothing trails the last option's value, so no argument can be adopted
        # as a prompt by the option before it.
        self.assertEqual(command[-2:], ["--session-id", ATTEMPT_SESSION_ID])

    def test_the_schema_is_supplied_without_its_draft_declaration_key(self) -> None:
        bundled = json.loads(ROLE_RESULT_SCHEMA.read_text(encoding="utf-8"))
        supplied = json.loads(claude_code.claude_json_schema_argument(ROLE_RESULT_SCHEMA))

        self.assertIn("$schema", bundled)
        self.assertNotIn("$schema", supplied)
        self.assertEqual(
            supplied,
            {key: value for key, value in bundled.items() if key != "$schema"},
        )
        # The bundled document is shared with the Codex CLI Backend and stays as
        # it is on disk.
        self.assertIn("$schema", ROLE_RESULT_SCHEMA.read_text(encoding="utf-8"))

    def test_an_unparsable_schema_is_refused_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            broken = Path(raw) / "role-result.schema.json"
            broken.write_text("not json", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                claude_code.claude_json_schema_argument(broken)

    def test_settings_naming_another_backend_cannot_become_a_claude_invocation(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "Codex CLI Backend"):
            self._command(CODEX_SETTINGS)

    def test_the_invocation_isolates_settings_sources_and_mcp_configuration(
        self,
    ) -> None:
        command = self._command()
        sources = command[command.index("--setting-sources") + 1].split(",")

        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(sources, ["project", "local"])
        self.assertNotIn(claude_code.ClaudeSettingSource.USER.value, sources)

    def test_the_target_repository_project_settings_still_load(self) -> None:
        """Isolation drops the operator, not the repository."""
        self.assertIn(
            claude_code.ClaudeSettingSource.PROJECT,
            claude_code.CLAUDE_SETTING_SOURCES,
        )
        self.assertIn(
            claude_code.ClaudeSettingSource.LOCAL,
            claude_code.CLAUDE_SETTING_SOURCES,
        )
        self.assertNotIn(
            claude_code.ClaudeSettingSource.USER,
            claude_code.CLAUDE_SETTING_SOURCES,
        )
        # The recorded isolated session still reports a project memory location,
        # so project instruction loading survives the isolation.
        self.assertIn("memory_paths", _session_init_event("isolated-stream.jsonl"))

    def test_permissions_are_bypassed_rather_than_merely_restricted(self) -> None:
        self.assertIs(
            claude_code.CLAUDE_PERMISSION_MODE,
            claude_code.ClaudePermissionMode.BYPASS_PERMISSIONS,
        )
        # Every less permissive mode the prototype tried denied a shell command
        # while reporting no error, a success subtype, a completed terminal
        # reason and a zero exit code.
        for fixture in (
            "permission-dontask.result.json",
            "permission-auto.result.json",
            "permission-acceptedits.result.json",
        ):
            with self.subTest(fixture=fixture):
                result = _fixture_json(fixture)

                self.assertFalse(result["is_error"])
                self.assertEqual(result["subtype"], "success")
                self.assertEqual(result["terminal_reason"], "completed")
                self.assertTrue(result["permission_denials"])

    def test_a_fresh_session_identity_is_a_uuid_per_attempt(self) -> None:
        first = claude_code.new_attempt_session_id()
        second = claude_code.new_attempt_session_id()

        self.assertNotEqual(first, second)
        for session_id in (first, second):
            with self.subTest(session_id=session_id):
                self.assertRegex(
                    session_id,
                    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
                    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
                )


class RecordedSettingsIsolationTests(unittest.TestCase):
    """The recorded session-initialisation events are the isolation evidence."""

    HOOK_SUBTYPES = frozenset(
        {
            claude_code.ClaudeSystemSubtype.HOOK_STARTED.value,
            claude_code.ClaudeSystemSubtype.HOOK_RESPONSE.value,
            claude_code.ClaudeSystemSubtype.HOOK_PROGRESS.value,
        }
    )

    def _hook_events(self, name: str) -> tuple[dict[str, object], ...]:
        return tuple(
            payload
            for payload in _fixture_events(name)
            if payload.get("subtype") in self.HOOK_SUBTYPES
        )

    def test_the_isolated_run_fired_no_hooks_and_loaded_no_plugins_or_mcp_servers(
        self,
    ) -> None:
        init = _session_init_event("isolated-stream.jsonl")

        self.assertEqual(self._hook_events("isolated-stream.jsonl"), ())
        self.assertEqual(init["plugins"], [])
        self.assertEqual(init["mcp_servers"], [])
        self.assertEqual(init["output_style"], "default")
        self.assertEqual(init["permissionMode"], "bypassPermissions")

    def test_without_the_isolation_the_operators_own_configuration_loads(self) -> None:
        """Why the flags are correctness rather than hygiene."""
        init = _session_init_event("bypass-stream.jsonl")

        self.assertNotEqual(self._hook_events("bypass-stream.jsonl"), ())
        self.assertNotEqual(init["plugins"], [])
        self.assertNotEqual(init["mcp_servers"], [])

    def test_a_short_alias_resolves_to_a_pinned_concrete_identifier(self) -> None:
        init = _session_init_event("alias-resolution-stream.jsonl")

        self.assertEqual(init["model"], "claude-haiku-4-5-20251001")


class RecordedStreamTranslationTests(unittest.TestCase):
    STREAM = "bypass-stream.jsonl"

    def _translated(
        self,
    ) -> tuple[tuple[dict[str, object], tuple[StepActivityEvent, ...]], ...]:
        return tuple(
            (payload, claude_code.claude_step_activity_events(payload))
            for payload in _fixture_events(self.STREAM)
        )

    def test_every_recorded_event_shape_maps_to_its_neutral_activity_kinds(
        self,
    ) -> None:
        # A set of pairs rather than a mapping, so one recorded shape translating
        # inconsistently across the stream fails instead of being overwritten.
        observed = {
            (_event_shape(payload), tuple(event.kind for event in events))
            for payload, events in self._translated()
        }

        expected = {
            # Hook events are deliberately ignored.
            "system/hook_started": (),
            "system/hook_response": (),
            "system/hook_progress": (),
            "system/init": (StepActivityKind.MESSAGE,),
            "system/thinking_tokens": (StepActivityKind.REASONING,),
            "assistant/thinking": (StepActivityKind.REASONING,),
            "assistant/tool_use": (StepActivityKind.TOOL_STARTED,),
            "assistant/text": (StepActivityKind.MESSAGE,),
            "user/tool_result": (StepActivityKind.TOOL_COMPLETED,),
            # An input the agent was handed, not progress it made.
            "user/text": (),
            "rate_limit_event": (StepActivityKind.RATE_LIMIT,),
            "result": (StepActivityKind.TURN_COMPLETED,),
        }

        self.assertEqual(observed, set(expected.items()))

    def test_a_multi_block_message_reports_every_block_in_order(self) -> None:
        payload = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash"},
                    {"type": "text", "text": "Ran the suite."},
                ]
            },
        }

        events = claude_code.claude_step_activity_events(payload)

        self.assertEqual(
            [event.kind for event in events],
            [
                StepActivityKind.REASONING,
                StepActivityKind.TOOL_STARTED,
                StepActivityKind.MESSAGE,
            ],
        )

    def test_reasoning_activity_never_exposes_chain_of_thought(self) -> None:
        thinking_texts: list[str] = []
        reasoning_activities: set[str | None] = set()
        for payload, events in self._translated():
            for block in _content_blocks_of(payload):
                if block.get("type") == "thinking":
                    thinking_texts.append(str(block.get("thinking")))
            for event in events:
                if event.kind is StepActivityKind.REASONING:
                    reasoning_activities.add(event.activity)

        self.assertTrue(thinking_texts)
        self.assertEqual(
            reasoning_activities,
            {"Claude is reasoning about the task.", None},
        )
        for text in thinking_texts:
            for activity in reasoning_activities:
                if activity:
                    self.assertNotIn(text, activity)

    def test_the_reasoning_heartbeat_reports_progress_without_display_text(
        self,
    ) -> None:
        """A dense heartbeat must not flood Portable Plain Mode."""
        heartbeats = tuple(
            events
            for payload, events in self._translated()
            if payload.get("subtype")
            == claude_code.ClaudeSystemSubtype.THINKING_TOKENS.value
        )

        self.assertTrue(heartbeats)
        for events in heartbeats:
            self.assertEqual(len(events), 1)
            self.assertIsNone(events[0].activity)

    def test_tool_activity_carries_the_recorded_tool_names(self) -> None:
        activities = [
            event.activity
            for _, events in self._translated()
            for event in events
            if event.kind is StepActivityKind.TOOL_STARTED
        ]

        self.assertEqual(
            activities,
            [
                "Using the Write tool.",
                "Using the Bash tool.",
                "Returning the structured role result.",
            ],
        )

    def test_the_session_initialisation_event_names_the_serving_model(self) -> None:
        events = claude_code.claude_step_activity_events(
            _session_init_event(self.STREAM)
        )

        self.assertEqual(
            [event.activity for event in events],
            ["Claude Code session started on model claude-haiku-4-5-20251001."],
        )

    def test_the_rate_limit_event_keeps_its_own_activity_kind(self) -> None:
        payload = next(
            payload
            for payload in _fixture_events(self.STREAM)
            if payload.get("type") == "rate_limit_event"
        )

        events = claude_code.claude_step_activity_events(payload)

        self.assertEqual(len(events), 1)
        self.assertIs(events[0].kind, StepActivityKind.RATE_LIMIT)
        self.assertEqual(
            events[0].activity,
            "Claude reported rate limit status allowed for the five_hour window.",
        )

    def test_a_failed_tool_result_is_reported_as_a_failure(self) -> None:
        payload = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "is_error": True,
                        "content": "command not found",
                    }
                ]
            },
        }

        events = claude_code.claude_step_activity_events(payload)

        self.assertEqual(
            [(event.kind, event.activity) for event in events],
            [(StepActivityKind.TOOL_COMPLETED, "A tool call failed.")],
        )

    def test_an_error_result_is_reported_as_an_error(self) -> None:
        payload = {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "The service is unavailable.",
        }

        events = claude_code.claude_step_activity_events(payload)

        self.assertEqual(
            [(event.kind, event.activity) for event in events],
            [
                (
                    StepActivityKind.ERROR,
                    "Claude reported an error: The service is unavailable.",
                )
            ],
        )

    def test_an_untranslated_event_reports_no_activity_instead_of_failing(self) -> None:
        self.assertEqual(
            claude_code.claude_step_activity_events({"type": "future_event"}),
            (),
        )
        self.assertEqual(claude_code.claude_step_activity_events(None), ())
        self.assertIsNone(claude_code.parse_claude_event("not json at all"))


def _content_blocks_of(payload: dict[str, object]) -> tuple[dict[str, object], ...]:
    message = payload.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    blocks = content if isinstance(content, list) else []
    return tuple(block for block in blocks if isinstance(block, dict))


class RecordedInactivityCheckpointTests(unittest.TestCase):
    def test_recorded_tool_activity_pauses_and_resumes_the_checkpoint_in_pairs(
        self,
    ) -> None:
        budget = _FakeCheckpointBudget()
        active_tools: set[str] = set()

        for payload in _fixture_events("bypass-stream.jsonl"):
            for event in claude_code.claude_step_activity_events(payload):
                update_checkpoint_for_step_activity(budget, event, active_tools)

        self.assertEqual(active_tools, set())
        self.assertEqual(budget.calls.count("pause"), budget.calls.count("resume"))
        self.assertTrue(budget.calls)
        self.assertEqual(budget.calls[0], "pause")
        self.assertEqual(budget.calls[-1], "resume")


class RoleResultRecoveryTests(unittest.TestCase):
    def _terminal_result(self, name: str = "bypass-stream.jsonl") -> dict[str, object]:
        terminal_result = claude_code.claude_terminal_result(_fixture_text(name))
        assert terminal_result is not None
        return terminal_result

    def test_the_role_result_comes_from_the_structured_output_field(self) -> None:
        terminal_result = self._terminal_result()

        message = claude_code.claude_role_message(terminal_result, stdout="")

        self.assertEqual(json.loads(message), terminal_result["structured_output"])
        result = RoleResult.from_message(message)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.changed_files, ["spike.txt"])
        self.assertEqual(result.verification_commands, ["git status --short"])

    def test_the_structured_output_field_wins_over_the_result_text(self) -> None:
        """The dedicated field is the source; the result text is only a fallback."""
        terminal_result = dict(self._terminal_result())
        terminal_result["result"] = json.dumps(
            {"status": "FAIL", "summary": "A stale copy of the role result."}
        )

        message = claude_code.claude_role_message(terminal_result, stdout="")

        self.assertEqual(json.loads(message), terminal_result["structured_output"])
        self.assertEqual(RoleResult.from_message(message).status, "PASS")

    def test_the_recovered_role_result_satisfies_the_shared_role_contract(self) -> None:
        terminal_result = _fixture_json("permission-bypass.result.json")
        claude_message = claude_code.claude_role_message(terminal_result, stdout="")
        # A Codex-backed attempt returns the same object as its last agent
        # message; the two must produce the same role result.
        codex_message = json.dumps(terminal_result["structured_output"])

        claude_result = RoleResult.from_message(claude_message)

        self.assertNotEqual(claude_message, codex_message)
        self.assertEqual(
            codex_runner.result_to_dict(claude_result),
            codex_runner.result_to_dict(RoleResult.from_message(codex_message)),
        )
        self.assertEqual(claude_result.status, "PASS")

    def test_the_role_result_falls_back_to_the_result_text(self) -> None:
        terminal_result = dict(self._terminal_result())
        terminal_result.pop("structured_output")

        message = claude_code.claude_role_message(terminal_result, stdout="")

        self.assertEqual(message, terminal_result["result"])
        self.assertEqual(RoleResult.from_message(message).status, "PASS")

    def test_the_result_text_wins_over_a_transcript_recovered_role_result(self) -> None:
        """The documented fallback order, not merely the individual fallbacks."""
        terminal = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps({"status": "PASS", "summary": "The final result."}),
        }
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "status": "FAIL",
                                            "summary": "An earlier draft.",
                                        }
                                    ),
                                }
                            ]
                        },
                    }
                ),
                json.dumps(terminal),
            ]
        )

        message = claude_code.claude_role_message(
            claude_code.claude_terminal_result(stdout),
            stdout=stdout,
        )

        self.assertEqual(
            RoleResult.from_message(message).summary,
            "The final result.",
        )

    def test_the_role_result_falls_back_to_lenient_extraction(self) -> None:
        embedded = json.dumps({"status": "FAIL", "summary": "Left work undone."})
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"Here is the result:\n```json\n{embedded}\n```",
                                }
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "Here is the result, described in prose.",
                    }
                ),
            ]
        )

        message = claude_code.claude_role_message(
            claude_code.claude_terminal_result(stdout),
            stdout=stdout,
        )

        self.assertEqual(RoleResult.from_message(message).status, "FAIL")
        self.assertEqual(
            RoleResult.from_message(message).summary,
            "Left work undone.",
        )

    def test_a_result_carrying_no_role_result_keeps_the_providers_own_words(
        self,
    ) -> None:
        terminal_result = _fixture_json("permission-dontask.result.json")

        message = claude_code.claude_role_message(terminal_result, stdout="")

        self.assertEqual(message, terminal_result["result"])
        self.assertIn("I need permission to run the Bash command", message)
        self.assertEqual(RoleResult.from_message(message).status, "BLOCKED")


class RecordedPermissionDenialTests(unittest.TestCase):
    def test_the_clean_terminal_result_records_no_denial(self) -> None:
        terminal_result = _fixture_json("permission-bypass.result.json")

        self.assertEqual(claude_code.claude_permission_denials(terminal_result), ())

    def test_recorded_denials_are_reported_as_refusal_records(self) -> None:
        for fixture in (
            "permission-dontask.result.json",
            "permission-auto.result.json",
            "permission-acceptedits.result.json",
        ):
            with self.subTest(fixture=fixture):
                refusals = claude_code.claude_permission_denials(
                    _fixture_json(fixture)
                )

                self.assertEqual([refusal.target for refusal in refusals], ["Bash"])

    def test_a_missing_or_malformed_denial_list_records_nothing(self) -> None:
        self.assertEqual(claude_code.claude_permission_denials(None), ())
        self.assertEqual(claude_code.claude_permission_denials({}), ())
        self.assertEqual(
            claude_code.claude_permission_denials({"permission_denials": "Bash"}),
            (),
        )


class ClaudeStreamingTests(unittest.TestCase):
    def _run(
        self,
        stdout_lines: tuple[str, ...],
        *,
        activity_callback=None,
    ):
        process = _FakeClaudeProcess(stdout_lines)
        process.stdin = _CapturingStdin()
        with mock.patch.object(
            claude_code.subprocess,
            "Popen",
            return_value=process,
        ) as popen:
            result = claude_code.run_streaming_claude_command(
                ["claude", "-p"],
                input_text="Implement the issue.",
                cwd=Path(__file__).parent,
                stage=Stage.DEVELOPMENT,
                activity_callback=activity_callback,
            )
        return result, process, popen

    def test_the_prompt_is_supplied_on_standard_input(self) -> None:
        lines = tuple(
            f"{line}\n" for line in _fixture_text("bypass-stream.jsonl").splitlines()
        )

        result, process, popen = self._run(lines, activity_callback=lambda _event: None)

        self.assertEqual(process.stdin.supplied, "Implement the issue.")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(popen.call_args.kwargs["stdin"], claude_code.subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs["cwd"], Path(__file__).parent)

    def test_the_activity_feed_receives_tool_activity_and_progress(self) -> None:
        events: list[StepActivityEvent | None] = []
        lines = tuple(
            f"{line}\n" for line in _fixture_text("bypass-stream.jsonl").splitlines()
        )

        self._run(lines, activity_callback=events.append)

        kinds = [event.kind for event in events if event is not None]
        self.assertIn(StepActivityKind.TOOL_STARTED, kinds)
        self.assertIn(StepActivityKind.TOOL_COMPLETED, kinds)
        self.assertIn(StepActivityKind.MESSAGE, kinds)
        self.assertIn(StepActivityKind.RATE_LIMIT, kinds)
        self.assertEqual(kinds[-1], StepActivityKind.TURN_COMPLETED)
        # Ignored hook events still register as progress so a long attempt with
        # nothing to show cannot look like a hang.
        self.assertTrue(any(event is None for event in events))

    def test_portable_plain_mode_prints_one_line_per_reportable_activity(self) -> None:
        """Without a live feed the same neutral activity becomes printed lines."""
        lines = tuple(
            f"{line}\n" for line in _fixture_text("bypass-stream.jsonl").splitlines()
        )

        with redirect_stdout(io.StringIO()) as printed:
            self._run(lines)

        rendered = printed.getvalue()
        for expected in (
            "[development] Claude Code session started on model",
            "[development] Claude is reasoning about the task.",
            "[development] Using the Write tool.",
            "[development] Using the Bash tool.",
            "[development] Tool call finished.",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, rendered)
        # The reasoning heartbeat is progress, not a printed line, so a dense
        # stream cannot bury the activity that matters.
        self.assertLessEqual(
            rendered.count("[development] Claude is reasoning about the task."),
            3,
        )

    def test_the_stream_stops_at_the_terminal_result(self) -> None:
        trailing = json.dumps({"type": "assistant", "message": {"content": []}})
        terminal = json.dumps(
            {"type": "result", "subtype": "success", "is_error": False}
        )
        lines = (f"{terminal}\n", f"{trailing}\n")

        result, _process, _popen = self._run(lines, activity_callback=lambda _e: None)

        self.assertNotIn(trailing, result.stdout)
        self.assertEqual(result.returncode, 0)

    def test_an_error_result_reports_a_failing_exit_status(self) -> None:
        terminal = json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
            }
        )

        result, _process, _popen = self._run(
            (f"{terminal}\n",),
            activity_callback=lambda _e: None,
        )

        self.assertEqual(result.returncode, 1)


class ClaudeBackendInvokeTests(unittest.TestCase):
    def _request(self, root: Path, written: dict[Path, str]) -> StepAttemptRequest:
        def write_log(path: Path, text: str) -> None:
            written[path] = text
            path.write_text(text, encoding="utf-8")

        return StepAttemptRequest(
            prompt="Implement the issue.",
            repo_root=root,
            schema_path=ROLE_RESULT_SCHEMA,
            message_path=root / "attempt.last-message.json",
            stdout_path=root / "attempt.stdout.jsonl",
            stderr_path=root / "attempt.stderr.txt",
            write_log=write_log,
            execution_settings=CLAUDE_SETTINGS,
            execution_budget=ExecutionBudget(
                timeout_seconds=60,
                checkpoint_seconds=30,
            ),
            activity_stage=Stage.DEVELOPMENT,
        )

    def _invoke(self, root: Path, process: CompletedProcess[str], written: dict):
        request = self._request(root, written)
        with mock.patch.object(
            claude_code,
            "run_streaming_claude_command",
            return_value=process,
        ) as streamed:
            result = ClaudeCodeExecutionBackend("claude").invoke(request)
        return request, result, streamed

    def test_invoke_returns_and_persists_the_recovered_role_result(self) -> None:
        stdout = _fixture_text("bypass-stream.jsonl")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            written: dict[Path, str] = {}

            request, result, streamed = self._invoke(
                root,
                CompletedProcess(["claude"], 0, stdout, ""),
                written,
            )

            self.assertEqual(RoleResult.from_message(result.message).status, "PASS")
            self.assertEqual(result.refusals, ())
            self.assertIsNone(result.run_wide_blocker)
            self.assertEqual(written[request.message_path], result.message)
            self.assertEqual(
                streamed.call_args.kwargs["input_text"],
                "Implement the issue.",
            )
            self.assertEqual(streamed.call_args.kwargs["cwd"], root)

    def test_invoke_runs_in_the_repository_root_the_runner_selected(self) -> None:
        stdout = _fixture_text("bypass-stream.jsonl")
        with tempfile.TemporaryDirectory() as raw:
            worktree = Path(raw) / "implementation-worktree"
            worktree.mkdir()
            written: dict[Path, str] = {}

            _request, _result, streamed = self._invoke(
                worktree,
                CompletedProcess(["claude"], 0, stdout, ""),
                written,
            )

            self.assertEqual(streamed.call_args.kwargs["cwd"], worktree)

    def test_invoke_recovers_no_role_message_from_a_failed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            written: dict[Path, str] = {}

            request, result, _streamed = self._invoke(
                root,
                CompletedProcess(["claude"], 1, "", "claude: command failed\n"),
                written,
            )

            self.assertEqual(result.message, "")
            self.assertFalse(request.message_path.exists())

    def test_invoke_records_the_terminal_results_permission_denials(self) -> None:
        stdout = _fixture_text("permission-dontask.result.json")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            written: dict[Path, str] = {}

            _request, result, _streamed = self._invoke(
                root,
                CompletedProcess(["claude"], 0, stdout, ""),
                written,
            )

            self.assertEqual([refusal.target for refusal in result.refusals], ["Bash"])

    def test_the_backend_reports_its_registered_identity(self) -> None:
        self.assertIs(
            ClaudeCodeExecutionBackend().backend_id,
            ExecutionBackendId.CLAUDE_CODE,
        )

    def test_browsing_the_bundled_catalog_still_costs_no_provider_call(self) -> None:
        """Discovery reads the bundle; only a selection or a run pays a call."""
        backend = ClaudeCodeExecutionBackend(
            session_factory=lambda _cwd: (_ for _ in ()).throw(
                AssertionError("Discovery must make no verification call.")
            ),
        )

        catalog = backend.discover_model_catalog(cwd=Path.cwd())

        self.assertIs(catalog.backend, ExecutionBackendId.CLAUDE_CODE)
        self.assertTrue(catalog.models)
        # A Workflow with no Claude-backed Workflow Steps reaches authorization
        # with nothing to authorize, and must still not call the provider.
        backend.authorize_execution_settings(
            (),
            model_catalog=catalog,
            cwd=Path.cwd(),
        )


class ClaudePermissionDenialActivityTests(unittest.TestCase):
    """A denial reaches the Portable Activity Feed live, not only the outcome."""

    DENIAL_FIXTURES = (
        "permission-dontask.result.json",
        "permission-auto.result.json",
        "permission-acceptedits.result.json",
    )

    def test_a_recorded_denial_emits_a_permission_denied_activity_event(self) -> None:
        for fixture in self.DENIAL_FIXTURES:
            with self.subTest(fixture=fixture):
                events = claude_code.claude_step_activity_events(
                    _fixture_json(fixture)
                )

                self.assertEqual(
                    [event.kind for event in events],
                    [
                        StepActivityKind.PERMISSION_DENIED,
                        StepActivityKind.TURN_COMPLETED,
                    ],
                )
                self.assertEqual(
                    events[0].activity,
                    "Claude was denied 1 tool call (Bash); "
                    "its result cannot be trusted.",
                )

    def test_the_denial_is_reported_before_how_the_turn_ended(self) -> None:
        """Ordering, so a denial is never buried behind the turn outcome."""
        events = claude_code.claude_step_activity_events(
            _fixture_json("permission-dontask.result.json")
        )

        kinds = [event.kind for event in events]
        self.assertLess(
            kinds.index(StepActivityKind.PERMISSION_DENIED),
            kinds.index(StepActivityKind.TURN_COMPLETED),
        )

    def test_the_clean_terminal_result_emits_no_permission_denied_activity(
        self,
    ) -> None:
        events = claude_code.claude_step_activity_events(
            _fixture_json("permission-bypass.result.json")
        )

        self.assertEqual(
            [event.kind for event in events],
            [StepActivityKind.TURN_COMPLETED],
        )

    def test_a_denial_reaches_the_activity_feed_while_the_attempt_streams(
        self,
    ) -> None:
        events: list[StepActivityEvent | None] = []
        process = _PacedClaudeProcess(
            (_fixture_text("permission-dontask.result.json"),)
        )
        with mock.patch.object(
            claude_code.subprocess,
            "Popen",
            return_value=process,
        ):
            claude_code.run_streaming_claude_command(
                ["claude", "-p"],
                input_text="Implement the issue.",
                cwd=Path(__file__).parent,
                stage=Stage.DEVELOPMENT,
                activity_callback=events.append,
            )

        self.assertIn(
            StepActivityKind.PERMISSION_DENIED,
            [event.kind for event in events if event is not None],
        )

    def test_portable_plain_mode_prints_the_denial(self) -> None:
        process = _PacedClaudeProcess(
            (_fixture_text("permission-dontask.result.json"),)
        )
        with mock.patch.object(
            claude_code.subprocess,
            "Popen",
            return_value=process,
        ), redirect_stdout(io.StringIO()) as printed:
            claude_code.run_streaming_claude_command(
                ["claude", "-p"],
                input_text="Implement the issue.",
                cwd=Path(__file__).parent,
                stage=Stage.DEVELOPMENT,
            )

        self.assertIn(
            "[development] Claude was denied 1 tool call (Bash);",
            printed.getvalue(),
        )


class ClaudeExecutionBudgetTests(unittest.TestCase):
    """The Execution Budget bounds a Claude attempt exactly as it bounds Codex."""

    HEARTBEAT_LINE_DELAY_SECONDS = 0.05
    HEARTBEAT_CHECKPOINT_SECONDS = 0.5
    EXPIRING_LIMIT_SECONDS = 0.4
    UNREACHABLE_LIMIT_SECONDS = 60.0

    def _run(
        self,
        process: _PacedClaudeProcess,
        *,
        timeout_seconds: float,
        checkpoint_seconds: float,
    ):
        with mock.patch.object(
            claude_code.subprocess,
            "Popen",
            return_value=process,
        ):
            return claude_code.run_streaming_claude_command(
                ["claude", "-p"],
                input_text="Implement the issue.",
                cwd=Path(__file__).parent,
                stage=Stage.DEVELOPMENT,
                activity_callback=lambda _event: None,
                execution_budget=ExecutionBudget(
                    timeout_seconds=timeout_seconds,
                    checkpoint_seconds=checkpoint_seconds,
                ),
            )

    def _heartbeat_lines(self) -> tuple[str, ...]:
        return tuple(
            f"{line}\n"
            for line in _fixture_text("bypass-stream.jsonl").splitlines()
            if claude_code.parse_claude_event(line) is not None
            and claude_code.parse_claude_event(line).get("subtype")
            == claude_code.ClaudeSystemSubtype.THINKING_TOKENS.value
        )

    def _expired_by_hard_timeout(self, process: _PacedClaudeProcess):
        """Run a busy attempt past its hard deadline.

        The attempt streams recorded activity throughout, so only the hard
        deadline can end it: this is also the assertion that backend activity
        does not push the hard timeout back.
        """
        return self._run(
            process,
            timeout_seconds=self.EXPIRING_LIMIT_SECONDS,
            checkpoint_seconds=self.EXPIRING_LIMIT_SECONDS,
        )

    def _busy_process(self) -> _PacedClaudeProcess:
        return _PacedClaudeProcess(
            self._heartbeat_lines(),
            line_delay=self.HEARTBEAT_LINE_DELAY_SECONDS,
        )

    def test_the_hard_timeout_terminates_the_attempt_and_reaps_its_process_tree(
        self,
    ) -> None:
        process = self._busy_process()

        result = self._expired_by_hard_timeout(process)

        self.assertTrue(process.signals)
        self.assertIsNotNone(process.poll())
        self.assertEqual(result.returncode, EXECUTION_BUDGET_EXPIRY_RETURNCODE)
        self.assertIn(
            "Execution Budget timeout (0.4 seconds) expired.",
            result.stderr,
        )

    def test_the_expiry_annotation_and_exit_status_match_the_codex_convention(
        self,
    ) -> None:
        """The same annotation the role runner already recognises, and 124."""
        result = self._expired_by_hard_timeout(self._busy_process())

        self.assertEqual(result.returncode, EXECUTION_BUDGET_EXPIRY_RETURNCODE)
        self.assertEqual(EXECUTION_BUDGET_EXPIRY_RETURNCODE, 124)
        self.assertIsNotNone(
            codex_runner.EXECUTION_BUDGET_EXPIRATION_PATTERN.search(result.stderr)
        )

    def test_an_attempt_reporting_no_backend_activity_expires_at_the_checkpoint(
        self,
    ) -> None:
        process = _PacedClaudeProcess()

        result = self._run(
            process,
            timeout_seconds=self.UNREACHABLE_LIMIT_SECONDS,
            checkpoint_seconds=self.EXPIRING_LIMIT_SECONDS,
        )

        self.assertTrue(process.signals)
        self.assertEqual(result.returncode, EXECUTION_BUDGET_EXPIRY_RETURNCODE)
        self.assertIn(
            "Execution Budget checkpoint deadline (0.4 seconds without backend "
            "activity) expired.",
            result.stderr,
        )

    def test_the_recorded_reasoning_heartbeat_is_not_mistaken_for_idleness(
        self,
    ) -> None:
        """Recorded thinking-token events alone must keep the attempt alive.

        The heartbeat carries no display text at all, so this is the case where a
        checkpoint reading only displayable activity would kill working work. The
        recorded events are paced so the attempt outlives its checkpoint window
        several times over while never pausing for a tool.
        """
        heartbeats = self._heartbeat_lines()
        terminal = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "terminal_reason": "completed",
                "result": json.dumps({"status": "PASS", "summary": "Done."}),
            }
        )
        process = _PacedClaudeProcess(
            heartbeats + (f"{terminal}\n",),
            line_delay=self.HEARTBEAT_LINE_DELAY_SECONDS,
        )

        self.assertGreater(
            len(heartbeats) * self.HEARTBEAT_LINE_DELAY_SECONDS,
            self.HEARTBEAT_CHECKPOINT_SECONDS,
        )

        result = self._run(
            process,
            timeout_seconds=self.UNREACHABLE_LIMIT_SECONDS,
            checkpoint_seconds=self.HEARTBEAT_CHECKPOINT_SECONDS,
        )

        self.assertEqual(result.returncode, 0)
        self.assertNotIn("Execution Budget", result.stderr)

    def test_an_attempt_without_an_execution_budget_is_never_terminated(
        self,
    ) -> None:
        lines = tuple(
            f"{line}\n" for line in _fixture_text("bypass-stream.jsonl").splitlines()
        )
        process = _PacedClaudeProcess(lines)

        with mock.patch.object(
            claude_code.subprocess,
            "Popen",
            return_value=process,
        ):
            result = claude_code.run_streaming_claude_command(
                ["claude", "-p"],
                input_text="Implement the issue.",
                cwd=Path(__file__).parent,
                stage=Stage.DEVELOPMENT,
                activity_callback=lambda _event: None,
            )

        self.assertEqual(result.returncode, 0)
        self.assertNotIn("Execution Budget", result.stderr)

    def test_invoke_supplies_the_requests_execution_budget_to_the_stream(
        self,
    ) -> None:
        budget = ExecutionBudget(timeout_seconds=1800, checkpoint_seconds=300)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def write_log(path: Path, text: str) -> None:
                path.write_text(text, encoding="utf-8")

            request = StepAttemptRequest(
                prompt="Implement the issue.",
                repo_root=root,
                schema_path=ROLE_RESULT_SCHEMA,
                message_path=root / "attempt.last-message.json",
                stdout_path=root / "attempt.stdout.jsonl",
                stderr_path=root / "attempt.stderr.txt",
                write_log=write_log,
                execution_settings=CLAUDE_SETTINGS,
                execution_budget=budget,
            )
            with mock.patch.object(
                claude_code,
                "run_streaming_claude_command",
                return_value=CompletedProcess(
                    ["claude"],
                    0,
                    _fixture_text("permission-bypass.result.json"),
                    "",
                ),
            ) as streamed:
                ClaudeCodeExecutionBackend("claude").invoke(request)

            self.assertIs(streamed.call_args.kwargs["execution_budget"], budget)


class ClaudeDurableEvidenceTests(unittest.TestCase):
    """What a Claude attempt leaves on disk, and what it deliberately does not."""

    def _reasoning_blocks(self, transcript: str) -> tuple[tuple[str, str], ...]:
        return tuple(
            (str(block.get("thinking")), str(block.get("signature")))
            for payload in (
                claude_code.parse_claude_event(line)
                for line in transcript.splitlines()
            )
            if payload is not None
            for block in _content_blocks_of(payload)
            if block.get("type") == "thinking"
        )

    def _recorded_reasoning(self) -> tuple[str, ...]:
        return tuple(
            reasoning
            for reasoning, _signature in self._reasoning_blocks(
                _fixture_text("bypass-stream.jsonl")
            )
        )

    def test_the_persisted_transcript_carries_no_verbatim_chain_of_thought(
        self,
    ) -> None:
        recorded = _fixture_text("bypass-stream.jsonl")

        redacted = claude_code.redact_claude_reasoning(recorded)

        recorded_blocks = self._reasoning_blocks(recorded)
        redacted_blocks = self._reasoning_blocks(redacted)
        self.assertTrue(recorded_blocks)
        self.assertEqual(len(redacted_blocks), len(recorded_blocks))
        for (reasoning, signature), (masked, masked_signature) in zip(
            recorded_blocks,
            redacted_blocks,
            strict=True,
        ):
            with self.subTest(characters=len(reasoning)):
                self.assertGreater(len(reasoning), 0)
                self.assertGreater(len(signature), 0)
                self.assertEqual(
                    masked,
                    f"[reasoning redacted: {len(reasoning)} characters]",
                )
                self.assertEqual(masked_signature, "[reasoning signature redacted]")
                # The recorded reasoning is gone from the file text as well as
                # from the parsed block, escaping included.
                self.assertNotIn(
                    json.dumps(reasoning, ensure_ascii=False)[1:-1],
                    redacted,
                )
        # The measurement this decision was taken on: one recorded attempt
        # persisted 1382 characters of verbatim chain of thought.
        self.assertEqual(
            sum(len(reasoning) for reasoning, _ in recorded_blocks),
            1382,
        )

    def test_the_redacted_transcript_keeps_every_event_and_stays_parseable(
        self,
    ) -> None:
        recorded = _fixture_text("bypass-stream.jsonl")

        redacted = claude_code.redact_claude_reasoning(recorded)

        self.assertEqual(
            [_event_shape(payload) for payload in _fixture_events("bypass-stream.jsonl")],
            [
                _event_shape(payload)
                for payload in (
                    claude_code.parse_claude_event(line)
                    for line in redacted.splitlines()
                )
                if payload is not None
            ],
        )
        self.assertEqual(
            claude_code.claude_terminal_result(redacted),
            claude_code.claude_terminal_result(recorded),
        )

    def test_a_transcript_without_reasoning_is_left_byte_for_byte_alone(self) -> None:
        recorded = _fixture_text("permission-bypass.result.json")

        self.assertEqual(claude_code.redact_claude_reasoning(recorded), recorded)

    def test_invoke_hands_back_the_redacted_transcript_for_persistence(self) -> None:
        stdout = _fixture_text("bypass-stream.jsonl")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def write_log(path: Path, text: str) -> None:
                path.write_text(text, encoding="utf-8")

            request = StepAttemptRequest(
                prompt="Implement the issue.",
                repo_root=root,
                schema_path=ROLE_RESULT_SCHEMA,
                message_path=root / "attempt.last-message.json",
                stdout_path=root / "attempt.stdout.jsonl",
                stderr_path=root / "attempt.stderr.txt",
                write_log=write_log,
                execution_settings=CLAUDE_SETTINGS,
            )
            with mock.patch.object(
                claude_code,
                "run_streaming_claude_command",
                return_value=CompletedProcess(["claude"], 0, stdout, ""),
            ):
                result = ClaudeCodeExecutionBackend("claude").invoke(request)

            for text in self._recorded_reasoning():
                self.assertNotIn(text, result.process.stdout)
            # The role result is still recovered from the recording as produced.
            self.assertEqual(RoleResult.from_message(result.message).status, "PASS")

    def test_the_durable_logs_of_both_backends_are_redacted_on_one_boundary(
        self,
    ) -> None:
        """The Redaction Service is the runner's log writer, not a backend habit."""
        leaked = (
            '{"type":"assistant","message":{"content":[{"type":"text",'
            '"text":"ran with GITHUB_TOKEN=ghp_0123456789abcdefghijABCDEFGHIJ and '
            'api_key: sk-abcdefghijklmnop"}]}}'
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = _role_runner(root, CodexCliExecutionBackend())
            written = runner.log_root / "leaked.stdout.jsonl"

            runner.write_log_text(written, leaked)

            persisted = written.read_text(encoding="utf-8")
            self.assertNotIn("ghp_0123456789abcdefghijABCDEFGHIJ", persisted)
            self.assertNotIn("sk-abcdefghijklmnop", persisted)
            self.assertIn("[redacted", persisted)
            # Masking a value must not swallow the surrounding record.
            self.assertIsNotNone(claude_code.parse_claude_event(persisted))

    def test_redaction_leaves_the_recorded_token_accounting_intact(self) -> None:
        """Cost and turn evidence survives; only detected secrets are masked."""
        recorded = _fixture_text("permission-bypass.result.json")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = _role_runner(root, ClaudeCodeExecutionBackend("claude"))
            written = runner.log_root / "attempt.stdout.jsonl"

            runner.write_log_text(written, recorded)

            persisted = json.loads(written.read_text(encoding="utf-8"))
            self.assertEqual(persisted, json.loads(recorded))

    def test_a_persisted_denial_names_only_the_redacted_tool(self) -> None:
        """The denied tool input stays in the transcript, not in the record."""
        denial = json.loads(_fixture_text("permission-auto.result.json"))
        command = denial["permission_denials"][0]["tool_input"]["command"]

        refusals = claude_code.claude_permission_denials(denial)

        self.assertEqual([refusal.target for refusal in refusals], ["Bash"])
        for refusal in refusals:
            self.assertNotIn(command, refusal.target)
            self.assertEqual(refusal.reason, "")


def _recorded_tool_use(command: str) -> str:
    """One durable JSONL record of a Bash tool call, as the stream writes it."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ]
            },
        },
        separators=(",", ":"),
    )


def _recorded_tool_result(content: str) -> str:
    """One durable JSONL record of a tool result, as the stream writes it."""
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "content": content}]
            },
        },
        separators=(",", ":"),
    )


class PersistedEvidenceRedactionInvariantTests(unittest.TestCase):
    """Two invariants of the Redaction Service, asserted together.

    A detected secret must not survive redaction, and a durable record that
    parsed as JSON before redaction must still parse afterwards. Both matter at
    once: these logs are what an operator reads to diagnose a failed attempt and
    what the run reviewer and role-pass recovery parse, so masking a secret by
    corrupting the record around it silently degrades both. Every case is asserted
    through the runner's log writer, which is the boundary both Execution Backends
    persist through.
    """

    def _persisted(self, text: str, name: str = "attempt.stdout.jsonl") -> str:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = _role_runner(root, ClaudeCodeExecutionBackend("claude"))
            written = runner.log_root / name

            runner.write_log_text(written, text)

            return written.read_text(encoding="utf-8")

    def _assert_masked_and_parseable(self, recorded: str, *secrets: str) -> None:
        persisted = self._persisted(recorded)
        for secret in secrets:
            self.assertNotIn(secret, persisted)
        self.assertIn("[redacted", persisted)
        for line in persisted.splitlines():
            if line.strip():
                json.loads(line)

    def test_a_secret_quoted_inside_a_recorded_command_survives_neither_way(
        self,
    ) -> None:
        """The reported corruption: an escaped quote is a value boundary too."""
        recorded = _recorded_tool_use(
            'export API_KEY="sk-abcdef123456789" && ./deploy.sh'
        )
        self.assertIn("sk-abcdef123456789", json.dumps(json.loads(recorded)))

        persisted = self._persisted(recorded)

        self.assertNotIn("sk-abcdef123456789", persisted)
        # The record still parses, and everything around the masked value is
        # exactly what the provider recorded.
        self.assertEqual(
            json.loads(persisted)["message"]["content"][0]["input"]["command"],
            'export API_KEY="[redacted]" && ./deploy.sh',
        )

    def test_every_recorded_secret_shape_is_masked_without_breaking_its_record(
        self,
    ) -> None:
        shapes = {
            "escaped-quote JSON": (
                _recorded_tool_use(
                    'export API_KEY="sk-abcdef123456789" && ./deploy.sh'
                ),
                ("sk-abcdef123456789",),
            ),
            "JSON inside JSON": (
                _recorded_tool_result(
                    json.dumps({"api_key": 'sk-"quoted', "password": "hunter2"})
                ),
                ('sk-"quoted', "hunter2"),
            ),
            "dotenv content read back": (
                _recorded_tool_result(
                    "API_KEY=sk-abcdef123456789\n"
                    "DB_PASSWORD=hunter2\n"
                    'GITHUB_TOKEN="ghp_0123456789abcdefghijABCDEFGHIJ"\n'
                ),
                (
                    "sk-abcdef123456789",
                    "hunter2",
                    "ghp_0123456789abcdefghijABCDEFGHIJ",
                ),
            ),
            "single-quoted shell value": (
                _recorded_tool_use("export DB_PASSWORD='hunter2' && ./run.sh"),
                ("hunter2",),
            ),
            "secret last on the line": (
                _recorded_tool_use('export API_KEY="hunter2"'),
                ("hunter2",),
            ),
            "secret-named field holding a structure": (
                json.dumps(
                    {
                        "type": "result",
                        "credentials": {"user": "svc", "value": ["hunter2"]},
                    },
                    separators=(",", ":"),
                ),
                ("hunter2",),
            ),
        }
        for shape, (recorded, secrets) in shapes.items():
            with self.subTest(shape=shape):
                json.loads(recorded)
                self._assert_masked_and_parseable(recorded, *secrets)

    def test_a_stream_keeps_every_record_parseable_when_one_carries_a_secret(
        self,
    ) -> None:
        recorded = (
            _fixture_text("bypass-stream.jsonl").rstrip("\n")
            + "\n"
            + _recorded_tool_use('export API_KEY="sk-abcdef123456789"')
            + "\n"
        )

        persisted = self._persisted(recorded)

        self.assertNotIn("sk-abcdef123456789", persisted)
        self.assertEqual(
            [_event_shape(payload) for payload in _fixture_events("bypass-stream.jsonl")]
            + ["assistant/tool_use"],
            [
                _event_shape(payload)
                for payload in (
                    claude_code.parse_claude_event(line)
                    for line in persisted.splitlines()
                )
                if payload is not None
            ],
        )

    def test_a_masked_diagnostic_keeps_the_escaping_it_was_written_with(
        self,
    ) -> None:
        """A truncated fragment is not JSON, so it is scanned as the text it is."""
        recorded = 'claude: failed while running API_KEY=\\"hunter2\\" && exit\n'

        persisted = self._persisted(recorded, name="attempt.stderr.txt")

        self.assertEqual(
            persisted,
            'claude: failed while running API_KEY=\\"[redacted]\\" && exit\n',
        )

    def test_a_private_key_spanning_plain_lines_is_still_recognised_whole(
        self,
    ) -> None:
        recorded = (
            "claude: the session failed\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIBOgIBAAJBAJHc\n"
            "-----END RSA PRIVATE KEY-----\n"
            "exiting\n"
        )

        persisted = self._persisted(recorded, name="attempt.stderr.txt")

        self.assertNotIn("MIIBOgIBAAJBAJHc", persisted)
        self.assertEqual(
            persisted,
            "claude: the session failed\n[redacted-private-key]\nexiting\n",
        )

    def test_a_record_with_nothing_to_mask_is_persisted_byte_for_byte(self) -> None:
        """Redaction rewrites only a record that actually carried a secret."""
        recorded = _fixture_text("bypass-stream.jsonl")

        self.assertEqual(self._persisted(recorded), recorded)

    def test_a_whole_attempt_persists_neither_stream_with_its_secrets_intact(
        self,
    ) -> None:
        """Both durable streams of one real attempt, asserted end to end.

        The attempt's own stderr is the case worth pinning down: it never passes
        through any backend-side rewriting, so the only thing masking it is the
        runner's log writer. Driving a whole attempt proves the writer is actually
        on that path rather than only reachable directly.
        """
        secret = "ghp_0123456789abcdefghijABCDEFGHIJ"
        stdout = (
            _recorded_tool_use(f'export GITHUB_TOKEN="{secret}" && ./deploy.sh')
            + "\n"
            + _fixture_text("permission-bypass.result.json")
        )
        stderr = f"claude: refused with GITHUB_TOKEN={secret}\n"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = _role_runner(root, ClaudeCodeExecutionBackend("claude"))
            with mock.patch.object(
                claude_code,
                "run_streaming_claude_command",
                return_value=CompletedProcess(["claude"], 0, stdout, stderr),
            ):
                runner.run_role(
                    role="coder",
                    issue=_issue(root),
                    pass_number=1,
                    execution_settings=CLAUDE_SETTINGS,
                )

            persisted = {
                path.suffixes[-2]: path.read_text(encoding="utf-8")
                for path in runner.log_root.iterdir()
            }
            self.assertIn(".stderr", persisted)
            self.assertIn(".stdout", persisted)
            for suffix, text in persisted.items():
                with self.subTest(log=suffix):
                    self.assertNotIn(secret, text)
            self.assertEqual(
                persisted[".stderr"],
                "claude: refused with GITHUB_TOKEN=[redacted]\n",
            )


class ClaudeBackendRegistryTests(unittest.TestCase):
    def test_the_claude_backend_is_registered_and_resolvable(self) -> None:
        backend = resolve_execution_backend(ExecutionBackendId.CLAUDE_CODE)

        self.assertIsInstance(backend, ClaudeCodeExecutionBackend)
        self.assertIs(backend.backend_id, ExecutionBackendId.CLAUDE_CODE)

    def test_resolving_the_claude_backend_resolves_its_executable(self) -> None:
        with mock.patch.object(
            claude_code,
            "resolve_claude_executable",
            return_value="/resolved/claude",
        ) as resolver:
            backend = resolve_execution_backend(ExecutionBackendId.CLAUDE_CODE)

        resolver.assert_called_once_with(claude_code.CLAUDE_CLI_COMMAND)
        self.assertEqual(backend.claude, "/resolved/claude")

    def test_an_unresolvable_claude_command_still_names_what_was_asked_for(
        self,
    ) -> None:
        with mock.patch.object(claude_code.shutil, "which", return_value=None):
            self.assertEqual(
                claude_code.resolve_claude_executable("definitely-not-installed"),
                "definitely-not-installed",
            )


class RunnerBackendDispatchTests(unittest.TestCase):
    """The runner dispatches each Workflow Step attempt to its own backend."""

    def _claude_attempt(self, runner: codex_runner.CodexRunner, issue: Issue):
        stdout = _fixture_text("bypass-stream.jsonl")
        with mock.patch.object(
            claude_code,
            "run_streaming_claude_command",
            return_value=CompletedProcess(["claude"], 0, stdout, ""),
        ) as streamed, mock.patch.object(
            claude_code,
            "resolve_claude_executable",
            return_value="claude",
        ):
            result = runner.run_role(
                role="coder",
                issue=issue,
                pass_number=1,
                execution_settings=CLAUDE_SETTINGS,
                execution_budget=ExecutionBudget(
                    timeout_seconds=120,
                    checkpoint_seconds=60,
                ),
                progress="1/2",
            )
        return result, streamed

    def test_a_claude_backed_step_never_reaches_the_configured_codex_backend(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = _role_runner(
                root,
                _RefusingExecutionBackend(ExecutionBackendId.CODEX_CLI),
            )

            result, streamed = self._claude_attempt(runner, _issue(root))

            self.assertEqual(result.status, "PASS")
            self.assertEqual(result.changed_files, ["spike.txt"])
            command = streamed.call_args.args[0]
            self.assertEqual(command[:2], ["claude", "-p"])
            self.assertEqual(streamed.call_args.kwargs["cwd"], root)

    def test_a_codex_backed_step_keeps_the_backend_the_run_was_started_with(
        self,
    ) -> None:
        configured = CodexCliExecutionBackend(codex="configured-codex")
        with tempfile.TemporaryDirectory() as raw:
            runner = _role_runner(Path(raw), configured)

            self.assertIs(runner.backend_for_step(CODEX_SETTINGS), configured)
            self.assertIs(runner.backend_for_step(None), configured)

    def test_an_all_codex_attempt_never_resolves_the_claude_executable(self) -> None:
        message = json.dumps({"status": "PASS", "summary": "Implemented the issue."})
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": message},
                    }
                ),
                '{"type":"turn.completed","usage":{}}',
            ]
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = _role_runner(root, CodexCliExecutionBackend())
            issue = _issue(root, "Codex-backed attempt")

            with mock.patch.object(
                claude_code,
                "resolve_claude_executable",
                side_effect=AssertionError("The Claude executable must not resolve."),
            ), mock.patch.object(
                claude_code.subprocess,
                "Popen",
                side_effect=AssertionError("The Claude CLI must not be spawned."),
            ), mock.patch(
                "devloop.portable_execution_backend.codex_cli"
                ".build_codex_exec_command",
                return_value=["codex"],
            ), mock.patch(
                "devloop.portable_execution_backend.codex_cli"
                ".run_codex_exec_with_connection_retries",
                return_value=CompletedProcess(["codex"], 0, stdout, ""),
            ):
                result = runner.run_role(
                    role="coder",
                    issue=issue,
                    pass_number=1,
                    execution_settings=CODEX_SETTINGS,
                )

            self.assertEqual(result.status, "PASS")

    def test_a_claude_backed_attempt_receives_the_codex_attempts_prompt(self) -> None:
        """The Context Manifest is the runner's, not the backend's."""
        message = json.dumps({"status": "PASS", "summary": "Implemented the issue."})
        codex_backend = _RecordingExecutionBackend(
            ExecutionBackendId.CODEX_CLI,
            message,
        )
        attempt = dict(
            role="coder",
            pass_number=1,
            fix_list=["Address the review finding."],
            step_instance_id="8e2c1c5a-1b0f-4f0a-9c5e-2c9c1d2f3a4b",
            step_display_name="Development",
            step_attempt_id="attempt-1",
            prompt_session_id="session-1",
            skill_paths=("skills/codex/tdd/SKILL.md",),
            agent_paths=("agents/codex/senior-code-reviewer.md",),
            step_guidance="Keep the change small.",
            execution_budget=ExecutionBudget(
                timeout_seconds=120,
                checkpoint_seconds=60,
            ),
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = _role_runner(root, codex_backend)
            issue = _issue(root)

            with mock.patch.object(codex_runner, "datetime", _FrozenClock):
                runner.run_role(
                    issue=issue,
                    execution_settings=CODEX_SETTINGS,
                    **attempt,
                )
                with mock.patch.object(
                    claude_code,
                    "run_streaming_claude_command",
                    return_value=CompletedProcess(
                        ["claude"],
                        0,
                        _fixture_text("permission-bypass.result.json"),
                        "",
                    ),
                ) as streamed, mock.patch.object(
                    claude_code,
                    "resolve_claude_executable",
                    return_value="claude",
                ):
                    runner.run_role(
                        issue=issue,
                        execution_settings=CLAUDE_SETTINGS,
                        **attempt,
                    )

            codex_prompt = codex_backend.requests[0].prompt
            claude_prompt = streamed.call_args.kwargs["input_text"]
            self.assertEqual(len(codex_backend.requests), 1)
            self.assertEqual(claude_prompt, codex_prompt)
            for expected in (
                "0003",
                "Keep the change small.",
                "Precedence:",
                "Address the review finding.",
                "skills/codex/tdd/SKILL.md",
                "agents/codex/senior-code-reviewer.md",
                "Hard timeout: 120 seconds",
                "Inactivity checkpoint: 60 seconds",
            ):
                with self.subTest(expected=expected):
                    self.assertIn(expected, claude_prompt)

    def test_no_role_prompt_template_instructs_a_step_as_though_it_were_codex(
        self,
    ) -> None:
        """The templates and the precedence statement are backend-neutral."""
        for template in ("coder.md", "reviewer.md", "qa.md"):
            with self.subTest(template=template):
                rendered = (REPOSITORY_ROOT / "prompts" / template).read_text(
                    encoding="utf-8"
                )

                self.assertNotIn("Codex", rendered)
                self.assertNotIn("Claude", rendered)

        self.assertNotIn("Codex", STEP_GUIDANCE_PRECEDENCE)
        self.assertIn("Step Execution Settings", STEP_GUIDANCE_PRECEDENCE)

    def test_a_dry_run_renders_the_prompt_and_invokes_no_provider(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = _role_runner(
                root,
                _RefusingExecutionBackend(ExecutionBackendId.CODEX_CLI),
            )
            issue = _issue(root)

            with mock.patch.object(
                claude_code.subprocess,
                "Popen",
                side_effect=AssertionError("A dry run must not spawn a provider."),
            ), mock.patch.object(
                claude_code,
                "resolve_claude_executable",
                side_effect=AssertionError("A dry run must not resolve a provider."),
            ), mock.patch.object(
                codex_runner,
                "resolve_execution_backend",
                side_effect=AssertionError("A dry run must not resolve a backend."),
            ), redirect_stdout(io.StringIO()):
                runner.render_dry_run_prompts(
                    issue,
                    (
                        (
                            "coder",
                            "coder",
                            "Development",
                            "8e2c1c5a-1b0f-4f0a-9c5e-2c9c1d2f3a4b",
                            (),
                            (),
                            "Keep the change small.",
                            ExecutionBudget(
                                timeout_seconds=120,
                                checkpoint_seconds=60,
                            ),
                        ),
                    ),
                )

            rendered = sorted(runner.log_root.glob("*-dry-run.prompt.md"))
            self.assertEqual(len(rendered), 1)
            self.assertIn(
                "Keep the change small.",
                rendered[0].read_text(encoding="utf-8"),
            )


class ClaudeOutcomePrecedenceTests(unittest.TestCase):
    """Step Outcome precedence for a Claude attempt, asserted as an order.

    Every case is driven from a recorded terminal-result envelope. Where a case
    needs two signals to disagree, the recording is copied and only the disputed
    fields are changed, so what is being asserted is which signal wins rather
    than any hand-written wire format.
    """

    DENIAL_FIXTURE = "permission-dontask.result.json"

    def _envelope(self, **overrides: object) -> str:
        recorded = _fixture_json(self.DENIAL_FIXTURE)
        return json.dumps({**recorded, **overrides})

    def _role_result(
        self,
        *,
        stdout: str,
        returncode: int = 0,
        stderr: str = "",
    ) -> RoleResult:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = _role_runner(
                root,
                _RefusingExecutionBackend(ExecutionBackendId.CODEX_CLI),
            )
            with mock.patch.object(
                claude_code,
                "run_streaming_claude_command",
                return_value=CompletedProcess(["claude"], returncode, stdout, stderr),
            ), mock.patch.object(
                claude_code,
                "resolve_claude_executable",
                return_value="claude",
            ):
                return runner.run_role(
                    role="coder",
                    issue=_issue(root),
                    pass_number=1,
                    execution_settings=CLAUDE_SETTINGS,
                    execution_budget=ExecutionBudget(
                        timeout_seconds=1800,
                        checkpoint_seconds=300,
                    ),
                )

    def test_a_denial_bearing_success_envelope_yields_blocked(self) -> None:
        """The recorded prototype finding: success everywhere, denied underneath."""
        recorded = _fixture_json(self.DENIAL_FIXTURE)
        self.assertFalse(recorded["is_error"])
        self.assertEqual(recorded["subtype"], "success")
        self.assertEqual(recorded["terminal_reason"], "completed")

        result = self._role_result(stdout=_fixture_text(self.DENIAL_FIXTURE))

        self.assertEqual(result.status, "BLOCKED")

    def test_the_blocked_summary_names_the_denied_tools_their_count_and_the_log(
        self,
    ) -> None:
        result = self._role_result(stdout=_fixture_text(self.DENIAL_FIXTURE))

        self.assertIn("1 tool call (Bash)", result.summary)
        self.assertIn("The Claude Code Backend was denied", result.summary)
        self.assertRegex(result.summary, r"See .*\.stdout\.jsonl\.$")
        self.assertIn(
            "Changes already written remain in the workspace",
            result.summary,
        )

    def test_the_summary_counts_every_denial_and_names_each_denied_tool(self) -> None:
        recorded = _fixture_json(self.DENIAL_FIXTURE)
        denial = recorded["permission_denials"][0]
        stdout = self._envelope(
            permission_denials=[denial, denial, {**denial, "tool_name": "Read"}]
        )

        result = self._role_result(stdout=stdout)

        self.assertIn("3 tool calls (Bash, Read)", result.summary)

    def test_the_denial_check_outranks_the_error_flag_reason_and_exit_code(
        self,
    ) -> None:
        """One envelope, every other signal saying failure differently."""
        stdout = self._envelope(
            is_error=True,
            subtype="error_during_execution",
            terminal_reason="error",
            result="The provider's own error text.",
        )

        result = self._role_result(
            stdout=stdout,
            returncode=1,
            stderr="claude: the session failed\n",
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("1 tool call (Bash)", result.summary)
        self.assertNotIn("The provider's own error text.", result.summary)
        self.assertNotIn("exit code", result.summary)

    def test_removing_only_the_denials_hands_the_outcome_to_the_next_rule(
        self,
    ) -> None:
        """The control for the ordering assertion above."""
        stdout = self._envelope(
            is_error=True,
            subtype="error_during_execution",
            terminal_reason="error",
            result="The provider's own error text.",
            permission_denials=[],
        )

        result = self._role_result(
            stdout=stdout,
            returncode=1,
            stderr="claude: the session failed\n",
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.summary, "The provider's own error text.")

    def test_the_denial_check_outranks_a_budget_expired_exit_status(self) -> None:
        result = self._role_result(
            stdout=_fixture_text(self.DENIAL_FIXTURE),
            returncode=EXECUTION_BUDGET_EXPIRY_RETURNCODE,
            stderr="Execution Budget timeout (1800 seconds) expired.\n",
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("1 tool call (Bash)", result.summary)
        self.assertNotIn("Execution Budget", result.summary)

    def test_a_terminal_reason_other_than_completion_yields_blocked(self) -> None:
        """Rule 2 does not wait for a failing exit code to agree with it."""
        stdout = self._envelope(
            terminal_reason="refusal",
            permission_denials=[],
            result="I will not do that.",
        )

        result = self._role_result(stdout=stdout, returncode=0)

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.summary, "I will not do that.")

    def test_a_reported_error_yields_blocked_in_the_providers_own_words(self) -> None:
        stdout = self._envelope(
            is_error=True,
            subtype="error_during_execution",
            terminal_reason="completed",
            permission_denials=[],
            result="Claude ran out of context.",
        )

        result = self._role_result(stdout=stdout)

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.summary, "Claude ran out of context.")

    def test_a_clean_terminal_result_yields_the_parsed_role_result(self) -> None:
        result = self._role_result(
            stdout=_fixture_text("permission-bypass.result.json")
        )

        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.changed_files, ["spike.txt"])
        self.assertEqual(result.verification_commands, ["git status --short"])

    def test_a_budget_expired_claude_attempt_names_claude_and_the_workspace(
        self,
    ) -> None:
        result = self._role_result(
            stdout="",
            returncode=EXECUTION_BUDGET_EXPIRY_RETURNCODE,
            stderr="Execution Budget timeout (1800 seconds) expired.\n",
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("Execution Budget timeout (1800 seconds) expired.", result.summary)
        self.assertIn(
            "The Claude Code Backend did not return a final role result",
            result.summary,
        )
        self.assertIn(
            "Changes already written remain in the workspace",
            result.summary,
        )
        self.assertIn("Rerun the unfinished issue", result.summary)
        self.assertNotIn("Codex", result.summary)

    def test_a_budget_expired_attempt_keeps_the_workspace_note_and_its_own_words(
        self,
    ) -> None:
        """The provider failed on its own terms as its budget expired.

        The stream captures a genuine non-completed terminal result and, in the
        same window, the budget detects that the deadline had passed: the exit
        status becomes the expiry convention's and stderr is annotated, while the
        provider's own words are still there to be used as the summary. The
        operator has to be told both.
        """
        stdout = self._envelope(
            is_error=True,
            subtype="error_during_execution",
            terminal_reason="error",
            permission_denials=[],
            result="Claude ran out of context",
        )

        result = self._role_result(
            stdout=stdout,
            returncode=EXECUTION_BUDGET_EXPIRY_RETURNCODE,
            stderr=(
                "Execution Budget checkpoint deadline (300 seconds) expired.\n"
            ),
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn(
            "Execution Budget checkpoint deadline (300 seconds) expired.",
            result.summary,
        )
        self.assertIn("Claude ran out of context.", result.summary)
        self.assertIn(
            "Changes already written remain in the workspace",
            result.summary,
        )
        self.assertIn("Rerun the unfinished issue", result.summary)
        self.assertRegex(result.summary, r"See .*\.stderr\.txt\.$")

    def test_a_providers_own_words_carry_no_budget_note_when_none_expired(
        self,
    ) -> None:
        """The control: only a terminated attempt gets the budget's promise."""
        stdout = self._envelope(
            is_error=True,
            terminal_reason="error",
            permission_denials=[],
            result="Claude ran out of context.",
        )

        result = self._role_result(
            stdout=stdout,
            returncode=EXECUTION_BUDGET_EXPIRY_RETURNCODE,
            stderr="claude: the session failed\n",
        )

        self.assertEqual(result.summary, "Claude ran out of context.")

    def test_a_failed_claude_attempt_never_reports_a_codex_failure(self) -> None:
        result = self._role_result(
            stdout="",
            returncode=1,
            stderr="claude: command failed\n",
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(
            result.summary.split(". See ")[0],
            "The Claude Code Backend failed with exit code 1",
        )
        self.assertNotIn("codex", result.summary.lower())

    def test_a_claude_attempt_returning_no_role_result_names_claude(self) -> None:
        result = self._role_result(
            stdout="claude printed prose and never returned a role result\n"
        )

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(
            result.summary,
            "The Claude Code Backend did not return valid JSON matching the "
            "role schema.",
        )

    def test_a_codex_attempt_returning_no_role_result_names_codex(self) -> None:
        self.assertEqual(
            RoleResult.from_message(
                "no json here",
                backend=ExecutionBackendId.CODEX_CLI,
            ).summary,
            "The Codex CLI Backend did not return valid JSON matching the "
            "role schema.",
        )
        # With no backend named the summary blames the boundary, never a provider.
        self.assertEqual(
            RoleResult.from_message("no json here").summary,
            "The Execution Backend did not return valid JSON matching the "
            "role schema.",
        )


class RecordedAttemptProvenanceTests(unittest.TestCase):
    """What the terminal result says about which model worked and what it cost."""

    def _provenance(
        self,
        fixture: str,
        *,
        requested_model: str | None = None,
    ) -> StepAttemptProvenance:
        terminal_result = claude_code.claude_terminal_result(_fixture_text(fixture))
        assert terminal_result is not None
        return claude_code.claude_attempt_provenance(
            terminal_result,
            requested_model=requested_model,
        )

    def test_the_serving_model_comes_from_the_turns_own_usage_accounting(self) -> None:
        recorded = claude_code.claude_terminal_result(
            _fixture_text("bypass-stream.jsonl")
        )
        assert recorded is not None
        accounted = tuple(recorded[claude_code.CLAUDE_MODEL_USAGE_KEY])

        provenance = self._provenance("bypass-stream.jsonl")

        self.assertEqual(accounted, ("claude-haiku-4-5-20251001",))
        self.assertEqual(provenance.serving_model, "claude-haiku-4-5-20251001")
        self.assertIs(provenance.backend, ExecutionBackendId.CLAUDE_CODE)

    def test_the_recorded_cost_and_turn_count_are_persisted_as_evidence(self) -> None:
        recorded = claude_code.claude_terminal_result(
            _fixture_text("bypass-stream.jsonl")
        )
        assert recorded is not None

        provenance = self._provenance("bypass-stream.jsonl")

        self.assertEqual(
            provenance.cost_usd,
            recorded[claude_code.CLAUDE_TOTAL_COST_KEY],
        )
        self.assertEqual(
            provenance.turn_count,
            recorded[claude_code.CLAUDE_TURN_COUNT_KEY],
        )
        self.assertEqual(provenance.turn_count, 5)

    def test_a_recording_disagrees_with_its_own_session_initialisation_event(
        self,
    ) -> None:
        """The observation this whole record exists for, from a committed capture.

        `alias-resolution-stream.jsonl` reports one model on its
        session-initialisation event and a different one in the finished turn's own
        usage accounting. A model selection is verified and pinned from that
        initialisation event, so if the serving model were read from there too, the
        two could never disagree and a substitution would leave no trace anywhere.
        """
        initialisation_model = _session_init_event("alias-resolution-stream.jsonl")[
            "model"
        ]

        provenance = self._provenance("alias-resolution-stream.jsonl")

        self.assertEqual(initialisation_model, "claude-haiku-4-5-20251001")
        self.assertEqual(provenance.serving_model, "claude-sonnet-5")
        self.assertNotEqual(provenance.serving_model, initialisation_model)

    def test_a_requested_model_the_turn_did_not_use_is_recorded_as_a_mismatch(
        self,
    ) -> None:
        provenance = self._provenance(
            "bypass-stream.jsonl",
            requested_model="claude-sonnet-5",
        )

        self.assertTrue(provenance.model_mismatch)
        self.assertEqual(provenance.requested_model, "claude-sonnet-5")
        self.assertEqual(provenance.serving_model, "claude-haiku-4-5-20251001")
        evidence = provenance.mismatch_evidence()
        assert evidence is not None
        self.assertIn(MODEL_MISMATCH_LABEL, evidence)
        self.assertIn("claude-sonnet-5", evidence)
        self.assertIn("claude-haiku-4-5-20251001", evidence)
        self.assertIn("neither is reconciled", evidence)

    def test_the_requested_model_the_turn_did_use_records_no_mismatch(self) -> None:
        provenance = self._provenance(
            "bypass-stream.jsonl",
            requested_model="claude-haiku-4-5-20251001",
        )

        self.assertFalse(provenance.model_mismatch)
        self.assertIsNone(provenance.mismatch_evidence())

    def test_a_turn_accounted_against_several_models_keeps_every_identifier(
        self,
    ) -> None:
        recorded = _fixture_json("permission-bypass.result.json")
        usage = recorded[claude_code.CLAUDE_MODEL_USAGE_KEY]
        accounted = next(iter(usage.values()))
        envelope = {
            **recorded,
            claude_code.CLAUDE_MODEL_USAGE_KEY: {
                "claude-haiku-4-5-20251001": accounted,
                "claude-sonnet-5": accounted,
            },
        }

        provenance = claude_code.claude_attempt_provenance(
            envelope,
            requested_model="claude-haiku-4-5-20251001",
        )

        self.assertEqual(
            provenance.serving_model,
            "claude-haiku-4-5-20251001, claude-sonnet-5",
        )
        # A turn served by more than one model was not served by the single model
        # the Workflow Step requested, so it still reads as a mismatch.
        self.assertTrue(provenance.model_mismatch)

    def test_an_unreported_serving_model_is_left_unknown_rather_than_assumed(
        self,
    ) -> None:
        recorded = _fixture_json("permission-bypass.result.json")
        envelope = {
            key: value
            for key, value in recorded.items()
            if key != claude_code.CLAUDE_MODEL_USAGE_KEY
        }

        provenance = claude_code.claude_attempt_provenance(
            envelope,
            requested_model="claude-haiku-4-5-20251001",
        )

        self.assertIsNone(provenance.serving_model)
        self.assertFalse(provenance.model_mismatch)
        self.assertEqual(provenance.requested_model, "claude-haiku-4-5-20251001")

    def test_a_malformed_cost_or_turn_count_records_nothing_rather_than_failing(
        self,
    ) -> None:
        recorded = _fixture_json("permission-bypass.result.json")

        provenance = claude_code.claude_attempt_provenance(
            {
                **recorded,
                claude_code.CLAUDE_TOTAL_COST_KEY: "not a number",
                claude_code.CLAUDE_TURN_COUNT_KEY: None,
            }
        )

        self.assertIsNone(provenance.cost_usd)
        self.assertIsNone(provenance.turn_count)
        self.assertEqual(provenance.serving_model, "claude-haiku-4-5-20251001")

    def test_an_attempt_that_reached_no_terminal_result_still_names_its_backend(
        self,
    ) -> None:
        provenance = claude_code.claude_attempt_provenance(
            None,
            requested_model="claude-sonnet-5",
        )

        self.assertIs(provenance.backend, ExecutionBackendId.CLAUDE_CODE)
        self.assertEqual(provenance.requested_model, "claude-sonnet-5")
        self.assertIsNone(provenance.serving_model)
        self.assertFalse(provenance.model_mismatch)


class ClaudeProvenanceReportingTests(unittest.TestCase):
    """A mismatch reaches the operator live, once, wherever they are watching."""

    def _invoke(
        self,
        stdout: str,
        *,
        settings: StepExecutionSettings,
        events: list[StepActivityEvent | None] | None = None,
    ):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def write_log(path: Path, text: str) -> None:
                path.write_text(text, encoding="utf-8")

            request = StepAttemptRequest(
                prompt="Implement the issue.",
                repo_root=root,
                schema_path=ROLE_RESULT_SCHEMA,
                message_path=root / "attempt.last-message.json",
                stdout_path=root / "attempt.stdout.jsonl",
                stderr_path=root / "attempt.stderr.txt",
                write_log=write_log,
                execution_settings=settings,
                activity_stage=Stage.DEVELOPMENT,
                activity_context="0003 p1",
                activity_callback=(
                    None if events is None else events.append
                ),
            )
            with mock.patch.object(
                claude_code,
                "run_streaming_claude_command",
                return_value=CompletedProcess(["claude"], 0, stdout, ""),
            ):
                return ClaudeCodeExecutionBackend("claude").invoke(request)

    def test_invoke_reports_the_provenance_of_a_completed_attempt(self) -> None:
        result = self._invoke(
            _fixture_text("bypass-stream.jsonl"),
            settings=CLAUDE_SETTINGS,
            events=[],
        )

        self.assertIs(result.provenance.backend, ExecutionBackendId.CLAUDE_CODE)
        self.assertEqual(result.provenance.requested_model, CLAUDE_SETTINGS.model)
        self.assertEqual(result.provenance.serving_model, "claude-haiku-4-5-20251001")
        self.assertEqual(result.provenance.turn_count, 5)
        assert result.provenance.cost_usd is not None
        self.assertGreater(result.provenance.cost_usd, 0)

    def test_a_mismatch_reaches_the_activity_feed_exactly_once(self) -> None:
        events: list[StepActivityEvent | None] = []

        result = self._invoke(
            _fixture_text("bypass-stream.jsonl"),
            settings=CLAUDE_SETTINGS,
            events=events,
        )

        reported = [
            event
            for event in events
            if event is not None and MODEL_MISMATCH_LABEL in (event.activity or "")
        ]
        self.assertEqual(len(reported), 1)
        self.assertIs(reported[0].kind, StepActivityKind.ERROR)
        self.assertEqual(reported[0].activity, result.provenance.mismatch_evidence())

    def test_a_matching_model_reports_no_mismatch_activity_at_all(self) -> None:
        events: list[StepActivityEvent | None] = []

        result = self._invoke(
            _fixture_text("bypass-stream.jsonl"),
            settings=StepExecutionSettings(
                ExecutionBackendId.CLAUDE_CODE,
                "claude-haiku-4-5-20251001",
                "high",
                FastPreference.OFF,
            ),
            events=events,
        )

        self.assertFalse(result.provenance.model_mismatch)
        self.assertEqual(
            [
                event
                for event in events
                if event is not None and MODEL_MISMATCH_LABEL in (event.activity or "")
            ],
            [],
        )

    def test_portable_plain_mode_prints_the_mismatch_as_one_line(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            self._invoke(_fixture_text("bypass-stream.jsonl"), settings=CLAUDE_SETTINGS)

        printed = [
            line
            for line in output.getvalue().splitlines()
            if MODEL_MISMATCH_LABEL in line
        ]
        self.assertEqual(len(printed), 1)
        self.assertTrue(printed[0].startswith("[development] 0003 p1:"))
        self.assertIn("claude-sonnet-5", printed[0])
        self.assertIn("claude-haiku-4-5-20251001", printed[0])

    def test_a_denied_attempt_still_records_which_model_did_the_work(self) -> None:
        result = self._invoke(
            _fixture_text("permission-dontask.result.json"),
            settings=CLAUDE_SETTINGS,
            events=[],
        )

        recorded = _fixture_json("permission-dontask.result.json")
        self.assertEqual([refusal.target for refusal in result.refusals], ["Bash"])
        self.assertEqual(result.provenance.serving_model, "claude-haiku-4-5-20251001")
        self.assertEqual(
            result.provenance.turn_count,
            recorded[claude_code.CLAUDE_TURN_COUNT_KEY],
        )
        self.assertEqual(
            result.provenance.cost_usd,
            recorded[claude_code.CLAUDE_TOTAL_COST_KEY],
        )


class ClaudeActivityFeedTests(unittest.TestCase):
    """The Portable Activity Feed for a Claude-backed step, bounded as for Codex.

    Driven through the real dashboard, from the recorded stream, so what is
    asserted is the feed an operator watches rather than the event list behind it.
    """

    def _dashboard(self, stream: io.StringIO) -> IssueDashboard:
        dashboard = IssueDashboard(
            issue_number="0003",
            issue_title="Claude-backed attempt",
            position=1,
            total=1,
            stream=stream,
        )
        dashboard.show_workflow_progress(
            project_workflow_progress(
                default_portable_workflow(),
                default_portable_component_catalog(),
                (
                    StepRuntimeState(
                        step_instance_id=DEVELOPMENT_STEP_ID,
                        issue_id="0003",
                        status=StepRuntimeStatus.RUNNING,
                        pass_number=1,
                    ),
                ),
                (),
                issue_id="0003",
            )
        )
        return dashboard

    def _recorded_activity(self) -> tuple[StepActivityEvent | None, ...]:
        """Every neutral activity the recorded attempt reported, in order."""
        reported: list[StepActivityEvent | None] = []
        for payload in _fixture_events("bypass-stream.jsonl"):
            translated = claude_code.claude_step_activity_events(payload)
            reported.extend(translated if translated else (None,))
        return tuple(reported)

    def test_the_feed_shows_the_latest_claude_activity_on_one_bounded_line(
        self,
    ) -> None:
        stream = io.StringIO()
        dashboard = self._dashboard(stream)
        reported = self._recorded_activity()

        for event in reported:
            dashboard.notify_activity(event)

        rendered = stream.getvalue()
        feed_lines = [line for line in rendered.splitlines() if line.startswith("AI ")]
        displayed = [
            event.activity for event in reported if event is not None and event.activity
        ]
        self.assertTrue(feed_lines)
        # Every rendered screen carries exactly one activity line, so however long
        # the attempt streams the feed stays one bounded row rather than growing
        # into a transcript.
        self.assertEqual(len(feed_lines), rendered.count("ACTIVE Development"))
        self.assertTrue(feed_lines[-1].endswith(displayed[-1]))
        self.assertLess(len(feed_lines), len(reported))

    def test_an_event_with_nothing_to_show_refreshes_no_feed_line(self) -> None:
        """The dense reasoning heartbeat is progress, and never feed spam."""
        stream = io.StringIO()
        dashboard = self._dashboard(stream)
        heartbeat = StepActivityEvent(kind=StepActivityKind.REASONING)
        self.assertIn(heartbeat, self._recorded_activity())
        before = stream.getvalue()

        for _ in range(20):
            dashboard.notify_activity(heartbeat)

        self.assertEqual(stream.getvalue(), before)

        dashboard.notify_activity(
            StepActivityEvent(
                kind=StepActivityKind.MESSAGE,
                activity="Using the Bash tool.",
            )
        )

        self.assertGreater(len(stream.getvalue()), len(before))


class RunnerAttemptProvenanceTests(unittest.TestCase):
    """The role runner completes provenance for every backend it dispatches to."""

    def _role_result(
        self,
        backend: ExecutionBackend,
        settings: StepExecutionSettings | None,
    ) -> RoleResult:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = _role_runner(root, backend)
            return runner.run_role(
                role="coder",
                issue=_issue(root),
                pass_number=1,
                execution_settings=settings,
            )

    def test_a_codex_backed_attempt_records_its_backend_and_requested_model(
        self,
    ) -> None:
        """Codex reports no serving model, so it is recorded as unknown, not equal."""
        result = self._role_result(
            _RecordingExecutionBackend(
                ExecutionBackendId.CODEX_CLI,
                '{"status":"PASS","summary":"done"}',
            ),
            CODEX_SETTINGS,
        )

        assert result.provenance is not None
        self.assertIs(result.provenance.backend, ExecutionBackendId.CODEX_CLI)
        self.assertEqual(result.provenance.requested_model, CODEX_SETTINGS.model)
        self.assertIsNone(result.provenance.serving_model)
        self.assertFalse(result.provenance.model_mismatch)

    def test_a_backend_that_reported_its_own_provenance_keeps_it(self) -> None:
        class ReportingBackend(_RecordingExecutionBackend):
            def invoke(self, request: StepAttemptRequest) -> StepAttemptResult:
                return StepAttemptResult(
                    process=CompletedProcess(["backend"], 0, "", ""),
                    message='{"status":"PASS","summary":"done"}',
                    provenance=StepAttemptProvenance(
                        backend=ExecutionBackendId.CLAUDE_CODE,
                        requested_model="claude-sonnet-5",
                        serving_model="claude-haiku-4-5-20251001",
                        cost_usd=0.25,
                        turn_count=3,
                    ),
                )

        result = self._role_result(
            ReportingBackend(ExecutionBackendId.CLAUDE_CODE, ""),
            CLAUDE_SETTINGS,
        )

        assert result.provenance is not None
        self.assertTrue(result.provenance.model_mismatch)
        self.assertEqual(result.provenance.cost_usd, 0.25)
        self.assertEqual(result.provenance.turn_count, 3)

    def test_a_blocked_attempt_still_carries_its_backend_and_model(self) -> None:
        class FailingBackend(_RecordingExecutionBackend):
            def invoke(self, request: StepAttemptRequest) -> StepAttemptResult:
                return StepAttemptResult(
                    process=CompletedProcess(["backend"], 1, "", "boom\n"),
                )

        result = self._role_result(
            FailingBackend(ExecutionBackendId.CLAUDE_CODE, ""),
            CLAUDE_SETTINGS,
        )

        self.assertEqual(result.status, "BLOCKED")
        assert result.provenance is not None
        self.assertIs(result.provenance.backend, ExecutionBackendId.CLAUDE_CODE)
        self.assertEqual(result.provenance.requested_model, CLAUDE_SETTINGS.model)

    def test_an_attempt_with_no_settings_records_the_dispatched_backend_only(
        self,
    ) -> None:
        result = self._role_result(
            _RecordingExecutionBackend(
                ExecutionBackendId.CODEX_CLI,
                '{"status":"PASS","summary":"done"}',
            ),
            None,
        )

        assert result.provenance is not None
        self.assertIs(result.provenance.backend, ExecutionBackendId.CODEX_CLI)
        self.assertIsNone(result.provenance.requested_model)

    def test_provenance_never_reaches_an_agent_prompt(self) -> None:
        """Provenance is Dev Loop's record of the attempt, not agent input."""
        result = RoleResult(
            status="PASS",
            summary="done",
            provenance=StepAttemptProvenance(
                backend=ExecutionBackendId.CLAUDE_CODE,
                requested_model="claude-sonnet-5",
                serving_model="claude-haiku-4-5-20251001",
            ),
        )

        self.assertNotIn("provenance", codex_runner.result_to_dict(result))
        self.assertNotIn(
            "claude-haiku-4-5-20251001",
            json.dumps(codex_runner.result_to_dict(result)),
        )


if __name__ == "__main__":
    unittest.main()
