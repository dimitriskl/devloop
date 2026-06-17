from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .issue_pack import Issue
from .templates import BundleContext, Preset, render_template


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
        self.codex = codex
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self.dry_run = dry_run
        self.log_root = issues_index.parent / ".loop.logs"
        self.log_root.mkdir(parents=True, exist_ok=True)

    def run_role(
        self,
        role: str,
        issue: Issue,
        pass_number: int,
        fix_list: list[str] | None = None,
        coder_result: RoleResult | None = None,
        review_result: RoleResult | None = None,
    ) -> RoleResult:
        prompt = self.build_prompt(
            role=role,
            issue=issue,
            pass_number=pass_number,
            fix_list=fix_list or [],
            coder_result=coder_result,
            review_result=review_result,
        )

        prefix = f"{issue.number}-{role}-pass{pass_number}"
        prompt_path = self.log_root / f"{prefix}.prompt.md"
        stdout_path = self.log_root / f"{prefix}.stdout.jsonl"
        stderr_path = self.log_root / f"{prefix}.stderr.txt"
        message_path = self.log_root / f"{prefix}.last-message.json"
        prompt_path.write_text(prompt, encoding="utf-8")

        schema_path = self.bundle.schemas / "role-result.schema.json"
        command = [
            self.codex,
            "exec",
            "-C",
            str(self.repo_root),
            "-s",
            self.sandbox,
            "-a",
            self.approval_policy,
            "--output-schema",
            str(schema_path),
            "-o",
            str(message_path),
            "--json",
            "-",
        ]

        result = subprocess.run(
            command,
            input=prompt,
            text=True,
            cwd=self.repo_root,
            capture_output=True,
            check=False,
        )
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")

        if result.returncode != 0:
            return RoleResult(
                status="BLOCKED",
                summary=f"codex exec failed with exit code {result.returncode}. See {stderr_path}.",
                raw_message=result.stderr,
            )

        message = message_path.read_text(encoding="utf-8") if message_path.is_file() else result.stdout
        return RoleResult.from_message(message)

    def render_dry_run_prompts(self, issue: Issue) -> None:
        for role in ("coder", "reviewer", "qa"):
            prompt = self.build_prompt(role=role, issue=issue, pass_number=1, fix_list=[])
            path = self.log_root / f"{issue.number}-{role}-dry-run.prompt.md"
            path.write_text(prompt, encoding="utf-8")
            print(f"[dry-run] Wrote {path}")

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
            "SKILL_PATHS": role_config.get("skills", []),
            "AGENT_PATHS": role_config.get("agents", []),
            "FIX_LIST": fix_list or ["None"],
            "CODER_RESULT": json.dumps(result_to_dict(coder_result), indent=2),
            "REVIEW_RESULT": json.dumps(result_to_dict(review_result), indent=2),
            "TIMESTAMP": datetime.now().isoformat(timespec="seconds"),
        }
        return render_template(self.bundle.prompts / template_name, values)


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

