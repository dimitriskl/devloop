from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog, Static

from devloop.domain.finalization import HandoffSummary
from devloop.domain.run import WorkflowRunSnapshot
from devloop.ui.shared import AttemptTimelineView


class FinalizationView(Vertical):
    DEFAULT_CSS = """
    FinalizationView { height: 1fr; display: none; padding: 0 1; }
    FinalizationView #finalization-heading { height: 3; }
    FinalizationView #finalization-summary { height: 1fr; border: solid $primary-background; }
    FinalizationView #finalization-timeline { height: 8; border: solid $primary-background; }
    """

    def compose(self) -> ComposeResult:
        yield Static("Workspace finalization", id="finalization-heading")
        yield RichLog(id="finalization-summary", wrap=True, markup=False)
        yield AttemptTimelineView(id="finalization-timeline", wrap=True, markup=False)

    def show_snapshot(self, snapshot: WorkflowRunSnapshot) -> None:
        self.display = True
        summary = self.query_one("#finalization-summary", RichLog)
        summary.clear()
        completed = sum(1 for issue in snapshot.issues if issue.status.value == "COMPLETED")
        summary.write(f"Completed Issues: {completed} of {len(snapshot.issues)}")
        summary.write("Workspace disposition: leave intact")
        summary.write(
            "No merge, push, pull request, branch deletion, or worktree removal is implicit."
        )
        self.query_one("#finalization-timeline", AttemptTimelineView).show_attempts(
            snapshot.attempts
        )

    def show_completed(
        self,
        snapshot: WorkflowRunSnapshot,
        handoff: HandoffSummary,
    ) -> None:
        self.show_snapshot(snapshot)
        summary = self.query_one("#finalization-summary", RichLog)
        summary.clear()
        summary.write(f"Completed Issues: {len(handoff.completed_issues)}")
        summary.write(f"Changed files: {len(handoff.changed_files)}")
        for path in handoff.changed_files:
            summary.write(f"  {path}")
        summary.write(f"Verification evidence: {len(handoff.verification_evidence)}")
        for evidence in handoff.verification_evidence:
            summary.write(f"  {evidence}")
        summary.write(f"Residual risks: {len(handoff.residual_risks)}")
        for risk in handoff.residual_risks:
            summary.write(f"  {risk}")
        summary.write("Workspace disposition: leave intact")
        summary.write(
            "No merge, push, pull request, branch deletion, or worktree removal was performed."
        )
