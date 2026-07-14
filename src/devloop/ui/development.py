from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog, Static

from devloop.application.development import DevelopmentCompleted, WorkspacePrepared
from devloop.ui.shared import IssueBriefView, StreamingOutputView


class DevelopmentView(Vertical):
    DEFAULT_CSS = """
    DevelopmentView { height: 1fr; display: none; padding: 0 1; }
    DevelopmentView #development-heading { height: 3; }
    DevelopmentView #development-issue { height: 7; border: solid $primary-background; }
    DevelopmentView #development-stream { height: 1fr; border: solid $primary-background; }
    DevelopmentView #development-result { height: 6; border: solid $primary-background; }
    """

    def compose(self) -> ComposeResult:
        yield Static("Development", id="development-heading")
        yield IssueBriefView(id="development-issue", wrap=True, markup=False)
        yield StreamingOutputView(id="development-stream", wrap=True, markup=False)
        yield RichLog(id="development-result", wrap=True, markup=False)

    def show_prepared(self, prepared: WorkspacePrepared) -> None:
        self.display = True
        cursor = prepared.snapshot.development
        if cursor is None:
            return
        self.query_one("#development-heading", Static).update(
            f"Development | {prepared.issue.issue_id.value} | "
            f"{cursor.position} of {cursor.total} | {cursor.attempt_id.value}"
        )
        self.query_one("#development-issue", IssueBriefView).show_brief(
            prepared.issue.issue_id,
            prepared.issue.title,
            prepared.issue.markdown,
        )
        result = self.query_one("#development-result", RichLog)
        result.clear()
        result.write(f"Workspace: {prepared.workspace.path}")
        result.write(f"Context: {cursor.context_manifest.content_hash}")

    def append_activity(self, delta: str) -> None:
        self.query_one("#development-stream", StreamingOutputView).append_delta(delta)

    def show_completed(self, completed: DevelopmentCompleted) -> None:
        result = self.query_one("#development-result", RichLog)
        result.clear()
        result.write(completed.result.summary)
        result.write(f"Diff: {completed.result.diff_hash}")
        result.write(f"Changed files: {len(completed.result.changed_files)}")
        result.write("Next: code review")
