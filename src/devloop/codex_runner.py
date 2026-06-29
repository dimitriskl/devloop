from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .issue_pack import Issue
from .self_improvement_wiki import DEFAULT_SELF_IMPROVEMENT_WIKI_PATH
from .subprocess_utils import output_text, run_captured_text
from .templates import BundleContext, Preset, render_template

_LEGACY_APPROVAL_FLAG: bool | None = None
CODEX_CONNECTION_RETRY_DELAY_SECONDS = 30


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
    return shutil.which(codex) or codex


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
    ) -> subprocess.CompletedProcess[str]:
        attempt = 1
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        while True:
            result = run_captured_text(
                command,
                input_text=prompt,
                cwd=self.repo_root,
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
            print(retry_message.strip())
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
            "BUNDLE_MEMORY_DOCS": [self.bundle.root / DEFAULT_SELF_IMPROVEMENT_WIKI_PATH / "index.md"],
            "SKILL_PATHS": role_config.get("skills", []),
            "AGENT_PATHS": role_config.get("agents", []),
            "FIX_LIST": fix_list or ["None"],
            "CODER_RESULT": json.dumps(result_to_dict(coder_result), indent=2),
            "REVIEW_RESULT": json.dumps(result_to_dict(review_result), indent=2),
            "TIMESTAMP": datetime.now().isoformat(timespec="seconds"),
        }
        return render_template(self.bundle.prompts / template_name, values)

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
