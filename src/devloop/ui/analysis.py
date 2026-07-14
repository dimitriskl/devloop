from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Label, RichLog, Static

from devloop.application.analysis import AnalysisRunResult
from devloop.domain.identifiers import WorkflowRunId
from devloop.domain.planning import ValidationFinding
from devloop.domain.run import AnalysisIntent, WorkflowRunStatus


@dataclass(frozen=True)
class AnalysisViewModel:
    run_id: WorkflowRunId
    status: WorkflowRunStatus
    feature_title: str
    prd_markdown: str
    issues: tuple[str, ...]
    findings: tuple[ValidationFinding, ...]
    clarification: str | None

    @classmethod
    def from_result(cls, result: AnalysisRunResult) -> AnalysisViewModel:
        draft = result.draft
        return cls(
            run_id=result.snapshot.run_id,
            status=result.snapshot.run_status,
            feature_title=result.snapshot.feature_title,
            prd_markdown="" if draft is None else draft.prd_markdown,
            issues=()
            if draft is None
            else tuple(
                f"{issue.issue_id.value}  {issue.title}\n{issue.markdown}"
                for issue in draft.issues
            ),
            findings=result.findings,
            clarification=result.clarification,
        )


class AnalysisIntentSelected(Message):
    def __init__(self, intent: AnalysisIntent) -> None:
        super().__init__()
        self.intent = intent


class AnalysisView(Vertical):
    DEFAULT_CSS = """
    AnalysisView { height: 1fr; display: none; }
    AnalysisView #analysis-heading { height: 2; padding: 0 1; }
    AnalysisView #analysis-columns { height: 1fr; }
    AnalysisView .analysis-column { width: 1fr; padding: 0 1; }
    AnalysisView .analysis-log { height: 1fr; border: solid $primary-background; }
    AnalysisView #analysis-activity { height: 5; border: solid $primary-background; }
    AnalysisView #analysis-actions { height: 3; align-horizontal: right; padding: 0 1; }
    """

    def compose(self) -> ComposeResult:
        self._busy = False
        self._can_accept = False
        yield Static("Analysis", id="analysis-heading")
        with Horizontal(id="analysis-columns"):
            with Vertical(classes="analysis-column"):
                yield Label("PRD Draft")
                yield RichLog(id="analysis-prd", classes="analysis-log", wrap=True, markup=False)
            with Vertical(classes="analysis-column"):
                yield Label("Issues")
                yield RichLog(id="analysis-issues", classes="analysis-log", wrap=True, markup=False)
            with Vertical(classes="analysis-column"):
                yield Label("Validation")
                yield RichLog(
                    id="analysis-validation",
                    classes="analysis-log",
                    wrap=True,
                    markup=False,
                )
        yield RichLog(id="analysis-activity", wrap=True, markup=False)
        with Horizontal(id="analysis-actions"):
            yield Button("Request changes", id="analysis-request-changes")
            yield Button("Accept", variant="primary", id="analysis-accept")

    def show_result(self, result: AnalysisRunResult) -> None:
        model = AnalysisViewModel.from_result(result)
        self.display = True
        self.query_one("#analysis-heading", Static).update(
            f"Analysis | {model.feature_title} | {model.run_id.value}"
        )
        prd = self.query_one("#analysis-prd", RichLog)
        issues = self.query_one("#analysis-issues", RichLog)
        validation = self.query_one("#analysis-validation", RichLog)
        activity = self.query_one("#analysis-activity", RichLog)
        for log in (prd, issues, validation):
            log.clear()
        prd.write(model.prd_markdown or "Draft not available yet.")
        for issue in model.issues:
            for line in issue.splitlines():
                issues.write(line)
        if not model.issues:
            issues.write("Issues not available yet.")
        if model.findings:
            for finding in model.findings:
                validation.write(f"{finding.code.value}: {finding.message}")
        else:
            validation.write("VALID")
        if model.clarification:
            activity.write(f"Clarification required: {model.clarification}")
        else:
            activity.write(f"Analysis status: {model.status.value}")
        self._can_accept = not model.findings and bool(model.prd_markdown)
        self._sync_action_state()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync_action_state()

    def _sync_action_state(self) -> None:
        self.query_one("#analysis-request-changes", Button).disabled = self._busy
        self.query_one("#analysis-accept", Button).disabled = self._busy or not self._can_accept

    def append_activity(self, message: str) -> None:
        self.query_one("#analysis-activity", RichLog).write(message)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "analysis-accept":
            self.post_message(AnalysisIntentSelected(AnalysisIntent.ACCEPT))
        elif event.button.id == "analysis-request-changes":
            self.post_message(AnalysisIntentSelected(AnalysisIntent.REQUEST_CHANGES))
