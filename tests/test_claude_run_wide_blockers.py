"""Claude Run-Wide Blocker classification, transient retries, and the pause path.

Every provider signal in this module comes from a committed recording of the
installed Claude CLI. Where no recording of a failure exists, a recorded terminal
result supplies the whole envelope and only the single field under test is set, so
what is asserted is how Dev Loop reads a real result shape rather than how it
reads a hand-written one. Nothing here spawns a provider executable.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from devloop import cli, codex_runner, statusui
from devloop.cli import execute_dependency_schedule
from devloop.codex_runner import RoleResult, RunWideBlockerError
from devloop.issue_pack import Issue
from devloop.issue_scheduler import IssueDependencyGraph, SchedulingPhase
from devloop.portable_execution_backend import (
    ClaudeCodeExecutionBackend,
    CodexCliExecutionBackend,
    ExecutionBackend,
    ExecutionBackendId,
    RunWideBlocker,
    RunWideBlockerKind,
    RunWideBlockerPolicy,
    StepAttemptRequest,
    claude_code,
    codex_cli,
)
from devloop.portable_workflow import (
    ExecutionBudget,
    FastPreference,
    StepExecutionSettings,
)
from devloop.state import IssueStatus, LoopStateWriter
from devloop.statusui import Stage
from devloop.templates import BundleContext, Preset

REPOSITORY_ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "claude_code"
ROLE_RESULT_SCHEMA = REPOSITORY_ROOT / "schemas" / "role-result.schema.json"
CLEAN_RESULT_FIXTURE = "permission-bypass.result.json"
DENIED_RESULT_FIXTURE = "permission-dontask.result.json"
RATE_LIMIT_STREAM_FIXTURE = "bypass-stream.jsonl"
CLAUDE_SETTINGS = StepExecutionSettings(
    ExecutionBackendId.CLAUDE_CODE,
    "claude-sonnet-5",
    "high",
    FastPreference.OFF,
)
# A transport failure the CLI reports without ever producing a terminal result.
TRANSIENT_STDERR = "Error: connect ECONNRESET 160.79.104.10:443\n"


def _fixture_text(name: str) -> str:
    return (FIXTURE_ROOT / name).read_text(encoding="utf-8")


def _recorded_terminal_result(name: str = CLEAN_RESULT_FIXTURE) -> dict[str, object]:
    """One whole terminal result exactly as the installed CLI recorded it."""
    recorded = json.loads(_fixture_text(name))
    assert isinstance(recorded, dict)
    return recorded


def _recorded_result_with_api_error(
    status: object,
    *,
    fixture: str = CLEAN_RESULT_FIXTURE,
) -> dict[str, object]:
    """The recorded terminal result of an attempt whose API call failed.

    Every field but the API status, the error flag, and the terminal reason stays
    as recorded, because those three are the only ones a failed API call changes.
    """
    result = _recorded_terminal_result(fixture)
    result[claude_code.CLAUDE_API_ERROR_STATUS_KEY] = status
    result["is_error"] = True
    result[claude_code.CLAUDE_TERMINAL_REASON_KEY] = "error"
    return result


def _transcript(*events: dict[str, object]) -> str:
    return "".join(f"{json.dumps(event)}\n" for event in events)


def _recorded_rate_limit_event(*, status: str | None = None) -> dict[str, object]:
    """The committed rate-limit event, optionally with another disposition.

    The recording carries an allowed status beside an unrelated rejected overage
    status, which is exactly the shape that makes reading the wrong field visible.
    """
    for line in _fixture_text(RATE_LIMIT_STREAM_FIXTURE).splitlines():
        payload = claude_code.parse_claude_event(line)
        if (
            payload is not None
            and payload.get("type") == claude_code.ClaudeEventType.RATE_LIMIT.value
        ):
            if status is not None:
                info = payload[claude_code.CLAUDE_RATE_LIMIT_INFO_KEY]
                assert isinstance(info, dict)
                info[claude_code.CLAUDE_RATE_LIMIT_STATUS_KEY] = status
            return payload
    raise AssertionError(
        f"{RATE_LIMIT_STREAM_FIXTURE} carries no rate-limit event to drive the test."
    )


def _classify(stdout: str) -> RunWideBlocker | None:
    return claude_code.claude_run_wide_blocker(
        claude_code.claude_terminal_result(stdout),
        stdout=stdout,
    )


def _classify_result(result: dict[str, object]) -> RunWideBlocker | None:
    return _classify(_transcript(result))


class FakeStream(io.StringIO):
    """A stream whose colour capability a test decides."""

    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _issue(root: Path, number: str, *, dependencies: tuple[str, ...] = ()) -> Issue:
    path = root / f"{number}.md"
    path.write_text(f"# Issue {number}\n", encoding="utf-8")
    return Issue(number, f"Issue {number}", path, False, dependencies)


def _claude_runner(root: Path) -> codex_runner.CodexRunner:
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
    runner.execution_backend = ClaudeCodeExecutionBackend("claude")
    runner.dry_run = False
    runner.ensure_log_root()
    return runner


def _attempt_request(root: Path, **overrides: object) -> StepAttemptRequest:
    def write_log(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")

    fields: dict[str, object] = {
        "prompt": "Implement the issue.",
        "repo_root": root,
        "schema_path": ROLE_RESULT_SCHEMA,
        "message_path": root / "attempt.last-message.json",
        "stdout_path": root / "attempt.stdout.jsonl",
        "stderr_path": root / "attempt.stderr.txt",
        "write_log": write_log,
        "execution_settings": CLAUDE_SETTINGS,
        "activity_stage": Stage.DEVELOPMENT,
    }
    fields.update(overrides)
    return StepAttemptRequest(**fields)  # type: ignore[arg-type]


class ClaudeRunWideBlockerClassificationTests(unittest.TestCase):
    """Which recorded provider signal means which Run-Wide Blocker."""

    def test_unauthorised_and_forbidden_statuses_classify_as_authentication(
        self,
    ) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                blocker = _classify_result(_recorded_result_with_api_error(status))

                self.assertIsNotNone(blocker)
                assert blocker is not None
                self.assertIs(blocker.kind, RunWideBlockerKind.AUTHENTICATION)
                self.assertIn("authentication", blocker.summary.lower())

    def test_a_rate_limit_status_classifies_as_exhausted_usage(self) -> None:
        blocker = _classify_result(_recorded_result_with_api_error(429))

        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertIs(blocker.kind, RunWideBlockerKind.USAGE_LIMIT)

    def test_a_rejecting_rate_limit_event_classifies_as_exhausted_usage(self) -> None:
        """The stream's own event can report exhaustion before any turn ends."""
        stdout = _transcript(
            _recorded_rate_limit_event(
                status=claude_code.ClaudeRateLimitStatus.REJECTED.value
            ),
            _recorded_terminal_result(),
        )

        blocker = _classify(stdout)

        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertIs(blocker.kind, RunWideBlockerKind.USAGE_LIMIT)

    def test_the_recorded_rate_limit_event_of_a_healthy_attempt_is_not_a_blocker(
        self,
    ) -> None:
        """The committed recording is a successful attempt that reports its window.

        Its rate-limit information carries an allowed status beside a rejected
        overage status. Treating the event's presence, or the wrong field, as
        exhausted usage would pause the run on every healthy attempt.
        """
        stdout = _fixture_text(RATE_LIMIT_STREAM_FIXTURE)
        recorded = _recorded_rate_limit_event()
        information = recorded[claude_code.CLAUDE_RATE_LIMIT_INFO_KEY]

        assert isinstance(information, dict)
        self.assertEqual(
            information[claude_code.CLAUDE_RATE_LIMIT_STATUS_KEY],
            claude_code.ClaudeRateLimitStatus.ALLOWED.value,
        )
        self.assertEqual(
            information["overageStatus"],
            claude_code.ClaudeRateLimitStatus.REJECTED.value,
        )
        self.assertIsNone(_classify(stdout))

    def test_server_error_statuses_classify_as_service_unavailability(self) -> None:
        for status in (500, 502, 503, 529, 599):
            with self.subTest(status=status):
                blocker = _classify_result(_recorded_result_with_api_error(status))

                self.assertIsNotNone(blocker)
                assert blocker is not None
                self.assertIs(blocker.kind, RunWideBlockerKind.SERVICE_UNAVAILABLE)

    def test_a_not_found_status_classifies_as_withdrawn_model_access(self) -> None:
        blocker = _classify_result(_recorded_result_with_api_error(404))

        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertIs(blocker.kind, RunWideBlockerKind.MODEL_ACCESS_WITHDRAWN)
        self.assertIn("/options", blocker.summary)

    def test_a_status_reported_as_a_string_is_read_as_the_same_status(self) -> None:
        blocker = _classify_result(_recorded_result_with_api_error("429"))

        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertIs(blocker.kind, RunWideBlockerKind.USAGE_LIMIT)

    def test_a_recorded_successful_attempt_reports_no_blocker(self) -> None:
        for fixture in (
            CLEAN_RESULT_FIXTURE,
            DENIED_RESULT_FIXTURE,
            RATE_LIMIT_STREAM_FIXTURE,
            "isolated-stream.jsonl",
            "alias-resolution-stream.jsonl",
        ):
            with self.subTest(fixture=fixture):
                self.assertIsNone(_classify(_fixture_text(fixture)))

    def test_an_unrecognised_status_shape_is_left_unclassified(self) -> None:
        """A shape Dev Loop cannot read is not evidence that the run must pause."""
        for status in (None, True, "", "unavailable", 200, 418, {"code": 429}):
            with self.subTest(status=status):
                self.assertIsNone(
                    _classify_result(_recorded_result_with_api_error(status))
                )

    def test_no_summary_carries_a_credential_or_a_raw_provider_payload(self) -> None:
        secret = "sk-ant-api03-THISMUSTNEVERAPPEAR"
        result = _recorded_result_with_api_error(429)
        result["result"] = f"Request failed with key {secret}: quota exhausted."
        stdout = _transcript(
            _recorded_rate_limit_event(
                status=claude_code.ClaudeRateLimitStatus.REJECTED.value
            ),
            result,
        )

        blocker = _classify(stdout)

        self.assertIsNotNone(blocker)
        assert blocker is not None
        self.assertNotIn(secret, blocker.summary)
        self.assertNotIn("quota exhausted", blocker.summary)
        self.assertEqual(
            blocker.summary,
            claude_code.CLAUDE_RUN_WIDE_BLOCKER_SUMMARIES[
                RunWideBlockerKind.USAGE_LIMIT
            ],
        )

    def test_classification_spawns_no_provider_executable(self) -> None:
        with mock.patch.object(
            claude_code.subprocess,
            "Popen",
            side_effect=AssertionError("Classification must start no process."),
        ):
            self.assertIsNone(_classify(_fixture_text(RATE_LIMIT_STREAM_FIXTURE)))
            self.assertIsNotNone(_classify_result(_recorded_result_with_api_error(429)))


class IssueSpecificFailureIsNeverRunWideTests(unittest.TestCase):
    """The failures that belong to one Issue, and must never pause the run."""

    def _recorded_result_saying(self, text: str) -> dict[str, object]:
        result = _recorded_terminal_result()
        result["result"] = text
        return result

    def test_an_issue_specific_failure_is_never_classified_as_run_wide(self) -> None:
        cases = {
            "a failing repository command": self._recorded_result_saying(
                "pytest exited 1: the service was unavailable in the test double."
            ),
            "a failing test": self._recorded_result_saying(
                json.dumps(
                    {
                        "status": "FAIL",
                        "summary": "3 tests failed with HTTP 503 assertions.",
                    }
                )
            ),
            "a review finding": self._recorded_result_saying(
                json.dumps(
                    {
                        "status": "FAIL",
                        "summary": "Review found unhandled 401 handling.",
                        "findings": ["Authentication failed is swallowed silently."],
                    }
                )
            ),
        }
        for description, result in cases.items():
            with self.subTest(case=description):
                self.assertIsNone(_classify_result(result))

    def test_a_failing_repository_command_in_the_stream_is_never_run_wide(self) -> None:
        stdout = _transcript(
            {
                "type": claude_code.ClaudeEventType.USER.value,
                "message": {
                    "content": [
                        {
                            "type": claude_code.ClaudeContentBlockType.TOOL_RESULT.value,
                            "tool_use_id": "toolu_01",
                            "is_error": True,
                            "content": "dotnet test failed: 503 Service Unavailable",
                        }
                    ]
                },
            },
            _recorded_terminal_result(),
        )

        self.assertIsNone(_classify(stdout))

    def test_a_permission_denial_alone_is_never_classified_as_run_wide(self) -> None:
        stdout = _fixture_text(DENIED_RESULT_FIXTURE)

        self.assertIsNone(_classify(stdout))
        self.assertEqual(
            [
                refusal.target
                for refusal in claude_code.claude_permission_denials(
                    claude_code.claude_terminal_result(stdout)
                )
            ],
            ["Bash"],
        )


class ClaudeTransientRetryTests(unittest.TestCase):
    """What a bounded retry may repeat, and what it must never repeat."""

    def _invoke(
        self,
        root: Path,
        results: tuple[CompletedProcess[str], ...],
        *,
        execution_budget: ExecutionBudget | None = None,
    ):
        request = _attempt_request(root, execution_budget=execution_budget)
        with mock.patch.object(
            claude_code,
            "CLAUDE_TRANSIENT_RETRY_DELAY_SECONDS",
            0.0,
        ), mock.patch.object(
            claude_code,
            "run_streaming_claude_command",
            side_effect=results,
        ) as streamed, redirect_stdout(io.StringIO()):
            result = ClaudeCodeExecutionBackend("claude").invoke(request)
        return result, streamed

    def test_a_transient_transport_failure_is_retried_and_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result, streamed = self._invoke(
                root,
                (
                    CompletedProcess(["claude"], 1, "", TRANSIENT_STDERR),
                    CompletedProcess(
                        ["claude"],
                        0,
                        _fixture_text(RATE_LIMIT_STREAM_FIXTURE),
                        "",
                    ),
                ),
            )

            self.assertEqual(streamed.call_count, 2)
            self.assertIsNone(result.run_wide_blocker)
            self.assertEqual(RoleResult.from_message(result.message).status, "PASS")

    def test_a_transient_server_error_reported_without_a_terminal_result_is_retried(
        self,
    ) -> None:
        for diagnostic in (
            "API Error: 503 upstream connect error\n",
            "Error: 502 Bad Gateway\n",
            "fetch failed: socket hang up\n",
        ):
            with self.subTest(diagnostic=diagnostic), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _result, streamed = self._invoke(
                    root,
                    (
                        CompletedProcess(["claude"], 1, "", diagnostic),
                        CompletedProcess(
                            ["claude"],
                            0,
                            _fixture_text(RATE_LIMIT_STREAM_FIXTURE),
                            "",
                        ),
                    ),
                )

                self.assertEqual(streamed.call_count, 2)

    def test_the_retries_of_one_attempt_share_one_execution_budget(self) -> None:
        budget = ExecutionBudget(timeout_seconds=60, checkpoint_seconds=30)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _result, streamed = self._invoke(
                root,
                (
                    CompletedProcess(["claude"], 1, "", TRANSIENT_STDERR),
                    CompletedProcess(
                        ["claude"],
                        0,
                        _fixture_text(RATE_LIMIT_STREAM_FIXTURE),
                        "",
                    ),
                ),
                execution_budget=budget,
            )

            attempt_budgets = [
                call.kwargs["attempt_budget"] for call in streamed.call_args_list
            ]
            self.assertEqual(streamed.call_count, 2)
            self.assertIsNotNone(attempt_budgets[0])
            self.assertIs(attempt_budgets[0], attempt_budgets[1])
            self.assertIs(streamed.call_args.kwargs["execution_budget"], budget)

    def test_a_run_wide_blocker_is_never_retried(self) -> None:
        """Even alongside a transient diagnostic, a classified answer is final."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result, streamed = self._invoke(
                root,
                (
                    CompletedProcess(
                        ["claude"],
                        1,
                        _transcript(_recorded_result_with_api_error(429)),
                        TRANSIENT_STDERR,
                    ),
                ),
            )

            self.assertEqual(streamed.call_count, 1)
            self.assertIsNotNone(result.run_wide_blocker)
            assert result.run_wide_blocker is not None
            self.assertIs(result.run_wide_blocker.kind, RunWideBlockerKind.USAGE_LIMIT)

    def test_a_transient_condition_is_never_recorded_as_a_pause(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result, streamed = self._invoke(
                root,
                (
                    CompletedProcess(["claude"], 1, "", TRANSIENT_STDERR),
                    CompletedProcess(["claude"], 1, "", TRANSIENT_STDERR),
                    CompletedProcess(["claude"], 1, "", TRANSIENT_STDERR),
                    CompletedProcess(["claude"], 0, "", ""),
                ),
            )

            self.assertGreater(streamed.call_count, 1)
            self.assertIsNone(result.run_wide_blocker)

    def test_a_failure_with_no_transient_evidence_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _result, streamed = self._invoke(
                root,
                (CompletedProcess(["claude"], 1, "", "claude: command failed\n"),),
            )

            self.assertEqual(streamed.call_count, 1)

    def test_the_retry_notice_names_the_invocation_and_no_provider_output(self) -> None:
        secret = "sk-ant-api03-THISMUSTNEVERAPPEAR"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = _attempt_request(root)
            with mock.patch.object(
                claude_code,
                "CLAUDE_TRANSIENT_RETRY_DELAY_SECONDS",
                0.0,
            ), mock.patch.object(
                claude_code,
                "run_streaming_claude_command",
                side_effect=(
                    CompletedProcess(
                        ["claude"],
                        1,
                        "",
                        f"Error: connection reset while sending {secret}\n",
                    ),
                    CompletedProcess(["claude"], 0, "", ""),
                ),
            ), redirect_stdout(io.StringIO()) as printed:
                ClaudeCodeExecutionBackend("claude").invoke(request)

            notice = printed.getvalue()
            self.assertIn(claude_code.CLAUDE_RETRY_SUBJECT, notice)
            self.assertNotIn(secret, notice)


class PerBackendRetryabilityPredicateTests(unittest.TestCase):
    """Retryability belongs to the Execution Backend interface, per backend."""

    def test_the_interface_declares_the_predicate_and_defaults_to_no_retry(
        self,
    ) -> None:
        class BackendWithoutTransientFailures(ExecutionBackend):
            @property
            def backend_id(self) -> ExecutionBackendId:
                return ExecutionBackendId.CODEX_CLI

            def invoke(self, request: StepAttemptRequest):
                raise AssertionError("This backend runs no attempt.")

            def discover_model_catalog(self, *, cwd: Path):
                raise AssertionError("This backend discovers no catalog.")

            def authorize_execution_settings(
                self,
                authorizations,
                *,
                model_catalog,
                cwd: Path,
            ) -> None:
                raise AssertionError("This backend authorizes nothing.")

        self.assertFalse(
            BackendWithoutTransientFailures().is_retryable_transient_failure(
                stdout="",
                stderr=TRANSIENT_STDERR,
            )
        )

    def test_the_codex_specific_string_match_is_no_longer_a_module_function(
        self,
    ) -> None:
        self.assertFalse(hasattr(codex_cli, "is_retryable_codex_connection_failure"))

    def test_each_backend_recognises_only_its_own_transient_conditions(self) -> None:
        codex = CodexCliExecutionBackend()
        claude = ClaudeCodeExecutionBackend("claude")
        codex_transient = "failed to connect to websocket\n"

        self.assertTrue(
            codex.is_retryable_transient_failure(stdout="", stderr=codex_transient)
        )
        self.assertFalse(
            codex.is_retryable_transient_failure(stdout="", stderr=TRANSIENT_STDERR)
        )
        self.assertTrue(
            claude.is_retryable_transient_failure(stdout="", stderr=TRANSIENT_STDERR)
        )
        self.assertFalse(
            claude.is_retryable_transient_failure(stdout="", stderr=codex_transient)
        )

    def test_the_codex_predicate_still_refuses_to_retry_a_codex_blocker(self) -> None:
        blocked_stdout = '{"type":"error","message":"You have hit your usage limit"}\n'

        self.assertIsNotNone(
            codex_cli.classify_run_wide_blocker(
                blocked_stdout,
                "failed to connect to websocket\n",
            )
        )
        self.assertFalse(
            CodexCliExecutionBackend().is_retryable_transient_failure(
                stdout=blocked_stdout,
                stderr="failed to connect to websocket\n",
            )
        )

    def test_the_codex_backend_consults_its_own_predicate(self) -> None:
        consulted: list[tuple[str, str]] = []

        def predicate(*, stdout: str, stderr: str) -> bool:
            consulted.append((stdout, stderr))
            return False

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(
                codex_cli,
                "run_streaming_codex_command",
                return_value=CompletedProcess(["codex"], 1, "", "boom\n"),
            ):
                codex_cli.run_codex_exec_with_connection_retries(
                    command=["codex"],
                    prompt="Implement the issue.",
                    stdout_path=root / "stdout.jsonl",
                    stderr_path=root / "stderr.txt",
                    cwd=root,
                    write_log=lambda path, text: None,
                    is_retryable=predicate,
                )

        self.assertEqual(consulted, [("", "boom\n")])


class RunWideBlockerPrecedenceTests(unittest.TestCase):
    """One terminal result can carry a denial and a run-wide status at once."""

    def _run_role(self, root: Path, stdout: str, *, returncode: int = 0):
        runner = _claude_runner(root)
        issue = _issue(root, "0001")
        with mock.patch.object(runner, "build_prompt", return_value="prompt"), \
             mock.patch.object(
                 claude_code,
                 "run_streaming_claude_command",
                 return_value=CompletedProcess(["claude"], returncode, stdout, ""),
             ):
            return runner.run_role("coder", issue, pass_number=1)

    def _denied_result_with_api_error(self, status: object) -> dict[str, object]:
        return _recorded_result_with_api_error(status, fixture=DENIED_RESULT_FIXTURE)

    def test_the_envelope_really_carries_both_signals(self) -> None:
        stdout = _transcript(self._denied_result_with_api_error(429))
        terminal_result = claude_code.claude_terminal_result(stdout)

        self.assertTrue(claude_code.claude_permission_denials(terminal_result))
        self.assertIsNotNone(_classify(stdout))

    def test_a_run_wide_blocker_outranks_a_permission_denial(self) -> None:
        """The pause wins: it publishes no Step Outcome and spends no budget.

        A denial makes an attempt's *claims* untrustworthy, which is why nothing
        that could look like success may overrule it. A pause is not a success —
        it publishes no Step Outcome at all — so honouring the account condition
        first still leaves denied work unable to be reported as done, while
        recording BLOCKED here would spend an Issue attempt budget on a provider
        that never ran the work.
        """
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaises(RunWideBlockerError) as raised:
                self._run_role(
                    root,
                    _transcript(self._denied_result_with_api_error(429)),
                )

        self.assertIs(raised.exception.blocker.kind, RunWideBlockerKind.USAGE_LIMIT)

    def test_a_permission_denial_without_a_run_wide_status_is_still_blocked(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = self._run_role(root, _fixture_text(DENIED_RESULT_FIXTURE))

        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("Bash", result.summary)

    def test_a_run_wide_blocker_publishes_no_role_message_for_the_issue(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = _attempt_request(root)
            with mock.patch.object(
                claude_code,
                "run_streaming_claude_command",
                return_value=CompletedProcess(
                    ["claude"],
                    0,
                    _transcript(_recorded_result_with_api_error(429)),
                    "",
                ),
            ):
                result = ClaudeCodeExecutionBackend("claude").invoke(request)

            self.assertEqual(result.message, "")
            self.assertFalse(request.message_path.exists())

    def test_an_attempt_outside_issue_execution_still_ignores_run_wide_conditions(
        self,
    ) -> None:
        """The post-run compiler has no run left to pause, so it classifies none."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request = _attempt_request(
                root,
                run_wide_blocker_policy=RunWideBlockerPolicy.IGNORE,
            )
            with mock.patch.object(
                claude_code,
                "run_streaming_claude_command",
                return_value=CompletedProcess(
                    ["claude"],
                    1,
                    _transcript(_recorded_result_with_api_error(429)),
                    "",
                ),
            ):
                result = ClaudeCodeExecutionBackend("claude").invoke(request)

            self.assertIsNone(result.run_wide_blocker)


class RunWidePausePathTests(unittest.TestCase):
    """The pause path a Claude blocker reuses, unchanged, from the Codex one."""

    USAGE_LIMIT_BLOCKER = RunWideBlocker(
        kind=RunWideBlockerKind.USAGE_LIMIT,
        summary=claude_code.CLAUDE_RUN_WIDE_BLOCKER_SUMMARIES[
            RunWideBlockerKind.USAGE_LIMIT
        ],
    )

    def _paused_run(self, root: Path):
        """Schedule three Issues and pause on the first, recording every call."""
        index = root / "README.md"
        index.write_text("", encoding="utf-8")
        first = _issue(root, "0001")
        dependent = _issue(root, "0002", dependencies=("0001",))
        independent = _issue(root, "0003")
        issues = [first, dependent, independent]
        writer = LoopStateWriter(index)
        executed: list[tuple[str, SchedulingPhase, int]] = []

        def execute(scheduled: Issue, phase: SchedulingPhase, ordinal: int):
            executed.append((scheduled.number, phase, ordinal))
            writer.issue_state(scheduled).update(
                {
                    "status": IssueStatus.IN_PROGRESS.value,
                    "current_step_instance_id": "step-id",
                    "current_pass": 1,
                }
            )
            writer.flush()
            raise RunWideBlockerError(self.USAGE_LIMIT_BLOCKER)

        with redirect_stderr(io.StringIO()), self.assertRaises(RunWideBlockerError):
            execute_dependency_schedule(
                issues=issues,
                graph=IssueDependencyGraph(issues),
                state_writer=writer,
                execute_issue=execute,
            )
        writer.record_run_paused(self.USAGE_LIMIT_BLOCKER)
        return index, issues, writer, executed

    def test_a_run_wide_blocker_stops_all_further_issue_scheduling_immediately(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _index, _issues, _writer, executed = self._paused_run(root)

        self.assertEqual(
            executed,
            [("0001", SchedulingPhase.NORMAL_SCHEDULING, 1)],
        )

    def test_waiting_and_independent_issues_remain_untouched(self) -> None:
        """Neither the waiting Issue nor the ready independent one is attempted.

        Both keep the readiness the scheduler projected before the pause. Neither
        receives a provider call, a Step Outcome, or an attempt reservation, so the
        independent Issue is still first in line on the rerun rather than having
        spent its normal attempt against an exhausted account.
        """
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index, issues, _writer, executed = self._paused_run(root)
            reloaded = LoopStateWriter(index)
            dependent, independent = issues[1], issues[2]

            self.assertNotIn("0002", [call[0] for call in executed])
            self.assertNotIn("0003", [call[0] for call in executed])
            self.assertEqual(
                reloaded.issue_state(dependent)["status"],
                IssueStatus.WAITING_ON_DEPENDENCY.value,
            )
            self.assertEqual(
                reloaded.issue_state(independent)["status"],
                IssueStatus.READY.value,
            )
            self.assertEqual(reloaded.normal_attempted_issues(), frozenset())
            active = reloaded.active_scheduling_attempt()
            assert active is not None
            self.assertEqual(active["issue"], "0001")

    def test_the_active_issue_keeps_its_outcome_and_spends_no_pass_budget(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index, issues, _writer, _executed = self._paused_run(root)
            reloaded = LoopStateWriter(index)

            self.assertEqual(
                reloaded.issue_state(issues[0])["status"],
                IssueStatus.IN_PROGRESS.value,
            )
            self.assertEqual(reloaded.normal_attempted_issues(), frozenset())
            self.assertEqual(reloaded.additional_passes(), {})

    def test_durable_state_records_the_redacted_reason_and_the_exact_cursor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index, _issues, _writer, _executed = self._paused_run(root)
            reloaded = LoopStateWriter(index)
            pause = reloaded.run_pause()

            assert pause is not None
            self.assertEqual(pause["kind"], RunWideBlockerKind.USAGE_LIMIT.value)
            self.assertEqual(pause["summary"], self.USAGE_LIMIT_BLOCKER.summary)
            self.assertEqual(pause["issue"], "0001")
            self.assertEqual(pause["step_instance_id"], "step-id")
            self.assertEqual(pause["pass"], 1)
            self.assertEqual(pause["phase"], SchedulingPhase.NORMAL_SCHEDULING.value)
            self.assertEqual(pause["ordinal"], 1)
            self.assertEqual(
                reloaded.state["dependency_scheduler"]["phase"],
                SchedulingPhase.RUN_PAUSED.value,
            )
            # The remaining budgets: neither the normal attempt nor an additional
            # pass was charged, so a rerun has the same allowance it started with.
            self.assertEqual(reloaded.normal_attempted_issues(), frozenset())
            self.assertEqual(reloaded.additional_passes(), {})

    def test_rerunning_the_same_command_resumes_the_exact_paused_work(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index, issues, _writer, _executed = self._paused_run(root)
            resumed = LoopStateWriter(index)
            replayed: list[tuple[str, SchedulingPhase, int]] = []

            def execute_after_recovery(
                scheduled: Issue,
                phase: SchedulingPhase,
                ordinal: int,
            ) -> RoleResult:
                replayed.append((scheduled.number, phase, ordinal))
                resumed.issue_state(scheduled)["status"] = (
                    IssueStatus.COMPLETED.value
                )
                resumed.flush()
                return RoleResult(status="PASS")

            with redirect_stderr(io.StringIO()):
                schedule = execute_dependency_schedule(
                    issues=issues,
                    graph=IssueDependencyGraph(issues),
                    state_writer=resumed,
                    execute_issue=execute_after_recovery,
                )

            self.assertTrue(schedule.completed)
            # The paused Issue is retried first, in the exact phase and round it
            # was paused in, and the run then continues into the work that waited.
            self.assertEqual(replayed[0], ("0001", SchedulingPhase.NORMAL_SCHEDULING, 1))
            self.assertEqual(
                sorted(call[0] for call in replayed),
                ["0001", "0002", "0003"],
            )

    def test_the_run_pause_reads_as_a_pause_rather_than_an_issue_status(self) -> None:
        environment = {
            key: value for key, value in os.environ.items() if key != "NO_COLOR"
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            interactive = cli.render_run_pause_notice(
                self.USAGE_LIMIT_BLOCKER,
                stream=FakeStream(tty=True),
            )
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            append_only = cli.render_run_pause_notice(
                self.USAGE_LIMIT_BLOCKER,
                stream=FakeStream(tty=False),
            )

        for notice in (interactive, append_only):
            self.assertIn(statusui.RUN_PAUSED_LABEL, notice)
            self.assertIn(RunWideBlockerKind.USAGE_LIMIT.value, notice)
            self.assertIn(self.USAGE_LIMIT_BLOCKER.summary, notice)
            for issue_status in ("BLOCKED", "FAILED", "PASS", "FAIL"):
                self.assertNotIn(issue_status, notice)
        self.assertNotEqual(interactive, append_only)
        self.assertIn("\x1b[", interactive)
        self.assertEqual(
            append_only,
            f"{statusui.RUN_PAUSED_LABEL} · "
            f"{RunWideBlockerKind.USAGE_LIMIT.value} · "
            f"{self.USAGE_LIMIT_BLOCKER.summary}",
        )

    def test_every_claude_blocker_kind_is_announced_without_provider_payload(
        self,
    ) -> None:
        for kind, summary in claude_code.CLAUDE_RUN_WIDE_BLOCKER_SUMMARIES.items():
            with self.subTest(kind=kind):
                notice = cli.render_run_pause_notice(
                    RunWideBlocker(kind=kind, summary=summary),
                    stream=FakeStream(tty=False),
                )

                self.assertIn(statusui.RUN_PAUSED_LABEL, notice)
                self.assertIn(kind.value, notice)
                self.assertIn("rerun the same command", notice)


if __name__ == "__main__":
    unittest.main()
