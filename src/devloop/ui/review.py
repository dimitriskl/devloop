from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from devloop.application.review_qa import ReviewCompleted
from devloop.domain.identifiers import IssueId
from devloop.ui.shared import FindingsView, StreamingOutputView


class CodeReviewView(Vertical):
    DEFAULT_CSS = """
    CodeReviewView { height: 1fr; display: none; padding: 0 1; }
    CodeReviewView #review-heading { height: 3; }
    CodeReviewView #review-stream { height: 1fr; border: solid $primary-background; }
    CodeReviewView #review-result { height: 9; border: solid $primary-background; }
    """

    def compose(self) -> ComposeResult:
        yield Static("Code review", id="review-heading")
        yield StreamingOutputView(id="review-stream", wrap=True, markup=False)
        yield FindingsView(id="review-result", wrap=True, markup=False)

    def show_running(self, issue_id: IssueId) -> None:
        self.display = True
        self.query_one("#review-heading", Static).update(
            f"Code review | {issue_id.value} | read-only"
        )
        self.query_one("#review-stream", StreamingOutputView).clear()
        self.query_one("#review-result", FindingsView).clear()

    def append_activity(self, delta: str) -> None:
        self.query_one("#review-stream", StreamingOutputView).append_delta(delta)

    def show_completed(self, completed: ReviewCompleted) -> None:
        result = self.query_one("#review-result", FindingsView)
        result.show_findings(completed.result.findings)
        result.write(f"Outcome: {completed.outcome.value}")
        result.write(completed.result.summary)
