from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

EXECUTION_PROFILE_SCHEMA = "devloop.execution-profile/v1"
EXECUTION_TELEMETRY_SCHEMA = "devloop.execution-telemetry/v1"
EXECUTION_POLICY_VERSION = "1.0.0"
MAX_EXECUTION_PHASE_EVENTS = 600


class ExecutionProfileId(str, Enum):
    FULL = "FULL"
    LIGHTWEIGHT = "LIGHTWEIGHT"


class ExecutionPhase(str, Enum):
    CONTEXT_LOADED = "CONTEXT_LOADED"
    FIRST_ACTIVITY = "FIRST_ACTIVITY"
    FIRST_FILE_CHANGE = "FIRST_FILE_CHANGE"
    VERIFICATION_STARTED = "VERIFICATION_STARTED"
    STRUCTURED_OUTPUT = "STRUCTURED_OUTPUT"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class ExecutionBudget:
    version: str
    timeout_seconds: float
    checkpoint_seconds: float

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("Execution budget version is required.")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 3600:
            raise ValueError("Execution timeout must be between zero and 3600 seconds.")
        if self.checkpoint_seconds <= 0 or self.checkpoint_seconds > self.timeout_seconds:
            raise ValueError("Checkpoint deadline must fit inside the execution timeout.")


@dataclass(frozen=True)
class ExecutionProfile:
    schema: str
    version: str
    component_id: str
    profile_id: ExecutionProfileId
    model: str
    reasoning_effort: str
    budget: ExecutionBudget

    def __post_init__(self) -> None:
        if self.schema != EXECUTION_PROFILE_SCHEMA:
            raise ValueError("Unsupported execution profile schema.")
        if not all((self.version, self.component_id, self.model, self.reasoning_effort)):
            raise ValueError("Execution profile provenance is incomplete.")

    @property
    def profile_hash(self) -> str:
        return _canonical_hash(execution_profile_payload(self))

    @classmethod
    def full(
        cls,
        component_id: str,
        model: str,
        reasoning_effort: str,
        timeout_seconds: float,
        checkpoint_seconds: float,
    ) -> ExecutionProfile:
        return cls(
            EXECUTION_PROFILE_SCHEMA,
            EXECUTION_POLICY_VERSION,
            component_id,
            ExecutionProfileId.FULL,
            model,
            reasoning_effort,
            ExecutionBudget(EXECUTION_POLICY_VERSION, timeout_seconds, checkpoint_seconds),
        )

    @classmethod
    def lightweight(
        cls,
        component_id: str,
        model: str,
        reasoning_effort: str,
        timeout_seconds: float,
        checkpoint_seconds: float,
    ) -> ExecutionProfile:
        return cls(
            EXECUTION_PROFILE_SCHEMA,
            EXECUTION_POLICY_VERSION,
            component_id,
            ExecutionProfileId.LIGHTWEIGHT,
            model,
            reasoning_effort,
            ExecutionBudget(EXECUTION_POLICY_VERSION, timeout_seconds, checkpoint_seconds),
        )


@dataclass(frozen=True)
class ExecutionPhaseEvent:
    phase: ExecutionPhase
    occurred_at: str
    elapsed_ms: int
    component_id: str = "workflow"
    attempt_key: str = "run"
    applicable: bool = True


@dataclass(frozen=True)
class ExecutionTelemetry:
    schema: str = EXECUTION_TELEMETRY_SCHEMA
    events: tuple[ExecutionPhaseEvent, ...] = ()

    def __post_init__(self) -> None:
        if self.schema != EXECUTION_TELEMETRY_SCHEMA:
            raise ValueError("Unsupported execution telemetry schema.")
        if len(self.events) > MAX_EXECUTION_PHASE_EVENTS:
            raise ValueError("Execution telemetry exceeds its bounded phase projection.")
        keys = tuple(dict.fromkeys((item.component_id, item.attempt_key) for item in self.events))
        for key in keys:
            matching = tuple(
                item
                for item in self.events
                if (item.component_id, item.attempt_key) == key
            )
            phases = tuple(item.phase for item in matching)
            expected = tuple(ExecutionPhase)[: len(phases)]
            if phases != expected:
                raise ValueError("Execution telemetry phases are out of order.")
            if not all(item.component_id and item.attempt_key for item in matching):
                raise ValueError("Execution telemetry identity is incomplete.")
            timestamps = tuple(_timestamp(item.occurred_at) for item in matching)
            if any(later < earlier for earlier, later in zip(timestamps, timestamps[1:])):
                raise ValueError("Execution telemetry timestamps must be monotonic.")
            first = timestamps[0]
            if any(
                item.elapsed_ms != round((timestamp - first).total_seconds() * 1000)
                for item, timestamp in zip(matching, timestamps)
            ):
                raise ValueError("Execution telemetry elapsed durations are inconsistent.")
        if any(item.elapsed_ms < 0 for item in self.events):
            raise ValueError("Execution telemetry durations cannot be negative.")

    def record(
        self,
        phase: ExecutionPhase,
        occurred_at: datetime,
        *,
        component_id: str = "workflow",
        attempt_key: str = "run",
        applicable: bool = True,
    ) -> ExecutionTelemetry:
        if occurred_at.tzinfo is None:
            raise ValueError("Execution telemetry timestamps must be timezone-aware.")
        matching = tuple(
            item
            for item in self.events
            if item.component_id == component_id and item.attempt_key == attempt_key
        )
        phases = tuple(ExecutionPhase)
        if len(matching) >= len(phases):
            raise ValueError("Execution telemetry attempt is already complete.")
        expected = phases[len(matching)]
        if phase is not expected:
            raise ValueError("Execution telemetry phase order is invalid.")
        timestamp = occurred_at.astimezone(timezone.utc)
        if matching:
            previous = datetime.fromisoformat(matching[-1].occurred_at)
            if timestamp < previous:
                raise ValueError("Execution telemetry timestamps must be monotonic.")
            first = datetime.fromisoformat(matching[0].occurred_at)
            elapsed_ms = round((timestamp - first).total_seconds() * 1000)
        else:
            elapsed_ms = 0
        return ExecutionTelemetry(
            events=(
                *self.events,
                ExecutionPhaseEvent(
                    phase,
                    timestamp.isoformat(),
                    elapsed_ms,
                    component_id,
                    attempt_key,
                    applicable,
                ),
            )
        )

    def has_phase(
        self,
        component_id: str,
        attempt_key: str,
        phase: ExecutionPhase,
    ) -> bool:
        return any(
            item.component_id == component_id
            and item.attempt_key == attempt_key
            and item.phase is phase
            for item in self.events
        )


def execution_profile_for_issue(
    profiles: Iterable[ExecutionProfile],
    *,
    acceptance_count: int,
    rework_count: int,
) -> ExecutionProfile:
    available = {item.profile_id: item for item in profiles}
    if acceptance_count <= 3 and rework_count == 0 and ExecutionProfileId.LIGHTWEIGHT in available:
        return available[ExecutionProfileId.LIGHTWEIGHT]
    try:
        return available[ExecutionProfileId.FULL]
    except KeyError:
        raise ValueError("The component does not provide a full execution profile.") from None


def locked_execution_profile(
    profiles: Iterable[ExecutionProfile],
    component_id: str,
    fallback: ExecutionProfile,
) -> ExecutionProfile:
    matches = tuple(item for item in profiles if item.component_id == component_id)
    if len(matches) > 1:
        raise ValueError("Workflow Run has multiple execution profiles for one component.")
    return fallback if not matches else matches[0]


def execution_profile_payload(profile: ExecutionProfile) -> Mapping[str, object]:
    return {
        "schema": profile.schema,
        "version": profile.version,
        "component_id": profile.component_id,
        "profile_id": profile.profile_id.value,
        "model": profile.model,
        "reasoning_effort": profile.reasoning_effort,
        "budget": {
            "version": profile.budget.version,
            "timeout_seconds": profile.budget.timeout_seconds,
            "checkpoint_seconds": profile.budget.checkpoint_seconds,
        },
    }


def execution_profile_from_payload(payload: Mapping[str, object]) -> ExecutionProfile:
    budget = payload.get("budget")
    if not isinstance(budget, Mapping):
        raise ValueError("Execution profile budget is invalid.")
    return ExecutionProfile(
        str(payload.get("schema", "")),
        str(payload.get("version", "")),
        str(payload.get("component_id", "")),
        ExecutionProfileId(str(payload.get("profile_id", ""))),
        str(payload.get("model", "")),
        str(payload.get("reasoning_effort", "")),
        ExecutionBudget(
            str(budget.get("version", "")),
            _number(budget.get("timeout_seconds")),
            _number(budget.get("checkpoint_seconds")),
        ),
    )


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Execution budget value is invalid.")
    return float(value)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("Execution telemetry timestamp is invalid.") from None
    if parsed.tzinfo is None:
        raise ValueError("Execution telemetry timestamp must be timezone-aware.")
    return parsed.astimezone(timezone.utc)


def _canonical_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
