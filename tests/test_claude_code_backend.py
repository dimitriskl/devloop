"""Claude Code Backend behaviour, driven from recorded provider output.

Every test in this module reads a committed fixture captured from a real run of
the installed Claude CLI. Nothing here spawns a provider executable.
"""

from __future__ import annotations

import io
import json
import tempfile
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
    StepAttemptRequest,
    StepAttemptResult,
    claude_code,
    resolve_execution_backend,
    update_checkpoint_for_step_activity,
)
from devloop.portable_workflow import (
    ExecutionBudget,
    FastPreference,
    StepExecutionSettings,
)
from devloop.statusui import Stage
from devloop.step_configuration import STEP_GUIDANCE_PRECEDENCE
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

    def authorize_execution_settings(self, authorizations, *, model_catalog) -> None:
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

    def authorize_execution_settings(self, authorizations, *, model_catalog) -> None:
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

    def test_model_selection_and_preflight_are_not_available_yet(self) -> None:
        backend = ClaudeCodeExecutionBackend()

        with self.assertRaises(NotImplementedError):
            backend.discover_model_catalog(cwd=Path.cwd())
        with self.assertRaises(NotImplementedError):
            backend.authorize_execution_settings((), model_catalog=None)


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


if __name__ == "__main__":
    unittest.main()
