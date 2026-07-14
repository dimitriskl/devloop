from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from devloop.analysis.package import parse_analysis_draft
from devloop.components.analysis import ANALYSIS_COMPONENT_ID, builtin_component_registry
from devloop.domain.development import (
    ArtifactRef,
    ChangeKind,
    IssueRuntimeState,
    IssueStatus,
    WorkspaceBaselineEntry,
)
from devloop.domain.identifiers import (
    AttemptId,
    ExecutionThreadId,
    ExecutionTurnId,
    FeatureSlug,
    IssueId,
    StepInstanceId,
    WorkflowRunId,
)
from devloop.domain.outcomes import StepOutcome
from devloop.domain.review_qa import QaCursor, ReviewCursor
from devloop.domain.run import (
    AnalysisCursor,
    ComponentLock,
    OperationState,
    OperationStatus,
    ResolvedWorkflow,
    RunEventType,
    StepRunStatus,
    WorkflowRunSnapshot,
    WorkflowRunStatus,
)
from devloop.domain.scheduler import AttemptStatus, IssueAttemptRecord
from devloop.infrastructure.paths import ANALYSIS_DRAFT_FILENAME
from devloop.persistence.run_store import (
    MAX_PERSISTED_TEXT_LENGTH,
    RUN_EVENT_SCHEMA,
    RUN_SNAPSHOT_SCHEMA,
    RunLeaseError,
    RunStore,
    RunStoreError,
    new_run_lease,
    snapshot_from_dict,
    snapshot_to_dict,
)
from devloop.workflow.definition import load_standard_workflow

RUN_ID = WorkflowRunId("run-20260710t120001-123456abcdef")
OTHER_RUN_ID = WorkflowRunId("run-20260710t120002-123456abcdef")


def snapshot(repository: Path) -> WorkflowRunSnapshot:
    workflow = load_standard_workflow()
    manifest, _ = builtin_component_registry().resolve(ANALYSIS_COMPONENT_ID)
    return WorkflowRunSnapshot(
        schema=RUN_SNAPSHOT_SCHEMA,
        run_id=RUN_ID,
        repository=str(repository),
        feature_title="Feature",
        feature_slug=FeatureSlug("feature"),
        workflow=ResolvedWorkflow(
            workflow.workflow_id,
            workflow.version,
            workflow.definition_hash,
        ),
        component_locks=(
            ComponentLock(
                manifest.component_id,
                manifest.version,
                manifest.distribution,
                manifest.package_hash,
            ),
        ),
        active_step=StepInstanceId("analysis"),
        run_status=WorkflowRunStatus.CREATED,
        step_status=StepRunStatus.NOT_STARTED,
        outcome=None,
        analysis=AnalysisCursor(),
        lease=new_run_lease(),
        event_sequence=0,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


def test_event_replay_recovers_newer_state_and_quarantines_partial_final_event(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(snapshot(tmp_path))
    paused = store.record(
        replace(created, run_status=WorkflowRunStatus.PAUSED),
        RunEventType.RUN_PAUSED,
    )
    run_directory = store.run_directory(RUN_ID)
    (run_directory / "snapshot.json").write_text(
        json.dumps(snapshot_to_dict(created)),
        encoding="utf-8",
    )
    with (run_directory / "events.jsonl").open("ab") as stream:
        stream.write(b'{"schema":"devloop.run-event/v1"')

    recovered = store.load(RUN_ID)

    assert recovered == paused
    assert recovered.event_sequence == 2
    assert list((run_directory / "quarantine").glob("rejected-event-*.json"))
    assert store.list_unfinished() == (paused,)


def test_event_replay_quarantines_a_structurally_partial_final_event(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(snapshot(tmp_path))
    paused = store.record(
        replace(created, run_status=WorkflowRunStatus.PAUSED),
        RunEventType.RUN_PAUSED,
    )
    run_directory = store.run_directory(RUN_ID)
    with (run_directory / "events.jsonl").open("ab") as stream:
        stream.write(b'{"schema":"devloop.run-event/v1"}\n')

    recovered = store.load(RUN_ID)

    assert recovered == paused
    assert list((run_directory / "quarantine").glob("rejected-event-*.json"))
    diagnostics = list((run_directory / "quarantine").glob("diagnostic-*.json"))
    assert len(diagnostics) == 1
    diagnostic = json.loads(diagnostics[0].read_text(encoding="utf-8"))
    assert diagnostic["code"] == "CORRUPT_FINAL_EVENT_QUARANTINED"
    assert "earlier valid state remains usable" in diagnostic["action"]


def test_event_quarantine_never_persists_the_rejected_binary_payload(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(snapshot(tmp_path))
    paused = store.record(
        replace(created, run_status=WorkflowRunStatus.PAUSED),
        RunEventType.RUN_PAUSED,
    )
    rejected = b"token=quarantine-secret\xff"
    run_directory = store.run_directory(RUN_ID)
    with (run_directory / "events.jsonl").open("ab") as stream:
        stream.write(rejected)

    recovered = store.load(RUN_ID)

    assert recovered == paused
    records = list((run_directory / "quarantine").glob("rejected-event-*.json"))
    assert len(records) == 1
    metadata = json.loads(records[0].read_text(encoding="utf-8"))
    assert metadata["byte_count"] == len(rejected)
    assert metadata["sha256"] == hashlib.sha256(rejected).hexdigest()
    quarantine_bytes = b"".join(
        path.read_bytes() for path in (run_directory / "quarantine").iterdir()
    )
    assert b"quarantine-secret" not in quarantine_bytes
    quarantine_bytes.decode("utf-8")


def test_event_replay_quarantines_a_final_event_with_invalid_checkpoint_state(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(snapshot(tmp_path))
    paused = store.record(
        replace(created, run_status=WorkflowRunStatus.PAUSED),
        RunEventType.RUN_PAUSED,
    )
    invalid_state = snapshot_to_dict(replace(paused, event_sequence=3))
    analysis = invalid_state["analysis"]
    assert isinstance(analysis, dict)
    analysis["completed_item_ids"] = "not-a-list"
    invalid_event = {
        "schema": RUN_EVENT_SCHEMA,
        "sequence": 3,
        "type": RunEventType.RUN_RESUMED.value,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "state": invalid_state,
    }
    run_directory = store.run_directory(RUN_ID)
    with (run_directory / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(invalid_event) + "\n")

    recovered = store.load(RUN_ID)

    assert recovered == paused
    assert list((run_directory / "quarantine").glob("rejected-event-*.json"))


def test_event_replay_recovers_when_snapshot_belongs_to_another_run(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(snapshot(tmp_path))
    foreign_snapshot = replace(created, run_id=OTHER_RUN_ID)
    snapshot_path = store.run_directory(RUN_ID) / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(snapshot_to_dict(foreign_snapshot)),
        encoding="utf-8",
    )

    recovered = store.load(RUN_ID)

    assert recovered == created
    assert recovered.run_id == RUN_ID


def test_event_replay_recovers_when_the_atomic_snapshot_is_truncated(tmp_path: Path) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(snapshot(tmp_path))
    paused = store.record(
        replace(created, run_status=WorkflowRunStatus.PAUSED),
        RunEventType.RUN_PAUSED,
    )
    snapshot_path = store.run_directory(RUN_ID) / "snapshot.json"
    snapshot_path.write_text('{"schema":', encoding="utf-8")

    recovered = store.load(RUN_ID)

    assert recovered == paused


def test_event_replay_quarantines_a_final_event_for_another_run(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(snapshot(tmp_path))
    foreign_state = replace(
        created,
        run_id=OTHER_RUN_ID,
        event_sequence=created.event_sequence + 1,
    )
    foreign_event = {
        "schema": RUN_EVENT_SCHEMA,
        "sequence": foreign_state.event_sequence,
        "type": RunEventType.RUN_PAUSED.value,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "state": snapshot_to_dict(foreign_state),
    }
    run_directory = store.run_directory(RUN_ID)
    with (run_directory / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(foreign_event) + "\n")

    recovered = store.load(RUN_ID)

    assert recovered == created
    assert list((run_directory / "quarantine").glob("rejected-event-*.json"))


def test_persisted_analysis_draft_cannot_be_reowned_by_another_run(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    store.create(snapshot(tmp_path))
    draft_path = store.run_directory(RUN_ID) / ANALYSIS_DRAFT_FILENAME
    draft_path.write_text(
        json.dumps(
            {
                "schema": "devloop.analysis-draft/v1",
                "run_id": OTHER_RUN_ID.value,
                "feature_title": "Foreign draft",
                "feature_slug": "foreign-draft",
                "prd_markdown": "PRD",
                "requirements": [],
                "issues": [],
                "revision": 1,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RunStoreError, match="missing or invalid"):
        store.load_draft(RUN_ID)


def test_analysis_draft_autosave_redacts_every_free_text_field(tmp_path: Path) -> None:
    secret = "draft-secret-value"
    draft = parse_analysis_draft(
        {
            "schema": "devloop.analysis-draft/v1",
            "feature_title": f"Price comparison token={secret}",
            "feature_slug": "price-comparison",
            "prd_markdown": f"# PRD\nAuthorization: Bearer {secret}",
            "requirements": ["REQ-001"],
            "issues": [
                {
                    "id": "ISSUE-001",
                    "slug": "compare-prices",
                    "title": f"Compare prices api_key={secret}",
                    "requirements": ["REQ-001"],
                    "dependencies": [],
                    "acceptance_criteria": [
                        {
                            "id": "AC-ISSUE-001-001",
                            "text": f"The result omits token={secret}",
                        }
                    ],
                    "markdown": f"# Issue\nBearer {secret}",
                }
            ],
            "revision": 1,
        },
        RUN_ID,
    )
    store = RunStore(tmp_path / ".devloop" / "runs")
    store.create(snapshot(tmp_path))

    store.save_draft(draft)

    draft_path = store.run_directory(RUN_ID) / ANALYSIS_DRAFT_FILENAME
    assert secret not in draft_path.read_text(encoding="utf-8")
    persisted = store.load_draft(RUN_ID)
    assert "token=[redacted]" in persisted.feature_title
    assert "Bearer [redacted]" in persisted.prd_markdown
    assert "api_key=[redacted]" in persisted.issues[0].title
    assert "token=[redacted]" in persisted.issues[0].acceptance_criteria[0].text
    assert "Bearer [redacted]" in persisted.issues[0].markdown


def test_stale_run_lease_cannot_append_an_event(tmp_path: Path) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(snapshot(tmp_path))
    store.take_lease(created)

    with pytest.raises(RunLeaseError, match="lease changed"):
        store.record(
            replace(created, run_status=WorkflowRunStatus.PAUSED),
            RunEventType.RUN_PAUSED,
        )


def test_stale_run_lease_cannot_write_an_artifact(tmp_path: Path) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(snapshot(tmp_path))
    stale_store = RunStore(store.run_root)
    stale_store.validate_lease(created)
    replacement = store.take_lease(created)

    with pytest.raises(RunLeaseError, match="lease"):
        stale_store.save_json_artifact(
            created.run_id,
            Path("results/stale.json"),
            {"schema": "test/v1"},
        )

    assert replacement.lease != created.lease
    assert not (store.run_directory(created.run_id) / "results/stale.json").exists()


def test_multiprocess_stale_lease_recovery_has_exactly_one_winner(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(snapshot(tmp_path))
    lease_path = store.run_directory(created.run_id) / "lease.json"
    stale_lease = json.loads(lease_path.read_text(encoding="utf-8"))
    stale_lease["process_id"] = 2_147_483_647
    lease_path.write_text(json.dumps(stale_lease), encoding="utf-8")
    coordination = tmp_path / "claim-coordination"
    coordination.mkdir()
    helper = tmp_path / "claim_lease.py"
    helper.write_text(
        """
import sys
import time
from pathlib import Path

from devloop.domain.identifiers import WorkflowRunId
from devloop.persistence.run_store import RunLeaseError, RunStore

run_root = Path(sys.argv[1])
run_id = WorkflowRunId(sys.argv[2])
coordination = Path(sys.argv[3])
contender = sys.argv[4]
store = RunStore(run_root)
snapshot = store.load(run_id)
(coordination / f\"ready-{contender}\").write_text(\"ready\\n\", encoding=\"ascii\")
while not (coordination / \"go\").exists():
    time.sleep(0.001)
try:
    claimed = store.take_lease(snapshot)
except RunLeaseError:
    result = \"LEASED\"
else:
    result = f\"CLAIMED:{claimed.lease.lease_id}\"
(coordination / f\"result-{contender}\").write_text(result, encoding=\"ascii\")
if result.startswith(\"CLAIMED:\"):
    while not (coordination / \"release\").exists():
        time.sleep(0.001)
""".lstrip(),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path.cwd() / "src")
    contenders = [
        subprocess.Popen(
            [
                sys.executable,
                str(helper),
                str(store.run_root),
                created.run_id.value,
                str(coordination),
                str(index),
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(8)
    ]
    try:
        deadline = time.monotonic() + 10.0
        while len(list(coordination.glob("ready-*"))) != len(contenders):
            if time.monotonic() >= deadline:
                raise AssertionError("Lease contenders did not reach the claim barrier.")
            time.sleep(0.01)
        (coordination / "go").write_text("go\n", encoding="ascii")
        while len(list(coordination.glob("result-*"))) != len(contenders):
            if time.monotonic() >= deadline:
                raise AssertionError("Lease contenders did not report claim results.")
            time.sleep(0.01)
        results = [
            path.read_text(encoding="ascii")
            for path in sorted(coordination.glob("result-*"))
        ]
        (coordination / "release").write_text("release\n", encoding="ascii")
        outputs = [process.communicate(timeout=5.0) for process in contenders]
    finally:
        (coordination / "release").write_text("release\n", encoding="ascii")
        for process in contenders:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5.0)

    assert len([result for result in results if result.startswith("CLAIMED:")]) == 1
    assert results.count("LEASED") == len(contenders) - 1
    assert all(process.returncode == 0 for process in contenders), outputs


def test_stale_running_lease_is_presented_as_paused_with_unknown_operation(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(
        replace(
            snapshot(tmp_path),
            run_status=WorkflowRunStatus.RUNNING,
            step_status=StepRunStatus.RUNNING,
            operation=OperationState("command-item", OperationStatus.RUNNING),
        )
    )
    lease_path = store.run_directory(RUN_ID) / "lease.json"
    lease = json.loads(lease_path.read_text(encoding="utf-8"))
    lease["process_id"] = 2_147_483_647
    lease_path.write_text(json.dumps(lease), encoding="utf-8")

    recovered = store.load(created.run_id)

    assert recovered.run_status is WorkflowRunStatus.PAUSED
    assert recovered.operation == OperationState("command-item", OperationStatus.UNKNOWN)


def test_pausing_a_running_operation_persists_it_as_unknown(tmp_path: Path) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(
        replace(
            snapshot(tmp_path),
            run_status=WorkflowRunStatus.RUNNING,
            step_status=StepRunStatus.RUNNING,
            operation=OperationState("command-item", OperationStatus.RUNNING),
        )
    )

    paused = store.record(
        replace(created, run_status=WorkflowRunStatus.PAUSED),
        RunEventType.RUN_PAUSED,
    )

    assert paused.operation == OperationState("command-item", OperationStatus.UNKNOWN)
    assert store.load(RUN_ID).operation == paused.operation


def test_loading_a_non_running_snapshot_with_a_running_operation_fails_closed(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(snapshot(tmp_path))
    invalid = replace(
        created,
        run_status=WorkflowRunStatus.PAUSED,
        operation=OperationState("command-item", OperationStatus.RUNNING),
    )
    snapshot_path = store.run_directory(RUN_ID) / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot_to_dict(invalid)), encoding="utf-8")

    with pytest.raises(RunStoreError, match="non-running.*RUNNING operation"):
        store.load(RUN_ID)


def test_stale_event_sequence_cannot_append_a_duplicate_event(tmp_path: Path) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(snapshot(tmp_path))
    store.record(
        replace(created, run_status=WorkflowRunStatus.PAUSED),
        RunEventType.RUN_PAUSED,
    )

    with pytest.raises(RunStoreError, match="stale"):
        store.record(created, RunEventType.RUN_RESUMED)


def test_event_replay_recovers_the_flushed_event_when_snapshot_replacement_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(snapshot(tmp_path))

    def fail_snapshot_replace(path: Path, payload: object) -> None:
        raise OSError("injected snapshot replacement failure")

    monkeypatch.setattr(
        "devloop.persistence.run_store._atomic_json",
        fail_snapshot_replace,
    )

    with pytest.raises(RunStoreError, match="persist"):
        store.record(
            replace(created, run_status=WorkflowRunStatus.PAUSED),
            RunEventType.RUN_PAUSED,
        )

    recovered = store.load(RUN_ID)
    assert recovered.run_status is WorkflowRunStatus.PAUSED
    assert recovered.event_sequence == created.event_sequence + 1


@st.composite
def _event_histories(
    draw: st.DrawFn,
) -> tuple[list[WorkflowRunStatus], int, bool]:
    statuses = draw(
        st.lists(
            st.sampled_from(
                [WorkflowRunStatus.PAUSED, WorkflowRunStatus.AWAITING_USER]
            ),
            min_size=1,
            max_size=8,
        )
    )
    snapshot_event_index = draw(st.integers(min_value=0, max_value=len(statuses)))
    return statuses, snapshot_event_index, draw(st.booleans())


@given(history=_event_histories())
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_event_replay_is_idempotent_at_every_snapshot_and_final_event_boundary(
    tmp_path: Path,
    history: tuple[list[WorkflowRunStatus], int, bool],
) -> None:
    statuses, snapshot_event_index, corrupt_final_event = history
    run_id = WorkflowRunId(f"run-20260712t120000-{uuid.uuid4().hex[:12]}")
    store = RunStore(tmp_path / ".devloop" / "property-runs")
    current = store.create(replace(snapshot(tmp_path), run_id=run_id))
    for status in statuses:
        current = store.record(
            replace(current, run_status=status),
            RunEventType.RUN_PAUSED
            if status is WorkflowRunStatus.PAUSED
            else RunEventType.RUN_RESUMED,
        )
    events_path = store.run_directory(run_id) / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    snapshot_path = store.run_directory(run_id) / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(events[snapshot_event_index]["state"]),
        encoding="utf-8",
    )
    if corrupt_final_event:
        with events_path.open("ab") as stream:
            stream.write(b'{"schema":"devloop.run-event/v1"')

    first = store.load(run_id)
    second = store.load(run_id)

    assert first == current
    assert second == first


@given(
    terminal_status=st.sampled_from(
        [
            WorkflowRunStatus.COMPLETED,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        ]
    )
)
@settings(
    max_examples=3,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_terminal_workflow_run_is_immutable(
    tmp_path: Path,
    terminal_status: WorkflowRunStatus,
) -> None:
    run_id = WorkflowRunId(f"run-20260712t130000-{uuid.uuid4().hex[:12]}")
    store = RunStore(tmp_path / ".devloop" / "terminal-runs")
    created = store.create(replace(snapshot(tmp_path), run_id=run_id))
    terminal = store.record(
        replace(created, run_status=terminal_status),
        RunEventType.RUN_COMPLETED,
    )

    with pytest.raises(RunStoreError, match="terminal"):
        store.record(terminal, RunEventType.RUN_RESUMED)


def test_run_registry_keeps_completed_runs_discoverable(tmp_path: Path) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    created = store.create(snapshot(tmp_path))
    completed = store.record(
        replace(created, run_status=WorkflowRunStatus.COMPLETED),
        RunEventType.RUN_COMPLETED,
    )

    assert store.list_unfinished() == ()
    assert store.list_runs() == (completed,)


def test_run_storage_ignore_keeps_project_workflows_trackable(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "-C", str(tmp_path), "init", "--quiet"],
        check=True,
        capture_output=True,
        text=True,
    )
    workflow_definition = tmp_path / ".devloop" / "workflows" / "project.json"
    workflow_definition.parent.mkdir(parents=True)
    workflow_definition.write_text("{}\n", encoding="utf-8")
    store = RunStore(tmp_path / ".devloop" / "runs")

    store.create(snapshot(tmp_path))

    run_snapshot = store.run_directory(RUN_ID) / "snapshot.json"
    assert (store.run_root / ".gitignore").read_text(encoding="ascii") == "*\n"
    assert not (store.run_root.parent / ".gitignore").exists()
    assert _git_check_ignore(tmp_path, run_snapshot) == 0
    assert _git_check_ignore(tmp_path, workflow_definition) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL behavior is Windows-only.")
def test_run_storage_is_current_user_protected_on_windows(tmp_path: Path) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")

    store.create(snapshot(tmp_path))

    identity = subprocess.run(
        ["whoami"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    acl = subprocess.run(
        ["icacls", str(store.run_root)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert identity.casefold() in acl.casefold()
    assert "(OI)(CI)(F)" in acl
    assert "(I)" not in acl


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL behavior is Windows-only.")
def test_recreated_windows_run_storage_is_protected_again(tmp_path: Path) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    store.create(snapshot(tmp_path))
    shutil.rmtree(store.run_root)
    replacement = replace(
        snapshot(tmp_path),
        run_id=OTHER_RUN_ID,
        lease=new_run_lease(),
    )

    store.create(replacement)

    identity = subprocess.run(
        ["whoami"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    acl = subprocess.run(
        ["icacls", str(store.run_root)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert identity.casefold() in acl.casefold()
    assert "(OI)(CI)(F)" in acl
    assert "(I)" not in acl


def test_run_artifacts_reject_disallowed_persistence_fields(tmp_path: Path) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    store.create(snapshot(tmp_path))

    with pytest.raises(RunStoreError, match="disallowed field"):
        store.save_json_artifact(
            RUN_ID,
            Path("qa-results/result.json"),
            {"schema": "devloop.qa-result/v1", "full_transcript": "private conversation"},
        )

    with pytest.raises(RunStoreError, match="binary"):
        store.save_json_artifact(
            RUN_ID,
            Path("qa-results/result.json"),
            {"schema": "devloop.qa-result/v1", "evidence": b"binary output"},
        )


def test_run_artifacts_reject_fields_outside_the_approved_allowlist(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    store.create(snapshot(tmp_path))

    with pytest.raises(RunStoreError, match="approved persistence allowlist"):
        store.save_json_artifact(
            RUN_ID,
            Path("qa-results/result.json"),
            {
                "schema": "devloop.qa-result/v1",
                "checks": [{"id": "QA-001", "unapproved_payload": "must not persist"}],
            },
        )


@pytest.mark.parametrize(
    "artifact_path",
    [
        Path("../escape.json"),
        Path("/rooted.json"),
        Path("C:drive-relative.json"),
        Path("C:/anchored.json"),
        Path(r"C:\anchored.json"),
        Path("//server/share/anchored.json"),
        Path(r"\\server\share\anchored.json"),
    ],
)
def test_run_artifacts_reject_escaping_and_anchored_paths(
    tmp_path: Path,
    artifact_path: Path,
) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    store.create(snapshot(tmp_path))

    with pytest.raises(RunStoreError, match="path"):
        store.save_json_artifact(RUN_ID, artifact_path, {"schema": "test/v1"})


def test_run_artifacts_reject_a_resolved_link_outside_the_run_and_repository(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    store.create(snapshot(tmp_path))
    outside = tmp_path.parent / f"outside-{uuid.uuid4().hex}"
    outside.mkdir()
    link = store.run_directory(RUN_ID) / "linked"
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                pytest.skip("Windows junction creation is unavailable.")
        else:
            link.symlink_to(outside, target_is_directory=True)

        with pytest.raises(RunStoreError, match="path"):
            store.save_json_artifact(
                RUN_ID,
                Path("linked/result.json"),
                {"schema": "test/v1"},
            )
        assert not (outside / "result.json").exists()
    finally:
        (outside / "result.json").unlink(missing_ok=True)
        if link.exists():
            if os.name == "nt":
                link.rmdir()
            else:
                link.unlink()
        outside.rmdir()


def test_run_artifacts_redact_secrets_and_bound_persisted_text(tmp_path: Path) -> None:
    store = RunStore(tmp_path / ".devloop" / "runs")
    store.create(snapshot(tmp_path))

    artifact = store.save_json_artifact(
        RUN_ID,
        Path("qa-results/result.json"),
        {
            "schema": "devloop.qa-result/v1",
            "evidence": "token=super-secret " + ("x" * 30_000),
        },
    )
    payload = store.load_json_artifact(RUN_ID, artifact)

    evidence = payload["evidence"]
    assert isinstance(evidence, str)
    assert "super-secret" not in evidence
    assert "token=[redacted]" in evidence
    assert len(evidence) <= 20_000


def test_run_snapshot_redacts_and_bounds_free_text(tmp_path: Path) -> None:
    secret = "snapshot-secret-value"
    unsafe = replace(
        snapshot(tmp_path),
        feature_title=f"Build the feature with api_key={secret}",
        analysis=AnalysisCursor(
            clarification=f"Authorization: Bearer {secret} "
            + ("x" * (MAX_PERSISTED_TEXT_LENGTH + 100)),
        ),
    )
    store = RunStore(tmp_path / ".devloop" / "runs")

    store.create(unsafe)

    run_directory = store.run_directory(RUN_ID)
    persisted = json.loads((run_directory / "snapshot.json").read_text(encoding="utf-8"))
    event_text = (run_directory / "events.jsonl").read_text(encoding="utf-8")
    feature_title = persisted["feature_title"]
    clarification = persisted["analysis"]["clarification"]
    assert secret not in json.dumps(persisted)
    assert secret not in event_text
    assert "api_key=[redacted]" in feature_title
    assert "Bearer [redacted]" in clarification
    assert len(feature_title) <= MAX_PERSISTED_TEXT_LENGTH
    assert len(clarification) <= MAX_PERSISTED_TEXT_LENGTH


def _git_check_ignore(repository: Path, path: Path) -> int:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.excludesFile=",
            "-C",
            str(repository),
            "check-ignore",
            "--quiet",
            "--no-index",
            str(path.relative_to(repository)),
        ],
        check=False,
        capture_output=True,
        text=True,
    ).returncode


def test_review_and_qa_cursors_round_trip_in_the_run_checkpoint(tmp_path: Path) -> None:
    artifact = ArtifactRef("inputs/attempt-001.json", "input-hash")
    review = ReviewCursor(
        IssueId("ISSUE-001"),
        AttemptId("attempt-001"),
        artifact,
        ExecutionThreadId("review-thread"),
        ExecutionTurnId("review-turn"),
        ("review-item",),
        ArtifactRef("review-results/attempt-001.json", "review-hash"),
        transient_retries=1,
    )
    qa = QaCursor(
        IssueId("ISSUE-001"),
        AttemptId("attempt-001"),
        artifact,
        ExecutionThreadId("qa-thread"),
        ExecutionTurnId("qa-turn"),
        ("qa-item",),
        ArtifactRef("qa-results/attempt-001.json", "qa-hash"),
        transient_retries=1,
    )
    attempt = IssueAttemptRecord(
        IssueId("ISSUE-001"),
        1,
        AttemptStatus.COMPLETED,
        StepOutcome.SUCCEEDED,
        ArtifactRef("implementation-results/attempt-001.json", "implementation-hash"),
        review.review_result,
        qa.qa_result,
        None,
        ExecutionThreadId("development-thread"),
        review.thread_id,
        qa.thread_id,
    )
    original = replace(snapshot(tmp_path), review=review, qa=qa, attempts=(attempt,))

    restored = snapshot_from_dict(snapshot_to_dict(original))

    assert restored.review == review
    assert restored.qa == qa
    assert restored.review.thread_id != restored.qa.thread_id
    assert restored.attempts == (attempt,)


def test_issue_baseline_and_terminal_step_round_trip_in_checkpoint(tmp_path: Path) -> None:
    issue = IssueRuntimeState(
        IssueId("ISSUE-001"),
        IssueStatus.BLOCKED,
        StepInstanceId("code-review"),
        (
            WorkspaceBaselineEntry(
                "existing.py",
                ChangeKind.MODIFIED,
                "a" * 64,
            ),
        ),
        ("greeting.py",),
    )
    original = replace(snapshot(tmp_path), issues=(issue,))

    restored = snapshot_from_dict(snapshot_to_dict(original))

    assert restored.issues == (issue,)
