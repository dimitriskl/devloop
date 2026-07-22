from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .issue_pack import Issue
from .portable_workflow import IssueStatus, parse_issue_status
from .terminal_text import compact_terminal_text


REVIEW_SCREEN_PATH = "Dev Loop > Completion Review"
REVIEW_SUCCESS_HEADING = "WORKFLOW FINISHED - SUCCESS"
REVIEW_ATTENTION_HEADING = "WORKFLOW FINISHED - ATTENTION REQUIRED"
ISSUE_DETAIL_MAX_LENGTH = 100


class RunReviewAction(str, Enum):
    RERUN_REMAINING = "rerun_remaining"
    EXIT = "exit"


@dataclass(frozen=True)
class IssueReviewItem:
    issue_number: str
    title: str
    status: IssueStatus
    detail: str = ""


@dataclass(frozen=True)
class RunReview:
    issues: tuple[IssueReviewItem, ...]
    loop_state_path: Path
    rerun_available: bool

    @property
    def completed_count(self) -> int:
        return sum(
            item.status is IssueStatus.COMPLETED
            for item in self.issues
        )

    @property
    def remaining_issue_numbers(self) -> tuple[str, ...]:
        return tuple(
            item.issue_number
            for item in self.issues
            if item.status is not IssueStatus.COMPLETED
        )

    @property
    def remaining_count(self) -> int:
        return len(self.remaining_issue_numbers)


def build_run_review(
    issues: Sequence[Issue],
    issue_states: Mapping[str, Any],
    *,
    loop_state_path: Path,
    rerun_available: bool,
) -> RunReview:
    items = tuple(
        _build_issue_review(issue, issue_states.get(issue.number))
        for issue in issues
    )
    return RunReview(
        issues=items,
        loop_state_path=loop_state_path,
        rerun_available=rerun_available and any(
            item.status is not IssueStatus.COMPLETED for item in items
        ),
    )


def run_review_options(review: RunReview) -> tuple[tuple[str, str], ...]:
    options: list[tuple[str, str]] = []
    if review.rerun_available:
        issue_label = _counted_issue(review.remaining_count)
        options.append(
            (
                RunReviewAction.RERUN_REMAINING.value,
                f"Rerun {review.remaining_count} unfinished {issue_label}",
            )
        )
    options.append((RunReviewAction.EXIT.value, "Exit Dev Loop"))
    return tuple(options)


def render_run_review(review: RunReview, selected_action: RunReviewAction) -> str:
    total = len(review.issues)
    heading = (
        REVIEW_SUCCESS_HEADING
        if review.remaining_count == 0
        else REVIEW_ATTENTION_HEADING
    )
    lines = [
        REVIEW_SCREEN_PATH,
        "",
        heading,
        f"Completed: {review.completed_count}/{total}    "
        f"Remaining: {review.remaining_count}",
        "",
        "Issue review",
    ]
    lines.extend(_render_issue_item(item) for item in review.issues)
    lines.extend(("", "Review conclusion"))
    if review.remaining_count == 0:
        lines.append("All selected issues were completed successfully.")
    elif review.rerun_available:
        issue_label = _counted_issue(review.remaining_count)
        remaining_verb = _remaining_verb(review.remaining_count)
        lines.append(
            f"{review.remaining_count} {issue_label} {remaining_verb}. "
            "A rerun will process only "
            f"the {review.remaining_count} unfinished {issue_label}; completed issues "
            "will be skipped."
        )
    else:
        issue_label = _counted_issue(review.remaining_count)
        remaining_verb = _remaining_verb(review.remaining_count)
        lines.append(
            f"{review.remaining_count} {issue_label} {remaining_verb}. "
            "Review the saved loop state "
            "before starting another run."
        )
    lines.extend(
        (
            f"Loop state: {review.loop_state_path}",
            "",
            "Selected action",
            _selected_action_summary(review, selected_action),
        )
    )
    return "\n".join(lines)


def _build_issue_review(issue: Issue, raw_state: Any) -> IssueReviewItem:
    state = raw_state if isinstance(raw_state, dict) else {}
    status = (
        IssueStatus.COMPLETED
        if issue.completed
        else parse_issue_status(state.get("status")) or IssueStatus.PENDING
    )
    return IssueReviewItem(
        issue_number=compact_terminal_text(
            issue.number,
            max_length=32,
        ),
        title=compact_terminal_text(issue.title, max_length=120),
        status=status,
        detail=_issue_detail(status, state),
    )


def _issue_detail(status: IssueStatus, state: Mapping[str, Any]) -> str:
    if status is IssueStatus.WAITING_ON_DEPENDENCY:
        waiting_on = state.get("waiting_on")
        if isinstance(waiting_on, list):
            dependencies = ", ".join(str(item) for item in waiting_on if item)
            if dependencies:
                return compact_terminal_text(
                    f"waiting on {dependencies}",
                    max_length=ISSUE_DETAIL_MAX_LENGTH,
                )
    for key in ("blocked_summary", "qa_summary", "review_summary"):
        value = state.get(key)
        if value:
            return compact_terminal_text(
                value,
                max_length=ISSUE_DETAIL_MAX_LENGTH,
            )
    return ""


def _render_issue_item(item: IssueReviewItem) -> str:
    status_label = {
        IssueStatus.WAITING_ON_DEPENDENCY: "WAITING",
        IssueStatus.WAITING_FOR_INPUT: "INPUT",
        IssueStatus.CHANGES_REQUESTED: "CHANGES",
        IssueStatus.IN_PROGRESS: "IN PROGRESS",
    }.get(item.status, item.status.value)
    rendered = f"{status_label:<10} {item.issue_number}  {item.title}"
    if item.detail:
        rendered = f"{rendered} - {item.detail}"
    return rendered


def _selected_action_summary(
    review: RunReview,
    selected_action: RunReviewAction,
) -> str:
    if selected_action is RunReviewAction.RERUN_REMAINING:
        issue_label = _counted_issue(review.remaining_count)
        return (
            f"Rerun only the {review.remaining_count} unfinished {issue_label} now. "
            "Completed issues remain skipped."
        )
    if review.remaining_count:
        return "Exit now. The unfinished issue state is saved for a later run."
    return "Exit Dev Loop. No unfinished selected issues remain."


def _counted_issue(count: int) -> str:
    return "issue" if count == 1 else "issues"


def _remaining_verb(count: int) -> str:
    return "remains" if count == 1 else "remain"
