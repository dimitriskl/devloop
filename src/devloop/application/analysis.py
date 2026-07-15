from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from devloop.analysis.package import (
    analysis_draft_to_dict,
    publish_analysis_package,
    validate_analysis_draft,
)
from devloop.application.capabilities import (
    CapabilityProfileService,
    standard_capability_catalog,
)
from devloop.application.config import ApplicationConfig
from devloop.application.telemetry import ExecutionTelemetryRecorder
from devloop.components.analysis import (
    ANALYSIS_COMPONENT_ID,
    AnalysisComponentRunner,
    AnalysisTurnOutput,
    analysis_prompt,
)
from devloop.components.builtin import installed_component_registry
from devloop.domain.development import PlanningPackageRef
from devloop.domain.execution import ExecutionPhase, locked_execution_profile
from devloop.domain.identifiers import (
    ExecutionThreadId,
    ExecutionTurnId,
    FeatureSlug,
    StepInstanceId,
    WorkflowRunId,
)
from devloop.domain.outcomes import StepOutcome
from devloop.domain.planning import AnalysisDraft, PublishedPackage, ValidationFinding
from devloop.domain.run import (
    AnalysisCursor,
    AnalysisResponseKind,
    ComponentLock,
    OperationState,
    OperationStatus,
    ResolvedWorkflow,
    RunEventType,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.persistence.run_store import (
    RUN_SNAPSHOT_SCHEMA,
    RunStore,
    RunStoreError,
    new_run_lease,
)
from devloop.workflow.definition import load_standard_workflow, validate_component_ports

ANALYSIS_STEP_ID = StepInstanceId("analysis")
_SLUG_TOKEN = re.compile(r"[^a-z0-9]+")
MAX_USER_MESSAGE_CHARS = 100_000


class AnalysisWorkflowError(RuntimeError):
    pass


@dataclass(frozen=True)
class AnalysisRunResult:
    snapshot: WorkflowRunSnapshot
    draft: AnalysisDraft | None
    findings: tuple[ValidationFinding, ...]
    clarification: str | None


@dataclass(frozen=True)
class AnalysisAcceptance:
    snapshot: WorkflowRunSnapshot
    package: PublishedPackage


class AnalysisWorkflowService:
    def __init__(self, config: ApplicationConfig) -> None:
        self._config = config
        self._store = RunStore(config.paths.run_root)
        self._workflow = load_standard_workflow()
        self._capability_profiles = CapabilityProfileService(
            config.paths.user_config,
            standard_capability_catalog(),
        )
        registry = installed_component_registry()
        self._registry = registry
        manifest, runner = registry.resolve(ANALYSIS_COMPONENT_ID)
        if not isinstance(runner, AnalysisComponentRunner):
            raise AnalysisWorkflowError("The registered analysis runner is incompatible.")
        self._manifest = manifest
        self._runner = runner
        self._telemetry = ExecutionTelemetryRecorder(self._store)
        self._component_locks = tuple(
            ComponentLock(
                item.component_id,
                item.version,
                item.distribution,
                item.package_hash,
            )
            for item in registry.manifests
        )
        for item in registry.manifests:
            validate_component_ports(
                self._workflow.step(StepInstanceId(item.component_id.value)),
                item,
            )

    def start(
        self,
        feature_request: str,
        *,
        on_activity: Callable[[str], None] | None = None,
    ) -> AnalysisRunResult:
        if not feature_request.strip():
            raise ValueError("A feature request is required.")
        if len(feature_request) > MAX_USER_MESSAGE_CHARS:
            raise ValueError("The feature request exceeds the supported size.")
        snapshot = self._new_snapshot(feature_request.strip())
        snapshot = self._store.create(snapshot)
        return self._run_turn(
            snapshot,
            analysis_prompt(feature_request.strip()),
            on_activity=on_activity,
        )

    def continue_analysis(
        self,
        run_id: WorkflowRunId,
        user_message: str,
        *,
        on_activity: Callable[[str], None] | None = None,
    ) -> AnalysisRunResult:
        if not user_message.strip():
            raise ValueError("A clarification or requested change is required.")
        if len(user_message) > MAX_USER_MESSAGE_CHARS:
            raise ValueError("The clarification exceeds the supported size.")
        snapshot = self._store.load(run_id)
        self._validate_awaiting_analysis(snapshot)
        snapshot = self._ensure_lease(snapshot)
        self._validate_locks(snapshot)
        if snapshot.analysis.thread_id is None:
            raise AnalysisWorkflowError("The analysis thread is not available for continuation.")
        prompt = (
            "Continue the same analysis using this user clarification or requested change. "
            "Return either one necessary clarification or a complete revised draft matching the "
            f"output schema.\n\nUser response:\n{user_message.strip()}"
        )
        return self._run_turn(snapshot, prompt, on_activity=on_activity)

    def accept(self, run_id: WorkflowRunId) -> AnalysisAcceptance:
        snapshot = self._store.load(run_id)
        self._validate_awaiting_analysis(snapshot)
        if snapshot.analysis.clarification is not None:
            raise AnalysisWorkflowError(
                "Analysis cannot be accepted while a clarification is pending."
            )
        snapshot = self._ensure_lease(snapshot)
        self._validate_locks(snapshot)
        draft = self._store.load_draft(run_id)
        findings = validate_analysis_draft(draft)
        if findings:
            raise AnalysisWorkflowError(
                "Analysis Draft has validation findings: "
                + "; ".join(item.message for item in findings)
            )
        package = publish_analysis_package(self._config.repository, draft)
        for manifest in self._registry.manifests:
            step = self._workflow.step(StepInstanceId(manifest.component_id.value))
            validate_component_ports(step, manifest)
        accepted = replace(
            snapshot,
            run_status=WorkflowRunStatus.AWAITING_USER,
            step_status=StepRunStatus.NOT_STARTED,
            outcome=None,
            active_step=self._workflow.required_transition_target(
                ANALYSIS_STEP_ID,
                StepOutcome.SUCCEEDED,
            ),
            planning_package=PlanningPackageRef(
                package.root,
                package.prd_hash,
                package.issue_set_hash,
            ),
            analysis=replace(snapshot.analysis, clarification=None),
        )
        accepted = self._store.record(accepted, RunEventType.ANALYSIS_ACCEPTED)
        return AnalysisAcceptance(accepted, package)

    def pause(self, run_id: WorkflowRunId) -> WorkflowRunSnapshot:
        snapshot = self._ensure_lease(self._store.load(run_id))
        paused = replace(snapshot, run_status=WorkflowRunStatus.PAUSED)
        paused = self._store.record(paused, RunEventType.RUN_PAUSED)
        self._store.release_lease(paused)
        return paused

    def list_resumable(self) -> tuple[WorkflowRunSnapshot, ...]:
        return self._store.list_unfinished()

    def resume(
        self,
        run_id: WorkflowRunId,
        *,
        on_activity: Callable[[str], None] | None = None,
    ) -> AnalysisRunResult:
        snapshot = self._store.load(run_id)
        if snapshot.terminal:
            raise AnalysisWorkflowError("A terminal Workflow Run cannot be resumed.")
        if snapshot.active_step != ANALYSIS_STEP_ID:
            raise AnalysisWorkflowError("The Workflow Run is not resumable through analysis.")
        if snapshot.operation.status is OperationStatus.UNKNOWN:
            raise AnalysisWorkflowError(
                "An unknown operation requires an explicit transcript-free Recovery Attempt."
            )
        snapshot = self._store.take_lease(snapshot)
        self._validate_locks(snapshot)
        thread_id = snapshot.analysis.thread_id
        if thread_id is None:
            self._store.release_lease(snapshot)
            raise AnalysisWorkflowError("The analysis thread is not available for resume.")
        if snapshot.step_status is StepRunStatus.RUNNING and snapshot.analysis.turn_id is not None:
            running = replace(
                snapshot,
                run_status=WorkflowRunStatus.RUNNING,
                step_status=StepRunStatus.RUNNING,
                outcome=None,
            )
            running = self._store.record(running, RunEventType.RUN_RESUMED)
            current = running

            def item_started(item_id: str) -> None:
                nonlocal current
                current = replace(
                    current,
                    operation=OperationState(item_id, OperationStatus.RUNNING),
                )
                current = self._store.record(current, RunEventType.OPERATION_STARTED)

            def item_completed(item_id: str) -> None:
                nonlocal current
                completed = tuple(
                    dict.fromkeys((*current.analysis.completed_item_ids, item_id))
                )
                current = replace(
                    current,
                    analysis=replace(
                        current.analysis,
                        completed_item_ids=completed,
                    ),
                    operation=OperationState(),
                )
                current = self._store.record(current, RunEventType.OPERATION_COMPLETED)

            try:
                output = self._runner.recover_turn(
                    repository=self._config.repository,
                    run_id=run_id,
                    thread_id=thread_id,
                    turn_id=snapshot.analysis.turn_id,
                    on_item_started=item_started,
                    on_item_completed=item_completed,
                    on_activity=on_activity,
                )
            except Exception as error:
                paused = replace(current, run_status=WorkflowRunStatus.PAUSED)
                paused = self._store.record(paused, RunEventType.RUN_PAUSED)
                self._store.release_lease(paused)
                raise AnalysisWorkflowError(
                    "The checkpointed analysis turn could not be recovered."
                ) from error
            return self._complete_turn(current, output)
        self._runner.validate_resume(self._config.repository, thread_id)
        resumed = replace(
            snapshot,
            run_status=WorkflowRunStatus.AWAITING_USER,
            step_status=StepRunStatus.AWAITING_USER,
            outcome=None,
            lease=snapshot.lease,
        )
        resumed = self._store.record(resumed, RunEventType.RUN_RESUMED)
        draft = self._optional_draft(run_id)
        findings = () if draft is None else validate_analysis_draft(draft)
        return AnalysisRunResult(
            resumed,
            draft,
            findings,
            resumed.analysis.clarification,
        )

    def recover_fresh(
        self,
        run_id: WorkflowRunId,
        *,
        on_activity: Callable[[str], None] | None = None,
    ) -> AnalysisRunResult:
        snapshot = self._store.load(run_id)
        if (
            snapshot.active_step != ANALYSIS_STEP_ID
            or snapshot.run_status is not WorkflowRunStatus.RUNNING
            or snapshot.step_status is not StepRunStatus.RUNNING
            or snapshot.analysis.thread_id is not None
            or snapshot.operation.status is not OperationStatus.IDLE
        ):
            raise AnalysisWorkflowError(
                "Analysis is not prepared for a transcript-free Recovery Attempt."
            )
        self._store.validate_lease(snapshot)
        self._validate_locks(snapshot)
        draft = self._optional_draft(run_id)
        context = {
            "schema": "devloop.analysis-recovery-context/v1",
            "run_id": run_id.value,
            "feature": {
                "title": snapshot.feature_title,
                "slug": snapshot.feature_slug.value,
            },
            "draft": None if draft is None else analysis_draft_to_dict(draft),
            "clarification": snapshot.analysis.clarification,
        }
        prompt = (
            "Start a fresh analysis Recovery Attempt from this locked structured context. "
            "Do not replay conversation history. Return either one necessary clarification "
            "or a complete revised draft matching the output schema.\n\n"
            + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        )
        return self._run_turn(snapshot, prompt, on_activity=on_activity)

    def _run_turn(
        self,
        snapshot: WorkflowRunSnapshot,
        message: str,
        *,
        on_activity: Callable[[str], None] | None,
    ) -> AnalysisRunResult:
        current = replace(
            snapshot,
            run_status=WorkflowRunStatus.RUNNING,
            step_status=StepRunStatus.RUNNING,
            outcome=None,
        )
        current = self._store.record(current, RunEventType.ANALYSIS_ATTEMPT_STARTED)
        attempt_key = f"analysis:{snapshot.event_sequence + 1}"
        current = self._telemetry.record(
            current,
            ANALYSIS_COMPONENT_ID.value,
            attempt_key,
            ExecutionPhase.CONTEXT_LOADED,
        )

        def phase(value: ExecutionPhase, *, applicable: bool = True) -> None:
            nonlocal current
            current = self._telemetry.record(
                current,
                ANALYSIS_COMPONENT_ID.value,
                attempt_key,
                value,
                applicable=applicable,
            )

        def activity(delta: str) -> None:
            phase(ExecutionPhase.FIRST_ACTIVITY)
            if on_activity is not None:
                on_activity(delta)

        def thread_bound(thread_id: ExecutionThreadId) -> None:
            nonlocal current
            if current.analysis.thread_id == thread_id:
                return
            current = replace(current, analysis=replace(current.analysis, thread_id=thread_id))
            current = self._store.record(current, RunEventType.ANALYSIS_THREAD_BOUND)

        def turn_started(turn_id: ExecutionTurnId) -> None:
            nonlocal current
            current = replace(current, analysis=replace(current.analysis, turn_id=turn_id))
            current = self._store.record(current, RunEventType.ANALYSIS_TURN_STARTED)

        def item_started(item_id: str) -> None:
            nonlocal current
            phase(ExecutionPhase.FIRST_ACTIVITY)
            current = replace(
                current,
                operation=OperationState(item_id, OperationStatus.RUNNING),
            )
            current = self._store.record(current, RunEventType.OPERATION_STARTED)

        def item_completed(item_id: str) -> None:
            nonlocal current
            completed = tuple(dict.fromkeys((*current.analysis.completed_item_ids, item_id)))
            current = replace(
                current,
                analysis=replace(current.analysis, completed_item_ids=completed),
                operation=OperationState(),
            )
            current = self._store.record(current, RunEventType.OPERATION_COMPLETED)

        try:
            output = self._runner.run_turn(
                repository=self._config.repository,
                run_id=current.run_id,
                message=message,
                thread_id=current.analysis.thread_id,
                on_thread_bound=thread_bound,
                on_turn_started=turn_started,
                on_item_started=item_started,
                on_item_completed=item_completed,
                on_activity=activity,
                execution_profile=locked_execution_profile(
                    current.execution_profiles,
                    ANALYSIS_COMPONENT_ID.value,
                    self._manifest.execution_profiles[0],
                ),
            )
        except Exception as error:
            paused = replace(current, run_status=WorkflowRunStatus.PAUSED)
            paused = self._store.record(paused, RunEventType.RUN_PAUSED)
            self._store.release_lease(paused)
            raise AnalysisWorkflowError(
                "The real Codex analysis turn did not complete. Resume this run to continue."
            ) from error

        phase(ExecutionPhase.FIRST_ACTIVITY)
        phase(ExecutionPhase.FIRST_FILE_CHANGE, applicable=False)
        phase(ExecutionPhase.VERIFICATION_STARTED, applicable=False)
        phase(ExecutionPhase.STRUCTURED_OUTPUT)
        result = self._complete_turn(current, output)
        completed = self._telemetry.record(
            result.snapshot,
            ANALYSIS_COMPONENT_ID.value,
            attempt_key,
            ExecutionPhase.COMPLETED,
        )
        return replace(result, snapshot=completed)

    def _complete_turn(
        self,
        current: WorkflowRunSnapshot,
        output: AnalysisTurnOutput,
    ) -> AnalysisRunResult:
        if output.kind is AnalysisResponseKind.CLARIFICATION:
            awaiting = replace(
                current,
                run_status=WorkflowRunStatus.AWAITING_USER,
                step_status=StepRunStatus.AWAITING_USER,
                operation=OperationState(),
                analysis=replace(
                    current.analysis,
                    thread_id=output.thread_id,
                    turn_id=output.turn_id,
                    clarification=output.clarification,
                    completed_item_ids=output.completed_item_ids,
                ),
            )
            awaiting = self._store.record(
                awaiting,
                RunEventType.ANALYSIS_CLARIFICATION_REQUESTED,
            )
            return AnalysisRunResult(awaiting, None, (), output.clarification)

        if output.draft is None:
            raise AnalysisWorkflowError("Analysis completed without a Draft.")
        self._store.save_draft(output.draft)
        findings = validate_analysis_draft(output.draft)
        awaiting = replace(
            current,
            feature_title=output.draft.feature_title,
            feature_slug=output.draft.feature_slug,
            run_status=WorkflowRunStatus.AWAITING_USER,
            step_status=StepRunStatus.AWAITING_USER,
            operation=OperationState(),
            analysis=AnalysisCursor(
                output.thread_id,
                output.turn_id,
                output.draft.revision,
                None,
                output.completed_item_ids,
            ),
        )
        awaiting = self._store.record(awaiting, RunEventType.ANALYSIS_DRAFT_SAVED)
        return AnalysisRunResult(awaiting, output.draft, findings, None)

    def _new_snapshot(
        self,
        feature_request: str,
    ) -> WorkflowRunSnapshot:
        return WorkflowRunSnapshot(
            schema=RUN_SNAPSHOT_SCHEMA,
            run_id=_new_run_id(),
            repository=str(self._config.repository),
            feature_title=feature_request,
            feature_slug=_feature_slug(feature_request),
            workflow=ResolvedWorkflow(
                self._workflow.workflow_id,
                self._workflow.version,
                self._workflow.definition_hash,
            ),
            component_locks=self._component_locks,
            active_step=ANALYSIS_STEP_ID,
            run_status=WorkflowRunStatus.CREATED,
            step_status=StepRunStatus.NOT_STARTED,
            outcome=None,
            analysis=AnalysisCursor(),
            lease=new_run_lease(),
            event_sequence=0,
            updated_at=datetime.now(timezone.utc).isoformat(),
            capability_profiles=self._capability_profiles.resolved_profiles(),
            execution_profiles=tuple(
                profile
                for manifest in self._registry.manifests
                for profile in manifest.execution_profiles
                if profile.profile_id.value == manifest.default_execution_profile
            ),
            approval_policies=tuple(
                manifest.approval_policy
                for manifest in self._registry.manifests
                if manifest.approval_policy is not None
            ),
        )

    def _validate_locks(self, snapshot: WorkflowRunSnapshot) -> None:
        if snapshot.workflow.definition_hash != self._workflow.definition_hash:
            raise AnalysisWorkflowError("The locked Workflow Definition has changed.")
        if snapshot.component_locks != self._component_locks:
            raise AnalysisWorkflowError(
                "The locked Workflow components are unavailable or changed."
            )

    def _validate_awaiting_analysis(self, snapshot: WorkflowRunSnapshot) -> None:
        if (
            snapshot.active_step != ANALYSIS_STEP_ID
            or snapshot.run_status is not WorkflowRunStatus.AWAITING_USER
            or snapshot.step_status is not StepRunStatus.AWAITING_USER
        ):
            raise AnalysisWorkflowError(
                "The Workflow Run is not awaiting user input in analysis."
            )

    def _ensure_lease(self, snapshot: WorkflowRunSnapshot) -> WorkflowRunSnapshot:
        try:
            self._store.validate_lease(snapshot)
            return snapshot
        except (RunStoreError, OSError, ValueError):
            return self._store.take_lease(snapshot)

    def _optional_draft(self, run_id: WorkflowRunId) -> AnalysisDraft | None:
        try:
            return self._store.load_draft(run_id)
        except RunStoreError:
            return None


def _new_run_id() -> WorkflowRunId:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")
    return WorkflowRunId(f"run-{timestamp}-{uuid.uuid4().hex[:12]}")


def _feature_slug(feature_request: str) -> FeatureSlug:
    normalized = unicodedata.normalize("NFKD", feature_request).encode("ascii", "ignore").decode()
    slug = _SLUG_TOKEN.sub("-", normalized.lower()).strip("-")[:63].strip("-")
    if not slug:
        digest = hashlib.sha256(feature_request.encode("utf-8")).hexdigest()[:12]
        slug = f"feature-{digest}"
    return FeatureSlug(slug)
