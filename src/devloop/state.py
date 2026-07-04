from __future__ import annotations

import json
import re
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
        self.prd_state_path: Path | None = None
        self.prd_board_path: Path | None = None
        self.state: dict[str, Any] = {
            "started_at": now(),
            "issues_index": str(issues_index),
            "events": [],
            "issues": {},
        }

    def record_run_start(self, repo_root: Path, prd_path: Path, issues: list[str], dry_run: bool) -> None:
        self.prd_state_path = prd_path.parent / "devloop.status.json"
        self.prd_board_path = prd_path.parent / "devloop.status.md"
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

    def record_issue_start(
        self,
        issue: Issue,
        attempt_label: str | None = None,
        retry_round: int | None = None,
    ) -> None:
        started_at = now()
        issue_state = self.issue_state(issue)
        issue_state.update(
            {
                "title": issue.title,
                "path": str(issue.path),
                "status": f"In Progress ({attempt_label})" if attempt_label else "In Progress",
                "last_started_at": started_at,
            }
        )
        issue_state.setdefault("started_at", started_at)
        if attempt_label:
            issue_state["attempt_label"] = attempt_label
        if retry_round is not None:
            issue_state["retry_round"] = retry_round

        event = {"issue": issue.number}
        if attempt_label:
            event["attempt"] = attempt_label
        if retry_round is not None:
            event["retry_round"] = retry_round
        self.add_event("issue-start", event)
        self.flush()

    def record_issue_dry_run(self, issue: Issue) -> None:
        self.issue_state(issue)["status"] = "Dry Run"
        self.add_event("issue-dry-run", {"issue": issue.number})
        self.flush()

    def record_role_result(
        self,
        issue: Issue,
        role: str,
        pass_number: int,
        result: RoleResult,
        attempt_label: str | None = None,
        retry_round: int | None = None,
    ) -> None:
        issue_state = self.issue_state(issue)
        pass_entry = {
            "role": role,
            "pass": pass_number,
            "result": result_summary(result),
            "timestamp": now(),
        }
        if attempt_label:
            pass_entry["attempt"] = attempt_label
        if retry_round is not None:
            pass_entry["retry_round"] = retry_round
        issue_state.setdefault("passes", []).append(pass_entry)
        event = {"issue": issue.number, "role": role, "pass": pass_number, "status": result.status}
        if attempt_label:
            event["attempt"] = attempt_label
        if retry_round is not None:
            event["retry_round"] = retry_round
        self.add_event(
            "role-result",
            event,
        )
        self.flush()

    def record_issue_completed(
        self,
        issue: Issue,
        coder: RoleResult,
        reviewer: RoleResult,
        qa: RoleResult,
        attempt_label: str | None = None,
        retry_round: int | None = None,
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
        if attempt_label:
            issue_state["completed_attempt"] = attempt_label
        if retry_round is not None:
            issue_state["completed_retry_round"] = retry_round
        event = {"issue": issue.number}
        if attempt_label:
            event["attempt"] = attempt_label
        if retry_round is not None:
            event["retry_round"] = retry_round
        self.add_event("issue-completed", event)
        self.flush()

    def record_issue_blocked(
        self,
        issue: Issue,
        gate: str,
        result: RoleResult,
        attempt_label: str | None = None,
        retry_round: int | None = None,
    ) -> None:
        issue_state = self.issue_state(issue)
        issue_state["status"] = "Blocked"
        issue_state["blocked_at"] = now()
        issue_state["blocked_gate"] = gate
        issue_state["blocked_summary"] = result.summary
        issue_state["fix_list"] = result.fix_list
        if attempt_label:
            issue_state["blocked_attempt"] = attempt_label
        if retry_round is not None:
            issue_state["blocked_retry_round"] = retry_round
        event = {"issue": issue.number, "gate": gate}
        if attempt_label:
            event["attempt"] = attempt_label
        if retry_round is not None:
            event["retry_round"] = retry_round
        self.add_event("issue-blocked", event)
        self.flush()

    def record_blocked_retry_round_start(self, retry_round: int, issues: list[str]) -> None:
        self.state["blocked_retry"] = {
            "current_round": retry_round,
            "remaining_issues": issues,
            "updated_at": now(),
        }
        self.add_event("blocked-retry-start", {"retry_round": retry_round, "issues": issues})
        self.flush()

    def record_self_improvement_wiki_result(self, wiki_root: Path, result: RoleResult) -> None:
        self.state["self_improvement_wiki"] = {
            "path": str(wiki_root),
            "status": result.status,
            "summary": result.summary,
            "changed_files": result.changed_files,
            "findings": result.findings,
            "residual_risks": result.residual_risks,
            "updated_at": now(),
        }
        self.add_event("self-improvement-wiki", {"status": result.status})
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
        state_text = json.dumps(self.state, indent=2)
        board_text = render_board(self.state)
        write_text_creating_parent(self.state_path, state_text)
        write_text_creating_parent(self.board_path, board_text)
        if self.prd_state_path is not None:
            write_text_creating_parent(self.prd_state_path, state_text)
        if self.prd_board_path is not None:
            write_text_creating_parent(self.prd_board_path, board_text)


def write_text_creating_parent(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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
        details = [
            f"issue={event.get('issue', '')}",
            f"status={event.get('status', '')}",
        ]
        if event.get("retry_round") is not None:
            details.append(f"retry_round={event.get('retry_round')}")
        if event.get("attempt"):
            details.append(f"attempt={event.get('attempt')}")
        if event.get("issues"):
            details.append(f"issues={', '.join(event.get('issues', []))}")
        lines.append(f"- {event.get('timestamp')} `{event.get('type')}` {' '.join(details)}")

    blocked_retry = state.get("blocked_retry")
    if blocked_retry:
        lines.extend(
            [
                "",
                "## Blocked Retry",
                "",
                f"Current round: `{blocked_retry.get('current_round', '')}`",
                f"Remaining issues: `{', '.join(blocked_retry.get('remaining_issues', []))}`",
            ]
        )

    self_improvement_wiki = state.get("self_improvement_wiki")
    if self_improvement_wiki:
        lines.extend(
            [
                "",
                "## Self-Improvement Wiki",
                "",
                f"Path: `{self_improvement_wiki.get('path', '')}`",
                f"Status: `{self_improvement_wiki.get('status', '')}`",
                f"Summary: {self_improvement_wiki.get('summary', '')}",
            ]
        )

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
