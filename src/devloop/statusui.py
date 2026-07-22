from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, TextIO

from .terminal_editor import display_width
from .terminal_text import compact_terminal_text

if TYPE_CHECKING:
    from .portable_workflow import (
        PortableStepComponentCatalog,
        StepAttemptRecord,
        StepRuntimeState,
        WorkflowDefinition,
    )


class Stage(Enum):
    ANALYSIS = "analysis"
    DEVELOPMENT = "development"
    REVIEW = "review"
    QA = "qa"


class DashboardStatus(Enum):
    WAITING = "WAITING"
    WORKING = "WORKING"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class WorkflowProgressScope(Enum):
    WORKFLOW = "WORKFLOW"
    ISSUE = "ISSUE"


PIPELINE = [Stage.ANALYSIS, Stage.DEVELOPMENT, Stage.REVIEW, Stage.QA]

_ACTIVE_COLOR = "\x1b[1;36m"
_PASS_COLOR = "\x1b[1;32m"
_FAIL_COLOR = "\x1b[1;31m"
_WORKING_COLOR = "\x1b[1;33m"
_RESET = "\x1b[0m"
_BANNER_WIDTH = 79
WAITING_FRAMES = ("|", "/", "-", "\\")
UNICODE_WAITING_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
WAITING_FRAME_SECONDS = 0.12
WAITING_STALL_SECONDS = 120.0
_ERASE_LINE = "\x1b[2K"
_CARRIAGE_RETURN = "\r"
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DELIVERY_STAGES = (Stage.DEVELOPMENT, Stage.REVIEW, Stage.QA)
_STATUS_FIELD_WIDTH = max(len(status.value) for status in DashboardStatus) + 2
_STAGE_FIELD_WIDTH = max(len(stage.value) for stage in _DELIVERY_STAGES)


@dataclass(frozen=True)
class IssueResultSummary:
    issue_number: str
    status: DashboardStatus
    pass_number: int
    elapsed_seconds: float


@dataclass(frozen=True)
class WorkflowStepProgress:
    step_instance_id: str
    display_name: str
    component_id: str
    status: DashboardStatus
    pass_number: int
    elapsed_seconds: float
    issue_id: str | None
    scope: WorkflowProgressScope
    model: str | None
    reasoning_effort: str | None
    fast: str | None
    latest_result: str | None
    primary_path: bool
    attempt_id: str | None


@dataclass(frozen=True)
class WorkflowProgressActivity:
    event_freshness_seconds: float
    safe_text: str


@dataclass(frozen=True)
class WorkflowProgress:
    workflow_steps: tuple[WorkflowStepProgress, ...]
    issue_steps: tuple[WorkflowStepProgress, ...]
    active_step_instance_id: str | None
    activity: WorkflowProgressActivity
    issue_title: str = ""
    issue_position: int = 0
    issue_total: int = 0
    issue_history: tuple[IssueResultSummary, ...] = ()
    scheduler_summary: str = ""

    @property
    def by_step_instance_id(self) -> Mapping[str, WorkflowStepProgress]:
        return {
            step.step_instance_id: step
            for step in (*self.workflow_steps, *self.issue_steps)
        }

    @property
    def active_step(self) -> WorkflowStepProgress | None:
        if self.active_step_instance_id is None:
            return None
        return next(
            (
                step
                for step in (*self.workflow_steps, *self.issue_steps)
                if step.step_instance_id == self.active_step_instance_id
            ),
            None,
        )


def project_workflow_progress(
    workflow: WorkflowDefinition,
    catalog: PortableStepComponentCatalog,
    runtime_states: Iterable[StepRuntimeState],
    attempts: Iterable[StepAttemptRecord],
    *,
    issue_id: str | None,
    expanded_branches: bool = False,
    active_elapsed_seconds: float = 0.0,
    event_freshness_seconds: float = 0.0,
    activity: str = "Waiting for the first Codex update.",
    issue_title: str = "",
    issue_position: int = 0,
    issue_total: int = 0,
    issue_history: Iterable[IssueResultSummary] = (),
    scheduler_summary: str = "",
) -> WorkflowProgress:
    runtime_list = list(runtime_states)
    attempt_list = list(attempts)
    primary_path = workflow.primary_path()
    primary_ids = {step.instance_id for step in primary_path}
    visited_ids = {
        item.step_instance_id for item in (*runtime_list, *attempt_list)
    }
    visible_steps = list(primary_path)
    visible_steps.extend(
        step
        for step in workflow.steps
        if step.instance_id not in primary_ids
        and (expanded_branches or step.instance_id in visited_ids)
    )
    projected: list[WorkflowStepProgress] = []
    active_step_instance_id: str | None = None
    for step in visible_steps:
        component = catalog.resolve(step.component_id)
        scope = WorkflowProgressScope(component.scope.value)
        scoped_issue_id = (
            None if scope is WorkflowProgressScope.WORKFLOW else issue_id
        )
        step_runtimes = [
            runtime
            for runtime in runtime_list
            if runtime.step_instance_id == step.instance_id
            and runtime.issue_id == scoped_issue_id
        ]
        step_attempts = [
            attempt
            for attempt in attempt_list
            if attempt.step_instance_id == step.instance_id
            and attempt.issue_id == scoped_issue_id
        ]
        runtime = step_runtimes[-1] if step_runtimes else None
        latest_attempt = step_attempts[-1] if step_attempts else None
        if runtime is not None and runtime.status.value == "RUNNING":
            active_step_instance_id = str(step.instance_id)
        settings = step.codex_settings
        projected_status = _projected_step_status(runtime, latest_attempt)
        if (
            scope is WorkflowProgressScope.WORKFLOW
            and issue_id is not None
            and runtime is None
            and latest_attempt is None
        ):
            projected_status = DashboardStatus.PASS
        projected.append(
            WorkflowStepProgress(
                step_instance_id=str(step.instance_id),
                display_name=step.display_name,
                component_id=str(component.component_id),
                status=projected_status,
                pass_number=max(
                    [attempt.pass_number for attempt in step_attempts]
                    + ([runtime.pass_number] if runtime is not None else [1])
                ),
                elapsed_seconds=sum(
                    max(0.0, attempt.elapsed_seconds) for attempt in step_attempts
                )
                + (
                    max(0.0, active_elapsed_seconds)
                    if runtime is not None and runtime.status.value == "RUNNING"
                    else 0.0
                ),
                issue_id=(
                    _safe_progress_text(scoped_issue_id, max_length=64)
                    if scoped_issue_id is not None
                    else None
                ),
                scope=scope,
                model=settings.model if settings is not None else None,
                reasoning_effort=(
                    settings.reasoning_effort if settings is not None else None
                ),
                fast=settings.fast.value if settings is not None else None,
                latest_result=(
                    _safe_progress_text(
                        latest_attempt.result.summary or latest_attempt.result.status
                    )
                    if latest_attempt is not None
                    else None
                ),
                primary_path=step.instance_id in primary_ids,
                attempt_id=(
                    runtime.attempt_id
                    if runtime is not None and runtime.attempt_id is not None
                    else latest_attempt.attempt_id
                    if latest_attempt is not None
                    else None
                ),
            )
        )
    return WorkflowProgress(
        workflow_steps=tuple(
            step
            for step in projected
            if step.scope is WorkflowProgressScope.WORKFLOW
        ),
        issue_steps=tuple(
            step
            for step in projected
            if step.scope is WorkflowProgressScope.ISSUE
        ),
        active_step_instance_id=active_step_instance_id,
        activity=WorkflowProgressActivity(
            event_freshness_seconds=max(0.0, event_freshness_seconds),
            safe_text=_safe_progress_text(activity),
        ),
        issue_title=_safe_progress_text(issue_title),
        issue_position=max(0, issue_position),
        issue_total=max(0, issue_total),
        issue_history=_safe_issue_history(issue_history),
        scheduler_summary=_safe_progress_text(scheduler_summary),
    )


def _safe_progress_text(value: str, *, max_length: int = 300) -> str:
    return compact_terminal_text(value, max_length=max_length)


def _safe_optional_progress_text(
    value: str | None,
    *,
    max_length: int,
) -> str | None:
    if value is None:
        return None
    return _safe_progress_text(value, max_length=max_length)


def _safe_issue_numbers(issue_numbers: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            _safe_progress_text(number, max_length=64)
            for number in issue_numbers
        )
    )


def _safe_issue_history(
    history: Iterable[IssueResultSummary],
) -> tuple[IssueResultSummary, ...]:
    normalized: list[IssueResultSummary] = []
    seen: set[str] = set()
    for entry in history:
        safe_entry = _terminal_safe_result_summary(entry)
        if safe_entry is None or safe_entry.issue_number in seen:
            continue
        seen.add(safe_entry.issue_number)
        normalized.append(safe_entry)
    return tuple(normalized)


def _issue_history_from_numbers(
    issue_numbers: Iterable[str],
) -> tuple[IssueResultSummary, ...]:
    return _safe_issue_history(
        IssueResultSummary(
            issue_number=number,
            status=DashboardStatus.PASS,
            pass_number=1,
            elapsed_seconds=0.0,
        )
        for number in issue_numbers
    )


def _terminal_safe_step_progress(
    step: WorkflowStepProgress,
) -> WorkflowStepProgress:
    return replace(
        step,
        step_instance_id=_safe_progress_text(
            step.step_instance_id,
            max_length=64,
        ),
        display_name=_safe_progress_text(step.display_name, max_length=160),
        component_id=_safe_progress_text(step.component_id, max_length=128),
        issue_id=_safe_optional_progress_text(step.issue_id, max_length=64),
        model=_safe_optional_progress_text(step.model, max_length=128),
        reasoning_effort=_safe_optional_progress_text(
            step.reasoning_effort,
            max_length=64,
        ),
        fast=_safe_optional_progress_text(step.fast, max_length=16),
        latest_result=_safe_optional_progress_text(
            step.latest_result,
            max_length=300,
        ),
        attempt_id=_safe_optional_progress_text(step.attempt_id, max_length=128),
    )


def _terminal_safe_result_summary(
    result: IssueResultSummary | None,
) -> IssueResultSummary | None:
    if result is None:
        return None
    return replace(
        result,
        issue_number=_safe_progress_text(result.issue_number, max_length=64),
    )


def _terminal_safe_workflow_progress(
    projection: WorkflowProgress,
) -> WorkflowProgress:
    return replace(
        projection,
        workflow_steps=tuple(
            _terminal_safe_step_progress(step) for step in projection.workflow_steps
        ),
        issue_steps=tuple(
            _terminal_safe_step_progress(step) for step in projection.issue_steps
        ),
        active_step_instance_id=_safe_optional_progress_text(
            projection.active_step_instance_id,
            max_length=64,
        ),
        activity=replace(
            projection.activity,
            safe_text=_safe_progress_text(
                projection.activity.safe_text,
                max_length=300,
            ),
        ),
        issue_title=_safe_progress_text(projection.issue_title, max_length=160),
        issue_history=_safe_issue_history(projection.issue_history),
        scheduler_summary=_safe_progress_text(
            projection.scheduler_summary,
            max_length=200,
        ),
    )


def project_workflow_step_progress(
    workflow: WorkflowDefinition,
    catalog: PortableStepComponentCatalog,
    runtime_states: Iterable[StepRuntimeState],
    attempts: Iterable[StepAttemptRecord],
    *,
    issue_id: str | None,
) -> tuple[WorkflowStepProgress, ...]:
    projection = project_workflow_progress(
        workflow,
        catalog,
        runtime_states,
        attempts,
        issue_id=issue_id,
    )
    return (
        projection.workflow_steps
        if issue_id is None
        else projection.issue_steps
    )


def _projected_step_status(
    runtime: StepRuntimeState | None,
    latest_attempt: StepAttemptRecord | None,
) -> DashboardStatus:
    if runtime is not None and runtime.status.value == "RUNNING":
        return DashboardStatus.WORKING
    if latest_attempt is None:
        return DashboardStatus.WAITING
    outcome = latest_attempt.outcome.value
    if outcome == "SUCCEEDED":
        return DashboardStatus.PASS
    if outcome == "BLOCKED":
        return DashboardStatus.BLOCKED
    return DashboardStatus.FAIL


def render_step_progress_rows(
    progress: Iterable[WorkflowStepProgress],
    *,
    width: int,
    color: bool,
    unicode: bool,
) -> str:
    safe_width = max(1, width)
    separator = " · " if unicode else " - "
    rows: list[str] = []
    statuses: list[DashboardStatus] = []
    for raw_step in progress:
        step = _terminal_safe_step_progress(raw_step)
        rows.append(
            _fit_plain_text(
                (
                    f"{step.status.value:<{_STATUS_FIELD_WIDTH}} {step.display_name}"
                    f"{separator}pass {step.pass_number}"
                    f"{separator}{format_duration(step.elapsed_seconds)}"
                ),
                safe_width,
                unicode=unicode,
            )
        )
        statuses.append(step.status)
    if not color:
        return "\n".join(rows)
    return "\n".join(
        _color_status_word(row, status)
        for row, status in zip(rows, statuses)
    )


def render_workflow_progress(
    projection: WorkflowProgress,
    *,
    width: int,
    color: bool,
    unicode: bool,
    frame: str,
    max_step_rows: int | None = None,
) -> str:
    projection = _terminal_safe_workflow_progress(projection)
    frame = _safe_progress_text(frame, max_length=4)
    safe_width = max(1, width)
    separator = " · " if unicode else " - "
    rule = ("─" if unicode else "-") * safe_width
    lines: list[str] = []
    colored_statuses: list[tuple[int, DashboardStatus]] = []

    if projection.issue_history:
        lines.append(
            _render_issue_history_line(
                projection.issue_history,
                width=safe_width,
                color=color,
                unicode=unicode,
            )
        )
    lines.append(rule)

    def append_steps(
        title: str,
        steps: Iterable[WorkflowStepProgress],
        hidden_count: int = 0,
    ) -> None:
        step_list = tuple(steps)
        if not step_list:
            return
        lines.append(_fit_plain_text(title, safe_width, unicode=unicode))
        for step in step_list:
            lines.append(
                _fit_plain_text(
                    (
                        f"{step.status.value:<{_STATUS_FIELD_WIDTH}} {step.display_name}"
                        f"{separator}pass {step.pass_number}"
                        f"{separator}{format_duration(step.elapsed_seconds)}"
                    ),
                    safe_width,
                    unicode=unicode,
                )
            )
            colored_statuses.append((len(lines) - 1, step.status))
        if hidden_count:
            hidden_label = (
                f"… {hidden_count} steps hidden …"
                if unicode
                else f"... {hidden_count} steps hidden ..."
            )
            lines.append(_fit_plain_text(hidden_label, safe_width, unicode=unicode))

    workflow_steps = projection.workflow_steps
    issue_steps = projection.issue_steps
    hidden_workflow = 0
    hidden_issue = 0
    if max_step_rows is not None:
        total_budget = max(1, max_step_rows)
        active_in_workflow = any(
            step.step_instance_id == projection.active_step_instance_id
            for step in workflow_steps
        )
        if workflow_steps and issue_steps:
            secondary_budget = 1 if total_budget > 1 else 0
            if active_in_workflow:
                workflow_budget = total_budget - secondary_budget
                issue_budget = secondary_budget
            else:
                workflow_budget = secondary_budget
                issue_budget = total_budget - secondary_budget
        else:
            workflow_budget = total_budget if workflow_steps else 0
            issue_budget = total_budget if issue_steps else 0
        workflow_steps, hidden_workflow = _window_progress_steps(
            workflow_steps,
            projection.active_step_instance_id,
            workflow_budget,
        )
        issue_steps, hidden_issue = _window_progress_steps(
            issue_steps,
            projection.active_step_instance_id,
            issue_budget,
        )

    if not _workflow_steps_are_complete(projection.workflow_steps):
        append_steps("WORKFLOW", workflow_steps, hidden_workflow)
    if issue_steps:
        issue_id = issue_steps[0].issue_id
        if issue_id is None:
            issue_parts = ["ISSUE STEPS"]
            issue_heading = "ISSUE STEPS"
        else:
            issue_parts = ["CURRENT ISSUE", issue_id]
            issue_heading = "CURRENT ISSUE"
        if (
            issue_id is not None
            and projection.issue_position
            and projection.issue_total
        ):
            issue_parts.extend(
                (
                    f"{projection.issue_position}/{projection.issue_total}",
                    f"{max(0, projection.issue_total - projection.issue_position)} remaining",
                )
            )
        append_steps(
            separator.join(issue_parts),
            issue_steps,
            hidden_issue,
        )
        if projection.issue_title:
            heading_index = next(
                index
                for index, line in enumerate(lines)
                if line.startswith(issue_heading)
            )
            lines.insert(
                heading_index + 1,
                _fit_plain_text(
                    projection.issue_title,
                    safe_width,
                    unicode=unicode,
                ),
            )
            colored_statuses = [
                (index + 1 if index > heading_index else index, status)
                for index, status in colored_statuses
            ]
        if projection.scheduler_summary:
            heading_index = next(
                index
                for index, line in enumerate(lines)
                if line.startswith(issue_heading)
            )
            insert_index = heading_index + (2 if projection.issue_title else 1)
            lines.insert(
                insert_index,
                _fit_plain_text(
                    projection.scheduler_summary,
                    safe_width,
                    unicode=unicode,
                ),
            )
            colored_statuses = [
                (index + 1 if index >= insert_index else index, status)
                for index, status in colored_statuses
            ]

    active = projection.active_step
    if active is not None:
        lines.append(rule)
        settings = (
            separator.join(
                (
                    f"model {active.model}",
                    f"effort {active.reasoning_effort}",
                    f"Fast {active.fast}",
                )
            )
            if active.model is not None
            else "local execution"
        )
        lines.append(
            _fit_plain_text(
                f"ACTIVE {active.display_name}{separator}{settings}",
                safe_width,
                unicode=unicode,
            )
        )
        lines.append(
            _fit_plain_text(
                separator.join(
                    (
                        f"{active.status.value} {frame}",
                        f"pass {active.pass_number}",
                        f"elapsed {format_duration(active.elapsed_seconds)}",
                        "event "
                        f"{format_duration(projection.activity.event_freshness_seconds)} ago",
                    )
                ),
                safe_width,
                unicode=unicode,
            )
        )
        colored_statuses.append((len(lines) - 1, active.status))
        activity_prefix = "AI › " if unicode else "AI > "
        lines.append(
            _fit_plain_text(
                f"{activity_prefix}"
                f"{_safe_progress_text(projection.activity.safe_text)}",
                safe_width,
                unicode=unicode,
            )
        )
    lines.append(rule)

    if not color:
        return "\n".join(lines)
    rendered_lines = list(lines)
    for index, status in colored_statuses:
        rendered_lines[index] = _color_status_word(rendered_lines[index], status)
    return "\n".join(rendered_lines)


def render_workflow_progress_for_stream(
    projection: WorkflowProgress,
    *,
    stream: TextIO | None = None,
    frame: str | None = None,
    max_step_rows: int | None = None,
) -> str:
    target = sys.stdout if stream is None else stream
    columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    unicode = _can_encode("─⠋›…", target)
    return render_workflow_progress(
        projection,
        width=max(1, columns - 1),
        color=_use_color(target),
        unicode=unicode,
        frame=frame or ("⠋" if unicode else "|"),
        max_step_rows=max_step_rows,
    )


def _window_progress_steps(
    steps: tuple[WorkflowStepProgress, ...],
    active_step_instance_id: str | None,
    limit: int,
) -> tuple[tuple[WorkflowStepProgress, ...], int]:
    if limit <= 0:
        return (), len(steps)
    if len(steps) <= limit:
        return steps, 0
    active_index = next(
        (
            index
            for index, step in enumerate(steps)
            if step.step_instance_id == active_step_instance_id
        ),
        0,
    )
    start = max(0, active_index - (limit // 2))
    start = min(start, len(steps) - limit)
    return steps[start : start + limit], len(steps) - limit


@dataclass(frozen=True)
class IssueDashboardSnapshot:
    issue_number: str
    issue_title: str
    position: int
    total: int
    pass_number: int
    active_stage: Stage
    statuses: Mapping[Stage, DashboardStatus] = field(default_factory=dict)
    stage_durations: Mapping[Stage, float] = field(default_factory=dict)
    step_progress: tuple[WorkflowStepProgress, ...] = ()
    workflow_progress: WorkflowProgress | None = None
    issue_history: tuple[IssueResultSummary, ...] = ()
    elapsed_seconds: float = 0.0
    inactivity_seconds: float = 0.0
    activity: str = "Waiting for the first Codex update."
    max_step_rows: int | None = None
    scheduler_summary: str = ""


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{remaining_seconds:02}"


def render_issue_dashboard(
    snapshot: IssueDashboardSnapshot,
    *,
    width: int,
    color: bool,
    unicode: bool,
    frame: str,
) -> str:
    snapshot = replace(
        snapshot,
        issue_number=_safe_progress_text(snapshot.issue_number, max_length=64),
        issue_title=_safe_progress_text(snapshot.issue_title, max_length=160),
        step_progress=tuple(
            _terminal_safe_step_progress(step) for step in snapshot.step_progress
        ),
        workflow_progress=(
            _terminal_safe_workflow_progress(snapshot.workflow_progress)
            if snapshot.workflow_progress is not None
            else None
        ),
        issue_history=_safe_issue_history(snapshot.issue_history),
        activity=_safe_progress_text(snapshot.activity, max_length=300),
        scheduler_summary=_safe_progress_text(
            snapshot.scheduler_summary,
            max_length=200,
        ),
    )
    frame = _safe_progress_text(frame, max_length=4)
    safe_width = max(1, width)
    if snapshot.workflow_progress is not None:
        return render_workflow_progress(
            snapshot.workflow_progress,
            width=safe_width,
            color=color,
            unicode=unicode,
            frame=frame,
            max_step_rows=snapshot.max_step_rows,
        )
    rule_character = "─" if unicode else "-"
    separator = " · " if unicode else " - "
    remaining = max(0, snapshot.total - snapshot.position)
    header = separator.join(
        (
            "CURRENT ISSUE",
            snapshot.issue_number,
            f"{snapshot.position}/{snapshot.total}",
            f"{remaining} remaining",
        )
    )
    activity_prefix = "AI › " if unicode else "AI > "
    event_age = format_duration(snapshot.inactivity_seconds)
    live_status = snapshot.statuses.get(
        snapshot.active_stage,
        DashboardStatus.WORKING,
    )
    live_elapsed_seconds = _stage_elapsed_seconds(
        snapshot,
        snapshot.active_stage,
    )
    live_line = separator.join(
        (
            snapshot.active_stage.value.upper(),
            snapshot.issue_number,
            f"pass {snapshot.pass_number}",
        )
    )
    live_line = (
        f"{live_line}    {live_status.value} {frame}    "
        f"{format_duration(live_elapsed_seconds)}    event {event_age} ago"
    )

    plain_lines: list[str] = []
    colored_statuses: list[tuple[int, DashboardStatus]] = []
    if snapshot.issue_history:
        plain_lines.append(
            _render_issue_history_line(
                snapshot.issue_history,
                width=safe_width,
                color=color,
                unicode=unicode,
            )
        )
    plain_lines.append(rule_character * safe_width)
    plain_lines.extend(
        (
            _fit_plain_text(header, safe_width, unicode=unicode),
            _fit_plain_text(snapshot.issue_title, safe_width, unicode=unicode),
        )
    )
    if snapshot.scheduler_summary:
        plain_lines.append(
            _fit_plain_text(
                snapshot.scheduler_summary,
                safe_width,
                unicode=unicode,
            )
        )
    plain_lines.append("")
    if snapshot.step_progress:
        for step in snapshot.step_progress:
            plain_lines.append(
                _fit_plain_text(
                    (
                        f"{step.status.value:<{_STATUS_FIELD_WIDTH}} {step.display_name}"
                        f"{separator}pass {step.pass_number}"
                        f"{separator}{format_duration(step.elapsed_seconds)}"
                    ),
                    safe_width,
                    unicode=unicode,
                )
            )
            colored_statuses.append((len(plain_lines) - 1, step.status))
    else:
        for stage in _DELIVERY_STAGES:
            status = snapshot.statuses.get(stage, DashboardStatus.WAITING)
            elapsed = format_duration(_stage_elapsed_seconds(snapshot, stage))
            plain_lines.append(
                _fit_plain_text(
                    (
                        f"{status.value:<{_STATUS_FIELD_WIDTH}} "
                        f"{stage.value.upper():<{_STAGE_FIELD_WIDTH}}"
                        f"{separator}pass {snapshot.pass_number}"
                        f"{separator}{elapsed}"
                    ),
                    safe_width,
                    unicode=unicode,
                )
            )
            colored_statuses.append((len(plain_lines) - 1, status))
    live_line_index = len(plain_lines) + 1
    plain_lines.extend(
        (
            rule_character * safe_width,
            _fit_plain_text(live_line, safe_width, unicode=unicode),
            _fit_plain_text(
                f"{activity_prefix}{_safe_progress_text(snapshot.activity)}",
                safe_width,
                unicode=unicode,
            ),
            rule_character * safe_width,
        )
    )

    if not color:
        return "\n".join(plain_lines)

    colored_lines = list(plain_lines)
    for index, status in colored_statuses:
        colored_lines[index] = _color_status_word(colored_lines[index], status)
    colored_lines[live_line_index] = _color_status_word(
        colored_lines[live_line_index],
        live_status,
    )
    return "\n".join(colored_lines)


def _stage_elapsed_seconds(
    snapshot: IssueDashboardSnapshot,
    stage: Stage,
) -> float:
    status = snapshot.statuses.get(stage, DashboardStatus.WAITING)
    if stage is snapshot.active_stage and status is DashboardStatus.WORKING:
        return max(
            0.0,
            snapshot.stage_durations.get(stage, 0.0) + snapshot.elapsed_seconds,
        )
    return max(0.0, snapshot.stage_durations.get(stage, 0.0))


def _workflow_steps_are_complete(
    steps: tuple[WorkflowStepProgress, ...],
) -> bool:
    return bool(steps) and all(
        step.status is DashboardStatus.PASS for step in steps
    )


def _color_issue_number(
    issue_number: str,
    status: DashboardStatus,
    *,
    color: bool,
) -> str:
    if not color:
        return issue_number
    issue_color = (
        _PASS_COLOR
        if status is DashboardStatus.PASS
        else _FAIL_COLOR
    )
    return f"{issue_color}{issue_number}{_RESET}"


def _render_issue_history_line(
    history: tuple[IssueResultSummary, ...],
    *,
    width: int,
    color: bool,
    unicode: bool,
) -> str:
    separator = " · " if unicode else " - "
    prefix = f"RUN{separator}"
    plain_line = _fit_plain_text(
        prefix + separator.join(entry.issue_number for entry in history),
        width,
        unicode=unicode,
    )
    if not color:
        return plain_line
    colored_line = plain_line
    for entry in history:
        colored_token = _color_issue_number(
            entry.issue_number,
            entry.status,
            color=True,
        )
        colored_line = colored_line.replace(
            entry.issue_number,
            colored_token,
            1,
        )
    return colored_line


def _fit_plain_text(text: str, width: int, *, unicode: bool) -> str:
    if display_width(text) <= width:
        return text
    ellipsis = "…" if unicode else "..."
    ellipsis_width = display_width(ellipsis)
    if width <= ellipsis_width:
        return ellipsis[:width]
    target_width = width - ellipsis_width
    characters: list[str] = []
    current_width = 0
    for character in text:
        character_width = display_width(character)
        if current_width + character_width > target_width:
            break
        characters.append(character)
        current_width += character_width
    return f"{''.join(characters)}{ellipsis}"


def _color_status_word(text: str, status: DashboardStatus) -> str:
    color = {
        DashboardStatus.PASS: _PASS_COLOR,
        DashboardStatus.FAIL: _FAIL_COLOR,
        DashboardStatus.BLOCKED: _FAIL_COLOR,
        DashboardStatus.WORKING: _WORKING_COLOR,
    }.get(status)
    if color is None or status.value not in text:
        return text
    return text.replace(status.value, f"{color}{status.value}{_RESET}", 1)


def render_status(status: str | DashboardStatus, stream=None) -> str:
    parsed = (
        status
        if isinstance(status, DashboardStatus)
        else DashboardStatus(status.upper())
    )
    if not _use_color(stream):
        return parsed.value
    return _color_status_word(parsed.value, parsed)


def _terminal_display_width(text: str) -> int:
    return display_width(_ANSI_ESCAPE_PATTERN.sub("", text))


def _rewrite_terminal_lines(
    stream: TextIO,
    lines: list[str],
    *,
    previous_line_count: int,
) -> None:
    if previous_line_count > 1:
        stream.write(f"\x1b[{previous_line_count - 1}A{_CARRIAGE_RETURN}")
    for index, line in enumerate(lines):
        stream.write(f"{_ERASE_LINE}{_CARRIAGE_RETURN}{line}")
        if index < len(lines) - 1:
            stream.write("\n")
    surplus_line_count = max(0, previous_line_count - len(lines))
    for _ in range(surplus_line_count):
        stream.write(f"\n{_ERASE_LINE}{_CARRIAGE_RETURN}")
    if surplus_line_count:
        stream.write(f"\x1b[{surplus_line_count}A{_CARRIAGE_RETURN}")


def _live_workflow_progress(
    progress: WorkflowProgress | None,
    *,
    elapsed_seconds: float,
    inactivity_seconds: float,
    activity: str,
    issue_title: str,
    issue_position: int,
    issue_total: int,
    issue_history: tuple[IssueResultSummary, ...] = (),
) -> WorkflowProgress | None:
    if progress is None:
        return None
    active_id = progress.active_step_instance_id

    def update(step: WorkflowStepProgress) -> WorkflowStepProgress:
        if (
            step.step_instance_id != active_id
            or step.status is not DashboardStatus.WORKING
        ):
            return step
        return replace(
            step,
            elapsed_seconds=step.elapsed_seconds + max(0.0, elapsed_seconds),
        )

    return replace(
        progress,
        workflow_steps=tuple(update(step) for step in progress.workflow_steps),
        issue_steps=tuple(update(step) for step in progress.issue_steps),
        activity=WorkflowProgressActivity(
            event_freshness_seconds=max(0.0, inactivity_seconds),
            safe_text=_safe_progress_text(activity),
        ),
        issue_title=_safe_progress_text(issue_title),
        issue_position=max(0, issue_position),
        issue_total=max(0, issue_total),
        issue_history=issue_history,
    )


def _finish_active_progress(
    progress: WorkflowProgress,
    status: DashboardStatus,
    elapsed_seconds: float,
) -> WorkflowProgress:
    active_id = progress.active_step_instance_id

    def finish(step: WorkflowStepProgress) -> WorkflowStepProgress:
        if step.step_instance_id != active_id:
            return step
        return replace(
            step,
            status=status,
            elapsed_seconds=step.elapsed_seconds + max(0.0, elapsed_seconds),
        )

    return replace(
        progress,
        workflow_steps=tuple(finish(step) for step in progress.workflow_steps),
        issue_steps=tuple(finish(step) for step in progress.issue_steps),
    )


class IssueDashboard:
    """Maintain one small in-place dashboard for the current delivery Issue."""

    def __init__(
        self,
        *,
        issue_number: str,
        issue_title: str,
        position: int,
        total: int,
        finished_issue_numbers: Iterable[str] = (),
        issue_history: Iterable[IssueResultSummary] = (),
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
        frame_seconds: float = WAITING_FRAME_SECONDS,
        terminal_size: Callable[..., os.terminal_size] = shutil.get_terminal_size,
    ) -> None:
        self._issue_number = _safe_progress_text(issue_number, max_length=64)
        self._issue_title = _safe_progress_text(issue_title)
        self._position = position
        self._total = total
        self._stream = sys.stdout if stream is None else stream
        self._clock = clock
        self._frame_seconds = frame_seconds
        self._terminal_size = terminal_size
        isatty = getattr(self._stream, "isatty", None)
        from .portable_runtime import (
            active_portable_runtime,
            portable_plain_mode_active,
        )

        self._portable_runtime = active_portable_runtime()
        self._enabled = bool(
            self._portable_runtime is not None
            or (
                not portable_plain_mode_active()
                and callable(isatty)
                and isatty()
            )
        )
        self._unicode = _can_encode("─⠋›", self._stream)
        self._statuses = {
            Stage.DEVELOPMENT: DashboardStatus.WAITING,
            Stage.REVIEW: DashboardStatus.WAITING,
            Stage.QA: DashboardStatus.WAITING,
        }
        self._stage_durations = {
            Stage.DEVELOPMENT: 0.0,
            Stage.REVIEW: 0.0,
            Stage.QA: 0.0,
        }
        self._step_progress: tuple[WorkflowStepProgress, ...] = ()
        self._workflow_progress: WorkflowProgress | None = None
        self._scheduler_summary = ""
        self._pending_last_result: IssueResultSummary | None = None
        self._issue_history = list(_safe_issue_history(issue_history))
        for number in _safe_issue_numbers(finished_issue_numbers):
            if any(entry.issue_number == number for entry in self._issue_history):
                continue
            self._issue_history.append(
                IssueResultSummary(
                    issue_number=number,
                    status=DashboardStatus.PASS,
                    pass_number=1,
                    elapsed_seconds=0.0,
                )
            )
        self._active_stage = Stage.DEVELOPMENT
        self._pass_number = 1
        self._activity = "Waiting for the first Codex update."
        self._started_at = self._clock()
        self._last_activity_at: float | None = None
        self._frame_index = 0
        self._rendered_lines = 0
        self._opened = False
        self._lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def has_workflow_progress(self) -> bool:
        return self._workflow_progress is not None

    def begin_role(self, stage: Stage, pass_number: int) -> None:
        if stage is Stage.ANALYSIS:
            raise ValueError("The Issue dashboard supports delivery Workflow Steps only.")
        with self._lock:
            self._active_stage = stage
            self._pass_number = pass_number
            self._started_at = self._clock()
            self._last_activity_at = None
            self._activity = "Waiting for the first Codex update."
            active_index = PIPELINE.index(stage)
            for candidate in _DELIVERY_STAGES:
                if candidate is stage:
                    self._statuses[candidate] = DashboardStatus.WORKING
                elif PIPELINE.index(candidate) > active_index:
                    self._statuses[candidate] = DashboardStatus.WAITING
            self._render_locked()
        self._start_animation()

    def show_issue(
        self,
        *,
        issue_number: str,
        issue_title: str,
        position: int,
        total: int,
    ) -> None:
        was_animating = self._thread is not None
        self._stop_animation()
        with self._lock:
            normalized_issue_number = _safe_progress_text(
                issue_number,
                max_length=64,
            )
            if (
                self._pending_last_result is not None
                and self._pending_last_result.issue_number != normalized_issue_number
            ):
                self._promote_pending_to_history()
            self._issue_number = normalized_issue_number
            self._issue_title = _safe_progress_text(issue_title)
            self._position = position
            self._total = total
            for stage in _DELIVERY_STAGES:
                self._statuses[stage] = DashboardStatus.WAITING
                self._stage_durations[stage] = 0.0
            self._step_progress = ()
            self._workflow_progress = None
            self._active_stage = Stage.DEVELOPMENT
            self._pass_number = 1
            self._activity = "Waiting for the first Codex update."
            self._started_at = self._clock()
            self._last_activity_at = None
            self._render_locked()
        if was_animating:
            self._start_animation()

    def _promote_pending_to_history(self) -> None:
        pending = self._pending_last_result
        if pending is None:
            return
        for index, entry in enumerate(self._issue_history):
            if entry.issue_number == pending.issue_number:
                self._issue_history[index] = pending
                self._pending_last_result = None
                return
        self._issue_history.append(pending)
        self._pending_last_result = None

    def finish_issue(self, status: str, activity: str = "") -> None:
        self._stop_animation()
        parsed_status = DashboardStatus(status.upper())
        with self._lock:
            if activity:
                self._activity = _safe_progress_text(activity)
                self._last_activity_at = self._clock()
            self._pending_last_result = IssueResultSummary(
                issue_number=self._issue_number,
                status=parsed_status,
                pass_number=self._pass_number,
                elapsed_seconds=sum(self._stage_durations.values()),
            )
            self._render_locked()

    def show_step_progress(
        self,
        progress: Iterable[WorkflowStepProgress],
    ) -> None:
        with self._lock:
            self._step_progress = tuple(progress)
            self._render_locked()

    def show_workflow_progress(self, progress: WorkflowProgress) -> None:
        with self._lock:
            previous_active = (
                self._workflow_progress.active_step
                if self._workflow_progress is not None
                else None
            )
            self._workflow_progress = replace(
                progress,
                scheduler_summary=self._scheduler_summary,
            )
            active = progress.active_step
            if active is not None and (
                previous_active is None
                or active.step_instance_id != previous_active.step_instance_id
                or active.attempt_id != previous_active.attempt_id
                or previous_active.status is not DashboardStatus.WORKING
            ):
                self._started_at = self._clock()
                self._last_activity_at = None
                self._activity = "Waiting for the first Codex update."
            self._render_locked()

    def show_scheduler_status(self, summary: str) -> None:
        with self._lock:
            self._scheduler_summary = _safe_progress_text(
                summary,
                max_length=200,
            )
            if self._workflow_progress is not None:
                self._workflow_progress = replace(
                    self._workflow_progress,
                    scheduler_summary=self._scheduler_summary,
                )
            self._render_locked()

    def restore_role(self, stage: Stage, status: str) -> None:
        if stage is Stage.ANALYSIS:
            raise ValueError("The Issue dashboard supports delivery Workflow Steps only.")
        with self._lock:
            self._statuses[stage] = DashboardStatus(status.upper())

    def notify_activity(self, activity: str | None = None) -> None:
        with self._lock:
            self._last_activity_at = self._clock()
            if activity:
                normalized = _safe_progress_text(activity)
                self._activity = normalized.removeprefix("Codex update: ")
                self._render_locked()

    def finish_role(self, stage: Stage, status: str, summary: str = "") -> None:
        parsed_status = DashboardStatus(status.upper())
        with self._lock:
            now = self._clock()
            self._active_stage = stage
            self._statuses[stage] = parsed_status
            self._stage_durations[stage] += max(0.0, now - self._started_at)
            if self._workflow_progress is not None:
                self._workflow_progress = _finish_active_progress(
                    self._workflow_progress,
                    parsed_status,
                    max(0.0, now - self._started_at),
                )
            self._last_activity_at = now
            if summary:
                self._activity = _safe_progress_text(summary)
            else:
                self._activity = f"{stage.value.title()} finished: {parsed_status.value}."
            self._render_locked()

    def close(self, activity: str | None = None) -> None:
        self._stop_animation()
        if self._portable_runtime is not None:
            if activity:
                with self._lock:
                    self._activity = _safe_progress_text(activity)
                    self._last_activity_at = self._clock()
                    self._render_locked()
            self._opened = False
            self._rendered_lines = 0
            return
        if not self._enabled:
            if self._workflow_progress is not None and activity:
                with self._lock:
                    self._activity = _safe_progress_text(activity)
                    self._last_activity_at = self._clock()
                    self._render_locked()
            return
        if not self._opened:
            return
        with self._lock:
            if activity:
                self._activity = _safe_progress_text(activity)
                self._last_activity_at = self._clock()
            self._render_locked()
            try:
                self._stream.write("\n")
                self._stream.flush()
            except (OSError, ValueError):
                self._enabled = False
            self._opened = False
            self._rendered_lines = 0

    def _start_animation(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def _stop_animation(self) -> None:
        self._stop_requested.set()
        thread = self._thread
        if thread is not None:
            thread.join()
        self._thread = None

    def _animate(self) -> None:
        while not self._stop_requested.wait(self._frame_seconds):
            with self._lock:
                self._frame_index += 1
                self._render_locked()

    def _render_locked(self) -> None:
        if not self._enabled and self._workflow_progress is None:
            return
        now = self._clock()
        elapsed_seconds = max(0.0, now - self._started_at)
        inactivity_seconds = (
            elapsed_seconds
            if self._last_activity_at is None
            else max(0.0, now - self._last_activity_at)
        )
        frames = UNICODE_WAITING_FRAMES if self._unicode else WAITING_FRAMES
        frame = frames[self._frame_index % len(frames)]
        terminal_size = self._terminal_size(fallback=(80, 24))
        width = max(1, terminal_size.columns - 1)
        rendered = render_issue_dashboard(
            IssueDashboardSnapshot(
                issue_number=self._issue_number,
                issue_title=self._issue_title,
                position=self._position,
                total=self._total,
                pass_number=self._pass_number,
                active_stage=self._active_stage,
                statuses=dict(self._statuses),
                stage_durations=dict(self._stage_durations),
                step_progress=self._step_progress,
                workflow_progress=_live_workflow_progress(
                    self._workflow_progress,
                    elapsed_seconds=elapsed_seconds,
                    inactivity_seconds=inactivity_seconds,
                    activity=self._activity,
                    issue_title=self._issue_title,
                    issue_position=self._position,
                    issue_total=self._total,
                    issue_history=tuple(self._issue_history),
                ),
                issue_history=tuple(self._issue_history),
                elapsed_seconds=elapsed_seconds,
                inactivity_seconds=inactivity_seconds,
                activity=self._activity,
                max_step_rows=max(1, terminal_size.lines - 11),
                scheduler_summary=self._scheduler_summary,
            ),
            width=width,
            color=_use_color(self._stream),
            unicode=self._unicode,
            frame=frame,
        )
        lines = rendered.splitlines()
        if self._portable_runtime is not None:
            self._portable_runtime.show_screen(rendered)
            self._rendered_lines = len(lines)
            self._opened = True
            return
        if not self._enabled:
            try:
                self._stream.write(f"{rendered}\n")
                self._stream.flush()
            except (OSError, ValueError):
                pass
            return
        try:
            _rewrite_terminal_lines(
                self._stream,
                lines,
                previous_line_count=self._rendered_lines if self._opened else 0,
            )
            self._stream.flush()
        except (OSError, ValueError):
            self._enabled = False
            return
        self._rendered_lines = len(lines)
        self._opened = True


class WaitingIndicator:
    def __init__(
        self,
        stream: TextIO | None = None,
        frame_seconds: float = WAITING_FRAME_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        stalled_after_seconds: float = WAITING_STALL_SECONDS,
        *,
        stage: Stage = Stage.ANALYSIS,
        context: str = "",
        workflow_progress: WorkflowProgress | None = None,
    ) -> None:
        self._stream = sys.stdout if stream is None else stream
        self._frame_seconds = frame_seconds
        self._clock = clock
        self._stalled_after_seconds = stalled_after_seconds
        self._stage = stage
        self._context = _safe_progress_text(context)
        self._workflow_progress = workflow_progress
        isatty = getattr(self._stream, "isatty", None)
        from .portable_runtime import (
            active_portable_runtime,
            portable_plain_mode_active,
        )

        self._portable_runtime = active_portable_runtime()
        self._enabled = bool(
            self._portable_runtime is not None
            or (
                not portable_plain_mode_active()
                and callable(isatty)
                and isatty()
            )
        )
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._activity_lock = threading.Lock()
        self._started_at = self._clock()
        self._last_activity_at: float | None = None
        self._activity = (
            workflow_progress.activity.safe_text
            if workflow_progress is not None
            else "Waiting for the first Codex update."
        )
        self._rendered_width = 0
        self._rendered_lines = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        if not self._enabled:
            if self._workflow_progress is not None:
                try:
                    self._stream.write(f"{self._progress_panel('|')}\n")
                    self._stream.flush()
                except (OSError, ValueError):
                    pass
            return
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_requested.set()
        self._thread.join()
        self._thread = None
        if self._workflow_progress is None:
            self._clear()
        else:
            self._clear_progress_panel()

    def notify_activity(self, activity: str | None = None) -> None:
        with self._activity_lock:
            self._last_activity_at = self._clock()
            if activity:
                self._activity = _safe_progress_text(activity)

    def _animate(self) -> None:
        frame_index = 0
        while True:
            frames = (
                UNICODE_WAITING_FRAMES
                if self._workflow_progress is not None
                and _can_encode("⠋", self._stream)
                else WAITING_FRAMES
            )
            frame = frames[frame_index % len(frames)]
            if self._workflow_progress is not None:
                self._write_progress_panel(self._progress_panel(frame))
                if self._stop_requested.wait(self._frame_seconds):
                    return
                frame_index += 1
                continue
            status_line = self._status_line(frame)
            if self._portable_runtime is not None:
                self._portable_runtime.show_screen(status_line)
                if self._stop_requested.wait(self._frame_seconds):
                    return
                frame_index += 1
                continue
            status_width = _terminal_display_width(status_line)
            padding = " " * max(0, self._rendered_width - status_width)
            try:
                self._stream.write(f"\r{status_line}{padding}")
                self._stream.flush()
            except (OSError, ValueError):
                return
            self._rendered_width = max(self._rendered_width, status_width)
            if self._stop_requested.wait(self._frame_seconds):
                return
            frame_index += 1

    def _progress_panel(self, frame: str) -> str:
        now = self._clock()
        with self._activity_lock:
            last_activity_at = self._last_activity_at
            activity = self._activity
        elapsed_seconds = max(0.0, now - self._started_at)
        inactivity_seconds = (
            elapsed_seconds
            if last_activity_at is None
            else max(0.0, now - last_activity_at)
        )
        progress = _live_workflow_progress(
            self._workflow_progress,
            elapsed_seconds=elapsed_seconds,
            inactivity_seconds=inactivity_seconds,
            activity=activity,
            issue_title="",
            issue_position=0,
            issue_total=0,
        )
        if progress is None:
            return self._status_line(frame)
        terminal_size = shutil.get_terminal_size(fallback=(80, 24))
        unicode = _can_encode("─⠋›…", self._stream)
        return render_workflow_progress(
            progress,
            width=max(1, terminal_size.columns - 1),
            color=_use_color(self._stream),
            unicode=unicode,
            frame=frame,
            max_step_rows=max(1, terminal_size.lines - 7),
        )

    def _write_progress_panel(self, rendered: str) -> None:
        lines = rendered.splitlines()
        if self._portable_runtime is not None:
            self._portable_runtime.show_screen(rendered)
            self._rendered_lines = len(lines)
            return
        try:
            _rewrite_terminal_lines(
                self._stream,
                lines,
                previous_line_count=self._rendered_lines,
            )
            self._stream.flush()
        except (OSError, ValueError):
            return
        self._rendered_lines = len(lines)

    def _clear_progress_panel(self) -> None:
        if self._portable_runtime is not None:
            self._rendered_lines = 0
            return
        if self._rendered_lines <= 0:
            return
        try:
            if self._rendered_lines > 1:
                self._stream.write(
                    f"\x1b[{self._rendered_lines - 1}A{_CARRIAGE_RETURN}"
                )
            for index in range(self._rendered_lines):
                self._stream.write(f"{_ERASE_LINE}{_CARRIAGE_RETURN}")
                if index < self._rendered_lines - 1:
                    self._stream.write("\n")
            if self._rendered_lines > 1:
                self._stream.write(
                    f"\x1b[{self._rendered_lines - 1}A{_CARRIAGE_RETURN}"
                )
            self._stream.flush()
        except (OSError, ValueError):
            pass
        self._rendered_lines = 0

    def _status_line(self, frame: str) -> str:
        now = self._clock()
        with self._activity_lock:
            last_activity_at = self._last_activity_at

        elapsed_seconds = max(0.0, now - self._started_at)
        inactivity_seconds = (
            elapsed_seconds
            if last_activity_at is None
            else max(0.0, now - last_activity_at)
        )
        elapsed = format_duration(elapsed_seconds)
        inactivity = format_duration(inactivity_seconds)
        prefix = f"[{self._stage.value}]"
        working = render_status(DashboardStatus.WORKING, self._stream)
        if self._context:
            prefix = f"{prefix} {self._context} |"

            if inactivity_seconds >= self._stalled_after_seconds:
                return (
                    f"{prefix} STALL? [{frame}] {elapsed} | "
                    f"silent {inactivity} | Ctrl+C"
                )
            if last_activity_at is None:
                return f"{prefix} {working} [{frame}] {elapsed} | awaiting event"
            return f"{prefix} {working} [{frame}] {elapsed} | evt {inactivity} ago"

        if inactivity_seconds >= self._stalled_after_seconds:
            return (
                f"{prefix} POSSIBLY STALLED [{frame}] elapsed {elapsed} | "
                f"silent {inactivity} | Ctrl+C"
            )
        if last_activity_at is None:
            return (
                f"{prefix} Codex is {working} [{frame}] elapsed {elapsed} | "
                "waiting for first event"
            )
        return (
            f"{prefix} Codex is {working} [{frame}] elapsed {elapsed} | "
            f"last event {inactivity} ago"
        )

    def _clear(self) -> None:
        if self._portable_runtime is not None:
            return
        try:
            self._stream.write(f"\r{' ' * self._rendered_width}\r")
            self._stream.flush()
        except (OSError, ValueError):
            pass


def _stream(stream=None):
    return stream if stream is not None else sys.stdout


def _use_color(stream=None) -> bool:
    from .portable_runtime import portable_plain_mode_active

    if portable_plain_mode_active():
        return False
    if os.environ.get("NO_COLOR"):
        return False
    stream = _stream(stream)
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _can_encode(text: str, stream=None) -> bool:
    encoding = getattr(_stream(stream), "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def render_banner(stage: Stage, context: str = "", stream=None) -> str:
    context = _safe_progress_text(context, max_length=300)
    unicode_ok = _can_encode("●○→·─", stream)
    active_marker = "●" if unicode_ok else "*"
    idle_marker = "○" if unicode_ok else "."
    separator = " > " if unicode_ok else " > "
    dot = " · " if unicode_ok else " - "
    rule_char = "─" if unicode_ok else "-"
    color = _use_color(stream)

    parts: list[str] = ["Dev Loop"]
    for item in PIPELINE:
        marker = active_marker if item is stage else idle_marker
        label = f"{item.value} {marker}"
        if item is stage and color:
            label = f"{_ACTIVE_COLOR}{label}{_RESET}"
        parts.append(label)

    suffix = f"{dot}{context}" if context else ""
    line = f" {separator.join(parts)}{suffix} "
    rule = rule_char * _BANNER_WIDTH
    return f"{rule}\n{line}\n{rule}"


def stage_prompt(stage: Stage) -> str:
    return f"[{stage.value}] > "
