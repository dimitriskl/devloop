from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, TextIO

from .codex_events import (
    CodexTurnOutcome,
    RunWideBlocker,
    classify_run_wide_blocker,
    codex_turn_outcome,
    extract_text,
    parse_codex_event,
    render_safe_codex_activity,
)
from .issue_pack import Issue
from .portable_text import normalize_single_line_display_name
from .self_improvement_wiki import DEFAULT_SELF_IMPROVEMENT_WIKI_PATH
from .statusui import Stage, WaitingIndicator
from .step_configuration import STEP_GUIDANCE_PRECEDENCE, StepGuidance
from .subprocess_utils import (
    AttemptExecutionBudget,
    ProcessExecutionBudget,
    output_text,
    process_tree_creation_kwargs,
    register_process_tree,
    reap_process_after_terminal_event,
    run_captured_text,
    terminate_process,
)
from .terminal_text import sanitize_terminal_text
from .templates import BundleContext, Preset, render_template

if TYPE_CHECKING:
    from .portable_workflow import CodexExecutionSettings, ExecutionBudget

_LEGACY_APPROVAL_FLAG: bool | None = None
CODEX_CONNECTION_RETRY_DELAY_SECONDS = 30
STREAM_THREAD_JOIN_SECONDS = 1.0
PORTABLE_LOG_MARKER = "portable-step"
PORTABLE_LOG_TOKEN_PATTERN = r"[a-z0-9]+(?:-[a-z0-9]+)*"
PORTABLE_STEP_INSTANCE_ID_PATTERN = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
LOG_ATTEMPT_TOKEN_MAX_LENGTH = 24
LOG_FALLBACK_ATTEMPT_TOKEN_MAX_LENGTH = 16
LOG_FALLBACK_ROLE_TOKEN_MAX_LENGTH = 12
LOG_TOKEN_HASH_LENGTH = 8
MAX_PORTABLE_LOG_PATH_LENGTH = 259
LONGEST_ROLE_LOG_SUFFIX = ".last-message.json"
FAST_CLI_SERVICE_TIER = "fast"
STANDARD_CLI_SERVICE_TIER = "default"
ROLE_STAGES = {
    "coder": Stage.DEVELOPMENT,
    "reviewer": Stage.REVIEW,
    "qa": Stage.QA,
}
DEVLOOP_RUN_GOAL = (
    "All selected issues from the issue pack must be developed, reviewed, "
    "and tested so the finished product has as few bugs and deficiencies as practical."
)
ActivityCallback = Callable[[str | None], None]


class RunWideBlockerError(RuntimeError):
    def __init__(self, blocker: RunWideBlocker) -> None:
        super().__init__(blocker.summary)
        self.blocker = blocker


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
    codex_settings: CodexExecutionSettings | None = None,
) -> list[str]:
    command = [
        codex,
        "exec",
        "-C",
        str(repo_root),
        "-s",
        sandbox,
    ]
    if codex_settings is not None:
        command.extend(codex_execution_settings_args(codex_settings))
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
    settings: CodexExecutionSettings,
) -> list[str]:
    fast_enabled = settings.fast.value == "ON"
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


def stage_for_role(role: str) -> Stage:
    try:
        return ROLE_STAGES[role]
    except KeyError as error:
        raise ValueError(f"Unsupported Dev Loop role: {role}") from error


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

        def stderr_activity_callback(_activity: str | None) -> None:
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

    def notify_stderr_activity(activity: str | None) -> None:
        if budget is not None:
            budget.notify_activity()
        stderr_activity_callback(activity)

    input_thread = threading.Thread(
        target=_write_process_input,
        args=(process.stdin, input_text),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_process_stream,
        args=(
            process.stderr,
            stderr_parts,
            notify_stderr_activity,
        ),
        daemon=True,
    )
    turn_outcome: CodexTurnOutcome | None = None

    if indicator is not None:
        indicator.start()
    input_thread.start()
    stderr_thread.start()
    if budget is not None:
        budget.start()
    budget_expiration: str | None = None
    try:
        for line in process.stdout:
            if budget is not None:
                budget.notify_activity()
            stdout_parts.append(line)
            event = parse_codex_event(line)
            activity = render_safe_codex_activity(event)
            if activity_callback is not None:
                activity_callback(activity)
            elif indicator is not None:
                indicator.notify_activity()
            if activity and indicator is not None:
                indicator.stop()
                _print_codex_activity(stage, activity_context, activity)
                indicator.start()
            turn_outcome = codex_turn_outcome(event)
            if turn_outcome is not None:
                break

        if turn_outcome is None:
            returncode = process.wait()
        else:
            reap_process_after_terminal_event(process)
            returncode = _terminal_returncode(turn_outcome, process.returncode)
    except KeyboardInterrupt:
        terminate_process(process)
        raise
    finally:
        if budget is not None:
            budget_expiration = budget.finish()
        if indicator is not None:
            indicator.stop()
        input_thread.join(timeout=STREAM_THREAD_JOIN_SECONDS)
        stderr_thread.join(timeout=STREAM_THREAD_JOIN_SECONDS)
        for stream in (process.stdout, process.stderr):
            close = getattr(stream, "close", None)
            if callable(close):
                close()

    if budget_expiration is not None:
        stderr_parts.append(f"{budget_expiration}\n")
        returncode = 124

    return subprocess.CompletedProcess(
        command,
        returncode,
        stdout="".join(stdout_parts),
        stderr="".join(stderr_parts),
    )


def _write_process_input(stream: TextIO, input_text: str) -> None:
    try:
        stream.write(input_text)
        stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _drain_process_stream(
    stream: TextIO,
    captured: list[str],
    notify_activity: ActivityCallback,
) -> None:
    try:
        for line in stream:
            captured.append(line)
            notify_activity(None)
    except (OSError, ValueError):
        pass


def _print_codex_activity(stage: Stage, context: str, activity: str) -> None:
    prefix = f"[{stage.value}]"
    if context:
        safe_context = sanitize_terminal_text(context, preserve_newlines=False)
        prefix = f"{prefix} {safe_context}:"
    safe_activity = sanitize_terminal_text(activity, preserve_newlines=False)
    print(f"{prefix} {safe_activity}")


def _terminal_returncode(
    outcome: CodexTurnOutcome,
    process_returncode: int | None,
) -> int:
    if outcome is CodexTurnOutcome.COMPLETED:
        return 0
    if isinstance(process_returncode, int) and process_returncode != 0:
        return process_returncode
    return 1


def _role_activity_context(*, progress: str, pass_number: int) -> str:
    pass_label = f"p{pass_number}"
    return f"{progress} {pass_label}" if progress else pass_label


@dataclass
class RoleResult:
    status: str
    summary: str = ""
    changed_files: list[str] = field(default_factory=list)
    verification_commands: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    fix_list: list[str] = field(default_factory=list)
    residual_risks: list[str] = field(default_factory=list)
    raw_message: str = ""

    @classmethod
    def from_message(cls, message: str) -> "RoleResult":
        data = extract_json_object(message)
        if not data:
            return cls(
                status="BLOCKED",
                summary="Codex did not return valid JSON matching the role schema.",
                raw_message=message,
            )

        status = str(data.get("status", "BLOCKED")).upper()
        if status not in {"PASS", "FAIL", "BLOCKED"}:
            status = "BLOCKED"

        return cls(
            status=status,
            summary=str(data.get("summary", "")),
            changed_files=list_of_strings(data.get("changed_files")),
            verification_commands=list_of_strings(data.get("verification_commands")),
            findings=list_of_strings(data.get("findings")),
            fix_list=list_of_strings(data.get("fix_list")),
            residual_risks=list_of_strings(data.get("residual_risks")),
            raw_message=message,
        )


class CodexRunner:
    def __init__(
        self,
        bundle: BundleContext,
        repo_root: Path,
        prd_path: Path,
        issues_index: Path,
        preset: Preset,
        codex: str,
        sandbox: str,
        approval_policy: str,
        dry_run: bool,
        use_self_improvement_wiki: bool,
    ) -> None:
        self.bundle = bundle
        self.repo_root = repo_root
        self.prd_path = prd_path
        self.issues_index = issues_index
        self.preset = preset
        self.codex = resolve_codex_executable(codex)
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self.dry_run = dry_run
        self.use_self_improvement_wiki = use_self_improvement_wiki
        self.log_root = issues_index.parent / ".loop.logs"
        self.ensure_log_root()

    def ensure_log_root(self) -> None:
        self.log_root.mkdir(parents=True, exist_ok=True)

    def write_log_text(
        self,
        path: Path,
        text: str,
        *,
        log_root: Path | None = None,
    ) -> None:
        configured_root = log_root or self.log_root
        configured_root.mkdir(parents=True, exist_ok=True)
        resolved_path = _confined_log_path(path, configured_root)
        resolved_path.write_text(text, encoding="utf-8")

    def run_role(
        self,
        role: str,
        issue: Issue,
        pass_number: int,
        fix_list: list[str] | None = None,
        coder_result: RoleResult | None = None,
        review_result: RoleResult | None = None,
        attempt_label: str | None = None,
        progress: str = "",
        activity_callback: ActivityCallback | None = None,
        step_instance_id: str | None = None,
        step_display_name: str | None = None,
        step_attempt_id: str | None = None,
        prompt_session_id: str | None = None,
        rework_attempt_record: Mapping[str, Any] | None = None,
        role_adapter: str | None = None,
        codex_settings: CodexExecutionSettings | None = None,
        execution_budget: ExecutionBudget | None = None,
        skill_paths: Iterable[str] | None = None,
        agent_paths: Iterable[str] | None = None,
        step_guidance: str | None = None,
    ) -> RoleResult:
        prompt = self.build_prompt(
            role=role,
            issue=issue,
            pass_number=pass_number,
            fix_list=fix_list or [],
            coder_result=coder_result,
            review_result=review_result,
            step_instance_id=step_instance_id,
            step_display_name=step_display_name,
            step_attempt_id=step_attempt_id,
            prompt_session_id=prompt_session_id,
            rework_attempt_record=rework_attempt_record,
            role_adapter=role_adapter,
            skill_paths=skill_paths,
            agent_paths=agent_paths,
            step_guidance=step_guidance,
        )

        prefix_parts = [slugify_log_token(issue.number) or "issue"]
        attempt_identity = step_attempt_id or prompt_session_id or str(uuid.uuid4())
        attempt_slug = compact_log_identity_token(attempt_identity)
        prefix_parts.append(f"attempt-{attempt_slug or uuid.uuid4()}")
        attempt_label_slug = slugify_log_token(attempt_label)
        if attempt_label_slug:
            prefix_parts.append(attempt_label_slug)
        if step_instance_id:
            prefix_parts.extend(
                [
                    PORTABLE_LOG_MARKER,
                    slugify_log_token(step_display_name) or "step",
                    slugify_log_token(step_instance_id) or "instance",
                ]
            )
        prefix_parts.extend(
            [slugify_log_token(role) or "role", f"pass{pass_number}"]
        )
        prefix = _fit_role_log_prefix(
            log_root=self.log_root,
            readable_prefix="-".join(prefix_parts),
            issue_slug=prefix_parts[0],
            attempt_identity=attempt_identity,
            step_instance_id=step_instance_id,
            role=role,
            pass_number=pass_number,
        )
        prompt_path = _confined_log_path(
            self.log_root / f"{prefix}.prompt.md",
            self.log_root,
        )
        stdout_path = _confined_log_path(
            self.log_root / f"{prefix}.stdout.jsonl",
            self.log_root,
        )
        stderr_path = _confined_log_path(
            self.log_root / f"{prefix}.stderr.txt",
            self.log_root,
        )
        message_path = _confined_log_path(
            self.log_root / f"{prefix}.last-message.json",
            self.log_root,
        )
        self.write_log_text(prompt_path, prompt)

        schema_path = self.bundle.schemas / "role-result.schema.json"
        command = build_codex_exec_command(
            codex=self.codex,
            repo_root=self.repo_root,
            sandbox=self.sandbox,
            approval_policy=self.approval_policy,
            schema_path=schema_path,
            message_path=message_path,
            codex_settings=codex_settings,
        )

        result = self.run_codex_exec_with_connection_retries(
            command=command,
            prompt=prompt,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stage=stage_for_role(role_adapter or role),
            activity_context=_role_activity_context(
                progress=progress,
                pass_number=pass_number,
            ),
            activity_callback=activity_callback,
            execution_budget=execution_budget,
        )
        self.write_log_text(stdout_path, result.stdout)
        self.write_log_text(stderr_path, result.stderr)

        run_wide_blocker = classify_run_wide_blocker(
            output_text(result.stdout),
            output_text(result.stderr),
        )
        if run_wide_blocker is not None:
            raise RunWideBlockerError(run_wide_blocker)

        if result.returncode != 0:
            return RoleResult(
                status="BLOCKED",
                summary=f"codex exec failed with exit code {result.returncode}. See {stderr_path}.",
                raw_message=result.stderr,
            )

        message = self.load_or_recover_role_message(
            message_path=message_path,
            stdout=result.stdout,
        )
        return RoleResult.from_message(message)

    def load_or_recover_role_message(
        self,
        *,
        message_path: Path,
        stdout: str,
        log_root: Path | None = None,
    ) -> str:
        if message_path.is_file():
            return message_path.read_text(encoding="utf-8")

        message = extract_last_structured_agent_message(stdout)
        if message is None and extract_json_object(stdout) is not None:
            message = stdout
        if message is None:
            return stdout

        self.write_log_text(message_path, message, log_root=log_root)
        return message

    def run_codex_exec_with_connection_retries(
        self,
        command: list[str],
        prompt: str,
        stdout_path: Path,
        stderr_path: Path,
        stage: Stage = Stage.DEVELOPMENT,
        activity_context: str = "",
        activity_callback: ActivityCallback | None = None,
        log_root: Path | None = None,
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
                cwd=self.repo_root,
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
                    result.returncode = 124
                    if expiration not in result.stderr:
                        result.stderr += f"{expiration}\n"
                    return result

            if classify_run_wide_blocker(current_stdout, current_stderr) is not None:
                return result

            if result.returncode == 0 or not is_retryable_codex_connection_failure(current_stderr):
                return result

            retry_message = (
                f"codex exec connection failed on attempt {attempt}; "
                f"retrying in {CODEX_CONNECTION_RETRY_DELAY_SECONDS} seconds.\n"
            )
            if activity_callback is None:
                print(retry_message.strip())
            else:
                activity_callback(retry_message.strip())
            stderr_parts.append(retry_message)
            result.stderr = "".join(stderr_parts)
            self.write_log_text(stdout_path, result.stdout, log_root=log_root)
            self.write_log_text(stderr_path, result.stderr, log_root=log_root)
            if attempt_budget is None:
                time.sleep(CODEX_CONNECTION_RETRY_DELAY_SECONDS)
            else:
                expiration = attempt_budget.wait_for_retry(
                    CODEX_CONNECTION_RETRY_DELAY_SECONDS
                )
                if expiration is not None:
                    result.returncode = 124
                    if expiration not in result.stderr:
                        result.stderr += f"{expiration}\n"
                    return result
            attempt += 1

    def render_dry_run_prompts(
        self,
        issue: Issue,
        workflow_steps: Iterable[tuple[Any, ...]] | None = None,
    ) -> None:
        steps = workflow_steps or (
            ("coder", "coder", "Development", "legacy-development"),
            ("reviewer", "reviewer", "Review", "legacy-review"),
            ("qa", "qa", "QA", "legacy-qa"),
        )
        for raw_step in steps:
            role, role_adapter, display_name, instance_id = raw_step[:4]
            skill_paths = raw_step[4] if len(raw_step) > 4 else None
            agent_paths = raw_step[5] if len(raw_step) > 5 else None
            step_guidance = raw_step[6] if len(raw_step) > 6 else None
            prompt_session_id = f"dry-run-{instance_id}"
            prompt = self.build_prompt(
                role=role,
                issue=issue,
                pass_number=1,
                fix_list=[],
                step_instance_id=instance_id,
                step_display_name=display_name,
                step_attempt_id=f"dry-run-{instance_id}",
                prompt_session_id=prompt_session_id,
                role_adapter=role_adapter,
                skill_paths=skill_paths,
                agent_paths=agent_paths,
                step_guidance=step_guidance,
            )
            issue_slug = slugify_log_token(issue.number) or "issue"
            step_slug = slugify_log_token(display_name) or "step"
            instance_slug = slugify_log_token(instance_id) or "instance"
            role_slug = slugify_log_token(role) or "role"
            path = _confined_log_path(
                self.log_root
                / (
                    f"{issue_slug}-{step_slug}-{instance_slug}-{role_slug}"
                    "-dry-run.prompt.md"
                ),
                self.log_root,
            )
            self.write_log_text(path, prompt)
            print(f"[dry-run] Wrote {path}")

    def run_self_improvement_compiler(
        self,
        state_path: Path,
        board_path: Path,
        wiki_root: Path,
        max_lessons: int,
        compiler_repo_root: Path | None = None,
        run_context_path: Path | None = None,
    ) -> RoleResult:
        compiler_repo_root = compiler_repo_root or self.repo_root
        log_root = self.log_root if compiler_repo_root == self.repo_root else wiki_root.parent / ".compiler-runs"
        log_root.mkdir(parents=True, exist_ok=True)
        prompt = self.build_self_improvement_prompt(
            state_path=state_path,
            board_path=board_path,
            wiki_root=wiki_root,
            max_lessons=max_lessons,
            run_context_path=run_context_path,
            compiler_repo_root=compiler_repo_root,
        )

        prefix = "self-improvement-compiler"
        prompt_path = _confined_log_path(
            log_root / f"{prefix}.prompt.md",
            log_root,
        )
        stdout_path = _confined_log_path(
            log_root / f"{prefix}.stdout.jsonl",
            log_root,
        )
        stderr_path = _confined_log_path(
            log_root / f"{prefix}.stderr.txt",
            log_root,
        )
        message_path = _confined_log_path(
            log_root / f"{prefix}.last-message.json",
            log_root,
        )
        self.write_log_text(prompt_path, prompt, log_root=log_root)

        schema_path = self.bundle.schemas / "role-result.schema.json"
        command = build_codex_exec_command(
            codex=self.codex,
            repo_root=compiler_repo_root,
            sandbox=self.sandbox,
            approval_policy=self.approval_policy,
            schema_path=schema_path,
            message_path=message_path,
        )

        result = self.run_codex_exec_with_connection_retries(
            command=command,
            prompt=prompt,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stage=Stage.QA,
            activity_context="self-improvement",
            log_root=log_root,
        )
        self.write_log_text(stdout_path, result.stdout, log_root=log_root)
        self.write_log_text(stderr_path, result.stderr, log_root=log_root)

        if result.returncode != 0:
            return RoleResult(
                status="BLOCKED",
                summary=f"self-improvement compiler failed with exit code {result.returncode}. See {stderr_path}.",
                raw_message=result.stderr,
            )

        message = self.load_or_recover_role_message(
            message_path=message_path,
            stdout=result.stdout,
            log_root=log_root,
        )
        return RoleResult.from_message(message)

    def build_prompt(
        self,
        role: str,
        issue: Issue,
        pass_number: int,
        fix_list: list[str],
        coder_result: RoleResult | None = None,
        review_result: RoleResult | None = None,
        step_instance_id: str | None = None,
        step_display_name: str | None = None,
        step_attempt_id: str | None = None,
        prompt_session_id: str | None = None,
        rework_attempt_record: Mapping[str, Any] | None = None,
        role_adapter: str | None = None,
        skill_paths: Iterable[str] | None = None,
        agent_paths: Iterable[str] | None = None,
        step_guidance: str | None = None,
    ) -> str:
        role_config = self.preset.roles.get(role, {})
        execution_role = role_adapter or role
        normalized_step_display_name = normalize_single_line_display_name(
            step_display_name or role,
            field_name="Workflow step display name",
        )
        template_name = {
            "coder": "coder.md",
            "reviewer": "reviewer.md",
            "qa": "qa.md",
        }[execution_role]

        values = {
            "ROLE": role,
            "PASS_NUMBER": pass_number,
            "BUNDLE_ROOT": self.bundle.root,
            "REPO_ROOT": self.repo_root,
            "PRD_PATH": self.prd_path,
            "ISSUES_INDEX": self.issues_index,
            "ISSUE_NUMBER": issue.number,
            "ISSUE_TITLE": issue.title,
            "ISSUE_PATH": issue.path,
            "STEP_INSTANCE_ID": step_instance_id or "Not applicable",
            "STEP_DISPLAY_NAME": normalized_step_display_name,
            "STEP_ATTEMPT_ID": step_attempt_id or "Not applicable",
            "PROMPT_SESSION_ID": prompt_session_id or "Not applicable",
            "REWORK_ATTEMPT_RECORD": json.dumps(
                rework_attempt_record,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            "REQUIRED_DOCS": self.preset.required_docs,
            "RUN_GOAL": DEVLOOP_RUN_GOAL,
            "BUNDLE_MEMORY_DOCS": self.bundle_memory_docs(),
            "SKILL_PATHS": (
                tuple(skill_paths)
                if skill_paths is not None
                else role_config.get("skills", [])
            ),
            "AGENT_PATHS": (
                tuple(agent_paths)
                if agent_paths is not None
                else role_config.get("agents", [])
            ),
            "STEP_GUIDANCE": (
                StepGuidance(step_guidance).text
                if step_guidance
                else "No additional Step Guidance."
            ),
            "STEP_GUIDANCE_PRECEDENCE": STEP_GUIDANCE_PRECEDENCE,
            "FIX_LIST": fix_list or ["None"],
            "CODER_RESULT": json.dumps(result_to_dict(coder_result), indent=2),
            "REVIEW_RESULT": json.dumps(result_to_dict(review_result), indent=2),
            "TIMESTAMP": datetime.now().isoformat(timespec="seconds"),
        }
        return render_template(self.bundle.prompts / template_name, values)

    def bundle_memory_docs(self) -> list[Path | str]:
        if not self.use_self_improvement_wiki:
            return ["Disabled for this run."]
        return [self.bundle.root / DEFAULT_SELF_IMPROVEMENT_WIKI_PATH / "index.md"]

    def build_self_improvement_prompt(
        self,
        state_path: Path,
        board_path: Path,
        wiki_root: Path,
        max_lessons: int,
        run_context_path: Path | None = None,
        compiler_repo_root: Path | None = None,
    ) -> str:
        compiler_repo_root = compiler_repo_root or self.repo_root
        values = {
            "BUNDLE_ROOT": self.bundle.root,
            "REPO_ROOT": self.repo_root,
            "COMPILER_REPO_ROOT": compiler_repo_root,
            "PRD_PATH": self.prd_path,
            "ISSUES_INDEX": self.issues_index,
            "LOOP_STATE_PATH": state_path,
            "LOOP_BOARD_PATH": board_path,
            "LOOP_LOG_ROOT": self.log_root,
            "RUN_CONTEXT_PATH": run_context_path or "None",
            "SELF_IMPROVEMENT_WIKI_ROOT": wiki_root,
            "SELF_IMPROVEMENT_WIKI_SCHEMA": wiki_root.parent / "SCHEMA.md",
            "SELF_IMPROVEMENT_WIKI_INDEX": wiki_root / "index.md",
            "MAX_LESSONS": max_lessons,
            "TIMESTAMP": datetime.now().isoformat(timespec="seconds"),
        }
        return render_template(self.bundle.prompts / "self-improvement.md", values)


def result_to_dict(result: RoleResult | None) -> dict[str, Any]:
    if result is None:
        return {}
    return {
        "status": result.status,
        "summary": result.summary,
        "changed_files": result.changed_files,
        "verification_commands": result.verification_commands,
        "findings": result.findings,
        "fix_list": result.fix_list,
        "residual_risks": result.residual_risks,
    }


def list_of_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def slugify_log_token(value: str | None) -> str:
    if not value:
        return ""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    # Truncation can expose the separator that was previously followed by
    # characters outside the length limit, so normalize the boundary again.
    return slug[:48].strip("-")


def compact_log_identity_token(
    value: str,
    max_length: int = LOG_ATTEMPT_TOKEN_MAX_LENGTH,
) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    if slug and len(slug) <= max_length:
        return slug

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:LOG_TOKEN_HASH_LENGTH]
    if max_length <= LOG_TOKEN_HASH_LENGTH:
        return digest[:max_length]

    prefix_length = max_length - LOG_TOKEN_HASH_LENGTH - 1
    prefix = slug[:prefix_length].rstrip("-")
    return f"{prefix}-{digest}" if prefix else digest


def _fit_role_log_prefix(
    *,
    log_root: Path,
    readable_prefix: str,
    issue_slug: str,
    attempt_identity: str,
    step_instance_id: str | None,
    role: str,
    pass_number: int,
) -> str:
    if _role_log_prefix_fits(log_root, readable_prefix):
        return readable_prefix

    prefix_parts = [
        issue_slug,
        (
            "attempt-"
            + compact_log_identity_token(
                attempt_identity,
                LOG_FALLBACK_ATTEMPT_TOKEN_MAX_LENGTH,
            )
        ),
    ]
    if step_instance_id:
        prefix_parts.extend(
            [
                PORTABLE_LOG_MARKER,
                "step",
                slugify_log_token(step_instance_id) or "instance",
            ]
        )
    prefix_parts.extend(
        [
            compact_log_identity_token(role, LOG_FALLBACK_ROLE_TOKEN_MAX_LENGTH),
            f"pass{pass_number}",
        ]
    )
    compact_prefix = "-".join(prefix_parts)
    if _role_log_prefix_fits(log_root, compact_prefix):
        return compact_prefix

    raise OSError(
        "Dev Loop's issue-local log path is too long for portable Windows "
        f"writes even after filename compaction: {log_root}. "
        "Choose a shorter implementation worktree path."
    )


def _role_log_prefix_fits(log_root: Path, prefix: str) -> bool:
    longest_path = (log_root / f"{prefix}{LONGEST_ROLE_LOG_SUFFIX}").resolve()
    return len(str(longest_path)) <= MAX_PORTABLE_LOG_PATH_LENGTH


def _confined_log_path(path: Path, log_root: Path) -> Path:
    resolved_root = log_root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError(
            f"Refusing to write a log outside the configured log root: {path}"
        ) from error
    return resolved_path


def extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    code_block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if code_block:
        try:
            return json.loads(code_block.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None

    return None


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


def is_retryable_codex_connection_failure(stderr: str) -> bool:
    lower = stderr.lower()
    return (
        "failed to connect to websocket" in lower
        or "responses_websocket" in lower
    )
