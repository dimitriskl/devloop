from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .codex_runner import RoleResult
from .codex_events import RunWideBlocker, RunWideBlockerKind
from .issue_pack import Issue
from .issue_scheduler import SchedulingPhase
from .portable_workflow import (
    DataContractId,
    InterruptedStepAttemptRecord,
    IssueStatus,
    PortableStepComponentCatalog,
    PortableWorkflowCheckpoint,
    PortableWorkflowRunResult,
    StepAttemptRecord,
    StepInstanceId,
    StepOutcome,
    StepRuntimeState,
    StepRuntimeStatus,
    TypedStepOutput,
    WorkflowDefinition,
    canonical_workflow_hash,
    load_portable_workflow,
    parse_issue_status,
    step_attempt_record_to_dict,
)
from .step_configuration import StepAttemptContext, StepCapabilityProfile


class ResumeRole(str, Enum):
    CODER = "coder"
    REVIEWER = "reviewer"
    QA = "qa"
    COMPLETE = "complete"


RESUMABLE_ROLE_ORDER = {
    ResumeRole.CODER.value: 0,
    ResumeRole.REVIEWER.value: 1,
    ResumeRole.QA.value: 2,
}
NORMAL_ROLE_LOG_PATTERN = re.compile(
    r"^(?P<issue>\d+)-(?P<role>coder|reviewer|qa)-pass(?P<pass>\d+)\.last-message\.json$"
)


@dataclass(frozen=True)
class IssueResumeCursor:
    pass_number: int = 1
    next_role: ResumeRole = ResumeRole.CODER
    fix_list: tuple[str, ...] = ()
    coder_result: RoleResult | None = None
    reviewer_result: RoleResult | None = None
    qa_result: RoleResult | None = None


class LoopStateWriter:
    def __init__(self, issues_index: Path) -> None:
        self.issues_index = issues_index
        self.state_path = issues_index.with_name(f"{issues_index.stem}.loop.state.json")
        self.board_path = issues_index.with_name(f"{issues_index.stem}.loop.md")
        self.prd_state_path: Path | None = None
        self.prd_board_path: Path | None = None
        self.state = load_existing_state(self.state_path, issues_index)

    def record_resolved_workflow(
        self,
        workflow: WorkflowDefinition,
        catalog: PortableStepComponentCatalog,
    ) -> WorkflowDefinition:
        validated_workflow = load_portable_workflow(workflow.to_dict(), catalog)
        workflow_hash = canonical_workflow_hash(validated_workflow)
        if (
            "resolved_workflow" in self.state
            or "resolved_workflow_hash" in self.state
        ):
            existing_workflow = self.resolved_workflow(catalog)
            if canonical_workflow_hash(existing_workflow) == workflow_hash:
                return existing_workflow
            raise ValueError("The resolved portable workflow is immutable for an active run.")
        self.state["resolved_workflow"] = validated_workflow.to_dict()
        self.state["resolved_workflow_hash"] = workflow_hash
        self.flush()
        return validated_workflow

    def resolved_workflow(
        self,
        catalog: PortableStepComponentCatalog,
    ) -> WorkflowDefinition:
        document = self.state.get("resolved_workflow")
        if not isinstance(document, dict):
            raise ValueError("Loop state has no resolved portable workflow.")
        workflow = load_portable_workflow(document, catalog)
        expected_hash = self.state.get("resolved_workflow_hash")
        actual_hash = canonical_workflow_hash(workflow)
        if expected_hash != actual_hash:
            raise ValueError("Resolved portable workflow hash does not match its content.")
        return workflow

    def record_step_runtime_state(
        self,
        issue: Issue,
        runtime: StepRuntimeState,
    ) -> None:
        self._store_step_runtime_state(runtime)
        issue_state = self.issue_state(issue)
        issue_state.update({"title": issue.title, "path": str(issue.path)})
        if runtime.status is StepRuntimeStatus.RUNNING:
            issue_state["status"] = IssueStatus.IN_PROGRESS.value
            issue_state["current_step_instance_id"] = str(runtime.step_instance_id)
        self.flush()

    def record_portable_checkpoint(
        self,
        issue: Issue,
        checkpoint: PortableWorkflowCheckpoint,
    ) -> None:
        if checkpoint.issue_id != issue.number:
            raise ValueError("Portable workflow checkpoint belongs to another Issue.")
        for runtime in checkpoint.runtime_states:
            if runtime.issue_id != issue.number:
                raise ValueError("Portable workflow runtime belongs to another Issue.")
            self._store_step_runtime_state(runtime)
        for attempt in checkpoint.attempts:
            if attempt.issue_id != issue.number:
                raise ValueError("Portable workflow attempt belongs to another Issue.")
            self._store_step_attempt_record(attempt)
        issue_state = self.issue_state(issue)
        issue_state.update(
            {
                "title": issue.title,
                "path": str(issue.path),
                "status": checkpoint.issue_status.value,
                "current_pass": checkpoint.pass_number,
            }
        )
        if checkpoint.current_step_instance_id is None:
            issue_state.pop("current_step_instance_id", None)
            issue_state.pop("cycle_path_step_instance_ids", None)
        else:
            issue_state["current_step_instance_id"] = str(
                checkpoint.current_step_instance_id
            )
            issue_state["cycle_path_step_instance_ids"] = [
                str(step_id)
                for step_id in checkpoint.cycle_path_step_instance_ids
            ]
        if checkpoint.pending_rework_attempt_id is None:
            issue_state.pop("pending_rework_attempt_id", None)
        else:
            issue_state["pending_rework_attempt_id"] = (
                checkpoint.pending_rework_attempt_id
            )
        self.flush()

    def resume_portable_workflow(
        self,
        issue: Issue,
        workflow: WorkflowDefinition,
    ) -> PortableWorkflowCheckpoint | None:
        issue_state = self.issue_state(issue)
        if parse_issue_status(issue_state.get("status")) is not IssueStatus.IN_PROGRESS:
            return None
        raw_step_id = issue_state.get("current_step_instance_id")
        if not isinstance(raw_step_id, str):
            return None
        current_step_id = StepInstanceId(raw_step_id)
        try:
            workflow.step(current_step_id)
        except KeyError as error:
            raise ValueError(
                "Portable workflow cursor references an unknown Step Instance ID."
            ) from error
        pass_number = issue_state.get("current_pass")
        if not isinstance(pass_number, int) or pass_number < 1:
            matching_runtime = next(
                (
                    runtime
                    for runtime in reversed(self.step_runtime_states(issue.number))
                    if runtime.step_instance_id == current_step_id
                ),
                None,
            )
            if matching_runtime is None:
                raise ValueError("Portable workflow cursor has no valid pass number.")
            pass_number = matching_runtime.pass_number
        pending_rework_attempt_id = optional_state_string(
            issue_state.get("pending_rework_attempt_id")
        )
        raw_cycle_path = issue_state.get("cycle_path_step_instance_ids", [])
        if not isinstance(raw_cycle_path, list) or not all(
            isinstance(step_id, str) for step_id in raw_cycle_path
        ):
            raise ValueError("Portable workflow cycle path must be a list of IDs.")
        return PortableWorkflowCheckpoint(
            issue_id=issue.number,
            issue_status=IssueStatus.IN_PROGRESS,
            current_step_instance_id=current_step_id,
            pass_number=pass_number,
            runtime_states=self.step_runtime_states(issue.number),
            attempts=self.step_attempt_records(issue.number),
            pending_rework_attempt_id=pending_rework_attempt_id,
            cycle_path_step_instance_ids=tuple(
                StepInstanceId(step_id) for step_id in raw_cycle_path
            ),
        )

    def retry_portable_workflow(
        self,
        issue: Issue,
        workflow: WorkflowDefinition,
        *,
        pass_number: int | None = None,
    ) -> PortableWorkflowCheckpoint | None:
        if pass_number is not None and pass_number < 1:
            raise ValueError("Portable workflow retry pass must be positive.")
        issue_state = self.issue_state(issue)
        issue_status = parse_issue_status(issue_state.get("status"))
        if issue_status not in {
            IssueStatus.CHANGES_REQUESTED,
            IssueStatus.BLOCKED,
            IssueStatus.FAILED,
            IssueStatus.CANCELLED,
        }:
            return None
        attempts = self.step_attempt_records(issue.number)
        if not attempts:
            return None
        latest_attempt = attempts[-1]
        if latest_attempt.outcome.value != issue_status.value:
            raise ValueError(
                "Portable workflow retry status does not match its latest attempt."
            )
        try:
            latest_step = workflow.step(latest_attempt.step_instance_id)
        except KeyError as error:
            raise ValueError(
                "Portable workflow retry references an unknown Step Instance ID."
            ) from error
        current_step_id = latest_attempt.step_instance_id
        persisted_rework_attempt_id = optional_state_string(
            issue_state.get("pending_rework_attempt_id")
        )
        linked_rework_attempt_id = latest_attempt.rework_attempt_id
        if (
            persisted_rework_attempt_id is not None
            and linked_rework_attempt_id is not None
            and persisted_rework_attempt_id != linked_rework_attempt_id
        ):
            raise ValueError("Portable workflow retry has conflicting rework triggers.")
        pending_rework_attempt_id = (
            linked_rework_attempt_id or persisted_rework_attempt_id
        )
        default_pass_number = latest_attempt.pass_number
        if issue_status is IssueStatus.CHANGES_REQUESTED:
            current_step_id = latest_step.transitions.get(
                StepOutcome.CHANGES_REQUESTED
            )
            if current_step_id is None:
                raise ValueError(
                    "Changes-requested retry has no configured destination."
                )
            pending_rework_attempt_id = latest_attempt.attempt_id
            default_pass_number += 1
        return PortableWorkflowCheckpoint(
            issue_id=issue.number,
            issue_status=IssueStatus.IN_PROGRESS,
            current_step_instance_id=current_step_id,
            pass_number=(
                default_pass_number
                if pass_number is None
                else pass_number
            ),
            runtime_states=self.step_runtime_states(issue.number),
            attempts=attempts,
            pending_rework_attempt_id=pending_rework_attempt_id,
            cycle_path_step_instance_ids=(current_step_id,),
        )

    def completed_portable_workflow(
        self,
        issue: Issue,
        workflow: WorkflowDefinition,
    ) -> PortableWorkflowRunResult | None:
        if (
            parse_issue_status(self.issue_state(issue).get("status"))
            is not IssueStatus.COMPLETED
        ):
            return None
        attempts = self.step_attempt_records(issue.number)
        if not attempts or attempts[-1].outcome is not StepOutcome.SUCCEEDED:
            raise ValueError(
                "Completed portable workflow has no successful terminal attempt."
            )
        terminal_step = workflow.step(attempts[-1].step_instance_id)
        if terminal_step.transitions.get(StepOutcome.SUCCEEDED) is not None:
            raise ValueError(
                "Completed portable workflow did not stop at a terminal step."
            )
        return PortableWorkflowRunResult(
            issue_status=IssueStatus.COMPLETED,
            current_step_instance_id=None,
            runtime_states=self.step_runtime_states(issue.number),
            attempts=attempts,
            role_result=attempts[-1].result,
        )

    def record_portable_execution_result(
        self,
        issue: Issue,
        execution: PortableWorkflowRunResult,
        *,
        attempt_label: str | None = None,
        retry_round: int | None = None,
    ) -> None:
        for runtime in execution.runtime_states:
            self._store_step_runtime_state(runtime)
        for attempt in execution.attempts:
            self._store_step_attempt_record(attempt)
        issue_state = self.issue_state(issue)
        issue_state.update(
            {
                "title": issue.title,
                "path": str(issue.path),
                "status": execution.issue_status.value,
            }
        )
        if execution.current_step_instance_id is None:
            issue_state.pop("current_step_instance_id", None)
            issue_state.pop("cycle_path_step_instance_ids", None)
        else:
            issue_state["current_step_instance_id"] = str(
                execution.current_step_instance_id
            )
        if execution.issue_status in {
            IssueStatus.CHANGES_REQUESTED,
            IssueStatus.BLOCKED,
            IssueStatus.FAILED,
        }:
            latest_attempt = execution.attempts[-1]
            issue_state.update(
                {
                    "blocked_at": now(),
                    "blocked_gate": str(latest_attempt.step_instance_id),
                    "blocked_summary": execution.role_result.summary,
                    "findings": list(execution.role_result.findings),
                    "fix_list": list(
                        execution.role_result.fix_list
                        or execution.role_result.findings
                    ),
                    "residual_risks": list(execution.role_result.residual_risks),
                }
            )
        elif execution.issue_status is IssueStatus.COMPLETED:
            successful_attempts = _latest_successful_portable_attempts(execution)
            successful_results = [attempt.result for attempt in successful_attempts]
            issue_state["completed_at"] = now()
            issue_state["changed_files"] = _unique_strings(
                result.changed_files for result in successful_results
            )
            issue_state["verification_commands"] = sorted(
                _unique_strings(
                    result.verification_commands for result in successful_results
                )
            )
            issue_state["step_summaries"] = [
                {
                    "step_instance_id": str(attempt.step_instance_id),
                    "summary": attempt.result.summary,
                }
                for attempt in successful_attempts
            ]
            if attempt_label:
                issue_state["completed_attempt"] = attempt_label
            if retry_round is not None:
                issue_state["completed_retry_round"] = retry_round
            for field_name in (
                "blocked_at",
                "blocked_gate",
                "blocked_summary",
                "findings",
                "fix_list",
                "residual_risks",
            ):
                issue_state.pop(field_name, None)
            event = {"issue": issue.number}
            if attempt_label:
                event["attempt"] = attempt_label
            if retry_round is not None:
                event["retry_round"] = retry_round
            self.add_event("issue-completed", event)
        self.flush()

    def _store_step_runtime_state(self, runtime: StepRuntimeState) -> None:
        issue_key = runtime.issue_id or "__workflow__"
        step_states = self.state.setdefault("step_runtime_states", {})
        issue_states = step_states.setdefault(str(runtime.step_instance_id), {})
        existing_runtime = issue_states.get(issue_key)
        if isinstance(existing_runtime, dict):
            self._preserve_interrupted_runtime(existing_runtime, runtime)
        issue_states[issue_key] = {
            "step_instance_id": str(runtime.step_instance_id),
            "issue_id": runtime.issue_id,
            "status": runtime.status.value,
            "pass": runtime.pass_number,
            "prompt_session_id": runtime.prompt_session_id,
            "attempt_id": runtime.attempt_id,
            "started_at": runtime.started_at,
            "outcome": runtime.outcome.value if runtime.outcome is not None else None,
            "backend_thread_id": runtime.backend_thread_id,
            "backend_turn_id": runtime.backend_turn_id,
            "checkpoint": runtime.checkpoint,
            "component_state": dict(runtime.component_state),
            "attempt_context": (
                runtime.attempt_context.to_dict()
                if runtime.attempt_context is not None
                else None
            ),
        }

    def _preserve_interrupted_runtime(
        self,
        stored_runtime: dict[str, Any],
        replacement: StepRuntimeState,
    ) -> None:
        previous = step_runtime_state_from_state(stored_runtime)
        if (
            previous.status is not StepRuntimeStatus.RUNNING
            or previous.attempt_id is None
            or previous.started_at is None
            or previous.prompt_session_id is None
            or replacement.attempt_id is None
            or previous.attempt_id == replacement.attempt_id
        ):
            return
        self._store_interrupted_step_attempt_record(
            InterruptedStepAttemptRecord(
                attempt_id=previous.attempt_id,
                step_instance_id=previous.step_instance_id,
                issue_id=previous.issue_id,
                pass_number=previous.pass_number,
                prompt_session_id=previous.prompt_session_id,
                started_at=previous.started_at,
                interrupted_at=now(),
                backend_thread_id=previous.backend_thread_id,
                backend_turn_id=previous.backend_turn_id,
                checkpoint=previous.checkpoint,
                attempt_context=previous.attempt_context,
            )
        )

    def _store_interrupted_step_attempt_record(
        self,
        attempt: InterruptedStepAttemptRecord,
    ) -> None:
        records = self.state.setdefault("interrupted_step_attempt_records", [])
        if not isinstance(records, list):
            raise ValueError("Interrupted portable step attempt history must be a list.")
        if any(
            isinstance(stored, dict)
            and stored.get("attempt_id") == attempt.attempt_id
            for stored in records
        ):
            return
        records.append(
            {
                "attempt_id": attempt.attempt_id,
                "step_instance_id": str(attempt.step_instance_id),
                "issue_id": attempt.issue_id,
                "pass": attempt.pass_number,
                "prompt_session_id": attempt.prompt_session_id,
                "started_at": attempt.started_at,
                "interrupted_at": attempt.interrupted_at,
                "backend_thread_id": attempt.backend_thread_id,
                "backend_turn_id": attempt.backend_turn_id,
                "checkpoint": attempt.checkpoint,
                "attempt_context": (
                    attempt.attempt_context.to_dict()
                    if attempt.attempt_context is not None
                    else None
                ),
            }
        )

    def _store_step_attempt_record(self, attempt: StepAttemptRecord) -> None:
        issue_key = attempt.issue_id or "__workflow__"
        step_attempts = self.state.setdefault("step_attempt_records", {})
        attempt_order = self.state.get("step_attempt_order")
        if attempt_order is None:
            attempt_order = [
                stored.get("attempt_id")
                for issue_records in step_attempts.values()
                if isinstance(issue_records, dict)
                for raw_attempts in issue_records.values()
                if isinstance(raw_attempts, list)
                for stored in raw_attempts
                if isinstance(stored, dict)
                and isinstance(stored.get("attempt_id"), str)
            ]
            self.state["step_attempt_order"] = attempt_order
        if not isinstance(attempt_order, list):
            raise ValueError("Portable step attempt order must be a list.")
        issue_attempts = step_attempts.setdefault(
            str(attempt.step_instance_id), {}
        ).setdefault(issue_key, [])
        if any(
            isinstance(stored, dict)
            and stored.get("attempt_id") == attempt.attempt_id
            for stored in issue_attempts
        ):
            return
        if attempt.attempt_id in attempt_order:
            raise ValueError("Portable Step Attempt ID must be globally unique.")
        issue_attempts.append(step_attempt_record_to_dict(attempt))
        attempt_order.append(attempt.attempt_id)

    def step_runtime_states(
        self,
        issue_id: str | None = None,
    ) -> tuple[StepRuntimeState, ...]:
        stored_runtimes = self.state.get("step_runtime_states", {})
        if not isinstance(stored_runtimes, dict):
            raise ValueError("Portable step runtime states must be an object.")
        runtimes: list[StepRuntimeState] = []
        for issue_states in stored_runtimes.values():
            if not isinstance(issue_states, dict):
                raise ValueError("Portable step runtime Issue states must be an object.")
            for raw_runtime in issue_states.values():
                runtime = step_runtime_state_from_state(raw_runtime)
                if issue_id is None or runtime.issue_id == issue_id:
                    runtimes.append(runtime)
        return tuple(runtimes)

    def step_attempt_records(
        self,
        issue_id: str | None = None,
    ) -> tuple[StepAttemptRecord, ...]:
        stored_attempts = self.state.get("step_attempt_records", {})
        if not isinstance(stored_attempts, dict):
            raise ValueError("Portable step attempt records must be an object.")
        attempts_by_id: dict[str, StepAttemptRecord] = {}
        fallback_order: list[str] = []
        for issue_records in stored_attempts.values():
            if not isinstance(issue_records, dict):
                raise ValueError("Portable step attempt issue records must be an object.")
            for raw_attempts in issue_records.values():
                if not isinstance(raw_attempts, list):
                    raise ValueError("Portable step attempt history must be a list.")
                for raw_attempt in raw_attempts:
                    attempt = step_attempt_record_from_state(raw_attempt)
                    attempts_by_id[attempt.attempt_id] = attempt
                    fallback_order.append(attempt.attempt_id)
        raw_order = self.state.get("step_attempt_order", fallback_order)
        if not isinstance(raw_order, list) or not all(
            isinstance(attempt_id, str) for attempt_id in raw_order
        ):
            raise ValueError("Portable step attempt order must be a list of IDs.")
        if len(set(raw_order)) != len(raw_order):
            raise ValueError("Portable step attempt order contains duplicate IDs.")
        unknown_order_ids = set(raw_order) - set(attempts_by_id)
        if unknown_order_ids:
            raise ValueError("Portable step attempt order references unknown IDs.")
        ordered_attempts = [
            attempts_by_id[attempt_id]
            for attempt_id in raw_order
            if attempt_id in attempts_by_id
        ]
        ordered_ids = set(raw_order)
        ordered_attempts.extend(
            attempt
            for attempt_id, attempt in attempts_by_id.items()
            if attempt_id not in ordered_ids
        )
        return tuple(
            attempt
            for attempt in ordered_attempts
            if issue_id is None or attempt.issue_id == issue_id
        )

    def interrupted_step_attempt_records(
        self,
        issue_id: str | None = None,
    ) -> tuple[InterruptedStepAttemptRecord, ...]:
        stored_attempts = self.state.get("interrupted_step_attempt_records", [])
        if not isinstance(stored_attempts, list):
            raise ValueError("Interrupted portable step attempt history must be a list.")
        attempts = tuple(
            interrupted_step_attempt_record_from_state(raw_attempt)
            for raw_attempt in stored_attempts
        )
        return tuple(
            attempt
            for attempt in attempts
            if issue_id is None or attempt.issue_id == issue_id
        )

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

    def record_dependency_projection(
        self,
        issues: Iterable[Issue],
        *,
        ready: Iterable[str],
        waiting: dict[str, tuple[str, ...]],
        phase: SchedulingPhase | None = None,
    ) -> None:
        ready_numbers = frozenset(ready)
        for issue in issues:
            issue_state = self.issue_state(issue)
            issue_state.update({"title": issue.title, "path": str(issue.path)})
            current_status = parse_issue_status(issue_state.get("status"))
            if issue.number in waiting:
                if current_status in {
                    None,
                    IssueStatus.PENDING,
                    IssueStatus.READY,
                    IssueStatus.WAITING_ON_DEPENDENCY,
                    IssueStatus.SKIPPED,
                }:
                    issue_state["status"] = IssueStatus.WAITING_ON_DEPENDENCY.value
                    issue_state["waiting_on"] = list(waiting[issue.number])
                continue
            issue_state.pop("waiting_on", None)
            if issue.number in ready_numbers and current_status in {
                None,
                IssueStatus.PENDING,
                IssueStatus.READY,
                IssueStatus.WAITING_ON_DEPENDENCY,
                IssueStatus.SKIPPED,
            }:
                issue_state["status"] = IssueStatus.READY.value
        scheduler_state = self.state.setdefault("dependency_scheduler", {})
        if phase is not None:
            scheduler_state["phase"] = phase.value
        scheduler_state["ready"] = sorted(ready_numbers)
        scheduler_state["waiting"] = {
            issue_number: list(dependencies)
            for issue_number, dependencies in waiting.items()
        }
        scheduler_state["updated_at"] = now()
        self.add_event(
            "dependency-projection",
            {
                "ready": sorted(ready_numbers),
                "waiting": sorted(waiting),
            },
        )
        self.flush()

    def reserve_scheduling_attempt(
        self,
        issue: Issue,
        *,
        phase: SchedulingPhase,
        ordinal: int,
    ) -> None:
        if ordinal < 1:
            raise ValueError("Scheduling attempt ordinal must be positive.")
        if phase not in {
            SchedulingPhase.NORMAL_SCHEDULING,
            SchedulingPhase.BLOCKER_RESOLUTION,
        }:
            raise ValueError("Only executable scheduling phases can reserve attempts.")
        scheduler_state = self.state.setdefault("dependency_scheduler", {})
        reservation = {
            "issue": issue.number,
            "phase": phase.value,
            "ordinal": ordinal,
        }
        active = scheduler_state.get("active_attempt")
        if active is not None and active != reservation:
            raise ValueError("Another scheduling attempt is already active.")
        scheduler_state["phase"] = phase.value
        scheduler_state["active_attempt"] = reservation
        scheduler_state["updated_at"] = now()
        if active is None:
            self.add_event("scheduling-attempt-reserved", reservation)
        self.flush()

    def active_scheduling_attempt(self) -> dict[str, Any] | None:
        scheduler_state = self.state.get("dependency_scheduler", {})
        if not isinstance(scheduler_state, dict):
            raise ValueError("Dependency Scheduler state must be an object.")
        active = scheduler_state.get("active_attempt")
        if active is None:
            return None
        if not isinstance(active, dict):
            raise ValueError("Active scheduling attempt must be an object.")
        issue = active.get("issue")
        phase = active.get("phase")
        ordinal = active.get("ordinal")
        if (
            not isinstance(issue, str)
            or not isinstance(phase, str)
            or not isinstance(ordinal, int)
            or ordinal < 1
        ):
            raise ValueError("Active scheduling attempt is invalid.")
        parsed_phase = SchedulingPhase(phase)
        if parsed_phase not in {
            SchedulingPhase.NORMAL_SCHEDULING,
            SchedulingPhase.BLOCKER_RESOLUTION,
        }:
            raise ValueError("Active scheduling attempt has a terminal phase.")
        return {"issue": issue, "phase": phase, "ordinal": ordinal}

    def normal_attempted_issues(self) -> frozenset[str]:
        scheduler_state = self.state.get("dependency_scheduler", {})
        if not isinstance(scheduler_state, dict):
            raise ValueError("Dependency Scheduler state must be an object.")
        values = scheduler_state.get("normal_attempted", [])
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise ValueError("Normal scheduling attempt history must be a list of IDs.")
        return frozenset(values)

    def additional_passes(self) -> dict[str, int]:
        scheduler_state = self.state.get("dependency_scheduler", {})
        if not isinstance(scheduler_state, dict):
            raise ValueError("Dependency Scheduler state must be an object.")
        values = scheduler_state.get("additional_passes", {})
        if not isinstance(values, dict):
            raise ValueError("Blocker Resolution counters must be an object.")
        parsed: dict[str, int] = {}
        for issue_number, count in values.items():
            if (
                not isinstance(issue_number, str)
                or not isinstance(count, int)
                or count < 0
            ):
                raise ValueError("Blocker Resolution counters are invalid.")
            parsed[issue_number] = count
        return parsed

    def complete_scheduling_attempt(
        self,
        issue: Issue,
        *,
        outcome: IssueStatus,
    ) -> None:
        active = self.active_scheduling_attempt()
        if active is None or active["issue"] != issue.number:
            raise ValueError(
                f"No active scheduling attempt exists for issue {issue.number}."
            )
        scheduler_state = self.state.setdefault("dependency_scheduler", {})
        phase = SchedulingPhase(active["phase"])
        if phase is SchedulingPhase.NORMAL_SCHEDULING:
            attempted = sorted(self.normal_attempted_issues())
            if issue.number not in attempted:
                attempted.append(issue.number)
            scheduler_state["normal_attempted"] = attempted
        else:
            additional = self.additional_passes()
            additional[issue.number] = max(
                additional.get(issue.number, 0),
                active["ordinal"],
            )
            scheduler_state["additional_passes"] = additional
        scheduler_state.setdefault("attempt_history", []).append(
            {**active, "outcome": outcome.value, "finished_at": now()}
        )
        scheduler_state.pop("active_attempt", None)
        scheduler_state["updated_at"] = now()
        self.add_event(
            "scheduling-attempt-completed",
            {**active, "status": outcome.value},
        )
        self.flush()

    def release_scheduling_attempt(
        self,
        issue: Issue,
        *,
        outcome: IssueStatus,
    ) -> None:
        active = self.active_scheduling_attempt()
        if active is None or active["issue"] != issue.number:
            raise ValueError(
                f"No active scheduling attempt exists for issue {issue.number}."
            )
        scheduler_state = self.state.setdefault("dependency_scheduler", {})
        scheduler_state.setdefault("attempt_history", []).append(
            {
                **active,
                "outcome": outcome.value,
                "consumed": False,
                "finished_at": now(),
            }
        )
        scheduler_state.pop("active_attempt", None)
        scheduler_state["updated_at"] = now()
        self.add_event(
            "scheduling-attempt-released",
            {**active, "status": outcome.value},
        )
        self.flush()

    def record_run_paused(self, blocker: RunWideBlocker) -> None:
        active = self.active_scheduling_attempt()
        if active is None:
            raise ValueError("A Run-Wide Blocker requires an active scheduling attempt.")
        issue_state = self.state.get("issues", {}).get(active["issue"], {})
        if not isinstance(issue_state, dict):
            raise ValueError("Paused issue state must be an object.")
        pause = {
            "kind": blocker.kind.value,
            "summary": blocker.summary,
            "issue": active["issue"],
            "phase": active["phase"],
            "ordinal": active["ordinal"],
            "step_instance_id": issue_state.get("current_step_instance_id"),
            "pass": issue_state.get("current_pass"),
            "paused_at": now(),
        }
        previous = self.state.get("run_pause")
        pause["occurrences"] = (
            int(previous.get("occurrences", 0)) + 1
            if isinstance(previous, dict)
            else 1
        )
        self.state["run_pause"] = pause
        scheduler_state = self.state.setdefault("dependency_scheduler", {})
        scheduler_state["phase"] = SchedulingPhase.RUN_PAUSED.value
        self.add_event(
            "run-paused",
            {
                "issue": active["issue"],
                "status": blocker.kind.value,
                "phase": active["phase"],
                "ordinal": active["ordinal"],
            },
        )
        self.flush()

    def run_pause(self) -> dict[str, Any] | None:
        pause = self.state.get("run_pause")
        if pause is None:
            return None
        if not isinstance(pause, dict):
            raise ValueError("Run pause state must be an object.")
        kind = pause.get("kind")
        if not isinstance(kind, str):
            raise ValueError("Run pause kind is invalid.")
        RunWideBlockerKind(kind)
        return dict(pause)

    def clear_run_pause(self) -> None:
        pause = self.run_pause()
        if pause is None:
            return
        self.state.pop("run_pause", None)
        self.add_event(
            "run-resumed",
            {
                "issue": pause.get("issue", ""),
                "phase": pause.get("phase", ""),
                "ordinal": pause.get("ordinal"),
            },
        )
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

    def resume_issue(self, issue: Issue) -> IssueResumeCursor:
        issue_state = self.issue_state(issue)
        if parse_issue_status(issue_state.get("status")) is not IssueStatus.IN_PROGRESS:
            return IssueResumeCursor()

        passes = issue_state.get("passes")
        if not isinstance(passes, list):
            passes = []

        normal_passes = [
            entry
            for entry in passes
            if isinstance(entry, dict) and not entry.get("attempt")
        ]
        if not normal_passes:
            normal_passes = recover_role_passes(self.issues_index.parent / ".loop.logs", issue)
            if not normal_passes:
                return IssueResumeCursor()
            issue_state["passes"] = [*passes, *normal_passes]

        latest = normal_passes[-1]
        pass_number = latest.get("pass")
        result_data = latest.get("result")
        if not isinstance(pass_number, int) or not isinstance(result_data, dict):
            return IssueResumeCursor()

        role = latest.get("role")
        result = role_result_from_state(result_data)
        if role == ResumeRole.CODER.value and result.status == "PASS":
            return IssueResumeCursor(
                pass_number=pass_number,
                next_role=ResumeRole.REVIEWER,
                coder_result=result,
            )

        if role == ResumeRole.REVIEWER.value and result.status == "PASS":
            coder_result = find_role_result(normal_passes, ResumeRole.CODER, pass_number)
            if coder_result is not None:
                return IssueResumeCursor(
                    pass_number=pass_number,
                    next_role=ResumeRole.QA,
                    coder_result=coder_result,
                    reviewer_result=result,
                )

        if role == ResumeRole.QA.value and result.status == "PASS":
            coder_result = find_role_result(normal_passes, ResumeRole.CODER, pass_number)
            reviewer_result = find_role_result(normal_passes, ResumeRole.REVIEWER, pass_number)
            if coder_result is not None and reviewer_result is not None:
                return IssueResumeCursor(
                    pass_number=pass_number,
                    next_role=ResumeRole.COMPLETE,
                    coder_result=coder_result,
                    reviewer_result=reviewer_result,
                    qa_result=result,
                )

        if role in {ResumeRole.REVIEWER.value, ResumeRole.QA.value} and result.status != "PASS":
            return IssueResumeCursor(
                pass_number=pass_number + 1,
                next_role=ResumeRole.CODER,
                fix_list=tuple(result.fix_list or result.findings),
            )

        return IssueResumeCursor()

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
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def load_existing_state(state_path: Path, issues_index: Path) -> dict[str, Any]:
    try:
        state_text = state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {
            "started_at": now(),
            "issues_index": str(issues_index),
            "events": [],
            "issues": {},
        }
    except OSError as error:
        raise ValueError(
            f"Cannot load existing loop state {state_path}: "
            f"file could not be read: {error}"
        ) from error

    try:
        state = json.loads(state_text)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Cannot load existing loop state {state_path}: content is not valid JSON."
        ) from error

    if not isinstance(state, dict):
        raise ValueError(
            f"Cannot load existing loop state {state_path}: content must be a JSON object."
        )
    state.setdefault("events", [])
    state.setdefault("issues", {})
    return state


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


def role_result_from_state(data: dict[str, Any]) -> RoleResult:
    return RoleResult(
        status=str(data.get("status", "BLOCKED")),
        summary=str(data.get("summary", "")),
        changed_files=state_string_list(data.get("changed_files")),
        verification_commands=state_string_list(data.get("verification_commands")),
        findings=state_string_list(data.get("findings")),
        fix_list=state_string_list(data.get("fix_list")),
        residual_risks=state_string_list(data.get("residual_risks")),
    )


def step_runtime_state_from_state(data: Any) -> StepRuntimeState:
    if not isinstance(data, dict):
        raise ValueError("Portable step runtime state must be an object.")
    pass_number = data.get("pass")
    if not isinstance(pass_number, int) or pass_number < 1:
        raise ValueError("Portable step runtime pass must be a positive integer.")
    issue_id = data.get("issue_id")
    if issue_id is not None and not isinstance(issue_id, str):
        raise ValueError("Portable step runtime Issue ID must be a string or null.")
    raw_outcome = data.get("outcome")
    component_state = data.get("component_state", {})
    if not isinstance(component_state, dict):
        raise ValueError("Portable step component state must be an object.")
    return StepRuntimeState(
        step_instance_id=StepInstanceId(data.get("step_instance_id")),
        issue_id=issue_id,
        status=StepRuntimeStatus(data.get("status")),
        pass_number=pass_number,
        prompt_session_id=optional_state_string(data.get("prompt_session_id")),
        attempt_id=optional_state_string(data.get("attempt_id")),
        started_at=optional_state_string(data.get("started_at")),
        outcome=None if raw_outcome is None else StepOutcome(raw_outcome),
        backend_thread_id=optional_state_string(data.get("backend_thread_id")),
        backend_turn_id=optional_state_string(data.get("backend_turn_id")),
        checkpoint=optional_state_string(data.get("checkpoint")),
        component_state=component_state,
        attempt_context=_step_attempt_context_from_state(
            data.get("attempt_context"),
            record_name="Portable step runtime",
        ),
    )


def interrupted_step_attempt_record_from_state(
    data: Any,
) -> InterruptedStepAttemptRecord:
    if not isinstance(data, dict):
        raise ValueError("Interrupted portable step attempt must be an object.")
    pass_number = data.get("pass")
    if not isinstance(pass_number, int) or pass_number < 1:
        raise ValueError(
            "Interrupted portable step attempt pass must be a positive integer."
        )
    issue_id = data.get("issue_id")
    if issue_id is not None and not isinstance(issue_id, str):
        raise ValueError(
            "Interrupted portable step attempt Issue ID must be a string or null."
        )
    return InterruptedStepAttemptRecord(
        attempt_id=required_state_string(data, "attempt_id"),
        step_instance_id=StepInstanceId(data.get("step_instance_id")),
        issue_id=issue_id,
        pass_number=pass_number,
        prompt_session_id=required_state_string(data, "prompt_session_id"),
        started_at=required_state_string(data, "started_at"),
        interrupted_at=required_state_string(data, "interrupted_at"),
        backend_thread_id=optional_state_string(data.get("backend_thread_id")),
        backend_turn_id=optional_state_string(data.get("backend_turn_id")),
        checkpoint=optional_state_string(data.get("checkpoint")),
        attempt_context=_step_attempt_context_from_state(
            data.get("attempt_context"),
            record_name="Interrupted portable step attempt",
        ),
    )


def step_attempt_record_from_state(data: Any) -> StepAttemptRecord:
    if not isinstance(data, dict):
        raise ValueError("Portable step attempt record must be an object.")
    raw_outputs = data.get("outputs", {})
    if not isinstance(raw_outputs, dict):
        raise ValueError("Portable step attempt outputs must be an object.")
    outputs: dict[str, TypedStepOutput] = {}
    for port_name, raw_output in raw_outputs.items():
        if not isinstance(port_name, str) or not isinstance(raw_output, dict):
            raise ValueError("Portable step attempt output is malformed.")
        raw_result = raw_output.get("result")
        if not isinstance(raw_result, dict):
            raise ValueError("Portable step attempt output has no structured result.")
        outputs[port_name] = TypedStepOutput(
            contract_id=DataContractId(raw_output.get("contract_id")),
            value=role_result_from_state(raw_result),
        )

    raw_result = data.get("result")
    if not isinstance(raw_result, dict):
        raw_result = next(
            (
                raw_output.get("result")
                for raw_output in raw_outputs.values()
                if isinstance(raw_output, dict)
                and isinstance(raw_output.get("result"), dict)
            ),
            None,
        )
    if not isinstance(raw_result, dict):
        raise ValueError("Portable step attempt has no structured result.")

    pass_number = data.get("pass")
    if not isinstance(pass_number, int) or pass_number < 1:
        raise ValueError("Portable step attempt pass must be a positive integer.")
    issue_id = data.get("issue_id")
    if issue_id is not None and not isinstance(issue_id, str):
        raise ValueError("Portable step attempt Issue ID must be a string or null.")
    attempt_context = _step_attempt_context_from_state(
        data.get("attempt_context"),
        record_name="Portable step attempt",
    )
    return StepAttemptRecord(
        attempt_id=required_state_string(data, "attempt_id"),
        step_instance_id=StepInstanceId(data.get("step_instance_id")),
        issue_id=issue_id,
        pass_number=pass_number,
        prompt_session_id=required_state_string(data, "prompt_session_id"),
        outcome=StepOutcome(data.get("outcome")),
        result=role_result_from_state(raw_result),
        outputs=outputs,
        started_at=required_state_string(data, "started_at"),
        finished_at=required_state_string(data, "finished_at"),
        elapsed_seconds=float(data.get("elapsed_seconds", 0.0)),
        backend_thread_id=optional_state_string(data.get("backend_thread_id")),
        backend_turn_id=optional_state_string(data.get("backend_turn_id")),
        blocked_reason=optional_state_string(data.get("blocked_reason")),
        blocker_details=tuple(state_string_list(data.get("blocker_details"))),
        failure_reason=optional_state_string(data.get("failure_reason")),
        rework_attempt_id=optional_state_string(data.get("rework_attempt_id")),
        attempt_context=attempt_context,
    )


def _step_attempt_context_from_state(
    value: Any,
    *,
    record_name: str,
) -> StepAttemptContext | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{record_name} context must be an object or null.")
    raw_guidance = value.get("guidance")
    raw_precedence = value.get("guidance_precedence")
    if raw_guidance is not None and not isinstance(raw_guidance, str):
        raise ValueError(f"{record_name} guidance must be text or null.")
    if not isinstance(raw_precedence, str) or not raw_precedence:
        raise ValueError(f"{record_name} guidance precedence is required.")
    return StepAttemptContext(
        capability_profile=StepCapabilityProfile.from_dict(
            value.get("capability_profile")
        ),
        guidance=raw_guidance,
        guidance_precedence=raw_precedence,
    )


def required_state_string(data: dict[str, Any], field_name: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Portable step attempt {field_name!r} must be a string.")
    return value


def optional_state_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def state_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def find_role_result(
    passes: list[dict[str, Any]],
    role: ResumeRole,
    pass_number: int,
) -> RoleResult | None:
    for entry in reversed(passes):
        if entry.get("role") != role.value or entry.get("pass") != pass_number:
            continue
        result = entry.get("result")
        if isinstance(result, dict):
            return role_result_from_state(result)
    return None


def recover_role_passes(log_root: Path, issue: Issue) -> list[dict[str, Any]]:
    if not log_root.is_dir():
        return []

    recovered: list[dict[str, Any]] = []
    for path in log_root.glob(f"{issue.number}-*-pass*.last-message.json"):
        match = NORMAL_ROLE_LOG_PATTERN.fullmatch(path.name)
        if not match or match.group("issue") != issue.number:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        recovered.append(
            {
                "role": match.group("role"),
                "pass": int(match.group("pass")),
                "result": result_summary(role_result_from_state(data)),
                "timestamp": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                "recovered_from": str(path),
            }
        )

    recovered.sort(
        key=lambda entry: (
            entry["pass"],
            RESUMABLE_ROLE_ORDER[entry["role"]],
        )
    )
    return recovered


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
        "| Issue | Title | Status | Waiting on |",
        "| --- | --- | --- | --- |",
    ]

    for number, item in state.get("issues", {}).items():
        waiting_on = item.get("waiting_on", [])
        waiting_text = (
            ", ".join(waiting_on)
            if isinstance(waiting_on, list)
            else ""
        )
        lines.append(
            f"| {number} | {item.get('title', '')} | "
            f"{item.get('status', '')} | {waiting_text} |"
        )

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

    dependency_scheduler = state.get("dependency_scheduler")
    if isinstance(dependency_scheduler, dict):
        waiting = dependency_scheduler.get("waiting", {})
        additional = dependency_scheduler.get("additional_passes", {})
        lines.extend(
            [
                "",
                "## Dependency Scheduler",
                "",
                f"Phase: `{dependency_scheduler.get('phase', '')}`",
                f"Ready: `{', '.join(dependency_scheduler.get('ready', []))}`",
                f"Waiting: `{', '.join(waiting) if isinstance(waiting, dict) else ''}`",
                f"Additional passes: `{json.dumps(additional, sort_keys=True)}`",
            ]
        )

    run_pause = state.get("run_pause")
    if isinstance(run_pause, dict):
        lines.extend(
            [
                "",
                "## Run Paused",
                "",
                f"Kind: `{run_pause.get('kind', '')}`",
                f"Issue: `{run_pause.get('issue', '')}`",
                f"Scheduling phase: `{run_pause.get('phase', '')}`",
                f"Workflow step: `{run_pause.get('step_instance_id', '')}`",
                f"Pass: `{run_pause.get('pass', '')}`",
                f"Recovery: {run_pause.get('summary', '')}",
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
        *[
            f"- `{command}`"
            for command in sorted(
                set(coder.verification_commands + qa.verification_commands)
            )
        ],
        "",
        "### Review",
        reviewer.summary or "- PASS",
        "",
        "### QA",
        qa.summary or "- PASS",
        "",
    ]
    _write_issue_completion(issue_path, notes)


def mark_portable_issue_completed(
    issue_path: Path,
    workflow: WorkflowDefinition,
    execution: PortableWorkflowRunResult,
) -> None:
    step_results = tuple(
        (
            workflow.step(attempt.step_instance_id).display_name,
            attempt.result,
        )
        for attempt in _latest_successful_portable_attempts(execution)
    )
    _mark_issue_completed_from_steps(issue_path, step_results)


def _mark_issue_completed_from_steps(
    issue_path: Path,
    step_results: tuple[tuple[str, RoleResult], ...],
) -> None:
    results = [result for _, result in step_results]
    notes = [
        "",
        "## Implementation Notes",
        "",
        f"Completed: {now()}",
        "",
        "### Changed Files",
        *[
            f"- `{path}`"
            for path in _unique_strings(result.changed_files for result in results)
        ],
        "",
        "### Verification",
        *[
            f"- `{command}`"
            for command in sorted(
                _unique_strings(
                    result.verification_commands for result in results
                )
            )
        ],
        "",
        "### Workflow Step Results",
        "",
    ]
    for display_name, result in step_results:
        notes.extend(
            (
                f"#### {display_name}",
                "",
                result.summary or "PASS",
                "",
            )
        )
    _write_issue_completion(issue_path, notes)


def _write_issue_completion(issue_path: Path, notes: list[str]) -> None:
    text = issue_path.read_text(encoding="utf-8")
    text = re.sub(r"(?im)^Completed:\s*\[\s*\]", "Completed: [x]", text, count=1)
    text = mark_acceptance_criteria(text)

    if "## Implementation Notes" not in text:
        text = text.rstrip() + "\n" + "\n".join(notes)

    write_text_creating_parent(issue_path, text)


def _latest_successful_portable_attempts(
    execution: PortableWorkflowRunResult,
) -> tuple[StepAttemptRecord, ...]:
    step_order: list[StepInstanceId] = []
    latest_by_step: dict[StepInstanceId, StepAttemptRecord] = {}
    for attempt in execution.attempts:
        if attempt.outcome is not StepOutcome.SUCCEEDED:
            continue
        if attempt.step_instance_id not in latest_by_step:
            step_order.append(attempt.step_instance_id)
        latest_by_step[attempt.step_instance_id] = attempt
    return tuple(latest_by_step[step_id] for step_id in step_order)


def _unique_strings(value_groups: Iterable[Iterable[str]]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for value_group in value_groups:
        for value in value_group:
            if value not in seen:
                seen.add(value)
                values.append(value)
    return values


def mark_acceptance_criteria(text: str) -> str:
    match = re.search(r"(?ims)^## Acceptance criteria\s*(?P<body>.*?)(?=^## |\Z)", text)
    if not match:
        return text

    body = match.group("body")
    updated_body = re.sub(r"(?m)^(\s*-\s*)\[\s*\]", r"\1[x]", body)
    return text[: match.start("body")] + updated_body + text[match.end("body") :]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
