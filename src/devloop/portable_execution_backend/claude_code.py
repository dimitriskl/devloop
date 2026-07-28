"""The Claude Code Backend: one Execution Backend built on the installed Claude CLI.

Everything Claude-specific about running a Workflow Step attempt lives here: the
``claude -p`` command construction and the isolation that makes a Workflow Step
reproducible, the streaming loop that consumes the CLI's ``stream-json`` event
stream under the Execution Budget, the translation of that vocabulary into
neutral step activity, the recognition of a Permission Denial, the recovery of the
structured role result from the terminal result, and the classification of this
provider's Run-Wide Blockers and its transient retryable failures.

The invocation this module builds is a decision established by prototype against
the installed CLI, not one shape among equals. Each non-obvious element carries
the finding that put it there, because a reader would otherwise reasonably remove
it.
"""

from __future__ import annotations

import json
import os
import re
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
from ..step_configuration import StepAttemptProvenance
from ..subprocess_utils import (
    EXECUTION_BUDGET_EXPIRY_RETURNCODE,
    AttemptExecutionBudget,
    ProcessExecutionBudget,
    process_tree_creation_kwargs,
    reap_process_after_terminal_event,
    register_process_tree,
    release_process_tree_if_stopped,
    terminate_process,
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
    report_model_mismatch,
)
from .blockers import RunWideBlocker, RunWideBlockerKind, RunWideBlockerPolicy
from .checkpoint import update_checkpoint_for_step_activity
from .claude_catalog import (
    ClaudeModelCatalogAdapter,
    ModelVerificationError,
    ModelVerificationFailure,
    VerificationSessionFactory,
)
from .process_stream import (
    drain_process_stream,
    print_step_activity,
    write_process_input,
)
from .structured_result import extract_json_object
from .transient_retry import (
    TRANSIENT_RETRY_DELAY_SECONDS,
    run_attempt_with_transient_retries,
)

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
# Where the provider records the HTTP status of the API call that ended the turn.
# It is ``null`` on every recorded successful attempt, so a present status is the
# provider's own statement that the call itself failed.
CLAUDE_API_ERROR_STATUS_KEY = "api_error_status"
# The rate-limit event's payload, and the field inside it that states the
# disposition rather than merely describing the window.
CLAUDE_RATE_LIMIT_INFO_KEY = "rate_limit_info"
CLAUDE_RATE_LIMIT_STATUS_KEY = "status"
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
# Where the provider accounts the finished turn's own token spend, keyed by the
# model that served it. This is the only field that states which model did the
# work, and a committed recording has it disagreeing with the model the
# session-initialisation event reported — which is the disagreement the Step
# Attempt Record exists to make visible. The initialisation event is deliberately
# not read here: it is what a model selection was verified and pinned from, so
# reporting it as the serving model would guarantee agreement with itself.
CLAUDE_MODEL_USAGE_KEY = "modelUsage"
# How the turn's cost and its number of turns are reported. Both are evidence
# only; the Execution Budget is time-based and neither value bounds anything.
CLAUDE_TOTAL_COST_KEY = "total_cost_usd"
CLAUDE_TURN_COUNT_KEY = "num_turns"
# How several accounted models become one serving-model identifier. A turn
# accounted against more than one model was not served by the single requested
# one, so every identifier is kept and the result still reads as a mismatch.
CLAUDE_SERVING_MODEL_SEPARATOR = ", "
MAX_SERVING_MODEL_LENGTH = 200
# Chain of thought is elided from the durable transcript rather than persisted
# verbatim. The event, its type and every other field stay exactly as recorded,
# so the log remains a faithful and parseable record of what the attempt did.
CLAUDE_REASONING_TEXT_KEY = "thinking"
CLAUDE_REASONING_SIGNATURE_KEY = "signature"
CLAUDE_REASONING_TEXT_REDACTION = "[reasoning redacted: {characters} characters]"
CLAUDE_REASONING_SIGNATURE_REDACTION = "[reasoning signature redacted]"
STREAM_THREAD_JOIN_SECONDS = 1.0
MAX_ACTIVITY_TEXT_LENGTH = 240
# A quoted provider reason is composed into Dev Loop's own sentences, and the
# provider does not always end its text. Without these the remedy ran straight on
# from the provider's last word, so a preflight refusal read as one broken
# sentence.
SENTENCE_ENDINGS = ".!?"
# The remedy each cause of a failed verification actually supports. Neither may
# borrow the other's: an operator whose CLI never started cannot repair anything
# by choosing another model, because every model fails identically until the CLI
# runs.
UNREACHABLE_PROVIDER_REMEDY = (
    "Install or repair the Claude CLI, or choose a different Execution Backend "
    "for that Workflow Step in /options."
)
REFUSED_MODEL_REMEDY = (
    "Choose a model this account can use for that Workflow Step in /options."
)

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


class ClaudeApiErrorStatus(int, Enum):
    """The closed set of individual API statuses this backend classifies.

    Server errors are deliberately not members. They are a whole status class
    rather than a handful of codes, and any provider is free to answer with one
    this set had never heard of, so they are recognised by range instead of being
    enumerated incompletely.
    """

    UNAUTHORIZED = 401
    FORBIDDEN = 403
    NOT_FOUND = 404
    RATE_LIMITED = 429


class ClaudeRateLimitStatus(str, Enum):
    """The closed set of dispositions the stream's own rate-limit event carries."""

    ALLOWED = "allowed"
    ALLOWED_WARNING = "allowed_warning"
    REJECTED = "rejected"


# Every status the provider answers with in this range is a fault on its side that
# no Issue caused and no Issue can avoid.
HTTP_SERVER_ERROR_STATUS_RANGE = range(500, 600)
# The one rate-limit disposition that means the account can spend nothing further.
# The other two accompany work that ran, and the committed recording of a healthy
# attempt carries this event with an allowed status — so the event's presence
# cannot be the signal, or every successful attempt would pause the run.
CLAUDE_EXHAUSTED_RATE_LIMIT_STATUS = ClaudeRateLimitStatus.REJECTED
# Which Run-Wide Blocker each individually classified API status is evidence of.
# 401 and 403 both mean the credentials this machine holds do not authorise the
# call; 429 is the provider stating the account's usage is spent; 404 for a model
# that run preflight verified against this same account means its access was
# withdrawn while the run was in flight.
CLAUDE_API_ERROR_BLOCKER_KINDS = {
    ClaudeApiErrorStatus.UNAUTHORIZED: RunWideBlockerKind.AUTHENTICATION,
    ClaudeApiErrorStatus.FORBIDDEN: RunWideBlockerKind.AUTHENTICATION,
    ClaudeApiErrorStatus.RATE_LIMITED: RunWideBlockerKind.USAGE_LIMIT,
    ClaudeApiErrorStatus.NOT_FOUND: RunWideBlockerKind.MODEL_ACCESS_WITHDRAWN,
}
# Dev Loop's own words for each classified condition, and the only text a pause
# ever shows. No provider payload is interpolated into any of them, so a pause
# notice and a persisted pause reason can carry neither a credential nor a raw
# provider message however the provider worded its refusal. Each names the remedy
# that can actually work and ends by pointing at the rerun that resumes the run.
CLAUDE_RUN_WIDE_BLOCKER_SUMMARIES = {
    RunWideBlockerKind.USAGE_LIMIT: (
        "Claude usage is exhausted. Restore usage availability, then rerun the "
        "same command."
    ),
    RunWideBlockerKind.AUTHENTICATION: (
        "Claude authentication is unavailable. Restore authentication, then "
        "rerun the same command."
    ),
    RunWideBlockerKind.SERVICE_UNAVAILABLE: (
        "The Claude service is unavailable. Wait for recovery, then rerun the "
        "same command."
    ),
    RunWideBlockerKind.MODEL_ACCESS_WITHDRAWN: (
        "This account can no longer use the Claude model this Workflow Step "
        "selects. Choose a model this account can use in /options, then rerun "
        "the same command."
    ),
}
# The transport-level failures a bounded retry can clear. Each is a condition in
# which the attempt never reached a terminal result at all, so the provider
# reported no API status and there is nothing to classify: the connection never
# opened, dropped mid-turn, or the far side answered with a server error before
# the turn began. A status that *did* reach the terminal result is the provider's
# decided answer to a call it received, and is classified rather than repeated.
CLAUDE_TRANSIENT_FAILURE_PATTERNS = (
    re.compile(
        r"\b(econnreset|econnrefused|econnaborted|etimedout|epipe|eai_again|"
        r"enotfound|eproto)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(socket hang ?up|connection (reset|refused|closed|aborted|error)|"
        r"network (error|timeout)|fetch failed|request timed out|"
        r"stream (error|closed))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(internal server error|bad gateway|service unavailable|"
        r"gateway timeout|overloaded)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:http|api error:?)\s*50[0-4]\b", re.IGNORECASE),
)
# What the operator-facing retry notice says failed. It names the invocation, not
# any captured provider text, so the notice can carry no provider payload.
CLAUDE_RETRY_SUBJECT = "claude -p connection"
# The wait between two process runs of one attempt, shared with the Codex CLI
# Backend so a transient failure costs the same everywhere.
CLAUDE_TRANSIENT_RETRY_DELAY_SECONDS = TRANSIENT_RETRY_DELAY_SECONDS


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


def claude_attempt_provenance(
    terminal_result: dict[str, Any] | None,
    *,
    requested_model: str | None = None,
) -> StepAttemptProvenance:
    """Read which model served the turn, and what it cost, from the result.

    The serving model comes from the turn's own usage accounting and from nowhere
    else. A prototype recorded that accounting naming a different model from the
    one the session-initialisation event reported, and since a model selection is
    verified and pinned from that initialisation event, this reading is the only
    thing that can ever reveal the disagreement. Falling back to the
    initialisation event when the accounting says nothing would make the two
    always agree and quietly remove the evidence.

    A turn accounted against several models keeps every identifier, joined: it was
    not served by the one model the Workflow Step requested, and reporting only
    the first would hide that.

    The identifiers are provider output on their way into a persisted attempt
    record, so they pass the Redaction Service and are bounded, exactly as a
    denied tool name is.
    """
    if terminal_result is None:
        return StepAttemptProvenance(
            backend=ExecutionBackendId.CLAUDE_CODE,
            requested_model=requested_model,
        )
    return StepAttemptProvenance(
        backend=ExecutionBackendId.CLAUDE_CODE,
        requested_model=requested_model,
        serving_model=_accounted_serving_model(terminal_result),
        cost_usd=_reported_cost(terminal_result.get(CLAUDE_TOTAL_COST_KEY)),
        turn_count=_reported_turn_count(terminal_result.get(CLAUDE_TURN_COUNT_KEY)),
    )


def claude_run_wide_blocker(
    terminal_result: dict[str, Any] | None,
    *,
    stdout: str,
) -> RunWideBlocker | None:
    """Recognise a Claude condition that blocks every Issue in the run.

    Two signals decide, and only these two. The first is the API status the
    terminal result carries: the provider's own statement about the call it
    received, which is evidence about the account or the service and never about
    the Issue. The second is the dedicated rate-limit event the stream emits as
    its own type, which can report exhausted usage before any turn ends.

    Nothing here reads the provider's prose. A diagnostic mentioning a limit is
    not the same as the provider reporting one, and matching text would let an
    Issue whose own work merely discussed an outage pause the whole run.

    The returned summary is Dev Loop's own wording for the classified condition,
    so a pause reason on its way into durable state carries no credential and no
    raw provider payload by construction rather than by redaction.
    """
    kind = _api_error_blocker_kind(terminal_result)
    if kind is None and _reports_exhausted_rate_limit(stdout):
        kind = RunWideBlockerKind.USAGE_LIMIT
    if kind is None:
        return None
    return RunWideBlocker(kind=kind, summary=CLAUDE_RUN_WIDE_BLOCKER_SUMMARIES[kind])


def is_retryable_claude_transient_failure(*, stdout: str, stderr: str) -> bool:
    """Whether a bounded retry could clear what ended this failed Claude attempt.

    A classified Run-Wide Blocker is refused outright, which is how the promise
    that a Run-Wide Blocker is never retried is kept for this backend: the
    provider has answered the call, and repeating it would spend the attempt's
    remaining budget to be told the same thing.

    What remains retryable is a transport-level failure, recognised from the
    attempt's diagnostics rather than from any structured field, because in these
    conditions the CLI never produced a terminal result to carry one.
    """
    if (
        claude_run_wide_blocker(claude_terminal_result(stdout), stdout=stdout)
        is not None
    ):
        return False
    return any(
        pattern.search(stderr) for pattern in CLAUDE_TRANSIENT_FAILURE_PATTERNS
    )


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
    attempt_budget: AttemptExecutionBudget | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one Claude attempt, reporting its stream as neutral step activity.

    The Execution Budget is enforced through the same mechanism the Codex CLI
    Backend uses: one watcher holding a hard deadline from attempt start and an
    inactivity checkpoint, terminating the process tree on expiry and annotating
    the attempt's diagnostics with which limit expired. Every stream line counts
    as activity, so this provider's dense reasoning heartbeat keeps a working
    attempt alive without any of it having to be displayable.

    ``attempt_budget`` is the one budget a whole Workflow Step attempt shares. A
    transient failure that costs the attempt a second process must not also hand
    it a second full deadline, so the caller that retries owns the budget and this
    process enforces it rather than starting its own.
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
            attempt_budget=attempt_budget,
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
        release_process_tree_if_stopped(process)

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
    # The bundled reference data and the one verification call are injectable at
    # the backend boundary for the same reason the catalog adapter makes them
    # injectable: run authorization can then be driven from recorded provider
    # output, so no test starts the CLI. Both default to the installed bundle and
    # a real short-lived session.
    catalog_path: Path | None = None
    session_factory: VerificationSessionFactory | None = None

    @classmethod
    def resolved(
        cls,
        claude: str = CLAUDE_CLI_COMMAND,
        *,
        catalog_path: Path | None = None,
        session_factory: VerificationSessionFactory | None = None,
    ) -> ClaudeCodeExecutionBackend:
        """Build the backend with the Claude command resolved to an executable."""
        return cls(
            claude=resolve_claude_executable(claude),
            catalog_path=catalog_path,
            session_factory=session_factory,
        )

    @property
    def backend_id(self) -> ExecutionBackendId:
        return ExecutionBackendId.CLAUDE_CODE

    def invoke(self, request: StepAttemptRequest) -> StepAttemptResult:
        """Run one Workflow Step attempt and recover its structured role result.

        A transient transport failure costs the attempt another process rather
        than costing the Issue anything, under the same bounded retry policy, the
        same delay, and the same single Execution Budget the Codex CLI Backend
        uses. Only once retries are done is the attempt classified.

        The terminal result is read before the transcript is handed back, because
        the transcript handed back is the redacted one: chain of thought is elided
        from what gets persisted, while classification still sees the recording
        exactly as the provider produced it.

        A reported Run-Wide Blocker deliberately carries no role message. The
        attempt is not an Issue result at all — it pauses the run — so recovering
        and persisting a `.last-message.json` for it would leave an Issue outcome
        on disk that no Workflow Step ever published.

        Provenance is read from the same terminal result and reported whatever the
        attempt's outcome was, because which model did the work is exactly as
        interesting for an attempt that ended BLOCKED as for one that succeeded.
        """
        command = build_claude_command(
            self.claude,
            schema_path=request.schema_path,
            session_id=new_attempt_session_id(),
            execution_settings=request.execution_settings,
        )

        def run_attempt(
            attempt_budget: AttemptExecutionBudget | None,
        ) -> subprocess.CompletedProcess[str]:
            return run_streaming_claude_command(
                command,
                input_text=request.prompt,
                cwd=request.repo_root,
                stage=request.activity_stage,
                activity_context=request.activity_context,
                activity_callback=request.activity_callback,
                execution_budget=request.execution_budget,
                attempt_budget=attempt_budget,
            )

        process = run_attempt_with_transient_retries(
            run_attempt,
            is_retryable=self.is_retryable_transient_failure,
            retry_subject=CLAUDE_RETRY_SUBJECT,
            retry_delay_seconds=CLAUDE_TRANSIENT_RETRY_DELAY_SECONDS,
            stdout_path=request.stdout_path,
            stderr_path=request.stderr_path,
            write_log=request.write_log,
            activity_callback=request.activity_callback,
            execution_budget=request.execution_budget,
        )
        terminal_result = claude_terminal_result(process.stdout)
        run_wide_blocker = (
            claude_run_wide_blocker(terminal_result, stdout=process.stdout)
            if request.run_wide_blocker_policy is RunWideBlockerPolicy.REPORT
            else None
        )
        refusals = claude_permission_denials(terminal_result)
        failure_summary = claude_failure_summary(terminal_result)
        provenance = claude_attempt_provenance(
            terminal_result,
            requested_model=(
                request.execution_settings.model
                if request.execution_settings is not None
                else None
            ),
        )
        report_model_mismatch(
            provenance,
            activity_callback=request.activity_callback,
            activity_stage=request.activity_stage,
            activity_context=request.activity_context,
        )
        message = (
            ""
            if run_wide_blocker is not None or process.returncode != 0
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
            run_wide_blocker=run_wide_blocker,
            refusals=refusals,
            failure_summary=failure_summary,
            provenance=provenance,
        )

    def is_retryable_transient_failure(self, *, stdout: str, stderr: str) -> bool:
        """Recognise the transport-level Claude failures a bounded retry can clear."""
        return is_retryable_claude_transient_failure(stdout=stdout, stderr=stderr)

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
        return self._catalog_adapter(cwd).discover()

    def verify_selected_model(self, model_id: str, *, cwd: Path) -> str:
        """Verify one selected model and return the identifier to persist.

        This is the one call a selection costs. A short alias resolves to the
        concrete pinned identifier the session-initialisation event reports, and
        that identifier — never the alias — is what the caller saves, so
        rerunning a Workflow Run cannot silently change which model works.
        """
        return self._catalog_adapter(cwd).verify(model_id)

    def authorize_execution_settings(
        self,
        authorizations: Sequence[StepSettingsAuthorization],
        *,
        model_catalog: ModelCatalog,
        cwd: Path,
    ) -> None:
        """Authorize Claude-backed Workflow Steps before any budget is spent.

        Each *distinct* model the Run Snapshot selects costs exactly one
        verification call, however many Workflow Steps select it: the selections
        are collected first and each model is verified once, so a five-step
        Workflow on two models pays two calls rather than five. It is the same
        roughly one-second, negligible-cost call a selection pays in `/options`.

        The account, not the bundle, decides. A model the account can no longer
        use fails here — before the first attempt and before any attempt budget
        is spent — and the refusal names the Workflow Step that selects it, the
        provider's own reason, and `/options` as the place to choose another
        model. Refreshing the catalog cannot fix that, because the bundled
        entries are not what refused it.

        A CLI that never started is a different failure and gets a different
        diagnosis. It is also the likelier one — a fresh machine, a CI runner, or
        a restored User Configuration Directory can all hold a Claude-backed
        saved Workflow Default with no Claude CLI on the executable search path —
        and nothing about it is evidence about the account, so it is never
        reported as one.

        Fast is not checked: Step Execution Settings already refuse Fast ON for a
        backend that advertises none, so this backend can never be handed it.
        """
        if not model_catalog.is_fresh:
            raise ValueError(
                "Run preflight requires a fresh live Claude Code Model Catalog; "
                "cached data is display-only. Start the run again once the "
                "Claude CLI is installed and reachable."
            )
        selections = self._selected_models(authorizations, model_catalog)
        adapter = self._catalog_adapter(cwd)
        for model, display_name in selections.items():
            try:
                adapter.verify(model)
            except ModelVerificationError as error:
                raise ValueError(
                    _verification_refusal(display_name, model, error)
                ) from error

    def _catalog_adapter(self, cwd: Path) -> ClaudeModelCatalogAdapter:
        return ClaudeModelCatalogAdapter(
            self.claude,
            cwd=cwd,
            catalog_path=self.catalog_path,
            session_factory=self.session_factory,
        )

    @staticmethod
    def _selected_models(
        authorizations: Sequence[StepSettingsAuthorization],
        model_catalog: ModelCatalog,
    ) -> dict[str, str]:
        """Each distinct selected model, keyed to the first step that selects it.

        Every Workflow Step's settings are validated here, before any
        verification call is made, so a Workflow Step configured against a
        reasoning effort this backend does not accept is refused without spending
        a call. The retained display name is the first Workflow Step selecting
        that model, which is the one a refusal names.
        """
        selections: dict[str, str] = {}
        for authorization in authorizations:
            display_name = authorization.step_display_name
            settings = authorization.settings
            if settings is None:
                raise ValueError(
                    f"Step {display_name!r} has no Step Execution Settings. "
                    "Repair it in /options."
                )
            if settings.backend is not ExecutionBackendId.CLAUDE_CODE:
                raise ValueError(
                    "The Claude Code Backend cannot authorize Step Execution "
                    f"Settings naming the {settings.backend.display_name} "
                    "Backend."
                )
            try:
                model = model_catalog.selectable_model(settings.model)
            except ValueError as error:
                raise ValueError(
                    f"Step {display_name!r} selects Claude model "
                    f"{settings.model!r}, which this backend's Model Catalog "
                    "does not offer. Choose a model in /options."
                ) from error
            if settings.reasoning_effort not in model.reasoning_efforts:
                offered = ", ".join(model.reasoning_efforts)
                raise ValueError(
                    f"Step {display_name!r} selects unsupported reasoning effort "
                    f"{settings.reasoning_effort!r} for model {settings.model!r}. "
                    f"Choose one of {offered} in /options."
                )
            selections.setdefault(settings.model, display_name)
        return selections


def _verification_refusal(
    display_name: str,
    model: str,
    error: ModelVerificationError,
) -> str:
    """The preflight diagnosis one failed verification actually supports.

    The two causes are worded apart deliberately. A single wording had to claim
    one of them for both, and claiming the account for a CLI that never started
    contradicted its own quoted reason and sent the operator to `/options` to pick
    another model — a loop in which every model fails the same way. Each cause
    now states only what it knows and offers a remedy that can work.
    """
    if error.failure is ModelVerificationFailure.PROVIDER_UNREACHABLE:
        head = (
            f"Step {display_name!r} selects Claude model {model!r}, which could "
            "not be verified on this machine"
        )
        remedy = UNREACHABLE_PROVIDER_REMEDY
    else:
        head = (
            f"Step {display_name!r} selects Claude model {model!r}, which this "
            "account cannot use"
        )
        remedy = REFUSED_MODEL_REMEDY
    reason = _ended_sentence(str(error))
    return f"{head}: {reason} {remedy}" if reason else f"{head}. {remedy}"


def _ended_sentence(text: str) -> str:
    """A quoted reason ended so whatever Dev Loop appends starts a new sentence."""
    reason = text.strip()
    if not reason or reason[-1] in SENTENCE_ENDINGS:
        return reason
    return f"{reason}."


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


def _api_error_blocker_kind(
    terminal_result: dict[str, Any] | None,
) -> RunWideBlockerKind | None:
    """Classify the API status one terminal result reported, if it reported one."""
    if terminal_result is None:
        return None
    status = _http_status(terminal_result.get(CLAUDE_API_ERROR_STATUS_KEY))
    if status is None:
        return None
    try:
        classified = ClaudeApiErrorStatus(status)
    except ValueError:
        classified = None
    if classified is not None:
        return CLAUDE_API_ERROR_BLOCKER_KINDS[classified]
    if status in HTTP_SERVER_ERROR_STATUS_RANGE:
        return RunWideBlockerKind.SERVICE_UNAVAILABLE
    return None


def _http_status(value: Any) -> int | None:
    """Parse the provider's API status field into an HTTP status code.

    The field is ``null`` on every recorded successful attempt, and a future CLI
    could report it as a string, so both a number and a decimal string are
    accepted. Anything else is not an HTTP status: it is left unclassified rather
    than crashing an attempt, because a status shape Dev Loop does not recognise
    is not evidence that the whole run must pause.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _reports_exhausted_rate_limit(stdout: str) -> bool:
    """Whether a rate-limit event reported the account's usage as spent.

    Only the disposition decides. The committed recording of a healthy attempt
    carries this event with an allowed status *and* an unrelated rejected overage
    status, so reading the wrong field — or treating the event's mere presence as
    exhaustion — would pause the run on an attempt that completed its work.
    """
    for line in stdout.splitlines():
        payload = parse_claude_event(line)
        if payload is None:
            continue
        if (
            _closed_value(ClaudeEventType, payload.get("type"))
            is not ClaudeEventType.RATE_LIMIT
        ):
            continue
        info = payload.get(CLAUDE_RATE_LIMIT_INFO_KEY)
        if not isinstance(info, dict):
            continue
        status = _closed_value(
            ClaudeRateLimitStatus,
            info.get(CLAUDE_RATE_LIMIT_STATUS_KEY),
        )
        if status is CLAUDE_EXHAUSTED_RATE_LIMIT_STATUS:
            return True
    return False


def _accounted_serving_model(terminal_result: dict[str, Any]) -> str | None:
    """Every model the turn's usage accounting charged, in the recorded order."""
    usage = terminal_result.get(CLAUDE_MODEL_USAGE_KEY)
    if not isinstance(usage, dict):
        return None
    models: list[str] = []
    for raw_model in usage:
        model = redact_persisted_evidence(str(raw_model).strip())
        if model and model not in models:
            models.append(model)
    if not models:
        return None
    return compact_terminal_text(
        CLAUDE_SERVING_MODEL_SEPARATOR.join(models),
        max_length=MAX_SERVING_MODEL_LENGTH,
    )


def _reported_cost(value: Any) -> float | None:
    """The turn's reported cost, or nothing when the provider reported none.

    A shape this backend does not recognise is left unrecorded rather than
    crashing an attempt: cost is evidence, and no Step Outcome depends on it.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


def _reported_turn_count(value: Any) -> int | None:
    """The turn count the provider reported, or nothing when it reported none."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


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
