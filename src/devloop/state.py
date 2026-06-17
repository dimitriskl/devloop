from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .codex_runner import RoleResult
from .issue_pack import Issue


class LoopStateWriter:
    def __init__(self, issues_index: Path) -> None:
        self.issues_index = issues_index
        self.state_path = issues_index.with_name(f"{issues_index.stem}.loop.state.json")
        self.board_path = issues_index.with_name(f"{issues_index.stem}.loop.md")
        self.state: dict[str, Any] = {
            "started_at": now(),
            "issues_index": str(issues_index),
            "events": [],
            "issues": {},
        }

    def record_run_start(self, repo_root: Path, prd_path: Path, issues: list[str], dry_run: bool) -> None:
        self.state.update(
            {
                "repo_root": str(repo_root),
                "prd_path": str(prd_path),
                "selected_issues": issues,
                "dry_run": dry_run,
            }
        )
        self.add_event("run-start", {"issues": issues, "dry_run": dry_run})
        self.flush()

    def record_issue_start(self, issue: Issue) -> None:
        self.issue_state(issue).update(
            {
                "title": issue.title,
                "path": str(issue.path),
                "status": "In Progress",
                "started_at": now(),
            }
        )
        self.add_event("issue-start", {"issue": issue.number})
        self.flush()

    def record_issue_dry_run(self, issue: Issue) -> None:
        self.issue_state(issue)["status"] = "Dry Run"
        self.add_event("issue-dry-run", {"issue": issue.number})
        self.flush()

    def record_role_result(self, issue: Issue, role: str, pass_number: int, result: RoleResult) -> None:
        issue_state = self.issue_state(issue)
        issue_state.setdefault("passes", []).append(
            {
                "role": role,
                "pass": pass_number,
                "result": result_summary(result),
                "timestamp": now(),
            }
        )
        self.add_event(
            "role-result",
            {"issue": issue.number, "role": role, "pass": pass_number, "status": result.status},
        )
        self.flush()

    def record_issue_completed(
        self,
        issue: Issue,
        coder: RoleResult,
        reviewer: RoleResult,
        qa: RoleResult,
    ) -> None:
        issue_state = self.issue_state(issue)
        issue_state["status"] = "Completed"
        issue_state["completed_at"] = now()
        issue_state["changed_files"] = coder.changed_files
        issue_state["verification_commands"] = sorted(
            set(coder.verification_commands + qa.verification_commands)
        )
        issue_state["review_summary"] = reviewer.summary
        issue_state["qa_summary"] = qa.summary
        self.add_event("issue-completed", {"issue": issue.number})
        self.flush()

    def record_issue_blocked(self, issue: Issue, gate: str, result: RoleResult) -> None:
        issue_state = self.issue_state(issue)
        issue_state["status"] = "Blocked"
        issue_state["blocked_at"] = now()
        issue_state["blocked_gate"] = gate
        issue_state["blocked_summary"] = result.summary
        issue_state["fix_list"] = result.fix_list
        self.add_event("issue-blocked", {"issue": issue.number, "gate": gate})
        self.flush()

    def issue_state(self, issue: Issue) -> dict[str, Any]:
        return self.state.setdefault("issues", {}).setdefault(issue.number, {})

    def add_event(self, event_type: str, data: dict[str, Any]) -> None:
        self.state.setdefault("events", []).append(
            {
                "type": event_type,
                "timestamp": now(),
                **data,
            }
        )

    def flush(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        self.board_path.write_text(render_board(self.state), encoding="utf-8")


def result_summary(result: RoleResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "summary": result.summary,
        "changed_files": result.changed_files,
        "verification_commands": result.verification_commands,
        "findings": result.findings,
        "fix_list": result.fix_list,
        "residual_risks": result.residual_risks,
    }


def render_board(state: dict[str, Any]) -> str:
    lines = [
        "# Dev Loop State",
        "",
        f"Started: {state.get('started_at', '')}",
        f"Repository: `{state.get('repo_root', '')}`",
        f"PRD: `{state.get('prd_path', '')}`",
        "",
        "## Task Board",
        "",
        "| Issue | Title | Status |",
        "| --- | --- | --- |",
    ]

    for number, item in state.get("issues", {}).items():
        lines.append(f"| {number} | {item.get('title', '')} | {item.get('status', '')} |")

    lines.extend(["", "## Events", ""])
    for event in state.get("events", []):
        lines.append(f"- {event.get('timestamp')} `{event.get('type')}` issue={event.get('issue', '')} status={event.get('status', '')}")

    return "\n".join(lines) + "\n"


def mark_issue_completed(
    issue_path: Path,
    coder: RoleResult,
    reviewer: RoleResult,
    qa: RoleResult,
) -> None:
    text = issue_path.read_text(encoding="utf-8")
    text = re.sub(r"(?im)^Completed:\s*\[\s*\]", "Completed: [x]", text, count=1)
    text = mark_acceptance_criteria(text)

    notes = [
        "",
        "## Implementation Notes",
        "",
        f"Completed: {now()}",
        "",
        "### Changed Files",
        *[f"- `{path}`" for path in coder.changed_files],
        "",
        "### Verification",
        *[f"- `{command}`" for command in sorted(set(coder.verification_commands + qa.verification_commands))],
        "",
        "### Review",
        reviewer.summary or "- PASS",
        "",
        "### QA",
        qa.summary or "- PASS",
        "",
    ]

    if "## Implementation Notes" not in text:
        text = text.rstrip() + "\n" + "\n".join(notes)

    issue_path.write_text(text, encoding="utf-8")


def mark_acceptance_criteria(text: str) -> str:
    match = re.search(r"(?ims)^## Acceptance criteria\s*(?P<body>.*?)(?=^## |\Z)", text)
    if not match:
        return text

    body = match.group("body")
    updated_body = re.sub(r"(?m)^(\s*-\s*)\[\s*\]", r"\1[x]", body)
    return text[: match.start("body")] + updated_body + text[match.end("body") :]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")

