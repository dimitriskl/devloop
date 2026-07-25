"""The Claude Code Backend: one Execution Backend built on the installed Claude CLI.

Everything Claude-specific about running a Workflow Step attempt lives here: the
``claude -p`` command construction and the isolation that makes a Workflow Step
reproducible, the streaming loop that consumes the CLI's ``stream-json`` event
stream under the Execution Budget, the translation of that vocabulary into
neutral step activity, the recognition of a Permission Denial, and the recovery
of the structured role result from the terminal result.

The invocation this module builds is a decision established by prototype against
the installed CLI, not one shape among equals. Each non-obvious element carries
the finding that put it there, because a reader would otherwise reasonably remove
it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from ..redaction import redact_persisted_evidence
from ..statusui import Stage, WaitingIndicator
from ..subprocess_utils import (
    EXECUTION_BUDGET_EXPIRY_RETURNCODE,
    ProcessExecutionBudget,
    process_tree_creation_kwargs,
    reap_process_after_terminal_event,
    register_process_tree,
    terminate_process,
    unregister_process_tree,
)
from ..terminal_text import compact_terminal_text
from .activity import ActivityCallback, StepActivityEvent, StepActivityKind
from .backend import (
    ExecutionBackend,
    ExecutionBackendId,
    RefusalRecord,
    StepAttemptRequest,
    StepAttemptResult,
    StepSettingsAuthorization,
    describe_refusals,
)
from .checkpoint import update_checkpoint_for_step_activity
from .claude_catalog import ClaudeModelCatalogAdapter
from .process_stream import (
    drain_process_stream,
    print_step_activity,
    write_process_input,
)
from .structured_result import extract_json_object

if TYPE_CHECKING:
    from ..model_catalog import ModelCatalog
    from ..portable_workflow import ExecutionBudget, StepExecutionSettings


CLAUDE_CLI_COMMAND = "claude"
# `stream-json` is the only output format that reports progress while the attempt
# runs, and the CLI refuses it under `--print` unless `--verbose` accompanies it.
CLAUDE_STREAM_JSON_OUTPUT_FORMAT = "stream-json"
# The bundled role result schema cannot be supplied verbatim: the CLI rejects the
# JSON Schema draft declaration key. With the key removed the structured result
# validates and the existing lenient role-result parser handles it unchanged.
JSON_SCHEMA_DRAFT_DECLARATION_KEY = "$schema"
# The tool the CLI uses to submit the structured result named by `--json-schema`.
CLAUDE_STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
CLAUDE_TERMINAL_RESULT_FAILURE_RETURNCODE = 1
# The one terminal reason that means the attempt ran to its own conclusion.
# Anything else the provider reports is a failure, even when it arrives with no
# error flag and a success subtype. The field is treated as absent-means-completed
# rather than absent-means-failed, because failing every attempt a future CLI
# stops annotating would be worse than trusting its error flag.
CLAUDE_COMPLETED_TERMINAL_REASON = "completed"
CLAUDE_TERMINAL_REASON_KEY = "terminal_reason"
CLAUDE_PERMISSION_DENIALS_KEY = "permission_denials"
CLAUDE_DENIED_TOOL_NAME_KEY = "tool_name"
UNNAMED_DENIED_TOOL = "an unnamed tool"
# Chain of thought is elided from the durable transcript rather than persisted
# verbatim. The event, its type and every other field stay exactly as recorded,
# so the log remains a faithful and parseable record of what the attempt did.
CLAUDE_REASONING_TEXT_KEY = "thinking"
CLAUDE_REASONING_SIGNATURE_KEY = "signature"
CLAUDE_REASONING_TEXT_REDACTION = "[reasoning redacted: {characters} characters]"
CLAUDE_REASONING_SIGNATURE_REDACTION = "[reasoning signature redacted]"
STREAM_THREAD_JOIN_SECONDS = 1.0
MAX_ACTIVITY_TEXT_LENGTH = 240

_ClosedValueT = TypeVar("_ClosedValueT", bound=Enum)


class ClaudeEventType(str, Enum):
    """The closed set of top-level `stream-json` event types Dev Loop reads."""

    SYSTEM = "system"
    ASSISTANT = "assistant"
    USER = "user"
    RATE_LIMIT = "rate_limit_event"
    RESULT = "result"


class ClaudeSystemSubtype(str, Enum):
    """The closed set of `system` event subtypes the stream carries."""

    INIT = "init"
    THINKING_TOKENS = "thinking_tokens"
    HOOK_STARTED = "hook_started"
    HOOK_RESPONSE = "hook_response"
    HOOK_PROGRESS = "hook_progress"


class ClaudeContentBlockType(str, Enum):
    """The closed set of message content blocks the stream carries."""

    THINKING = "thinking"
    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"


class ClaudePermissionMode(str, Enum):
    """The closed set of permission modes the CLI accepts."""

    DEFAULT = "default"
    PLAN = "plan"
    ACCEPT_EDITS = "acceptEdits"
    AUTO = "auto"
    DONT_ASK = "dontAsk"
    BYPASS_PERMISSIONS = "bypassPermissions"


class ClaudeSettingSource(str, Enum):
    """The closed set of settings sources the CLI can be told to load."""

    USER = "user"
    PROJECT = "project"
    LOCAL = "local"


# Permissions are bypassed because every less permissive mode was found to fail
# dishonestly rather than merely strictly: a denied shell command produced no
# error flag, a success subtype, a completed terminal reason and a zero exit
# code, and two of the three modes then asserted the command had run. Only the
# terminal result's denial record contradicted them. A stricter mode is not a
# safer mode here.
CLAUDE_PERMISSION_MODE = ClaudePermissionMode.BYPASS_PERMISSIONS
# Isolating settings sources is correctness, not hygiene. Left alone, the
# operator's personal hooks execute inside every Workflow Step attempt, their
# output style applies, and their personal MCP servers load — observed in a
# prototype run in a directory holding no project configuration at all. Omitting
# the user source drops all of that while the target repository's own project and
# local settings still load, which matches the Codex CLI Backend reading the
# repository's agent instructions.
CLAUDE_SETTING_SOURCES = (ClaudeSettingSource.PROJECT, ClaudeSettingSource.LOCAL)
CLAUDE_SETTING_SOURCES_ARGUMENT = ",".join(
    source.value for source in CLAUDE_SETTING_SOURCES
)


def resolve_claude_executable(claude: str) -> str:
    """Resolve a Claude command name to a concrete executable path.

    ``subprocess`` with ``shell=False`` invokes Win32 ``CreateProcess``, which
    does not consult ``PATHEXT``, so a bare ``"claude"`` fails with WinError 2
    whenever the CLI is installed as a shim rather than an ``.exe``.
    ``shutil.which`` does honour ``PATHEXT``, so resolving up front fixes Windows
    while staying a no-op on POSIX. If resolution fails, return the original
    value so the downstream error still names what the user asked for.
    """
    resolved = shutil.which(claude)
    if resolved:
        return resolved

    candidate = Path(claude).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())

    if candidate.name != claude:
        return claude

    install_dirs = [Path.home() / ".local" / "bin"]
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if appdata:
            install_dirs.append(Path(appdata) / "npm")
        install_dirs.append(Path.home() / "AppData" / "Roaming" / "npm")
        suffixes = (".exe", ".cmd", "")
    else:
        suffixes = ("",)
    for install_dir in install_dirs:
        for suffix in suffixes:
            install_candidate = install_dir / f"{claude}{suffix}"
            if install_candidate.is_file():
                return str(install_candidate.resolve())

    return claude


def new_attempt_session_id() -> str:
    """A fresh session identity for one Workflow Step attempt.

    The CLI requires a UUID, and one attempt is one session: reusing an identity
    across attempts would ask the provider to resume a conversation the runner
    has already recorded as finished.
    """
    return str(uuid.uuid4())


def claude_json_schema_argument(schema_path: Path) -> str:
    """Render the bundled role result schema as the CLI's schema argument.

    The option takes the schema itself rather than a path, and rejects the JSON
    Schema draft declaration key, so the bundled document is parsed, stripped of
    that one key, and supplied inline. The bundled file is shared with the Codex
    CLI Backend and is never rewritten.
    """
    raw = schema_path.read_text(encoding="utf-8")
    try:
        schema = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"The role result schema at {schema_path} is not valid JSON."
        ) from error
    if not isinstance(schema, dict):
        raise ValueError(
            f"The role result schema at {schema_path} must be a JSON object."
        )
    schema.pop(JSON_SCHEMA_DRAFT_DECLARATION_KEY, None)
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))


def claude_execution_settings_args(settings: StepExecutionSettings) -> list[str]:
    """Render one Workflow Step's settings as Claude CLI arguments.

    Backend identity is checked here, at the command-line boundary, so settings
    naming another Execution Backend can never be turned into a Claude
    invocation. Fast is not rendered at all: the Claude Code Backend advertises
    none, and Step Execution Settings already refuse to hold Fast ON for it.
    """
    if settings.backend is not ExecutionBackendId.CLAUDE_CODE:
        raise ValueError(
            "The Claude Code Backend cannot run Step Execution Settings naming "
            f"the {settings.backend.display_name} Backend."
        )
    return [
        "--model",
        settings.model,
        "--effort",
        settings.reasoning_effort,
    ]


def build_claude_command(
    claude: str,
    *,
    schema_path: Path,
    session_id: str,
    execution_settings: StepExecutionSettings | None = None,
) -> list[str]:
    """Build the one invocation the Claude Code Backend runs.

    The prompt is deliberately absent: it is supplied on standard input, exactly
    as the Codex invocation supplies it. That is required rather than stylistic —
    several of this CLI's options accept multiple values and silently consume a
    positional prompt argument, leaving the run to fail with a missing-input
    error. The working directory is the repository root the runner selected,
    which is the dedicated implementation worktree when one is in use.
    """
    command = [
        claude,
        "-p",
        "--output-format",
        CLAUDE_STREAM_JSON_OUTPUT_FORMAT,
        "--verbose",
    ]
    if execution_settings is not None:
        command.extend(claude_execution_settings_args(execution_settings))
    command.extend(
        [
            "--json-schema",
            claude_json_schema_argument(schema_path),
            "--permission-mode",
            CLAUDE_PERMISSION_MODE.value,
            "--setting-sources",
            CLAUDE_SETTING_SOURCES_ARGUMENT,
            "--strict-mcp-config",
            "--session-id",
            session_id,
        ]
    )
    return command


def parse_claude_event(line: str) -> dict[str, Any] | None:
    """Parse one `stream-json` line, or report that it carries no event."""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def claude_step_activity_events(
    payload: dict[str, Any] | None,
) -> tuple[StepActivityEvent, ...]:
    """Translate one Claude stream event into neutral step activity.

    This is the single translation point out of the Claude event vocabulary, so
    neither the Portable Activity Feed nor Execution Budget checkpointing reads
    this provider's wire format. One event can carry several content blocks, so
    the translation returns every activity that event produced, in order.

    Hook events are deliberately ignored: under this backend's settings
    isolation no hook fires, and translating one would give an operator's
    personal hooks a voice inside a Workflow Step attempt.
    """
    if payload is None:
        return ()
    event_type = _closed_value(ClaudeEventType, payload.get("type"))
    if event_type is ClaudeEventType.SYSTEM:
        return _system_activity(payload)
    if event_type is ClaudeEventType.ASSISTANT:
        return _assistant_activity(payload)
    if event_type is ClaudeEventType.USER:
        return _tool_result_activity(payload)
    if event_type is ClaudeEventType.RATE_LIMIT:
        return (
            StepActivityEvent(
                kind=StepActivityKind.RATE_LIMIT,
                activity=_rate_limit_activity(payload),
            ),
        )
    if event_type is ClaudeEventType.RESULT:
        return _terminal_result_activities(payload)
    return ()


def claude_terminal_result(stdout: str) -> dict[str, Any] | None:
    """The terminal result event a captured attempt transcript ended with."""
    terminal_result: dict[str, Any] | None = None
    for line in stdout.splitlines():
        payload = parse_claude_event(line)
        if payload is not None and _is_terminal_result(payload):
            terminal_result = payload
    return terminal_result


def claude_permission_denials(
    terminal_result: dict[str, Any] | None,
) -> tuple[RefusalRecord, ...]:
    """The tool-permission denials the terminal result recorded.

    This is the provider boundary at which a denial becomes a domain Permission
    Denial, so the recorded tool name passes through the Redaction Service before
    any of it can reach a persisted summary or a durable log. The denied tool
    input is deliberately not carried: the summary needs the tool and the count,
    and the full record stays in the durable transcript.
    """
    if terminal_result is None:
        return ()
    denials = terminal_result.get(CLAUDE_PERMISSION_DENIALS_KEY)
    if not isinstance(denials, list):
        return ()
    records: list[RefusalRecord] = []
    for denial in denials:
        if not isinstance(denial, dict):
            continue
        tool_name = denial.get(CLAUDE_DENIED_TOOL_NAME_KEY)
        target = tool_name.strip() if isinstance(tool_name, str) else ""
        records.append(
            RefusalRecord(
                target=redact_persisted_evidence(target) or UNNAMED_DENIED_TOOL
            )
        )
    return tuple(records)


def claude_terminal_result_completed(terminal_result: dict[str, Any]) -> bool:
    """Whether the terminal result reports the attempt reaching completion."""
    if terminal_result.get("is_error") is True:
        return False
    terminal_reason = terminal_result.get(CLAUDE_TERMINAL_REASON_KEY)
    if not isinstance(terminal_reason, str):
        return True
    return terminal_reason.strip() == CLAUDE_COMPLETED_TERMINAL_REASON


def claude_failure_summary(terminal_result: dict[str, Any] | None) -> str:
    """The provider's own words for a terminal result that did not complete.

    Returned only for a result the provider itself reports as an error or as
    ending for a reason other than completion; a completed result has no failure
    to describe. The text is redacted because it is provider output on its way
    into a persisted Step Outcome summary.
    """
    if terminal_result is None or claude_terminal_result_completed(terminal_result):
        return ""
    return redact_persisted_evidence(_claude_result_text(terminal_result).strip())


def redact_claude_reasoning(stdout: str) -> str:
    """Elide chain of thought from a transcript that is about to be persisted.

    Reasoning content is never shown in the Portable Activity Feed, so persisting
    it verbatim to a durable log would contradict that in the one place an
    operator is most likely to copy from. Each ``thinking`` block keeps its event,
    its position and its type, and loses only its text and its signature — the
    two fields that carry the reasoning itself and nothing an operator can act
    on. Every other line is left byte-for-byte as recorded.
    """
    rewritten: list[str] = []
    for raw_line in stdout.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        ending = raw_line[len(line) :]
        payload = parse_claude_event(line)
        if payload is None or not _redact_reasoning_blocks(payload):
            rewritten.append(raw_line)
            continue
        rewritten.append(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ending
        )
    return "".join(rewritten)


def claude_role_message(
    terminal_result: dict[str, Any] | None,
    *,
    stdout: str,
) -> str:
    """Recover the role result the attempt returned, in documented fallback order.

    1. the terminal result's dedicated structured-output field, which is where a
       schema-validated result arrives;
    2. its result text, which carries the same JSON when structured output was
       not populated;
    3. the existing lenient extraction, applied to the assistant messages in the
       transcript and then to the whole transcript, which recovers a role result
       embedded in prose.

    When none of those yields a JSON object the provider's own result text is
    returned unchanged, so the role result parser reports the refusal in the
    provider's words instead of Dev Loop's paraphrase.
    """
    result_text = _claude_result_text(terminal_result)
    for candidate in (
        _structured_output_message(terminal_result),
        result_text,
        _last_structured_assistant_message(stdout),
        stdout,
    ):
        if candidate and extract_json_object(candidate) is not None:
            return candidate
    return result_text or stdout


def run_streaming_claude_command(
    command: list[str],
    *,
    input_text: str,
    cwd: Path,
    stage: Stage,
    activity_context: str = "",
    activity_callback: ActivityCallback | None = None,
    execution_budget: ExecutionBudget | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one Claude attempt, reporting its stream as neutral step activity.

    The Execution Budget is enforced through the same mechanism the Codex CLI
    Backend uses: one watcher holding a hard deadline from attempt start and an
    inactivity checkpoint, terminating the process tree on expiry and annotating
    the attempt's diagnostics with which limit expired. Every stream line counts
    as activity, so this provider's dense reasoning heartbeat keeps a working
    attempt alive without any of it having to be displayable.
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **process_tree_creation_kwargs(),
    )
    register_process_tree(process)
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    indicator = (
        WaitingIndicator(stage=stage, context=activity_context)
        if activity_callback is None
        else None
    )
    budget = (
        ProcessExecutionBudget(
            process,
            timeout_seconds=execution_budget.timeout_seconds,
            checkpoint_seconds=execution_budget.checkpoint_seconds,
        )
        if execution_budget is not None
        else None
    )

    def notify_activity(event: StepActivityEvent | None) -> None:
        if budget is not None:
            budget.notify_activity()
        if activity_callback is not None:
            activity_callback(event)
            return
        assert indicator is not None
        indicator.notify_activity()
        if event is not None and event.activity:
            indicator.stop()
            print_step_activity(stage, activity_context, event)
            indicator.start()

    input_thread = threading.Thread(
        target=write_process_input,
        args=(process.stdin, input_text),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=drain_process_stream,
        args=(process.stderr, stderr_parts, notify_activity),
        daemon=True,
    )
    terminal_result: dict[str, Any] | None = None
    active_tools: set[str] = set()

    if indicator is not None:
        indicator.start()
    input_thread.start()
    stderr_thread.start()
    if budget is not None:
        budget.start()
    budget_expiration: str | None = None
    budget_finished = False
    try:
        for line in process.stdout:
            if budget is not None:
                budget.notify_activity()
            stdout_parts.append(line)
            payload = parse_claude_event(line)
            events = claude_step_activity_events(payload)
            if not events:
                # Every stream line is progress even when it has no activity to
                # report, so an ignored event can never look like a hang.
                notify_activity(None)
            for event in events:
                update_checkpoint_for_step_activity(budget, event, active_tools)
                notify_activity(event)
            if payload is not None and _is_terminal_result(payload):
                terminal_result = payload
                break

        if terminal_result is None:
            returncode = process.wait()
        else:
            if budget is not None:
                budget_expiration = budget.finish()
                budget_finished = True
            reap_process_after_terminal_event(process)
            returncode = _terminal_returncode(terminal_result, process.returncode)
    except KeyboardInterrupt:
        terminate_process(process)
        raise
    finally:
        if budget is not None and not budget_finished:
            budget_expiration = budget.finish()
        if indicator is not None:
            indicator.stop()
        input_thread.join(timeout=STREAM_THREAD_JOIN_SECONDS)
        stderr_thread.join(timeout=STREAM_THREAD_JOIN_SECONDS)
        for stream in (process.stdout, process.stderr):
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        unregister_process_tree(process)

    if budget_expiration is not None:
        stderr_parts.append(f"{budget_expiration}\n")
        returncode = EXECUTION_BUDGET_EXPIRY_RETURNCODE

    return subprocess.CompletedProcess(
        command,
        returncode,
        stdout="".join(stdout_parts),
        stderr="".join(stderr_parts),
    )


@dataclass(frozen=True)
class ClaudeCodeExecutionBackend(ExecutionBackend):
    """The Execution Backend that delegates agent runs to the installed Claude CLI."""

    claude: str = CLAUDE_CLI_COMMAND

    @classmethod
    def resolved(cls, claude: str = CLAUDE_CLI_COMMAND) -> ClaudeCodeExecutionBackend:
        """Build the backend with the Claude command resolved to an executable."""
        return cls(claude=resolve_claude_executable(claude))

    @property
    def backend_id(self) -> ExecutionBackendId:
        return ExecutionBackendId.CLAUDE_CODE

    def invoke(self, request: StepAttemptRequest) -> StepAttemptResult:
        """Run one Workflow Step attempt and recover its structured role result.

        The terminal result is read before the transcript is handed back, because
        the transcript handed back is the redacted one: chain of thought is elided
        from what gets persisted, while classification still sees the recording
        exactly as the provider produced it.

        No Run-Wide Blocker is reported yet: classifying exhausted usage,
        invalid authentication and service outages from this provider's API
        status and rate-limit event is separate work.
        """
        command = build_claude_command(
            self.claude,
            schema_path=request.schema_path,
            session_id=new_attempt_session_id(),
            execution_settings=request.execution_settings,
        )
        process = run_streaming_claude_command(
            command,
            input_text=request.prompt,
            cwd=request.repo_root,
            stage=request.activity_stage,
            activity_context=request.activity_context,
            activity_callback=request.activity_callback,
            execution_budget=request.execution_budget,
        )
        terminal_result = claude_terminal_result(process.stdout)
        refusals = claude_permission_denials(terminal_result)
        failure_summary = claude_failure_summary(terminal_result)
        message = (
            ""
            if process.returncode != 0
            else claude_role_message(terminal_result, stdout=process.stdout)
        )
        process.stdout = redact_claude_reasoning(process.stdout)
        if message:
            # The CLI writes no last-message file of its own, so the recovered
            # role result is persisted through the runner's confined log writer,
            # in the same place and naming scheme as a Codex attempt's.
            request.write_log(request.message_path, message)
        return StepAttemptResult(
            process=process,
            message=message,
            refusals=refusals,
            failure_summary=failure_summary,
        )

    @property
    def provider_command(self) -> str:
        return self.claude

    def discover_model_catalog(self, *, cwd: Path) -> ModelCatalog:
        """Return the bundled Claude catalog as live, at no verification cost.

        Browsing must stay free: the executable is resolved and the bundled
        entries are returned. Nothing here calls the provider, so opening
        `/options` for a Claude-backed Workflow Step costs one path lookup and
        one file read however many models the bundle lists.
        """
        return ClaudeModelCatalogAdapter(self.claude, cwd=cwd).discover()

    def verify_selected_model(self, model_id: str, *, cwd: Path) -> str:
        """Verify one selected model and return the identifier to persist.

        This is the one call a selection costs. A short alias resolves to the
        concrete pinned identifier the session-initialisation event reports, and
        that identifier — never the alias — is what the caller saves, so
        rerunning a Workflow Run cannot silently change which model works.
        """
        return ClaudeModelCatalogAdapter(self.claude, cwd=cwd).verify(model_id)

    def authorize_execution_settings(
        self,
        authorizations: Sequence[StepSettingsAuthorization],
        *,
        model_catalog: ModelCatalog,
    ) -> None:
        raise NotImplementedError(
            "The Claude Code Backend cannot authorize a run yet. Verifying each "
            "selected Claude model against the operator's own account arrives "
            "with Claude-backed run preflight."
        )


def _system_activity(payload: dict[str, Any]) -> tuple[StepActivityEvent, ...]:
    subtype = _closed_value(ClaudeSystemSubtype, payload.get("subtype"))
    if subtype is ClaudeSystemSubtype.INIT:
        model = payload.get("model")
        detail = (
            f" on model {_compact(model)}"
            if isinstance(model, str) and model.strip()
            else ""
        )
        return (
            StepActivityEvent(
                kind=StepActivityKind.MESSAGE,
                activity=f"Claude Code session started{detail}.",
            ),
        )
    if subtype is ClaudeSystemSubtype.THINKING_TOKENS:
        # A dense reasoning heartbeat: strong evidence that the attempt is
        # working, and nothing worth printing on every batch of tokens.
        return (StepActivityEvent(kind=StepActivityKind.REASONING),)
    return ()


def _assistant_activity(payload: dict[str, Any]) -> tuple[StepActivityEvent, ...]:
    events: list[StepActivityEvent] = []
    for block in _content_blocks(payload):
        block_type = _closed_value(ClaudeContentBlockType, block.get("type"))
        if block_type is ClaudeContentBlockType.THINKING:
            # Never surface raw reasoning content.
            events.append(
                StepActivityEvent(
                    kind=StepActivityKind.REASONING,
                    activity="Claude is reasoning about the task.",
                )
            )
        elif block_type is ClaudeContentBlockType.TOOL_USE:
            events.append(_tool_use_activity(block))
        elif block_type is ClaudeContentBlockType.TEXT:
            message = _text_activity(block)
            if message is not None:
                events.append(message)
    return tuple(events)


def _tool_result_activity(payload: dict[str, Any]) -> tuple[StepActivityEvent, ...]:
    """Translate the tool results a `user` event carries back to the agent.

    A `user` event's plain text blocks are inputs to the agent rather than the
    agent's own progress, so they are deliberately ignored.
    """
    events: list[StepActivityEvent] = []
    for block in _content_blocks(payload):
        if (
            _closed_value(ClaudeContentBlockType, block.get("type"))
            is not ClaudeContentBlockType.TOOL_RESULT
        ):
            continue
        failed = block.get("is_error") is True
        activity = "A tool call failed." if failed else "Tool call finished."
        events.append(_tool_activity(activity, block.get("tool_use_id"), completed=True))
    return tuple(events)


def _tool_use_activity(block: dict[str, Any]) -> StepActivityEvent:
    name = block.get("name")
    tool_name = name.strip() if isinstance(name, str) else ""
    if tool_name == CLAUDE_STRUCTURED_OUTPUT_TOOL:
        activity = "Returning the structured role result."
    elif tool_name:
        activity = f"Using the {_compact(tool_name)} tool."
    else:
        activity = "Using a tool."
    return _tool_activity(activity, block.get("id"), completed=False)


def _tool_activity(
    activity: str,
    tool_use_id: Any,
    *,
    completed: bool,
) -> StepActivityEvent:
    """One half of a tool-activity pair, keyed by its tool-use identity.

    Without that identity the inactivity checkpoint could never pair the start
    with the completion, so the activity is reported as a plain message rather
    than as a tool the checkpoint would wait on forever.
    """
    tool_key = tool_use_id.strip() if isinstance(tool_use_id, str) else ""
    if not tool_key:
        return StepActivityEvent(kind=StepActivityKind.MESSAGE, activity=activity)
    return StepActivityEvent(
        kind=(
            StepActivityKind.TOOL_COMPLETED
            if completed
            else StepActivityKind.TOOL_STARTED
        ),
        activity=activity,
        tool_key=tool_key,
    )


def _text_activity(block: dict[str, Any]) -> StepActivityEvent | None:
    text = block.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if extract_json_object(text) is not None:
        return StepActivityEvent(
            kind=StepActivityKind.MESSAGE,
            activity="Structured role result received.",
        )
    return StepActivityEvent(
        kind=StepActivityKind.MESSAGE,
        activity=_compact(text),
    )


def _rate_limit_activity(payload: dict[str, Any]) -> str:
    info = payload.get("rate_limit_info")
    if not isinstance(info, dict):
        return "Claude reported a rate limit update."
    status = info.get("status")
    window = info.get("rateLimitType")
    if not isinstance(status, str) or not status.strip():
        return "Claude reported a rate limit update."
    detail = (
        f" for the {_compact(window)} window"
        if isinstance(window, str) and window.strip()
        else ""
    )
    return f"Claude reported rate limit status {_compact(status)}{detail}."


def _terminal_result_activities(
    payload: dict[str, Any],
) -> tuple[StepActivityEvent, ...]:
    """Report a Permission Denial live, ahead of how the turn itself ended.

    A denial is the one condition this provider reports without any other signal
    changing, so it gets its own activity kind and is reported before the turn
    outcome. An operator watching the Portable Activity Feed then sees that the
    attempt was prevented from working at the moment it is known, rather than
    only once the Step Outcome is published.
    """
    denials = claude_permission_denials(payload)
    if not denials:
        return (_terminal_result_activity(payload),)
    return (
        StepActivityEvent(
            kind=StepActivityKind.PERMISSION_DENIED,
            activity=(
                f"Claude was denied {describe_refusals(denials)}; "
                "its result cannot be trusted."
            ),
        ),
        _terminal_result_activity(payload),
    )


def _terminal_result_activity(payload: dict[str, Any]) -> StepActivityEvent:
    if claude_terminal_result_completed(payload):
        return StepActivityEvent(kind=StepActivityKind.TURN_COMPLETED)
    result_text = _claude_result_text(payload)
    activity = (
        f"Claude reported an error: {_compact(result_text)}"
        if result_text
        else "Claude reported an error."
    )
    return StepActivityEvent(kind=StepActivityKind.ERROR, activity=activity)


def _redact_reasoning_blocks(payload: dict[str, Any]) -> bool:
    """Elide every reasoning block in one event, reporting whether any changed."""
    redacted = False
    for block in _content_blocks(payload):
        if (
            _closed_value(ClaudeContentBlockType, block.get("type"))
            is not ClaudeContentBlockType.THINKING
        ):
            continue
        reasoning = block.get(CLAUDE_REASONING_TEXT_KEY)
        if isinstance(reasoning, str):
            block[CLAUDE_REASONING_TEXT_KEY] = CLAUDE_REASONING_TEXT_REDACTION.format(
                characters=len(reasoning)
            )
            redacted = True
        if isinstance(block.get(CLAUDE_REASONING_SIGNATURE_KEY), str):
            block[CLAUDE_REASONING_SIGNATURE_KEY] = (
                CLAUDE_REASONING_SIGNATURE_REDACTION
            )
            redacted = True
    return redacted


def _structured_output_message(terminal_result: dict[str, Any] | None) -> str:
    if terminal_result is None:
        return ""
    structured_output = terminal_result.get("structured_output")
    if not isinstance(structured_output, dict) or not structured_output:
        return ""
    return json.dumps(structured_output, ensure_ascii=False, indent=2)


def _last_structured_assistant_message(stdout: str) -> str:
    last_message = ""
    for line in stdout.splitlines():
        payload = parse_claude_event(line)
        if payload is None:
            continue
        if (
            _closed_value(ClaudeEventType, payload.get("type"))
            is not ClaudeEventType.ASSISTANT
        ):
            continue
        for block in _content_blocks(payload):
            if (
                _closed_value(ClaudeContentBlockType, block.get("type"))
                is not ClaudeContentBlockType.TEXT
            ):
                continue
            text = block.get("text")
            if isinstance(text, str) and extract_json_object(text) is not None:
                last_message = text
    return last_message


def _claude_result_text(terminal_result: dict[str, Any] | None) -> str:
    if terminal_result is None:
        return ""
    result_text = terminal_result.get("result")
    return result_text if isinstance(result_text, str) else ""


def _content_blocks(payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    message = payload.get("message")
    if not isinstance(message, dict):
        return ()
    content = message.get("content")
    if not isinstance(content, list):
        return ()
    return tuple(block for block in content if isinstance(block, dict))


def _is_terminal_result(payload: dict[str, Any]) -> bool:
    return (
        _closed_value(ClaudeEventType, payload.get("type")) is ClaudeEventType.RESULT
    )


def _terminal_returncode(
    terminal_result: dict[str, Any],
    process_returncode: int | None,
) -> int:
    """The exit status one terminal result deserves, ignoring what the CLI said.

    A terminal reason other than completion is a failure whatever the process
    exit code was, because the prototype established that this CLI reports a zero
    exit code for work it did not do.
    """
    if claude_terminal_result_completed(terminal_result):
        return 0
    if isinstance(process_returncode, int) and process_returncode != 0:
        return process_returncode
    return CLAUDE_TERMINAL_RESULT_FAILURE_RETURNCODE


def _compact(text: Any) -> str:
    return compact_terminal_text(str(text), max_length=MAX_ACTIVITY_TEXT_LENGTH)


def _closed_value(enum_type: type[_ClosedValueT], value: Any) -> _ClosedValueT | None:
    """Parse a provider-supplied string into one of its closed sets, or ``None``.

    An unrecognised value is not an error: the provider may add an event type or
    a content block Dev Loop does not translate, and an untranslated event is
    reported as progress rather than crashing an attempt.
    """
    if not isinstance(value, str):
        return None
    try:
        return enum_type(value)
    except ValueError:
        return None
