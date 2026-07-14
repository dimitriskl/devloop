from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from devloop.domain.development import ArtifactRef, IssueStatus
from devloop.domain.identifiers import AttemptId, IssueId, StepInstanceId
from devloop.domain.review_qa import QaCheck, ReviewFinding
from devloop.domain.run import BackendActivity, WorkflowRunSnapshot, WorkflowRunStatus
from devloop.domain.scheduler import IssueAttemptRecord, IssueBoardRow


@dataclass(frozen=True)
class WorkflowStatusModel:
    workflow_status: WorkflowRunStatus
    step: StepInstanceId
    issue_id: IssueId | None
    issue_position: int | None
    issue_total: int | None
    issue_status: IssueStatus | None
    attempt: AttemptId | None
    backend_activity: BackendActivity
    elapsed: timedelta

    @classmethod
    def from_snapshot(
        cls,
        snapshot: WorkflowRunSnapshot,
        *,
        backend_activity: BackendActivity,
        elapsed: timedelta,
    ) -> WorkflowStatusModel:
        cursor = snapshot.development
        issue_id = None if cursor is None else cursor.issue_id
        issue_state = next(
            (item for item in snapshot.issues if item.issue_id == issue_id),
            None,
        )
        return cls(
            snapshot.run_status,
            snapshot.active_step,
            issue_id,
            None if cursor is None else cursor.position,
            None if cursor is None else cursor.total,
            None if issue_state is None else issue_state.status,
            None if cursor is None else cursor.attempt_id,
            backend_activity,
            elapsed,
        )

    def render(self) -> str:
        issue = "NO ISSUE"
        if self.issue_id is not None:
            position = (
                "-"
                if self.issue_position is None or self.issue_total is None
                else f"{self.issue_position}/{self.issue_total}"
            )
            status = "-" if self.issue_status is None else self.issue_status.value
            issue = f"{self.issue_id.value} {position} {status}"
        attempt = "NO ATTEMPT" if self.attempt is None else self.attempt.value
        total_seconds = max(0, int(self.elapsed.total_seconds()))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return (
            f"{self.workflow_status.value} | "
            f"{self.step.value.upper().replace('-', ' ')} | {issue} | "
            f"{attempt} | {self.backend_activity.value} | "
            f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        )


class WorkflowStatusBar(Static):
    DEFAULT_CSS = """
    WorkflowStatusBar {
        height: 1;
        min-height: 1;
        max-height: 1;
        padding: 0 1;
        overflow: hidden hidden;
        text-wrap: nowrap;
        background: $boost;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(
            "READY | LAUNCHER | NO WORKFLOW RUN | NO ISSUE | NO ATTEMPT | IDLE | 00:00:00",
            id=id,
        )

    def show_status(self, status: WorkflowStatusModel) -> None:
        self.update(status.render())


class IssueBoard(Vertical):
    """Read-only workflow projection; it deliberately exposes no scheduling action."""

    DEFAULT_CSS = """
    IssueBoard { width: 38; min-width: 28; height: 1fr; border-left: solid $panel; }
    IssueBoard #issue-board-title { height: 2; padding: 0 1; }
    IssueBoard #issue-board-list { height: 1fr; }
    IssueBoard #issue-board-detail { height: 5; padding: 0 1; overflow: hidden auto; }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._rows: tuple[IssueBoardRow, ...] = ()

    def compose(self) -> ComposeResult:
        yield Label("Issue Board (read-only)", id="issue-board-title")
        yield OptionList(id="issue-board-list")
        yield Static("No Issue selected.", id="issue-board-detail")

    def show_rows(self, rows: tuple[IssueBoardRow, ...]) -> None:
        self._rows = rows
        issue_list = self.query_one("#issue-board-list", OptionList)
        issue_list.clear_options()
        issue_list.add_options(
            [
                Option(
                    f"{row.position}. {row.issue_id.value} | {row.status.value} | "
                    f"attempt {row.attempt_number}",
                    id=row.issue_id.value,
                )
                for row in rows
            ]
        )

    def inspect(self, issue_id: IssueId) -> IssueBoardRow:
        try:
            return next(row for row in self._rows if row.issue_id == issue_id)
        except StopIteration as error:
            raise ValueError(f"Issue is not on the board: {issue_id.value}.") from error

    @on(OptionList.OptionSelected, "#issue-board-list")
    def inspect_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id
        if option_id is None:
            return
        try:
            row = self.inspect(IssueId(option_id))
        except ValueError:
            return
        blockers = ", ".join(item.value for item in row.blocking_dependencies) or "none"
        step = "-" if row.current_step is None else row.current_step.value
        self.query_one("#issue-board-detail", Static).update(
            f"{row.title}\nStep: {step}\nDependencies: {blockers}"
        )


class ArtifactView(RichLog):
    def show_artifact(self, artifact: ArtifactRef, content: str) -> None:
        self.clear()
        self.write(f"Artifact: {artifact.path} | {artifact.content_hash}")
        for line in content.splitlines() or ("",):
            self.write(line)


class IssueBriefView(RichLog):
    def show_brief(self, issue_id: IssueId, title: str, markdown: str) -> None:
        self.clear()
        self.write(f"{issue_id.value} | {title}")
        for line in markdown.splitlines():
            self.write(line)


class DiffView(RichLog):
    def show_diff(self, diff: str) -> None:
        self.clear()
        for line in diff.splitlines() or ("No diff.",):
            self.write(line)


class FindingsView(RichLog):
    def show_findings(self, findings: tuple[ReviewFinding, ...]) -> None:
        self.clear()
        for finding in findings:
            line = "" if finding.line is None else f":{finding.line}"
            self.write(
                f"{finding.finding_id.value} | {finding.severity.value} | "
                f"{finding.disposition.value} | {finding.file_path}{line} | {finding.title}"
            )
        if not findings:
            self.write("No findings.")


class CheckMatrixView(RichLog):
    def show_checks(self, checks: tuple[QaCheck, ...]) -> None:
        self.clear()
        for check in checks:
            self.write(
                f"{check.check_id.value} | {check.criterion_id.value} | "
                f"{check.requirement.value} | {check.status.value}"
            )


class StreamingOutputView(RichLog):
    def append_delta(self, delta: str) -> None:
        self.write(delta)


class AttemptTimelineView(RichLog):
    def show_attempts(self, attempts: tuple[IssueAttemptRecord, ...]) -> None:
        self.clear()
        for attempt in attempts:
            self.write(
                f"{attempt.issue_id.value} | attempt-{attempt.attempt_number:03d} | "
                f"{attempt.status.value} | {attempt.outcome.value}"
            )
