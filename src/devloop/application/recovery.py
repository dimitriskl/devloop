from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from devloop.application.config import ApplicationConfig
from devloop.components.builtin import installed_component_registry
from devloop.domain.development import ArtifactRef, IssueStatus
from devloop.domain.identifiers import (
    ExecutionThreadId,
    ExecutionTurnId,
    IssueId,
    StepInstanceId,
    WorkflowRunId,
)
from devloop.domain.run import (
    ComponentLock,
    OperationState,
    OperationStatus,
    RunEventType,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.execution.app_server import (
    AppServerClient,
    AppServerError,
    AppServerTurnStatus,
)
from devloop.infrastructure.codex import resolve_codex_executable
from devloop.infrastructure.git import (
    GitOperationError,
    capture_repository_state_hash,
    current_branch,
    head_commit,
    repository_root,
)
from devloop.persistence.run_store import RunStore, RunStoreError
from devloop.planning.package_reader import PlanningPackageError, load_planning_package
from devloop.workflow.definition import load_standard_workflow, validate_component_ports

ANALYSIS_STEP_ID = StepInstanceId("analysis")
WORKSPACE_STEP_ID = StepInstanceId("workspace-preparation")
DEVELOPMENT_STEP_ID = StepInstanceId("development")
CODE_REVIEW_STEP_ID = StepInstanceId("code-review")
QA_STEP_ID = StepInstanceId("qa")
FINALIZATION_STEP_ID = StepInstanceId("workspace-finalization")
WORKSPACE_STATE_STEPS = {
    DEVELOPMENT_STEP_ID,
    CODE_REVIEW_STEP_ID,
    QA_STEP_ID,
    FINALIZATION_STEP_ID,
}


class RecoveryDisposition(str, Enum):
    CONTINUE_THREAD = "CONTINUE_THREAD"
    CONTINUE_WORKFLOW = "CONTINUE_WORKFLOW"
    FRESH_ATTEMPT = "FRESH_ATTEMPT"
    REFUSE = "REFUSE"


class RecoveryValidation(str, Enum):
    VALID = "VALID"
    UNKNOWN_OPERATION = "UNKNOWN_OPERATION"
    DRIFT = "DRIFT"
    APP_SERVER_INCOMPATIBLE = "APP_SERVER_INCOMPATIBLE"


class RecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecoveryBackendStatus:
    compatible: bool
    thread_available: bool
    diagnostic: str | None = None
    turn_status: AppServerTurnStatus | None = None


@dataclass(frozen=True)
class ResumeCandidate:
    run_id: WorkflowRunId
    feature: str
    workflow: str
    step: StepInstanceId
    issue_id: IssueId | None
    status: str
    workspace: str | None
    last_activity: str
    validation: RecoveryValidation


@dataclass(frozen=True)
class RecoveryPlan:
    snapshot: WorkflowRunSnapshot
    disposition: RecoveryDisposition
    validation: RecoveryValidation
    diagnostics: tuple[str, ...]


class RecoveryService:
    """Inspect exact resume state without taking a lease or starting a backend."""

    def __init__(
        self,
        config: ApplicationConfig,
        *,
        backend_probe: Callable[[WorkflowRunSnapshot], RecoveryBackendStatus] | None = None,
        source_state_probe: Callable[[Path], str] | None = None,
    ) -> None:
        self._config = config
        self._store = RunStore(config.paths.run_root)
        self._workflow = load_standard_workflow()
        self._registry = installed_component_registry()
        self._backend_probe = backend_probe or self._probe_backend
        self._source_state_probe = source_state_probe or capture_repository_state_hash

    def list_candidates(self) -> tuple[ResumeCandidate, ...]:
        return tuple(self._candidate(snapshot) for snapshot in self._store.list_unfinished())

    def list_runs(self) -> tuple[ResumeCandidate, ...]:
        return tuple(self._candidate(snapshot) for snapshot in self._store.list_runs())

    def inspect(self, run_id: WorkflowRunId) -> RecoveryPlan:
        snapshot = self._store.load(run_id)
        diagnostics = self._drift_diagnostics(snapshot)
        if diagnostics:
            return RecoveryPlan(
                snapshot,
                RecoveryDisposition.REFUSE,
                RecoveryValidation.DRIFT,
                diagnostics,
            )
        if snapshot.active_step in {WORKSPACE_STEP_ID, FINALIZATION_STEP_ID}:
            if snapshot.operation.status is OperationStatus.UNKNOWN:
                phase = (
                    "Workspace preparation"
                    if snapshot.active_step == WORKSPACE_STEP_ID
                    else "Workspace finalization"
                )
                return RecoveryPlan(
                    snapshot,
                    RecoveryDisposition.REFUSE,
                    RecoveryValidation.UNKNOWN_OPERATION,
                    (
                        f"{phase} stopped during an unknown local operation; "
                        "inspect the repository manually before continuing.",
                    ),
                )
            return RecoveryPlan(
                snapshot,
                RecoveryDisposition.CONTINUE_WORKFLOW,
                RecoveryValidation.VALID,
                (),
            )
        backend = self._backend_probe(snapshot)
        if not backend.compatible:
            return RecoveryPlan(
                snapshot,
                RecoveryDisposition.REFUSE,
                RecoveryValidation.APP_SERVER_INCOMPATIBLE,
                (
                    backend.diagnostic
                    or "Codex App Server compatibility could not be validated.",
                ),
            )
        if snapshot.operation.status is OperationStatus.UNKNOWN:
            return RecoveryPlan(
                snapshot,
                RecoveryDisposition.FRESH_ATTEMPT,
                RecoveryValidation.UNKNOWN_OPERATION,
                (
                    "An interrupted operation has unknown effects and will not be replayed. "
                    "Use a transcript-free Recovery Attempt from the locked context.",
                ),
            )
        if _active_thread(snapshot) is None or not backend.thread_available:
            return RecoveryPlan(
                snapshot,
                RecoveryDisposition.FRESH_ATTEMPT,
                RecoveryValidation.VALID,
                (
                    backend.diagnostic
                    or "The App Server thread is unavailable; use the locked structured context.",
                ),
            )
        if _active_turn(snapshot) is not None and backend.turn_status in {
            None,
            AppServerTurnStatus.INTERRUPTED,
            AppServerTurnStatus.FAILED,
        }:
            return RecoveryPlan(
                snapshot,
                RecoveryDisposition.FRESH_ATTEMPT,
                RecoveryValidation.VALID,
                (
                    backend.diagnostic
                    or "The checkpointed App Server turn is absent or cannot continue; "
                    "use the locked structured context.",
                ),
            )
        return RecoveryPlan(
            snapshot,
            RecoveryDisposition.CONTINUE_THREAD,
            RecoveryValidation.VALID,
            (),
        )

    def start_fresh_attempt(self, run_id: WorkflowRunId) -> WorkflowRunSnapshot:
        plan = self.inspect(run_id)
        if plan.disposition is RecoveryDisposition.REFUSE:
            raise RecoveryError(" ".join(plan.diagnostics))
        if plan.disposition is not RecoveryDisposition.FRESH_ATTEMPT:
            raise RecoveryError("The Workflow Run can continue its checkpointed thread.")
        current = self._store.take_lease(plan.snapshot)
        try:
            if current.operation.status is OperationStatus.UNKNOWN:
                current = self._store.record(current, RunEventType.OPERATION_UNKNOWN)
            current = _fresh_attempt_snapshot(current)
            return self._store.record(current, RunEventType.RECOVERY_ATTEMPT_STARTED)
        except Exception:
            self._store.release_lease(current)
            raise

    def _candidate(self, snapshot: WorkflowRunSnapshot) -> ResumeCandidate:
        workspace = snapshot.workspace
        if self._drift_diagnostics(snapshot):
            validation = RecoveryValidation.DRIFT
        elif snapshot.operation.status is OperationStatus.UNKNOWN:
            validation = RecoveryValidation.UNKNOWN_OPERATION
        else:
            validation = RecoveryValidation.VALID
        return ResumeCandidate(
            run_id=snapshot.run_id,
            feature=snapshot.feature_title,
            workflow=f"{snapshot.workflow.workflow_id.value}@{snapshot.workflow.version}",
            step=snapshot.active_step,
            issue_id=_active_issue(snapshot),
            status=snapshot.run_status.value,
            workspace=None if workspace is None else workspace.path,
            last_activity=snapshot.updated_at,
            validation=validation,
        )

    def _probe_backend(self, snapshot: WorkflowRunSnapshot) -> RecoveryBackendStatus:
        workspace = snapshot.workspace
        cwd = self._config.repository if workspace is None else Path(workspace.path)
        try:
            executable = resolve_codex_executable()
            with AppServerClient(
                str(executable),
                experimental_api=True,
                timeout_seconds=self._config.app_server_timeout_seconds,
                process_cwd=cwd,
            ) as client:
                status = client.probe()
                if not status.authentication.ready:
                    return RecoveryBackendStatus(
                        compatible=False,
                        thread_available=False,
                        diagnostic="Codex App Server authentication is not ready.",
                    )
                thread_id = _active_thread(snapshot)
                if thread_id is None:
                    return RecoveryBackendStatus(
                        compatible=True,
                        thread_available=False,
                        diagnostic="The checkpoint has no App Server thread.",
                    )
                try:
                    turn_id = _active_turn(snapshot)
                    turn_status = (
                        None
                        if turn_id is None
                        else client.read_thread_turn_status(
                            thread_id.value,
                            turn_id.value,
                        )
                    )
                except AppServerError:
                    return RecoveryBackendStatus(
                        compatible=True,
                        thread_available=False,
                        diagnostic=(
                            "The checkpointed App Server thread or turn is unavailable."
                        ),
                    )
        except Exception:
            return RecoveryBackendStatus(
                compatible=False,
                thread_available=False,
                diagnostic="Codex App Server compatibility could not be validated.",
            )
        return RecoveryBackendStatus(
            compatible=True,
            thread_available=True,
            turn_status=turn_status,
        )

    def _drift_diagnostics(self, snapshot: WorkflowRunSnapshot) -> tuple[str, ...]:
        diagnostics: list[str] = []
        if Path(snapshot.repository).resolve() != self._config.repository:
            diagnostics.append("Repository identity differs from the checkpoint.")
        if (
            snapshot.workflow.workflow_id != self._workflow.workflow_id
            or snapshot.workflow.version != self._workflow.version
            or snapshot.workflow.definition_hash != self._workflow.definition_hash
        ):
            diagnostics.append("Workflow Definition hash or identity changed.")
        current_locks = {
            manifest.component_id: ComponentLock(
                manifest.component_id,
                manifest.version,
                manifest.distribution,
                manifest.package_hash,
            )
            for manifest in self._registry.manifests
        }
        checkpoint_locks = {item.component_id: item for item in snapshot.component_locks}
        if (
            len(checkpoint_locks) != len(snapshot.component_locks)
            or checkpoint_locks.keys() != current_locks.keys()
            or any(current_locks[item_id] != item for item_id, item in checkpoint_locks.items())
        ):
            diagnostics.append("Locked Workflow components changed or are unavailable.")
        for manifest in self._registry.manifests:
            try:
                validate_component_ports(
                    self._workflow.step(StepInstanceId(manifest.component_id.value)),
                    manifest,
                )
            except ValueError:
                diagnostics.append("A locked component no longer matches its Workflow ports.")
                break
        if snapshot.planning_package is not None:
            try:
                load_planning_package(
                    self._config.repository,
                    snapshot.planning_package,
                    snapshot.run_id,
                )
            except PlanningPackageError as error:
                diagnostics.append(str(error))
        for artifact in _active_artifacts(snapshot):
            try:
                self._store.load_json_artifact(snapshot.run_id, artifact)
            except RunStoreError as error:
                diagnostics.append(str(error))
        workspace = snapshot.workspace
        implementation = (
            None
            if snapshot.development is None
            else snapshot.development.implementation_result
        )
        expected_state = snapshot.workspace_state_hash
        if workspace is not None and snapshot.active_step in WORKSPACE_STATE_STEPS:
            if expected_state is None:
                diagnostics.append("Workspace source state checkpoint is missing.")
            else:
                try:
                    current_state = self._source_state_probe(Path(workspace.path))
                except (GitOperationError, OSError):
                    diagnostics.append("Workspace source state cannot be validated.")
                else:
                    if current_state != expected_state:
                        diagnostics.append("Workspace source state differs from the checkpoint.")
        if implementation is not None and snapshot.active_step in {
            CODE_REVIEW_STEP_ID,
            QA_STEP_ID,
            FINALIZATION_STEP_ID,
        }:
            try:
                payload = self._store.load_json_artifact(snapshot.run_id, implementation)
            except RunStoreError:
                pass
            else:
                implementation_state = payload.get("repository_state_hash")
                if not isinstance(implementation_state, str) or not implementation_state:
                    diagnostics.append(
                        "Implementation Result has no valid repository state checkpoint."
                    )
                elif implementation_state != expected_state:
                    diagnostics.append(
                        "Implementation Result differs from the workspace state checkpoint."
                    )
        if workspace is not None:
            try:
                path = Path(workspace.path)
                if not path.exists() or repository_root(path) != path.resolve():
                    diagnostics.append("Checkpointed worktree is unavailable.")
                if repository_root(Path(workspace.repository_root)) != Path(
                    workspace.repository_root
                ).resolve():
                    diagnostics.append("Checkpointed repository identity is invalid.")
                if current_branch(path) != workspace.branch:
                    diagnostics.append("Workspace branch differs from the checkpoint.")
                if head_commit(path) != workspace.base_commit:
                    diagnostics.append("Workspace HEAD differs from the checkpoint.")
            except (GitOperationError, OSError):
                diagnostics.append("Checkpointed repository or worktree cannot be validated.")
        return tuple(dict.fromkeys(diagnostics))


def _active_thread(snapshot: WorkflowRunSnapshot) -> ExecutionThreadId | None:
    if snapshot.active_step == ANALYSIS_STEP_ID:
        return snapshot.analysis.thread_id
    if snapshot.active_step == DEVELOPMENT_STEP_ID:
        return None if snapshot.development is None else snapshot.development.thread_id
    if snapshot.active_step == CODE_REVIEW_STEP_ID:
        return None if snapshot.review is None else snapshot.review.thread_id
    if snapshot.active_step == QA_STEP_ID:
        return None if snapshot.qa is None else snapshot.qa.thread_id
    return None


def _active_turn(snapshot: WorkflowRunSnapshot) -> ExecutionTurnId | None:
    if snapshot.active_step == ANALYSIS_STEP_ID:
        return snapshot.analysis.turn_id
    if snapshot.active_step == DEVELOPMENT_STEP_ID:
        return None if snapshot.development is None else snapshot.development.turn_id
    if snapshot.active_step == CODE_REVIEW_STEP_ID:
        return None if snapshot.review is None else snapshot.review.turn_id
    if snapshot.active_step == QA_STEP_ID:
        return None if snapshot.qa is None else snapshot.qa.turn_id
    return None


def _fresh_attempt_snapshot(snapshot: WorkflowRunSnapshot) -> WorkflowRunSnapshot:
    fresh = replace(
        snapshot,
        run_status=WorkflowRunStatus.RUNNING,
        step_status=StepRunStatus.RUNNING,
        outcome=None,
        operation=OperationState(),
    )
    if fresh.active_step == ANALYSIS_STEP_ID:
        return replace(
            fresh,
            analysis=replace(
                fresh.analysis,
                thread_id=None,
                turn_id=None,
                completed_item_ids=(),
            ),
        )
    if fresh.active_step == DEVELOPMENT_STEP_ID and fresh.development is not None:
        return replace(
            fresh,
            development=replace(
                fresh.development,
                thread_id=None,
                turn_id=None,
                completed_item_ids=(),
                implementation_result=None,
                approval_request=None,
                transient_retries=0,
            ),
        )
    if fresh.active_step == CODE_REVIEW_STEP_ID and fresh.review is not None:
        return replace(
            fresh,
            review=replace(
                fresh.review,
                thread_id=None,
                turn_id=None,
                completed_item_ids=(),
                review_result=None,
                rework_request=None,
                transient_retries=0,
            ),
        )
    if fresh.active_step == QA_STEP_ID and fresh.qa is not None:
        return replace(
            fresh,
            qa=replace(
                fresh.qa,
                thread_id=None,
                turn_id=None,
                completed_item_ids=(),
                qa_result=None,
                rework_request=None,
                transient_retries=0,
            ),
        )
    raise RecoveryError("The active Workflow phase cannot start a Recovery Attempt.")


def _active_issue(snapshot: WorkflowRunSnapshot) -> IssueId | None:
    for cursor in (snapshot.qa, snapshot.review, snapshot.development):
        if cursor is not None:
            return cursor.issue_id
    return next(
        (item.issue_id for item in snapshot.issues if item.status is not IssueStatus.COMPLETED),
        None,
    )


def _active_artifacts(snapshot: WorkflowRunSnapshot) -> tuple[ArtifactRef, ...]:
    artifacts: list[ArtifactRef] = []

    def add(artifact: ArtifactRef | None) -> None:
        if artifact is not None:
            artifacts.append(artifact)

    if snapshot.development is not None:
        add(
            ArtifactRef(
                snapshot.development.context_manifest.path,
                snapshot.development.context_manifest.content_hash,
            )
        )
        add(snapshot.development.implementation_result)
        add(snapshot.development.approval_request)
    if snapshot.review is not None:
        add(snapshot.review.input_manifest)
        add(snapshot.review.review_result)
        add(snapshot.review.rework_request)
    if snapshot.qa is not None:
        add(snapshot.qa.input_manifest)
        add(snapshot.qa.qa_result)
        add(snapshot.qa.rework_request)
    for attempt in snapshot.attempts:
        add(attempt.implementation)
        add(attempt.review)
        add(attempt.qa_result)
        add(attempt.rework_request)
    return tuple(
        {
            (artifact.path, artifact.content_hash): artifact
            for artifact in artifacts
        }.values()
    )
