from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TextIO

from .codex_events import (
    CodexTurnOutcome,
    codex_turn_outcome,
    parse_codex_event,
    render_safe_codex_activity,
)
from .issue_pack import Issue
from .self_improvement_wiki import DEFAULT_SELF_IMPROVEMENT_WIKI_PATH
from .statusui import Stage, WaitingIndicator
from .subprocess_utils import (
    output_text,
    reap_process_after_terminal_event,
    run_captured_text,
    terminate_process,
)
from .templates import BundleContext, Preset, render_template

_LEGACY_APPROVAL_FLAG: bool | None = None
CODEX_CONNECTION_RETRY_DELAY_SECONDS = 30
STREAM_THREAD_JOIN_SECONDS = 1.0
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
) -> list[str]:
    command = [
        codex,
        "exec",
        "-C",
        str(repo_root),
        "-s",
        sandbox,
    ]
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
    )
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
            stderr_activity_callback,
        ),
        daemon=True,
    )
    turn_outcome: CodexTurnOutcome | None = None

    if indicator is not None:
        indicator.start()
    input_thread.start()
    stderr_thread.start()
    try:
        for line in process.stdout:
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
        if indicator is not None:
            indicator.stop()
        input_thread.join(timeout=STREAM_THREAD_JOIN_SECONDS)
        stderr_thread.join(timeout=STREAM_THREAD_JOIN_SECONDS)

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
        prefix = f"{prefix} {context}:"
    print(f"{prefix} {activity}")


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

    def write_log_text(self, path: Path, text: str) -> None:
        self.ensure_log_root()
        path.write_text(text, encoding="utf-8")

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
    ) -> RoleResult:
        prompt = self.build_prompt(
            role=role,
            issue=issue,
            pass_number=pass_number,
            fix_list=fix_list or [],
            coder_result=coder_result,
            review_result=review_result,
        )

        attempt_slug = slugify_log_token(attempt_label)
        prefix_parts = [issue.number]
        if attempt_slug:
            prefix_parts.append(attempt_slug)
        prefix_parts.extend([role, f"pass{pass_number}"])
        prefix = "-".join(prefix_parts)
        prompt_path = self.log_root / f"{prefix}.prompt.md"
        stdout_path = self.log_root / f"{prefix}.stdout.jsonl"
        stderr_path = self.log_root / f"{prefix}.stderr.txt"
        message_path = self.log_root / f"{prefix}.last-message.json"
        self.write_log_text(prompt_path, prompt)

        schema_path = self.bundle.schemas / "role-result.schema.json"
        command = build_codex_exec_command(
            codex=self.codex,
            repo_root=self.repo_root,
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
            stage=stage_for_role(role),
            activity_context=_role_activity_context(
                progress=progress,
                pass_number=pass_number,
            ),
            activity_callback=activity_callback,
        )
        self.write_log_text(stdout_path, result.stdout)
        self.write_log_text(stderr_path, result.stderr)

        if result.returncode != 0:
            return RoleResult(
                status="BLOCKED",
                summary=f"codex exec failed with exit code {result.returncode}. See {stderr_path}.",
                raw_message=result.stderr,
            )

        message = message_path.read_text(encoding="utf-8") if message_path.is_file() else result.stdout
        return RoleResult.from_message(message)

    def run_codex_exec_with_connection_retries(
        self,
        command: list[str],
        prompt: str,
        stdout_path: Path,
        stderr_path: Path,
        stage: Stage = Stage.DEVELOPMENT,
        activity_context: str = "",
        activity_callback: ActivityCallback | None = None,
    ) -> subprocess.CompletedProcess[str]:
        attempt = 1
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        while True:
            result = run_streaming_codex_command(
                command,
                input_text=prompt,
                cwd=self.repo_root,
                stage=stage,
                activity_context=activity_context,
                activity_callback=activity_callback,
            )
            current_stdout = output_text(result.stdout)
            current_stderr = output_text(result.stderr)
            stdout_parts.append(current_stdout)
            stderr_parts.append(current_stderr)
            result.stdout = "".join(stdout_parts)
            result.stderr = "".join(stderr_parts)

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
            self.write_log_text(stdout_path, result.stdout)
            self.write_log_text(stderr_path, result.stderr)
            time.sleep(CODEX_CONNECTION_RETRY_DELAY_SECONDS)
            attempt += 1

    def render_dry_run_prompts(self, issue: Issue) -> None:
        for role in ("coder", "reviewer", "qa"):
            prompt = self.build_prompt(role=role, issue=issue, pass_number=1, fix_list=[])
            path = self.log_root / f"{issue.number}-{role}-dry-run.prompt.md"
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
        prompt_path = log_root / f"{prefix}.prompt.md"
        stdout_path = log_root / f"{prefix}.stdout.jsonl"
        stderr_path = log_root / f"{prefix}.stderr.txt"
        message_path = log_root / f"{prefix}.last-message.json"
        self.write_log_text(prompt_path, prompt)

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
        )
        self.write_log_text(stdout_path, result.stdout)
        self.write_log_text(stderr_path, result.stderr)

        if result.returncode != 0:
            return RoleResult(
                status="BLOCKED",
                summary=f"self-improvement compiler failed with exit code {result.returncode}. See {stderr_path}.",
                raw_message=result.stderr,
            )

        message = message_path.read_text(encoding="utf-8") if message_path.is_file() else result.stdout
        return RoleResult.from_message(message)

    def build_prompt(
        self,
        role: str,
        issue: Issue,
        pass_number: int,
        fix_list: list[str],
        coder_result: RoleResult | None = None,
        review_result: RoleResult | None = None,
    ) -> str:
        role_config = self.preset.roles.get(role, {})
        template_name = {
            "coder": "coder.md",
            "reviewer": "reviewer.md",
            "qa": "qa.md",
        }[role]

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
            "REQUIRED_DOCS": self.preset.required_docs,
            "RUN_GOAL": DEVLOOP_RUN_GOAL,
            "BUNDLE_MEMORY_DOCS": self.bundle_memory_docs(),
            "SKILL_PATHS": role_config.get("skills", []),
            "AGENT_PATHS": role_config.get("agents", []),
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
    return slug[:48]


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


def is_retryable_codex_connection_failure(stderr: str) -> bool:
    lower = stderr.lower()
    return (
        "failed to connect to websocket" in lower
        or "responses_websocket" in lower
    )
