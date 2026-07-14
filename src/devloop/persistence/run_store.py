from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import cast

from devloop.analysis.package import analysis_draft_to_dict, parse_analysis_draft
from devloop.domain.capabilities import ResolvedCapabilityProfile
from devloop.domain.development import (
    ArtifactRef,
    ChangeKind,
    ContextManifestRef,
    DevelopmentCursor,
    IssueRuntimeState,
    IssueStatus,
    PlanningPackageRef,
    WorkspaceBaselineEntry,
    WorkspaceKind,
    WorkspaceRef,
)
from devloop.domain.doctor import redact_sensitive_text
from devloop.domain.finalization import FinalizationCursor, WorkspaceDisposition
from devloop.domain.identifiers import (
    AttemptId,
    CapabilityId,
    ExecutionThreadId,
    ExecutionTurnId,
    FeatureSlug,
    IssueId,
    StepComponentId,
    StepInstanceId,
    WorkflowId,
    WorkflowRunId,
)
from devloop.domain.planning import (
    ANALYSIS_ACCEPTANCE_TEXT_MAX_LENGTH,
    ANALYSIS_FEATURE_TITLE_MAX_LENGTH,
    ANALYSIS_ISSUE_MARKDOWN_MAX_LENGTH,
    ANALYSIS_ISSUE_TITLE_MAX_LENGTH,
    ANALYSIS_PRD_MARKDOWN_MAX_LENGTH,
    AnalysisDraft,
)
from devloop.domain.review_qa import QaCursor, ReviewCursor
from devloop.domain.run import (
    AnalysisCursor,
    ComponentLock,
    OperationState,
    OperationStatus,
    ResolvedWorkflow,
    RunEventType,
    RunLease,
    StepOutcome,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.domain.scheduler import (
    AttemptStatus,
    IssueAttemptRecord,
    validate_attempt_history,
    validate_attempt_record,
)
from devloop.infrastructure.paths import (
    ANALYSIS_DRAFT_FILENAME,
    RUN_EVENTS_FILENAME,
    RUN_LEASE_FILENAME,
    RUN_QUARANTINE_DIRECTORY,
    RUN_SNAPSHOT_FILENAME,
)
from devloop.infrastructure.windows_acl import (
    WindowsAclError,
    protect_current_windows_user_path,
)

RUN_SNAPSHOT_SCHEMA = "devloop.run-snapshot/v1"
RUN_EVENT_SCHEMA = "devloop.run-event/v1"
RUN_LEASE_SCHEMA = "devloop.run-lease/v1"
RECOVERY_DIAGNOSTIC_SCHEMA = "devloop.recovery-diagnostic/v1"
QUARANTINED_EVENT_SCHEMA = "devloop.quarantined-event/v1"
MAX_PERSISTED_TEXT_LENGTH = 20_000
MAX_PERSISTED_COLLECTION_ITEMS = 1_000
_WINDOWS_ACL_LOCK = threading.Lock()
_PROTECTED_WINDOWS_DIRECTORIES: dict[Path, tuple[int, int, int]] = {}
_DISALLOWED_PERSISTED_FIELDS = frozenset(
    {
        "auth_data",
        "authentication_data",
        "binary",
        "binary_output",
        "connection_string",
        "credentials",
        "environment",
        "environment_dump",
        "full_transcript",
        "hidden_reasoning",
        "model_reasoning",
        "raw_output",
        "reasoning",
        "transcript",
    }
)
_ALLOWED_PERSISTED_FIELDS = frozenset(
    {
        "acceptance_condition",
        "acceptance_criteria",
        "action",
        "assumptions",
        "attempt",
        "attempt_id",
        "authorization",
        "base_commit",
        "base_state",
        "blocked_reason",
        "branch",
        "capability_profile",
        "changed_files",
        "checks",
        "command",
        "commands",
        "completed_issues",
        "criteria",
        "criterion_id",
        "decision",
        "diff_hash",
        "disposition",
        "duration_ms",
        "evidence",
        "exit_code",
        "expected_behavior",
        "file_path",
        "findings",
        "from_attempt",
        "id",
        "implementation",
        "implementation_diff_hash",
        "instructions",
        "issue",
        "issue_id",
        "item_id",
        "items",
        "kind",
        "line",
        "markdown",
        "method",
        "outcome",
        "path",
        "position",
        "prd_sections",
        "prohibited_operations",
        "rationale",
        "reason",
        "relevant_diff",
        "repository_constraints",
        "repository_state",
        "repository_state_hash",
        "request_id",
        "requirement",
        "requirements",
        "residual_risks",
        "result_state",
        "review",
        "rework_request",
        "rework_resolutions",
        "risks",
        "run_id",
        "schema",
        "severity",
        "source",
        "source_state_changed",
        "state_change_evidence",
        "status",
        "step",
        "step_id",
        "summary",
        "supported_decisions",
        "target",
        "thread_id",
        "title",
        "total",
        "turn_id",
        "verification_evidence",
        "workspace",
        "workspace_disposition",
        "workspace_path",
    }
)


class RunStoreError(RuntimeError):
    pass


class RunLeaseError(RunStoreError):
    pass


class RunStore:
    def __init__(self, run_root: Path) -> None:
        self.run_root = run_root
        self._repository_root = run_root.parent.parent
        self._owned_leases: dict[WorkflowRunId, str] = {}

    def create(self, snapshot: WorkflowRunSnapshot) -> WorkflowRunSnapshot:
        run_directory = self.run_directory(snapshot.run_id)
        self._ensure_ignored_storage()
        try:
            run_directory.mkdir(parents=True, exist_ok=False)
            _protect_private_path(run_directory, directory=True)
        except FileExistsError:
            raise RunStoreError(f"Workflow Run already exists: {snapshot.run_id}.") from None
        self._write_lease(snapshot.run_id, snapshot.lease, exclusive=True)
        self._owned_leases[snapshot.run_id] = snapshot.lease.lease_id
        return self.record(snapshot, RunEventType.RUN_CREATED)

    def record(
        self,
        snapshot: WorkflowRunSnapshot,
        event_type: RunEventType,
    ) -> WorkflowRunSnapshot:
        if (
            snapshot.run_status is not WorkflowRunStatus.RUNNING
            and snapshot.operation.status is OperationStatus.RUNNING
        ):
            snapshot = replace(
                snapshot,
                operation=replace(snapshot.operation, status=OperationStatus.UNKNOWN),
            )
        with self._lease_guard(snapshot.run_id):
            self._validate_lease_unlocked(snapshot)
            snapshot_path = self.run_directory(snapshot.run_id) / RUN_SNAPSHOT_FILENAME
            if snapshot_path.exists():
                persisted = self.load(snapshot.run_id)
                if persisted.event_sequence != snapshot.event_sequence:
                    raise RunStoreError(
                        "Workflow Run state is stale; reload before appending an event."
                    )
                if persisted.terminal:
                    raise RunStoreError("A terminal Workflow Run is immutable.")
            next_snapshot = replace(
                snapshot,
                event_sequence=snapshot.event_sequence + 1,
                updated_at=_now(),
            )
            event = {
                "schema": RUN_EVENT_SCHEMA,
                "sequence": next_snapshot.event_sequence,
                "type": event_type.value,
                "occurred_at": next_snapshot.updated_at,
                "state": snapshot_to_dict(next_snapshot),
            }
            events_path = self.run_directory(snapshot.run_id) / RUN_EVENTS_FILENAME
            encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            try:
                with events_path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                _protect_private_path(events_path, directory=False)
                _atomic_json(
                    self.run_directory(snapshot.run_id) / RUN_SNAPSHOT_FILENAME,
                    snapshot_to_dict(next_snapshot),
                )
            except OSError as error:
                raise RunStoreError("Unable to persist Workflow Run state.") from error
        return next_snapshot

    def load(self, run_id: WorkflowRunId) -> WorkflowRunSnapshot:
        run_directory = self.run_directory(run_id)
        snapshot_path = run_directory / RUN_SNAPSHOT_FILENAME
        snapshot: WorkflowRunSnapshot | None = None
        snapshot_error: OSError | ValueError | json.JSONDecodeError | None = None
        if snapshot_path.exists():
            try:
                candidate = snapshot_from_dict(_read_object(snapshot_path))
                if candidate.run_id == run_id:
                    snapshot = candidate
            except (OSError, ValueError, json.JSONDecodeError) as error:
                snapshot_error = error
        events = self._read_valid_events(run_id)
        for event in events:
            state = event.get("state")
            sequence = event.get("sequence")
            if (
                not isinstance(state, dict)
                or isinstance(sequence, bool)
                or not isinstance(sequence, int)
            ):
                raise RunStoreError(f"Workflow Run event is invalid: {run_id}.")
            replayed = snapshot_from_dict(cast(dict[str, object], state))
            if replayed.event_sequence != sequence:
                raise RunStoreError(f"Workflow Run event sequence is invalid: {run_id}.")
            if snapshot is None or replayed.event_sequence > snapshot.event_sequence:
                snapshot = replayed
        if snapshot is None:
            if snapshot_error is not None:
                raise RunStoreError(
                    f"Workflow Run snapshot is invalid and no event can recover it: {run_id}."
                ) from snapshot_error
            raise RunStoreError(f"Workflow Run has no recoverable state: {run_id}.")
        return self._recover_stale_presentation(snapshot)

    def list_unfinished(self) -> tuple[WorkflowRunSnapshot, ...]:
        return tuple(snapshot for snapshot in self.list_runs() if not snapshot.terminal)

    def list_runs(self) -> tuple[WorkflowRunSnapshot, ...]:
        if not self.run_root.exists():
            return ()
        runs: list[WorkflowRunSnapshot] = []
        for path in sorted(self.run_root.iterdir()):
            if not path.is_dir():
                continue
            try:
                run_id = WorkflowRunId(path.name)
                snapshot = self.load(run_id)
            except (ValueError, RunStoreError):
                continue
            runs.append(snapshot)
        return tuple(sorted(runs, key=lambda item: item.updated_at, reverse=True))

    def save_draft(self, draft: AnalysisDraft) -> None:
        path = self.run_directory(draft.run_id) / ANALYSIS_DRAFT_FILENAME
        safe_draft = _sanitize_analysis_draft(draft)
        try:
            with self._lease_guard(draft.run_id):
                self._validate_owned_lease_unlocked(draft.run_id)
                _atomic_json(path, analysis_draft_to_dict(safe_draft))
        except OSError as error:
            raise RunStoreError("Unable to save the Analysis Draft.") from error

    def load_draft(self, run_id: WorkflowRunId) -> AnalysisDraft:
        path = self.run_directory(run_id) / ANALYSIS_DRAFT_FILENAME
        try:
            return parse_analysis_draft(_read_object(path), run_id)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RunStoreError("Analysis Draft is missing or invalid.") from error

    def save_json_artifact(
        self,
        run_id: WorkflowRunId,
        relative_path: Path,
        payload: Mapping[str, object],
    ) -> ArtifactRef:
        path = self._artifact_path(run_id, relative_path)
        safe_payload = _sanitize_persisted_value(payload)
        if not isinstance(safe_payload, dict):
            raise RunStoreError("Run Artifact must be a JSON object.")
        encoded = (
            json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
            + b"\n"
        )
        try:
            with self._lease_guard(run_id):
                self._validate_owned_lease_unlocked(run_id)
                _atomic_bytes(path, encoded)
        except OSError as error:
            raise RunStoreError("Unable to save Run Artifact.") from error
        return ArtifactRef(
            str(relative_path).replace("\\", "/"),
            hashlib.sha256(encoded).hexdigest(),
        )

    def load_json_artifact(
        self,
        run_id: WorkflowRunId,
        artifact: ArtifactRef | ContextManifestRef,
    ) -> dict[str, object]:
        relative_path = Path(artifact.path)
        path = self._artifact_path(run_id, relative_path)
        try:
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != artifact.content_hash:
                raise RunStoreError("Run Artifact hash does not match its checkpoint.")
            value = json.loads(content)
        except (OSError, json.JSONDecodeError) as error:
            raise RunStoreError("Run Artifact is missing or invalid.") from error
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise RunStoreError("Run Artifact must be a JSON object.")
        return cast(dict[str, object], value)

    def take_lease(self, snapshot: WorkflowRunSnapshot) -> WorkflowRunSnapshot:
        with self._lease_guard(snapshot.run_id):
            current_path = self.run_directory(snapshot.run_id) / RUN_LEASE_FILENAME
            if current_path.exists():
                current = _read_object(current_path)
                process_id = current.get("process_id")
                lease_id = current.get("lease_id")
                if (
                    isinstance(process_id, int)
                    and not isinstance(process_id, bool)
                    and process_id != os.getpid()
                    and _process_is_running(process_id)
                ):
                    raise RunLeaseError(
                        f"Workflow Run is already leased by another process ({lease_id})."
                    )
            lease = new_run_lease()
            self._write_lease(snapshot.run_id, lease, exclusive=False)
            self._owned_leases[snapshot.run_id] = lease.lease_id
        return replace(snapshot, lease=lease)

    def validate_lease(self, snapshot: WorkflowRunSnapshot) -> None:
        with self._lease_guard(snapshot.run_id):
            self._validate_lease_unlocked(snapshot)

    def _validate_lease_unlocked(self, snapshot: WorkflowRunSnapshot) -> None:
        lease = _read_object(self.run_directory(snapshot.run_id) / RUN_LEASE_FILENAME)
        if (
            lease.get("schema") != RUN_LEASE_SCHEMA
            or lease.get("lease_id") != snapshot.lease.lease_id
            or lease.get("process_id") != os.getpid()
        ):
            raise RunLeaseError("Workflow Run lease changed; reload before continuing.")
        self._owned_leases[snapshot.run_id] = snapshot.lease.lease_id

    def _validate_owned_lease_unlocked(self, run_id: WorkflowRunId) -> None:
        lease = _read_object(self.run_directory(run_id) / RUN_LEASE_FILENAME)
        expected = self._owned_leases.get(run_id)
        lease_id = lease.get("lease_id")
        if (
            expected is None
            and lease.get("schema") == RUN_LEASE_SCHEMA
            and lease.get("process_id") == os.getpid()
            and isinstance(lease_id, str)
            and lease_id
        ):
            expected = lease_id
            self._owned_leases[run_id] = lease_id
        if expected is None:
            raise RunLeaseError("Workflow Run lease ownership is not established.")
        if (
            lease.get("schema") != RUN_LEASE_SCHEMA
            or lease_id != expected
            or lease.get("process_id") != os.getpid()
        ):
            raise RunLeaseError("Workflow Run lease changed; reload before continuing.")

    def release_lease(self, snapshot: WorkflowRunSnapshot) -> None:
        path = self.run_directory(snapshot.run_id) / RUN_LEASE_FILENAME
        with self._lease_guard(snapshot.run_id):
            try:
                self._validate_lease_unlocked(snapshot)
                path.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError, ValueError) as error:
                raise RunLeaseError("Unable to release the Workflow Run lease.") from error
            finally:
                self._owned_leases.pop(snapshot.run_id, None)

    def run_directory(self, run_id: WorkflowRunId) -> Path:
        repository = self._repository_root.resolve(strict=False)
        run_root = self.run_root.resolve(strict=False)
        candidate = self.run_root / run_id.value
        resolved = candidate.resolve(strict=False)
        if (
            not run_root.is_relative_to(repository)
            or not resolved.is_relative_to(run_root)
            or not resolved.is_relative_to(repository)
        ):
            raise RunStoreError(
                "Run Directory path resolves outside the repository storage boundary."
            )
        return candidate

    def _artifact_path(self, run_id: WorkflowRunId, relative_path: Path) -> Path:
        windows_path = PureWindowsPath(str(relative_path))
        if (
            not relative_path.parts
            or relative_path.anchor
            or relative_path.drive
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or windows_path.anchor
            or windows_path.drive
            or windows_path.is_absolute()
            or ".." in windows_path.parts
        ):
            raise RunStoreError("Run Artifact path must be an unanchored relative path.")
        run_directory = self.run_directory(run_id)
        repository = self._repository_root.resolve(strict=False)
        resolved_run_directory = run_directory.resolve(strict=False)
        resolved = (run_directory / relative_path).resolve(strict=False)
        if (
            not resolved_run_directory.is_relative_to(repository)
            or not resolved.is_relative_to(resolved_run_directory)
            or not resolved.is_relative_to(repository)
        ):
            raise RunStoreError(
                "Run Artifact path resolves outside the Run Directory or repository."
            )
        return run_directory / relative_path

    @contextmanager
    def _lease_guard(self, run_id: WorkflowRunId) -> Iterator[None]:
        lock_path = self.run_directory(run_id) / ".lease.lock"
        with _exclusive_file_lock(lock_path):
            yield

    def _read_valid_events(self, run_id: WorkflowRunId) -> tuple[dict[str, object], ...]:
        path = self.run_directory(run_id) / RUN_EVENTS_FILENAME
        if not path.exists():
            return ()
        try:
            lines = path.read_bytes().splitlines(keepends=True)
        except OSError as error:
            raise RunStoreError("Unable to read Workflow Run events.") from error
        events: list[dict[str, object]] = []
        valid_bytes = bytearray()
        for index, raw_line in enumerate(lines):
            try:
                decoded = json.loads(raw_line)
                if not isinstance(decoded, dict) or not _is_complete_event(
                    decoded,
                    run_id,
                    expected_sequence=len(events) + 1,
                ):
                    raise ValueError("invalid event")
                event = cast(dict[str, object], decoded)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                if index != len(lines) - 1:
                    raise RunStoreError(
                        "Workflow Run contains a corrupt non-final event."
                    ) from None
                self._quarantine_final_event(run_id, raw_line, bytes(valid_bytes))
                break
            events.append(event)
            valid_bytes.extend(raw_line)
        return tuple(events)

    def _quarantine_final_event(
        self,
        run_id: WorkflowRunId,
        invalid_line: bytes,
        valid_content: bytes,
    ) -> None:
        run_directory = self.run_directory(run_id)
        quarantine = run_directory / RUN_QUARANTINE_DIRECTORY
        quarantine.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        record_name = f"rejected-event-{stamp}.json"
        _atomic_json(
            quarantine / record_name,
            {
                "schema": QUARANTINED_EVENT_SCHEMA,
                "sha256": hashlib.sha256(invalid_line).hexdigest(),
                "byte_count": len(invalid_line),
            },
        )
        _atomic_json(
            quarantine / f"diagnostic-{stamp}.json",
            {
                "schema": RECOVERY_DIAGNOSTIC_SCHEMA,
                "code": "CORRUPT_FINAL_EVENT_QUARANTINED",
                "message": "The final Workflow Run event was corrupt or truncated.",
                "action": (
                    "Inspect the quarantined record before retrying; earlier valid state "
                    "remains usable."
                ),
                "quarantined_record": record_name,
            },
        )
        _atomic_bytes(run_directory / RUN_EVENTS_FILENAME, valid_content)

    def _write_lease(
        self,
        run_id: WorkflowRunId,
        lease: RunLease,
        *,
        exclusive: bool,
    ) -> None:
        path = self.run_directory(run_id) / RUN_LEASE_FILENAME
        payload = {
            "schema": RUN_LEASE_SCHEMA,
            "lease_id": lease.lease_id,
            "process_id": lease.process_id,
            "acquired_at": lease.acquired_at,
        }
        if not exclusive:
            _atomic_json(path, payload)
            return
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise RunLeaseError(f"Workflow Run is already leased: {run_id}.") from None

    def _recover_stale_presentation(
        self,
        snapshot: WorkflowRunSnapshot,
    ) -> WorkflowRunSnapshot:
        if snapshot.run_status is not WorkflowRunStatus.RUNNING:
            if snapshot.operation.status is OperationStatus.RUNNING:
                raise RunStoreError(
                    "A non-running Workflow Run contains a RUNNING operation."
                )
            return snapshot
        lease_path = self.run_directory(snapshot.run_id) / RUN_LEASE_FILENAME
        try:
            lease = _read_object(lease_path)
            process_id = lease.get("process_id")
            lease_id = lease.get("lease_id")
        except (OSError, ValueError, json.JSONDecodeError):
            process_id = None
            lease_id = None
        lease_is_live = (
            lease_id == snapshot.lease.lease_id
            and isinstance(process_id, int)
            and not isinstance(process_id, bool)
            and _process_is_running(process_id)
        )
        if lease_is_live:
            return snapshot
        operation = snapshot.operation
        if operation.status is OperationStatus.RUNNING:
            operation = replace(operation, status=OperationStatus.UNKNOWN)
        return replace(
            snapshot,
            run_status=WorkflowRunStatus.PAUSED,
            operation=operation,
        )

    def _ensure_ignored_storage(self) -> None:
        self.run_root.mkdir(parents=True, exist_ok=True)
        _protect_private_path(self.run_root, directory=True)
        ignore_file = self.run_root / ".gitignore"
        if ignore_file.exists():
            try:
                existing = ignore_file.read_text(encoding="ascii")
            except (OSError, UnicodeError) as error:
                raise RunStoreError("Project run storage ignore file is unreadable.") from error
            if "*" in {line.strip() for line in existing.splitlines()}:
                return
            suffix = "" if not existing or existing.endswith("\n") else "\n"
            _atomic_bytes(ignore_file, (existing + suffix + "*\n").encode("ascii"))
            return
        try:
            with ignore_file.open("x", encoding="ascii", newline="\n") as stream:
                stream.write("*\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass


def _is_complete_event(
    value: Mapping[str, object],
    run_id: WorkflowRunId,
    *,
    expected_sequence: int,
) -> bool:
    sequence = value.get("sequence")
    event_type = value.get("type")
    occurred_at = value.get("occurred_at")
    state = value.get("state")
    if (
        value.get("schema") != RUN_EVENT_SCHEMA
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence != expected_sequence
        or not isinstance(event_type, str)
        or not isinstance(occurred_at, str)
        or not occurred_at
        or not isinstance(state, dict)
        or state.get("run_id") != run_id.value
    ):
        return False
    try:
        RunEventType(event_type)
    except ValueError:
        return False
    try:
        replayed = snapshot_from_dict(cast(dict[str, object], state))
    except ValueError:
        return False
    return replayed.run_id == run_id and replayed.event_sequence == sequence


def new_run_lease() -> RunLease:
    return RunLease(uuid.uuid4().hex, os.getpid(), _now())


def snapshot_to_dict(snapshot: WorkflowRunSnapshot) -> dict[str, object]:
    validate_attempt_history(snapshot.attempts)
    return {
        "schema": snapshot.schema,
        "run_id": snapshot.run_id.value,
        "repository": snapshot.repository,
        "feature_title": _sanitize_persisted_text(snapshot.feature_title),
        "feature_slug": snapshot.feature_slug.value,
        "workflow": {
            "id": snapshot.workflow.workflow_id.value,
            "version": snapshot.workflow.version,
            "definition_hash": snapshot.workflow.definition_hash,
        },
        "component_locks": [
            {
                "id": item.component_id.value,
                "version": item.version,
                "distribution": item.distribution,
                "package_hash": item.package_hash,
            }
            for item in snapshot.component_locks
        ],
        "capability_profiles": [
            {
                "component_id": item.component_id.value,
                "capabilities": [capability.value for capability in item.capabilities],
            }
            for item in snapshot.capability_profiles
        ],
        "active_step": snapshot.active_step.value,
        "run_status": snapshot.run_status.value,
        "step_status": snapshot.step_status.value,
        "outcome": snapshot.outcome.value if snapshot.outcome else None,
        "analysis": {
            "thread_id": snapshot.analysis.thread_id.value if snapshot.analysis.thread_id else None,
            "turn_id": snapshot.analysis.turn_id.value if snapshot.analysis.turn_id else None,
            "draft_revision": snapshot.analysis.draft_revision,
            "clarification": None
            if snapshot.analysis.clarification is None
            else _sanitize_persisted_text(snapshot.analysis.clarification),
            "completed_item_ids": list(snapshot.analysis.completed_item_ids),
        },
        "lease": {
            "lease_id": snapshot.lease.lease_id,
            "process_id": snapshot.lease.process_id,
            "acquired_at": snapshot.lease.acquired_at,
        },
        "event_sequence": snapshot.event_sequence,
        "updated_at": snapshot.updated_at,
        "planning_package": _planning_package_to_dict(snapshot.planning_package),
        "workspace": _workspace_to_dict(snapshot.workspace),
        "issues": [
            {
                "id": item.issue_id.value,
                "status": item.status.value,
                "current_step": item.current_step.value if item.current_step is not None else None,
                "repository_baseline": None
                if item.repository_baseline is None
                else _baseline_to_list(item.repository_baseline),
                "owned_paths": list(item.owned_paths),
            }
            for item in snapshot.issues
        ],
        "development": _development_to_dict(snapshot.development),
        "review": _review_to_dict(snapshot.review),
        "qa": _qa_to_dict(snapshot.qa),
        "finalization": _finalization_to_dict(snapshot.finalization),
        "attempts": [_attempt_to_dict(item) for item in snapshot.attempts],
        "operation": {
            "item_id": snapshot.operation.item_id,
            "status": snapshot.operation.status.value,
        },
        "workspace_state_hash": snapshot.workspace_state_hash,
    }


def snapshot_from_dict(data: Mapping[str, object]) -> WorkflowRunSnapshot:
    if data.get("schema") != RUN_SNAPSHOT_SCHEMA:
        raise ValueError("Unsupported Workflow Run snapshot schema.")
    workflow = _object(data, "workflow")
    analysis = _object(data, "analysis")
    lease = _object(data, "lease")
    locks_value = data.get("component_locks")
    profiles_value = data.get("capability_profiles", [])
    if not isinstance(locks_value, list):
        raise ValueError("Workflow Run component locks are invalid.")
    locks: list[ComponentLock] = []
    for value in locks_value:
        if not isinstance(value, dict):
            raise ValueError("Workflow Run component lock is invalid.")
        item = cast(dict[str, object], value)
        locks.append(
            ComponentLock(
                StepComponentId(_string(item, "id")),
                _string(item, "version"),
                _string(item, "distribution"),
                _string(item, "package_hash"),
            )
        )
    if not isinstance(profiles_value, list):
        raise ValueError("Workflow Run capability profiles are invalid.")
    profiles: list[ResolvedCapabilityProfile] = []
    for value in profiles_value:
        if not isinstance(value, dict):
            raise ValueError("Workflow Run capability profile is invalid.")
        item = cast(dict[str, object], value)
        capabilities_value = item.get("capabilities")
        if not isinstance(capabilities_value, list) or not all(
            isinstance(capability, str) for capability in capabilities_value
        ):
            raise ValueError("Workflow Run capability profile is invalid.")
        profiles.append(
            ResolvedCapabilityProfile(
                StepComponentId(_string(item, "component_id")),
                tuple(CapabilityId(capability) for capability in capabilities_value),
            )
        )
    if len({profile.component_id for profile in profiles}) != len(profiles):
        raise ValueError("Workflow Run capability profiles must be unique.")
    outcome_value = data.get("outcome")
    thread_value = analysis.get("thread_id")
    turn_value = analysis.get("turn_id")
    draft_revision = analysis.get("draft_revision")
    event_sequence = data.get("event_sequence")
    process_id = lease.get("process_id")
    planning_value = data.get("planning_package")
    workspace_value = data.get("workspace")
    development_value = data.get("development")
    review_value = data.get("review")
    qa_value = data.get("qa")
    finalization_value = data.get("finalization")
    attempts_value = data.get("attempts", [])
    operation_value = data.get("operation")
    workspace_state_hash = data.get("workspace_state_hash")
    issues_value = data.get("issues", [])
    if (
        isinstance(draft_revision, bool)
        or not isinstance(draft_revision, int)
        or isinstance(event_sequence, bool)
        or not isinstance(event_sequence, int)
        or isinstance(process_id, bool)
        or not isinstance(process_id, int)
    ):
        raise ValueError("Workflow Run numeric fields are invalid.")
    clarification = analysis.get("clarification")
    completed_items = analysis.get("completed_item_ids", [])
    if clarification is not None and not isinstance(clarification, str):
        raise ValueError("Workflow Run clarification is invalid.")
    if not isinstance(completed_items, list) or not all(
        isinstance(item, str) for item in completed_items
    ):
        raise ValueError("Workflow Run completed protocol items are invalid.")
    if not isinstance(issues_value, list):
        raise ValueError("Workflow Run Issue states are invalid.")
    issue_states: list[IssueRuntimeState] = []
    for issue_value in issues_value:
        if not isinstance(issue_value, dict):
            raise ValueError("Workflow Run Issue state is invalid.")
        issue = cast(dict[str, object], issue_value)
        current_step = issue.get("current_step")
        baseline_value = issue.get("repository_baseline")
        owned_paths = issue.get("owned_paths", [])
        if not isinstance(owned_paths, list) or not all(
            isinstance(item, str) for item in owned_paths
        ):
            raise ValueError("Workflow Run Issue owned paths are invalid.")
        issue_states.append(
            IssueRuntimeState(
                IssueId(_string(issue, "id")),
                IssueStatus(_string(issue, "status")),
                None
                if current_step is None
                else StepInstanceId(_typed_string(current_step)),
                None
                if baseline_value is None
                else _baseline_from_value(baseline_value, "Issue repository"),
                tuple(cast(list[str], owned_paths)),
            )
        )
    if operation_value is None:
        operation = OperationState()
    elif isinstance(operation_value, dict):
        operation_data = cast(dict[str, object], operation_value)
        item_id = operation_data.get("item_id")
        if item_id is not None and not isinstance(item_id, str):
            raise ValueError("Workflow Run operation item ID is invalid.")
        operation = OperationState(
            item_id,
            OperationStatus(_string(operation_data, "status")),
        )
    else:
        raise ValueError("Workflow Run operation is invalid.")
    if workspace_state_hash is not None and (
        not isinstance(workspace_state_hash, str) or not workspace_state_hash
    ):
        raise ValueError("Workflow Run workspace state hash is invalid.")
    return WorkflowRunSnapshot(
        schema=RUN_SNAPSHOT_SCHEMA,
        run_id=WorkflowRunId(_string(data, "run_id")),
        repository=_string(data, "repository"),
        feature_title=_string(data, "feature_title"),
        feature_slug=FeatureSlug(_string(data, "feature_slug")),
        workflow=ResolvedWorkflow(
            WorkflowId(_string(workflow, "id")),
            _string(workflow, "version"),
            _string(workflow, "definition_hash"),
        ),
        component_locks=tuple(locks),
        active_step=StepInstanceId(_string(data, "active_step")),
        run_status=WorkflowRunStatus(_string(data, "run_status")),
        step_status=StepRunStatus(_string(data, "step_status")),
        outcome=None if outcome_value is None else StepOutcome(_typed_string(outcome_value)),
        analysis=AnalysisCursor(
            None if thread_value is None else ExecutionThreadId(_typed_string(thread_value)),
            None if turn_value is None else ExecutionTurnId(_typed_string(turn_value)),
            draft_revision,
            clarification,
            tuple(cast(list[str], completed_items)),
        ),
        lease=RunLease(
            _string(lease, "lease_id"),
            process_id,
            _string(lease, "acquired_at"),
        ),
        event_sequence=event_sequence,
        updated_at=_string(data, "updated_at"),
        planning_package=_planning_package_from_value(planning_value),
        workspace=_workspace_from_value(workspace_value),
        issues=tuple(issue_states),
        development=_development_from_value(development_value),
        review=_review_from_value(review_value),
        qa=_qa_from_value(qa_value),
        finalization=_finalization_from_value(finalization_value),
        attempts=_attempts_from_value(attempts_value),
        operation=operation,
        workspace_state_hash=workspace_state_hash,
        capability_profiles=tuple(profiles),
    )


def _planning_package_to_dict(value: PlanningPackageRef | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"root": value.root, "prd_hash": value.prd_hash, "issue_set_hash": value.issue_set_hash}


def _workspace_to_dict(value: WorkspaceRef | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "kind": value.kind.value,
        "repository_root": value.repository_root,
        "path": value.path,
        "branch": value.branch,
        "base_commit": value.base_commit,
        "baseline": _baseline_to_list(value.baseline),
    }


def _development_to_dict(value: DevelopmentCursor | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "issue_id": value.issue_id.value,
        "position": value.position,
        "total": value.total,
        "attempt_id": value.attempt_id.value,
        "context_manifest": {
            "path": value.context_manifest.path,
            "content_hash": value.context_manifest.content_hash,
        },
        "thread_id": value.thread_id.value if value.thread_id else None,
        "turn_id": value.turn_id.value if value.turn_id else None,
        "completed_item_ids": list(value.completed_item_ids),
        "implementation_result": None
        if value.implementation_result is None
        else {
            "path": value.implementation_result.path,
            "content_hash": value.implementation_result.content_hash,
        },
        "approval_request": _artifact_to_dict(value.approval_request),
        "transient_retries": value.transient_retries,
    }


def _review_to_dict(value: ReviewCursor | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "issue_id": value.issue_id.value,
        "attempt_id": value.attempt_id.value,
        "input_manifest": _artifact_to_dict(value.input_manifest),
        "thread_id": value.thread_id.value if value.thread_id else None,
        "turn_id": value.turn_id.value if value.turn_id else None,
        "completed_item_ids": list(value.completed_item_ids),
        "review_result": _artifact_to_dict(value.review_result),
        "rework_request": _artifact_to_dict(value.rework_request),
        "transient_retries": value.transient_retries,
    }


def _qa_to_dict(value: QaCursor | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "issue_id": value.issue_id.value,
        "attempt_id": value.attempt_id.value,
        "input_manifest": _artifact_to_dict(value.input_manifest),
        "thread_id": value.thread_id.value if value.thread_id else None,
        "turn_id": value.turn_id.value if value.turn_id else None,
        "completed_item_ids": list(value.completed_item_ids),
        "qa_result": _artifact_to_dict(value.qa_result),
        "rework_request": _artifact_to_dict(value.rework_request),
        "transient_retries": value.transient_retries,
    }


def _finalization_to_dict(value: FinalizationCursor | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "handoff_summary": _artifact_to_dict(value.handoff_summary),
        "workspace_disposition": value.workspace_disposition.value,
    }


def _artifact_to_dict(value: ArtifactRef | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"path": value.path, "content_hash": value.content_hash}


def _attempt_to_dict(value: IssueAttemptRecord) -> dict[str, object]:
    validate_attempt_record(value)
    return {
        "issue_id": value.issue_id.value,
        "attempt_number": value.attempt_number,
        "status": value.status.value,
        "outcome": value.outcome.value,
        "implementation": _artifact_to_dict(value.implementation),
        "review": _artifact_to_dict(value.review),
        "qa_result": _artifact_to_dict(value.qa_result),
        "rework_request": _artifact_to_dict(value.rework_request),
        "development_thread": (
            value.development_thread.value if value.development_thread is not None else None
        ),
        "review_thread": value.review_thread.value if value.review_thread is not None else None,
        "qa_thread": value.qa_thread.value if value.qa_thread is not None else None,
    }


def _planning_package_from_value(value: object) -> PlanningPackageRef | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Workflow Run planning package is invalid.")
    data = cast(dict[str, object], value)
    return PlanningPackageRef(
        _string(data, "root"),
        _string(data, "prd_hash"),
        _string(data, "issue_set_hash"),
    )


def _workspace_from_value(value: object) -> WorkspaceRef | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Workflow Run Workspace Ref is invalid.")
    data = cast(dict[str, object], value)
    branch = data.get("branch")
    baseline_value = data.get("baseline", [])
    if branch is not None and not isinstance(branch, str):
        raise ValueError("Workflow Run workspace branch is invalid.")
    baseline = _baseline_from_value(baseline_value, "workspace")
    return WorkspaceRef(
        WorkspaceKind(_string(data, "kind")),
        _string(data, "repository_root"),
        _string(data, "path"),
        branch,
        _string(data, "base_commit"),
        baseline,
    )


def _baseline_to_list(
    value: tuple[WorkspaceBaselineEntry, ...],
) -> list[dict[str, object]]:
    return [
        {
            "path": item.path,
            "kind": item.kind.value,
            "content_hash": item.content_hash,
        }
        for item in value
    ]


def _baseline_from_value(value: object, label: str) -> tuple[WorkspaceBaselineEntry, ...]:
    if not isinstance(value, list):
        raise ValueError(f"Workflow Run {label} baseline is invalid.")
    baseline: list[WorkspaceBaselineEntry] = []
    for entry_value in value:
        if not isinstance(entry_value, dict):
            raise ValueError(f"Workflow Run {label} baseline entry is invalid.")
        entry = cast(dict[str, object], entry_value)
        content_hash = entry.get("content_hash")
        if content_hash is not None and not isinstance(content_hash, str):
            raise ValueError(f"Workflow Run {label} baseline hash is invalid.")
        baseline.append(
            WorkspaceBaselineEntry(
                _string(entry, "path"),
                ChangeKind(_string(entry, "kind")),
                content_hash,
            )
        )
    return tuple(baseline)


def _development_from_value(value: object) -> DevelopmentCursor | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Workflow Run development cursor is invalid.")
    data = cast(dict[str, object], value)
    context = _object(data, "context_manifest")
    result_value = data.get("implementation_result")
    approval_value = data.get("approval_request")
    thread_value = data.get("thread_id")
    turn_value = data.get("turn_id")
    completed = data.get("completed_item_ids", [])
    position = data.get("position")
    total = data.get("total")
    if (
        isinstance(position, bool)
        or not isinstance(position, int)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or not isinstance(completed, list)
        or not all(isinstance(item, str) for item in completed)
    ):
        raise ValueError("Workflow Run development cursor fields are invalid.")
    result: ArtifactRef | None = None
    if result_value is not None:
        if not isinstance(result_value, dict):
            raise ValueError("Workflow Run implementation result is invalid.")
        result_data = cast(dict[str, object], result_value)
        result = ArtifactRef(
            _string(result_data, "path"),
            _string(result_data, "content_hash"),
        )
    return DevelopmentCursor(
        issue_id=IssueId(_string(data, "issue_id")),
        position=position,
        total=total,
        attempt_id=AttemptId(_string(data, "attempt_id")),
        context_manifest=ContextManifestRef(
            _string(context, "path"),
            _string(context, "content_hash"),
        ),
        thread_id=None if thread_value is None else ExecutionThreadId(_typed_string(thread_value)),
        turn_id=None if turn_value is None else ExecutionTurnId(_typed_string(turn_value)),
        completed_item_ids=tuple(cast(list[str], completed)),
        implementation_result=result,
        approval_request=_artifact_from_value(
            approval_value,
            "development approval request",
        ),
        transient_retries=_nonnegative_int(data, "transient_retries"),
    )


def _review_from_value(value: object) -> ReviewCursor | None:
    data = _optional_phase_value(value, "review")
    if data is None:
        return None
    input_manifest = _artifact_from_value(
        data.get("input_manifest"), "review input manifest", required=True
    )
    if input_manifest is None:
        raise ValueError("Workflow Run review input manifest is missing.")
    return ReviewCursor(
        IssueId(_string(data, "issue_id")),
        AttemptId(_string(data, "attempt_id")),
        input_manifest,
        _optional_thread_id(data.get("thread_id")),
        _optional_turn_id(data.get("turn_id")),
        _completed_items(data.get("completed_item_ids"), "review"),
        _artifact_from_value(data.get("review_result"), "review result"),
        _artifact_from_value(data.get("rework_request"), "review rework request"),
        _nonnegative_int(data, "transient_retries"),
    )


def _qa_from_value(value: object) -> QaCursor | None:
    data = _optional_phase_value(value, "QA")
    if data is None:
        return None
    input_manifest = _artifact_from_value(
        data.get("input_manifest"), "QA input manifest", required=True
    )
    if input_manifest is None:
        raise ValueError("Workflow Run QA input manifest is missing.")
    return QaCursor(
        IssueId(_string(data, "issue_id")),
        AttemptId(_string(data, "attempt_id")),
        input_manifest,
        _optional_thread_id(data.get("thread_id")),
        _optional_turn_id(data.get("turn_id")),
        _completed_items(data.get("completed_item_ids"), "QA"),
        _artifact_from_value(data.get("qa_result"), "QA result"),
        _artifact_from_value(data.get("rework_request"), "QA rework request"),
        _nonnegative_int(data, "transient_retries"),
    )


def _finalization_from_value(value: object) -> FinalizationCursor | None:
    data = _optional_phase_value(value, "Finalization")
    if data is None:
        return None
    handoff = _artifact_from_value(data.get("handoff_summary"), "Handoff Summary")
    if handoff is None:
        raise ValueError("Finalization Handoff Summary is missing.")
    return FinalizationCursor(
        handoff,
        WorkspaceDisposition(_string(data, "workspace_disposition")),
    )


def _attempts_from_value(value: object) -> tuple[IssueAttemptRecord, ...]:
    if not isinstance(value, list):
        raise ValueError("Workflow Run attempt history is invalid.")
    attempts: list[IssueAttemptRecord] = []
    for item_value in value:
        if not isinstance(item_value, dict):
            raise ValueError("Workflow Run attempt record is invalid.")
        item = cast(dict[str, object], item_value)
        number = item.get("attempt_number")
        if isinstance(number, bool) or not isinstance(number, int):
            raise ValueError("Workflow Run attempt number is invalid.")
        record = IssueAttemptRecord(
            IssueId(_string(item, "issue_id")),
            number,
            AttemptStatus(_string(item, "status")),
            StepOutcome(_string(item, "outcome")),
            _artifact_from_value(item.get("implementation"), "attempt implementation"),
            _artifact_from_value(item.get("review"), "attempt review"),
            _artifact_from_value(item.get("qa_result"), "attempt QA result"),
            _artifact_from_value(item.get("rework_request"), "attempt rework request"),
            _optional_thread_id(item.get("development_thread")),
            _optional_thread_id(item.get("review_thread")),
            _optional_thread_id(item.get("qa_thread")),
        )
        validate_attempt_record(record)
        attempts.append(record)
    result = tuple(attempts)
    validate_attempt_history(result)
    return result


def _optional_phase_value(value: object, name: str) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"Workflow Run {name} cursor is invalid.")
    return cast(dict[str, object], value)


def _artifact_from_value(
    value: object,
    name: str,
    *,
    required: bool = False,
) -> ArtifactRef | None:
    if value is None:
        if required:
            raise ValueError(f"Workflow Run {name} is missing.")
        return None
    if not isinstance(value, dict):
        raise ValueError(f"Workflow Run {name} is invalid.")
    data = cast(dict[str, object], value)
    return ArtifactRef(_string(data, "path"), _string(data, "content_hash"))


def _optional_thread_id(value: object) -> ExecutionThreadId | None:
    return None if value is None else ExecutionThreadId(_typed_string(value))


def _optional_turn_id(value: object) -> ExecutionTurnId | None:
    return None if value is None else ExecutionTurnId(_typed_string(value))


def _completed_items(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Workflow Run {name} completed items are invalid.")
    return tuple(cast(list[str], value))


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )
    _atomic_bytes(path, encoded)


def _sanitize_persisted_value(value: object, *, depth: int = 0) -> object:
    if depth > 20:
        raise RunStoreError("Run Artifact nesting exceeds the persistence boundary.")
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RunStoreError("Run Artifact fields must be strings.")
            normalized = key.casefold().replace("-", "_")
            if normalized in _DISALLOWED_PERSISTED_FIELDS:
                raise RunStoreError(f"Run Artifact contains a disallowed field: {key}.")
            if key not in _ALLOWED_PERSISTED_FIELDS:
                raise RunStoreError(
                    f"Run Artifact field is outside the approved persistence allowlist: {key}."
                )
            result[key] = _sanitize_persisted_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_PERSISTED_COLLECTION_ITEMS:
            raise RunStoreError("Run Artifact collection exceeds the persistence boundary.")
        return [_sanitize_persisted_value(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return _sanitize_persisted_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise RunStoreError("Run Artifact binary output is not persistable.")
    raise RunStoreError("Run Artifact contains a value outside the persistence allowlist.")


def _sanitize_persisted_text(
    value: str,
    *,
    limit: int = MAX_PERSISTED_TEXT_LENGTH,
) -> str:
    return redact_sensitive_text(value, limit=limit)


def _sanitize_analysis_draft(draft: AnalysisDraft) -> AnalysisDraft:
    return replace(
        draft,
        feature_title=_sanitize_persisted_text(
            draft.feature_title,
            limit=ANALYSIS_FEATURE_TITLE_MAX_LENGTH,
        ),
        prd_markdown=_sanitize_persisted_text(
            draft.prd_markdown,
            limit=ANALYSIS_PRD_MARKDOWN_MAX_LENGTH,
        ),
        issues=tuple(
            replace(
                issue,
                title=_sanitize_persisted_text(
                    issue.title,
                    limit=ANALYSIS_ISSUE_TITLE_MAX_LENGTH,
                ),
                acceptance_criteria=tuple(
                    replace(
                        criterion,
                        text=_sanitize_persisted_text(
                            criterion.text,
                            limit=ANALYSIS_ACCEPTANCE_TEXT_MAX_LENGTH,
                        ),
                    )
                    for criterion in issue.acceptance_criteria
                ),
                markdown=_sanitize_persisted_text(
                    issue.markdown,
                    limit=ANALYSIS_ISSUE_MARKDOWN_MAX_LENGTH,
                ),
            )
            for issue in draft.issues
        ),
    )


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _protect_private_path(path.parent, directory=True)
    temporary = path.with_name(f".tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _protect_private_path(path, directory=False)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        _protect_private_path(path, directory=False)
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        deadline = time.monotonic() + 10.0
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RunLeaseError(
                            "Timed out waiting for the Workflow Run lease claim lock."
                        ) from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        while True:
            try:
                fcntl.flock(  # type: ignore[attr-defined]
                    stream.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                )
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RunLeaseError(
                        "Timed out waiting for the Workflow Run lease claim lock."
                    ) from None
                time.sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(  # type: ignore[attr-defined]
                stream.fileno(),
                fcntl.LOCK_UN,  # type: ignore[attr-defined]
            )


def _protect_private_path(path: Path, *, directory: bool) -> None:
    if os.name == "nt":
        if not directory:
            return
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise RunStoreError("Unable to protect project-local Run storage.") from error
        with _WINDOWS_ACL_LOCK:
            for candidate in (resolved, *resolved.parents):
                protected_identity = _PROTECTED_WINDOWS_DIRECTORIES.get(candidate)
                if protected_identity is None:
                    continue
                try:
                    current_identity = _windows_path_identity(candidate)
                except OSError:
                    _PROTECTED_WINDOWS_DIRECTORIES.pop(candidate, None)
                    continue
                if current_identity == protected_identity:
                    return
                _PROTECTED_WINDOWS_DIRECTORIES.pop(candidate, None)
            try:
                protect_current_windows_user_path(resolved, directory=True)
            except WindowsAclError as error:
                raise RunStoreError("Unable to protect project-local Run storage.") from error
            _PROTECTED_WINDOWS_DIRECTORIES[resolved] = _windows_path_identity(resolved)
        return
    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError as error:
        raise RunStoreError("Unable to protect project-local Run storage.") from error


def _windows_path_identity(path: Path) -> tuple[int, int, int]:
    status = path.stat()
    return status.st_dev, status.st_ino, status.st_ctime_ns


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"Expected a JSON object in {path.name}.")
    return cast(dict[str, object], value)


def _object(data: Mapping[str, object], name: str) -> dict[str, object]:
    value = data.get(name)
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"Workflow Run field is not an object: {name}.")
    return cast(dict[str, object], value)


def _string(data: Mapping[str, object], name: str) -> str:
    return _typed_string(data.get(name))


def _typed_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Workflow Run contains a missing string value.")
    return value


def _nonnegative_int(data: Mapping[str, object], name: str) -> int:
    value = data.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Workflow Run field must be a nonnegative integer: {name}.")
    return value


def _process_is_running(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except OSError:
        return False
    return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
