from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from devloop.application.review_qa import QaCompleted
from devloop.domain.identifiers import IssueId
from devloop.ui.shared import CheckMatrixView, StreamingOutputView


class QaView(Vertical):
    DEFAULT_CSS = """
    QaView { height: 1fr; display: none; padding: 0 1; }
    QaView #qa-heading { height: 3; }
    QaView #qa-stream { height: 1fr; border: solid $primary-background; }
    QaView #qa-result { height: 10; border: solid $primary-background; }
    """

    def compose(self) -> ComposeResult:
        yield Static("QA", id="qa-heading")
        yield StreamingOutputView(id="qa-stream", wrap=True, markup=False)
        yield CheckMatrixView(id="qa-result", wrap=True, markup=False)

    def show_running(self, issue_id: IssueId) -> None:
        self.display = True
        self.query_one("#qa-heading", Static).update(f"QA | {issue_id.value} | verification-only")
        self.query_one("#qa-stream", StreamingOutputView).clear()
        self.query_one("#qa-result", CheckMatrixView).clear()

    def append_activity(self, delta: str) -> None:
        self.query_one("#qa-stream", StreamingOutputView).append_delta(delta)

    def show_completed(self, completed: QaCompleted) -> None:
        result = self.query_one("#qa-result", CheckMatrixView)
        result.show_checks(completed.result.checks)
        result.write(f"Outcome: {completed.outcome.value}")
        result.write(completed.result.summary)
        if completed.result.source_state_changed:
            result.write(completed.result.state_change_evidence)
