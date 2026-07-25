"""The Codex CLI Backend: one Execution Backend built on the installed Codex CLI.

Everything Codex-specific about running a Workflow Step attempt lives here: the
`codex exec` command construction, the streaming loop that consumes its event
stream, the translation of that stream into neutral step activity, the recovery
of the structured role message, and Run-Wide Blocker classification. The Codex
event parser itself stays in ``codex_events``; only its callers live here.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..codex_events import (
    CodexItemType,
    CodexTurnOutcome,
    codex_turn_outcome,
    extract_text,
    parse_codex_event,
    render_safe_codex_activity,
)
from ..model_catalog import CodexModelCatalogAdapter, ModelCatalog
from ..statusui import Stage, WaitingIndicator
from ..subprocess_utils import (
    EXECUTION_BUDGET_EXPIRY_RETURNCODE,
    AttemptExecutionBudget,
    ProcessExecutionBudget,
    output_text,
    process_tree_creation_kwargs,
    reap_process_after_terminal_event,
    register_process_tree,
    run_captured_text,
    terminate_process,
    unregister_process_tree,
)
from .activity import ActivityCallback, StepActivityEvent, StepActivityKind
from .backend import (
    ExecutionBackend,
    ExecutionBackendId,
    LogWriter,
    StepAttemptRequest,
    StepAttemptResult,
    StepSettingsAuthorization,
)
from .blockers import RunWideBlocker, RunWideBlockerKind, RunWideBlockerPolicy
from .checkpoint import update_checkpoint_for_step_activity
from .process_stream import (
    drain_process_stream,
    print_step_activity,
    write_process_input,
)
from .structured_result import extract_json_object

if TYPE_CHECKING:
    from ..portable_workflow import ExecutionBudget, StepExecutionSettings


CODEX_CLI_COMMAND = "codex"
CODEX_CLI_DEFAULT_SANDBOX = "workspace-write"
CODEX_CLI_DEFAULT_APPROVAL_POLICY = "never"
CODEX_CONNECTION_RETRY_DELAY_SECONDS = 30
STREAM_THREAD_JOIN_SECONDS = 1.0
FAST_CLI_SERVICE_TIER = "fast"
STANDARD_CLI_SERVICE_TIER = "default"
# Codex items Dev Loop treats as an active backend operation: while one is
# running the attempt is working even though the stream stays silent.
CHECKPOINT_PAUSING_ITEM_TYPES = frozenset(
    {
        CodexItemType.COMMAND_EXECUTION.value,
        CodexItemType.MCP_TOOL_CALL.value,
        CodexItemType.WEB_SEARCH.value,
    }
)
RUN_WIDE_BLOCKER_PATTERNS = (
    (
        RunWideBlockerKind.USAGE_LIMIT,
        re.compile(
            r"\b(usage limit|rate limit exceeded|insufficient_quota|"
            r"out of credits|credits? exhausted)\b",
            re.IGNORECASE,
        ),
        "Codex usage is exhausted. Restore usage availability, then rerun the same command.",
    ),
    (
        RunWideBlockerKind.AUTHENTICATION,
        re.compile(
            r"\b(invalid api key|authentication failed|unauthorized|"
            r"not authenticated|login required|http 401)\b",
            re.IGNORECASE,
        ),
        "Codex authentication is unavailable. Restore authentication, then rerun the same command.",
    ),
    (
        RunWideBlockerKind.SERVICE_UNAVAILABLE,
        re.compile(
            r"\b(service unavailable|temporarily unavailable|backend unavailable|"
            r"server overloaded|http 503)\b",
            re.IGNORECASE,
        ),
        "The Codex service is unavailable. Wait for recovery, then rerun the same command.",
    ),
)
_LEGACY_APPROVAL_FLAG: bool | None = None


def resolve_codex_executable(codex: str) -> str:
    """Resolve a Codex command name to a concrete executable path.

    On Windows the Codex CLI is typically an npm shim (``codex.cmd`` /
    ``codex.ps1``) with no ``codex.exe``. ``subprocess`` with ``shell=False``
    invokes Win32 ``CreateProcess``, which does not consult ``PATHEXT``, so a
    bare ``"codex"`` fails with ``FileNotFoundError`` (WinError 2).
    ``shutil.which`` does honour ``PATHEXT``, so resolving up front fixes
    Windows while staying a no-op on POSIX. If resolution fails (Codex not on
    PATH), return the original value so the downstream error still names what
    the user asked for.
    """
    resolved = shutil.which(codex)
    if resolved:
        return resolved

    candidate = Path(codex).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())

    if sys.platform.startswith("win") and candidate.name == codex:
        appdata = os.environ.get("APPDATA")
        npm_dirs = []
        if appdata:
            npm_dirs.append(Path(appdata) / "npm")
        npm_dirs.append(Path.home() / "AppData" / "Roaming" / "npm")
        for npm_dir in npm_dirs:
            for suffix in (".cmd", ".exe", ""):
                npm_candidate = npm_dir / f"{codex}{suffix}"
                if npm_candidate.is_file():
                    return str(npm_candidate.resolve())

    return codex


def uses_legacy_approval_flag(codex: str) -> bool:
    global _LEGACY_APPROVAL_FLAG
    if _LEGACY_APPROVAL_FLAG is None:
        result = run_captured_text(
            [codex, "exec", "--help"],
        )
        help_text = f"{result.stdout}\n{result.stderr}"
        _LEGACY_APPROVAL_FLAG = "  -a," in help_text or "  -a <" in help_text
    return _LEGACY_APPROVAL_FLAG


def build_codex_exec_command(
    codex: str,
    repo_root: Path,
    sandbox: str,
    approval_policy: str,
    schema_path: Path,
    message_path: Path,
    execution_settings: StepExecutionSettings | None = None,
) -> list[str]:
    command = [
        codex,
        "exec",
        "-C",
        str(repo_root),
        "-s",
        sandbox,
    ]
    if execution_settings is not None:
        command.extend(codex_execution_settings_args(execution_settings))
    if uses_legacy_approval_flag(codex):
        command.extend(["-a", approval_policy])
    else:
        command.extend(["-c", f'approval_policy="{approval_policy}"'])
    command.extend(
        [
            "--output-schema",
            str(schema_path),
            "-o",
            str(message_path),
            "--json",
            "-",
        ]
    )
    return command


def codex_execution_settings_args(
    settings: StepExecutionSettings,
) -> list[str]:
    """Render one Workflow Step's settings as Codex CLI arguments.

    Backend identity is checked here, at the command-line boundary, so settings
    naming another Execution Backend can never be turned into a Codex
    invocation.
    """
    if settings.backend is not ExecutionBackendId.CODEX_CLI:
        raise ValueError(
            "The Codex CLI Backend cannot run Step Execution Settings naming the "
            f"{settings.backend.display_name} Backend."
        )
    fast_enabled = settings.fast_enabled
    service_tier = (
        FAST_CLI_SERVICE_TIER if fast_enabled else STANDARD_CLI_SERVICE_TIER
    )
    return [
        "-m",
        settings.model,
        "-c",
        f'model_reasoning_effort="{settings.reasoning_effort}"',
        "-c",
        f'service_tier="{service_tier}"',
        "--enable" if fast_enabled else "--disable",
        "fast_mode",
    ]


def codex_step_activity_event(
    payload: dict[str, Any] | None,
) -> StepActivityEvent | None:
    """Translate one parsed Codex event into neutral step activity.

    This is the single translation point out of the Codex event vocabulary: the
    Portable Activity Feed shows the event's safe text and the Execution Budget
    checkpoint pauses on its tool kinds, so neither consumer reads Codex wire
    format.
    """
    if payload is None:
        return None
    activity = render_safe_codex_activity(payload)
    tool_key = _active_tool_key(payload)
    if tool_key is not None:
        started = payload.get("type") == "item.started"
        return StepActivityEvent(
            kind=(
                StepActivityKind.TOOL_STARTED
                if started
                else StepActivityKind.TOOL_COMPLETED
            ),
            activity=activity,
            tool_key=tool_key,
        )
    event_type = payload.get("type")
    if event_type == CodexTurnOutcome.COMPLETED.value:
        return StepActivityEvent(
            kind=StepActivityKind.TURN_COMPLETED,
            activity=activity,
        )
    if event_type in {"error", CodexTurnOutcome.FAILED.value}:
        return StepActivityEvent(kind=StepActivityKind.ERROR, activity=activity)
    if activity is None:
        return None
    if _item_type(payload) == CodexItemType.REASONING.value:
        return StepActivityEvent(kind=StepActivityKind.REASONING, activity=activity)
    return StepActivityEvent(kind=StepActivityKind.MESSAGE, activity=activity)


def _item_type(payload: dict[str, Any]) -> str | None:
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    return item_type if isinstance(item_type, str) else None


def _active_tool_key(payload: dict[str, Any]) -> str | None:
    if payload.get("type") not in {"item.started", "item.completed"}:
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type not in CHECKPOINT_PAUSING_ITEM_TYPES:
        return None
    item_id = item.get("id")
    return f"{item_type}:{item_id if isinstance(item_id, str) else item_type}"


def classify_run_wide_blocker(stdout: str, stderr: str) -> RunWideBlocker | None:
    """Recognise a Codex condition that blocks every Issue in the run."""
    terminal_errors: list[str] = []
    for line in stdout.splitlines():
        payload = parse_codex_event(line)
        if payload is None or payload.get("type") not in {
            "error",
            CodexTurnOutcome.FAILED.value,
        }:
            continue
        message = extract_text(payload.get("message")) or extract_text(
            payload.get("error")
        )
        if message:
            terminal_errors.append(message)
    if stderr:
        terminal_errors.append(stderr)
    error_text = "\n".join(terminal_errors)
    for kind, pattern, summary in RUN_WIDE_BLOCKER_PATTERNS:
        if pattern.search(error_text):
            return RunWideBlocker(kind=kind, summary=summary)
    return None


def is_retryable_codex_connection_failure(stderr: str) -> bool:
    lower = stderr.lower()
    return (
        "failed to connect to websocket" in lower
        or "responses_websocket" in lower
    )


def run_streaming_codex_command(
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
    if activity_callback is not None:
        stderr_activity_callback = activity_callback
    else:
        assert indicator is not None

        def stderr_activity_callback(_event: StepActivityEvent | None) -> None:
            indicator.notify_activity()
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

    def notify_stderr_activity(event: StepActivityEvent | None) -> None:
        if budget is not None:
            budget.notify_activity()
        stderr_activity_callback(event)

    input_thread = threading.Thread(
        target=write_process_input,
        args=(process.stdin, input_text),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=drain_process_stream,
        args=(
            process.stderr,
            stderr_parts,
            notify_stderr_activity,
        ),
        daemon=True,
    )
    turn_outcome: CodexTurnOutcome | None = None
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
            event = parse_codex_event(line)
            step_activity = codex_step_activity_event(event)
            update_checkpoint_for_step_activity(budget, step_activity, active_tools)
            if activity_callback is not None:
                activity_callback(step_activity)
            elif indicator is not None:
                indicator.notify_activity()
                if step_activity is not None and step_activity.activity:
                    indicator.stop()
                    print_step_activity(stage, activity_context, step_activity)
                    indicator.start()
            turn_outcome = codex_turn_outcome(event)
            if turn_outcome is not None:
                break

        if turn_outcome is None:
            returncode = process.wait()
        else:
            if budget is not None:
                budget_expiration = budget.finish()
                budget_finished = True
            reap_process_after_terminal_event(process)
            returncode = _terminal_returncode(turn_outcome, process.returncode)
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


def run_codex_exec_with_connection_retries(
    command: list[str],
    prompt: str,
    stdout_path: Path,
    stderr_path: Path,
    *,
    cwd: Path,
    write_log: LogWriter,
    stage: Stage = Stage.DEVELOPMENT,
    activity_context: str = "",
    activity_callback: ActivityCallback | None = None,
    execution_budget: ExecutionBudget | None = None,
) -> subprocess.CompletedProcess[str]:
    attempt = 1
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    attempt_budget = (
        AttemptExecutionBudget(
            timeout_seconds=execution_budget.timeout_seconds,
            checkpoint_seconds=execution_budget.checkpoint_seconds,
        )
        if execution_budget is not None
        else None
    )

    while True:
        result = run_streaming_codex_command(
            command,
            input_text=prompt,
            cwd=cwd,
            stage=stage,
            activity_context=activity_context,
            activity_callback=activity_callback,
            execution_budget=execution_budget,
            attempt_budget=attempt_budget,
        )
        current_stdout = output_text(result.stdout)
        current_stderr = output_text(result.stderr)
        if attempt_budget is not None and (current_stdout or current_stderr):
            attempt_budget.notify_activity()
        stdout_parts.append(current_stdout)
        stderr_parts.append(current_stderr)
        result.stdout = "".join(stdout_parts)
        result.stderr = "".join(stderr_parts)

        if attempt_budget is not None:
            expiration = attempt_budget.expiration()
            if expiration is not None:
                result.returncode = EXECUTION_BUDGET_EXPIRY_RETURNCODE
                if expiration not in result.stderr:
                    result.stderr += f"{expiration}\n"
                return result

        if classify_run_wide_blocker(current_stdout, current_stderr) is not None:
            return result

        if result.returncode == 0 or not is_retryable_codex_connection_failure(
            current_stderr
        ):
            return result

        retry_message = (
            f"codex exec connection failed on attempt {attempt}; "
            f"retrying in {CODEX_CONNECTION_RETRY_DELAY_SECONDS} seconds.\n"
        )
        if activity_callback is None:
            print(retry_message.strip())
        else:
            activity_callback(
                StepActivityEvent(
                    kind=StepActivityKind.ERROR,
                    activity=retry_message.strip(),
                )
            )
        stderr_parts.append(retry_message)
        result.stderr = "".join(stderr_parts)
        write_log(stdout_path, result.stdout)
        write_log(stderr_path, result.stderr)
        if attempt_budget is None:
            time.sleep(CODEX_CONNECTION_RETRY_DELAY_SECONDS)
        else:
            expiration = attempt_budget.wait_for_retry(
                CODEX_CONNECTION_RETRY_DELAY_SECONDS
            )
            if expiration is not None:
                result.returncode = EXECUTION_BUDGET_EXPIRY_RETURNCODE
                if expiration not in result.stderr:
                    result.stderr += f"{expiration}\n"
                return result
        attempt += 1


def load_or_recover_role_message(
    *,
    message_path: Path,
    stdout: str,
    write_log: LogWriter,
) -> str:
    if message_path.is_file():
        return message_path.read_text(encoding="utf-8")

    message = extract_last_structured_agent_message(stdout)
    if message is None and extract_json_object(stdout) is not None:
        message = stdout
    if message is None:
        return stdout

    write_log(message_path, message)
    return message


def extract_last_structured_agent_message(text: str) -> str | None:
    last_message: str | None = None
    for line in text.splitlines():
        payload = parse_codex_event(line)
        if payload is None or payload.get("type") != "item.completed":
            continue

        item = payload.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message" and item.get("role") != "assistant":
            continue
        if item_type not in {"agent_message", "assistant_message", "message"}:
            continue

        message = (
            extract_text(item.get("text"))
            or extract_text(item.get("message"))
            or extract_text(item.get("content"))
        )
        if message and extract_json_object(message) is not None:
            last_message = message

    return last_message


def _terminal_returncode(
    outcome: CodexTurnOutcome,
    process_returncode: int | None,
) -> int:
    if outcome is CodexTurnOutcome.COMPLETED:
        return 0
    if isinstance(process_returncode, int) and process_returncode != 0:
        return process_returncode
    return 1


@dataclass(frozen=True)
class CodexCliExecutionBackend(ExecutionBackend):
    """The Execution Backend that delegates agent runs to the installed Codex CLI."""

    codex: str = CODEX_CLI_COMMAND
    sandbox: str = CODEX_CLI_DEFAULT_SANDBOX
    approval_policy: str = CODEX_CLI_DEFAULT_APPROVAL_POLICY

    @classmethod
    def resolved(
        cls,
        codex: str = CODEX_CLI_COMMAND,
        *,
        sandbox: str = CODEX_CLI_DEFAULT_SANDBOX,
        approval_policy: str = CODEX_CLI_DEFAULT_APPROVAL_POLICY,
    ) -> CodexCliExecutionBackend:
        """Build the backend with the Codex command resolved to an executable."""
        return cls(
            codex=resolve_codex_executable(codex),
            sandbox=sandbox,
            approval_policy=approval_policy,
        )

    @property
    def backend_id(self) -> ExecutionBackendId:
        return ExecutionBackendId.CODEX_CLI

    def invoke(self, request: StepAttemptRequest) -> StepAttemptResult:
        command = build_codex_exec_command(
            codex=self.codex,
            repo_root=request.repo_root,
            sandbox=self.sandbox,
            approval_policy=self.approval_policy,
            schema_path=request.schema_path,
            message_path=request.message_path,
            execution_settings=request.execution_settings,
        )
        process = run_codex_exec_with_connection_retries(
            command=command,
            prompt=request.prompt,
            stdout_path=request.stdout_path,
            stderr_path=request.stderr_path,
            cwd=request.repo_root,
            write_log=request.write_log,
            stage=request.activity_stage,
            activity_context=request.activity_context,
            activity_callback=request.activity_callback,
            execution_budget=request.execution_budget,
        )
        run_wide_blocker = (
            classify_run_wide_blocker(
                output_text(process.stdout),
                output_text(process.stderr),
            )
            if request.run_wide_blocker_policy is RunWideBlockerPolicy.REPORT
            else None
        )
        if run_wide_blocker is not None or process.returncode != 0:
            return StepAttemptResult(
                process=process,
                run_wide_blocker=run_wide_blocker,
            )
        return StepAttemptResult(
            process=process,
            message=load_or_recover_role_message(
                message_path=request.message_path,
                stdout=process.stdout,
                write_log=request.write_log,
            ),
        )

    @property
    def provider_command(self) -> str:
        return self.codex

    def discover_model_catalog(self, *, cwd: Path) -> ModelCatalog:
        return CodexModelCatalogAdapter(self.codex, cwd=cwd).discover()

    def authorize_execution_settings(
        self,
        authorizations: Sequence[StepSettingsAuthorization],
        *,
        model_catalog: ModelCatalog,
    ) -> None:
        if not model_catalog.is_fresh:
            raise ValueError(
                "Run preflight requires a fresh live Codex Model Catalog; cached data "
                "is display-only. Use Retry Catalog in /options and start again."
            )
        for authorization in authorizations:
            self._authorize_step_settings(authorization, model_catalog)

    def _authorize_step_settings(
        self,
        authorization: StepSettingsAuthorization,
        model_catalog: ModelCatalog,
    ) -> None:
        display_name = authorization.step_display_name
        settings = authorization.settings
        if settings is None:
            raise ValueError(
                f"Step {display_name!r} has no Step Execution Settings. "
                "Repair it in /options."
            )
        try:
            model = model_catalog.model(settings.model)
        except ValueError as error:
            raise ValueError(
                f"Step {display_name!r} selects unavailable model "
                f"{settings.model!r}. Use Retry Catalog in /options."
            ) from error
        if settings.reasoning_effort not in model.reasoning_efforts:
            raise ValueError(
                f"Step {display_name!r} selects unsupported reasoning effort "
                f"{settings.reasoning_effort!r} for model {settings.model!r}. "
                "Use Retry Catalog in /options."
            )
        if settings.fast_enabled and not model.supports_fast:
            raise ValueError(
                f"Step {display_name!r} selects Fast ON, but model "
                f"{settings.model!r} does not advertise Fast. Use Retry Catalog "
                "in /options."
            )
