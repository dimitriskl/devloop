from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol

from .execution_backend_id import parse_execution_backend_id
from .portable_sessions import (
    PortableSessionLaunch,
    PortableSessionStatus,
    PortableWorktreeLease,
    PortableWorktreeLeaseConflict,
    PortableWorkflowOperation,
)
from .portable_workflow import ExecutionBudget, FastPreference, StepExecutionSettings
from .redaction import redact_persisted_evidence

CATALOG_SCHEMA_VERSION = 3
CATALOG_FILENAME = "portable-sessions.sqlite3"
DEFAULT_PORTABLE_SESSION_CONCURRENCY_LIMIT = 2
MINIMUM_PORTABLE_SESSION_CONCURRENCY_LIMIT = 1
MAXIMUM_PORTABLE_SESSION_CONCURRENCY_LIMIT = 64
_CONCURRENCY_LIMIT_SETTING_KEY = "session_concurrency_limit"
PORTABLE_SESSION_CATALOG_ENV = "DEVLOOP_PORTABLE_SESSION_CATALOG"
PORTABLE_SESSION_ID_ENV = "DEVLOOP_PORTABLE_SESSION_ID"
PORTABLE_SESSION_OWNER_ID_ENV = "DEVLOOP_PORTABLE_SESSION_OWNER_ID"
PORTABLE_SESSION_RESTORE_ENV = "DEVLOOP_PORTABLE_SESSION_RESTORE"
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_APPROVAL_POLICIES = frozenset({"never", "on-request", "untrusted", "on-failure"})
_SANDBOX_MODES = frozenset({"read-only", "workspace-write", "danger-full-access"})


class PortableSessionCatalogError(RuntimeError):
    """Raised when the machine catalog cannot be read safely."""


class _UnsupportedPortablePlanningSetting(ValueError):
    """Raised when a planning snapshot contains a value outside a closed set."""


@dataclass(frozen=True)
class PortablePlanningSettings:
    backend: str
    model: str
    reasoning_effort: str
    fast: str
    timeout_seconds: float
    checkpoint_seconds: float

    def __post_init__(self) -> None:
        for field_name, value in (
            ("backend", self.backend),
            ("model", self.model),
            ("reasoning effort", self.reasoning_effort),
            ("fast preference", self.fast),
        ):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 200
                or "\n" in value
                or "\r" in value
                or redact_persisted_evidence(value) != value
            ):
                raise ValueError(
                    f"Portable planning {field_name} must be bounded secret-free text."
                )
        try:
            backend = parse_execution_backend_id(self.backend)
        except ValueError as error:
            raise _UnsupportedPortablePlanningSetting(str(error)) from error
        if backend.value != self.backend:
            raise _UnsupportedPortablePlanningSetting(
                f"Unsupported Execution Backend {self.backend!r}; "
                f"expected {backend.value!r}."
            )
        try:
            fast = FastPreference(self.fast)
        except ValueError as error:
            supported = ", ".join(member.value for member in FastPreference)
            raise _UnsupportedPortablePlanningSetting(
                f"Unsupported Fast preference {self.fast!r}; "
                f"expected one of {supported}."
            ) from error
        StepExecutionSettings(
            backend=backend,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            fast=fast,
        )
        ExecutionBudget(self.timeout_seconds, self.checkpoint_seconds)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PortablePlanningSettings:
        expected_keys = {
            "backend",
            "model",
            "reasoning_effort",
            "fast",
            "timeout_seconds",
            "checkpoint_seconds",
        }
        if set(value) != expected_keys:
            raise PortableSessionCatalogError(
                "Portable planning settings are corrupt."
            )
        try:
            return cls(
                backend=_required_text(value, "backend"),
                model=_required_text(value, "model"),
                reasoning_effort=_required_text(value, "reasoning_effort"),
                fast=_required_text(value, "fast"),
                timeout_seconds=_required_number(value, "timeout_seconds"),
                checkpoint_seconds=_required_number(value, "checkpoint_seconds"),
            )
        except _UnsupportedPortablePlanningSetting as error:
            raise PortableSessionCatalogError(
                f"Portable planning settings are corrupt: {error}"
            ) from error
        except (OverflowError, TypeError, ValueError) as error:
            raise PortableSessionCatalogError(
                "Portable planning settings are corrupt."
            ) from error


@dataclass(frozen=True)
class PortableLaunchSettings:
    codex_launcher: str = "codex"
    sandbox: str = "workspace-write"
    approval_policy: str = "never"
    native_editor: bool = False
    delivery_prd_path: str | None = None
    delivery_issues_path: str | None = None
    delivery_options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_bounded_secret_free_text(
            self.codex_launcher,
            field_name="Codex launcher",
            maximum_length=1024,
        )
        if self.sandbox not in _SANDBOX_MODES:
            raise ValueError("Portable launch sandbox mode is unsupported.")
        if self.approval_policy not in _APPROVAL_POLICIES:
            raise ValueError("Portable launch approval policy is unsupported.")
        if not isinstance(self.native_editor, bool):
            raise ValueError("Portable native-editor setting must be boolean.")
        if (self.delivery_prd_path is None) != (
            self.delivery_issues_path is None
        ):
            raise ValueError(
                "Portable delivery launch context requires both PRD and issues paths."
            )
        for field_name, path_text in (
            ("delivery PRD path", self.delivery_prd_path),
            ("delivery issues path", self.delivery_issues_path),
        ):
            if path_text is not None:
                _validate_bounded_secret_free_text(
                    path_text,
                    field_name=field_name,
                    maximum_length=4096,
                )
                if not Path(path_text).is_absolute():
                    raise ValueError(
                        f"Portable {field_name} must be an absolute path."
                    )
        if not isinstance(self.delivery_options, tuple):
            raise ValueError("Portable delivery options must be an immutable sequence.")
        for option in self.delivery_options:
            _validate_bounded_secret_free_text(
                option,
                field_name="delivery option",
                maximum_length=1024,
            )
        if sum(len(option) for option in self.delivery_options) > 2048:
            raise ValueError("Portable delivery options are oversized.")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_arguments(
        self,
        *,
        checkout: Path,
        prd_path: Path | None,
        issues_index_path: Path | None,
        operation: PortableWorkflowOperation,
    ) -> tuple[str, ...]:
        if operation is PortableWorkflowOperation.DELIVERY:
            delivery_prd = prd_path or (
                Path(self.delivery_prd_path)
                if self.delivery_prd_path is not None
                else None
            )
            delivery_issues = issues_index_path or (
                Path(self.delivery_issues_path)
                if self.delivery_issues_path is not None
                else None
            )
            if delivery_prd is None or delivery_issues is None:
                raise PortableSessionCatalogError(
                    "Portable delivery launch context is incomplete."
                )
            arguments: list[str] = [
                "--prd",
                str(delivery_prd),
                "--issues",
                str(delivery_issues),
                *self.delivery_options,
            ]
        else:
            arguments = [
                "--prd" if prd_path is not None else "--repo",
                str(prd_path if prd_path is not None else checkout),
            ]
        if self.codex_launcher != "codex":
            arguments.extend(("--codex", self.codex_launcher))
        if self.sandbox != "workspace-write":
            arguments.extend(("--sandbox", self.sandbox))
        if self.approval_policy != "never":
            arguments.extend(("--approval-policy", self.approval_policy))
        if self.native_editor:
            arguments.append("--native-editor")
        return tuple(arguments)

    @classmethod
    def from_arguments(
        cls,
        arguments: Iterable[str],
        *,
        operation: PortableWorkflowOperation,
        checkout: Path,
    ) -> PortableLaunchSettings:
        values = tuple(arguments)
        selected: dict[str, object] = {}
        delivery_prd_path: Path | None = None
        delivery_issues_path: Path | None = None
        delivery_options: list[str] = []
        index = 0
        value_options = {
            "--codex": "codex_launcher",
            "--sandbox": "sandbox",
            "--approval-policy": "approval_policy",
        }
        delivery_value_options = {
            "--preset",
            "--start-issue",
            "--max-passes",
            "--blocked-retry-rounds",
            "--blocked-retry-max-passes",
            "--self-improvement-wiki-path",
            "--self-improvement-max-lessons",
            "--worktree-path",
            "--branch-name",
        }
        delivery_flag_options = {
            "--all",
            "--no-blocked-retry",
            "--dry-run",
            "--plain",
            "--self-improvement-wiki",
            "--no-self-improvement-wiki",
            "--create-worktree",
            "--no-worktree",
            "--non-interactive",
        }
        ignored_value_options = {"--repo", "--goal"}
        while index < len(values):
            argument = values[index]
            if argument in value_options:
                if index + 1 >= len(values):
                    raise ValueError(f"Portable launch option {argument} has no value.")
                selected[value_options[argument]] = values[index + 1]
                index += 2
                continue
            matching = next(
                (
                    (option, field_name)
                    for option, field_name in value_options.items()
                    if argument.startswith(option + "=")
                ),
                None,
            )
            if matching is not None:
                option, field_name = matching
                selected[field_name] = argument[len(option) + 1 :]
                index += 1
                continue
            if operation is PortableWorkflowOperation.DELIVERY:
                path_option = next(
                    (
                        option
                        for option in ("--prd", "--issues")
                        if argument == option or argument.startswith(option + "=")
                    ),
                    None,
                )
                if path_option is not None:
                    if argument == path_option:
                        if index + 1 >= len(values):
                            raise ValueError(
                                f"Portable launch option {argument} has no value."
                            )
                        path_value = values[index + 1]
                        index += 2
                    else:
                        path_value = argument[len(path_option) + 1 :]
                        index += 1
                    resolved_path = Path(path_value).expanduser()
                    if not resolved_path.is_absolute():
                        resolved_path = checkout / resolved_path
                    if path_option == "--prd":
                        delivery_prd_path = resolved_path.resolve()
                    else:
                        delivery_issues_path = resolved_path.resolve()
                    continue
                delivery_value_option = next(
                    (
                        option
                        for option in delivery_value_options
                        if argument == option or argument.startswith(option + "=")
                    ),
                    None,
                )
                if delivery_value_option is not None:
                    if argument == delivery_value_option:
                        if index + 1 >= len(values):
                            raise ValueError(
                                f"Portable launch option {argument} has no value."
                            )
                        delivery_options.extend(
                            (delivery_value_option, values[index + 1])
                        )
                        index += 2
                    else:
                        delivery_options.extend(
                            (
                                delivery_value_option,
                                argument[len(delivery_value_option) + 1 :],
                            )
                        )
                        index += 1
                    continue
                if argument in delivery_flag_options:
                    delivery_options.append(argument)
                    index += 1
                    continue
            if argument in ignored_value_options:
                index += 2
                continue
            if any(
                argument.startswith(option + "=")
                for option in ignored_value_options
            ):
                index += 1
                continue
            if argument == "--native-editor":
                selected["native_editor"] = True
            index += 1
        if operation is PortableWorkflowOperation.DELIVERY:
            if delivery_prd_path is None or delivery_issues_path is None:
                raise ValueError(
                    "Portable delivery launch requires --prd and --issues."
                )
            selected["delivery_prd_path"] = str(delivery_prd_path)
            selected["delivery_issues_path"] = str(delivery_issues_path)
            selected["delivery_options"] = tuple(delivery_options)
        return cls(**selected)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PortableLaunchSettings:
        legacy_keys = {
            "codex_launcher",
            "sandbox",
            "approval_policy",
            "native_editor",
        }
        expected_keys = legacy_keys | {
            "delivery_prd_path",
            "delivery_issues_path",
            "delivery_options",
        }
        if frozenset(value) not in {
            frozenset(legacy_keys),
            frozenset(expected_keys),
        }:
            raise PortableSessionCatalogError(
                "Portable launch settings are corrupt."
            )
        try:
            delivery_options_value = value.get("delivery_options", ())
            if not isinstance(delivery_options_value, (list, tuple)) or not all(
                isinstance(option, str) for option in delivery_options_value
            ):
                raise TypeError
            delivery_prd_value = value.get("delivery_prd_path")
            delivery_issues_value = value.get("delivery_issues_path")
            if delivery_prd_value is not None and not isinstance(
                delivery_prd_value,
                str,
            ):
                raise TypeError
            if delivery_issues_value is not None and not isinstance(
                delivery_issues_value,
                str,
            ):
                raise TypeError
            return cls(
                codex_launcher=_required_text(value, "codex_launcher"),
                sandbox=_required_text(value, "sandbox"),
                approval_policy=_required_text(value, "approval_policy"),
                native_editor=_required_bool(value, "native_editor"),
                delivery_prd_path=delivery_prd_value,
                delivery_issues_path=delivery_issues_value,
                delivery_options=tuple(delivery_options_value),
            )
        except (TypeError, ValueError) as error:
            raise PortableSessionCatalogError(
                "Portable launch settings are corrupt."
            ) from error


@dataclass(frozen=True)
class PortableSavedProject:
    project_id: str
    checkout: Path
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class PortableCatalogSession:
    session_id: str
    project_id: str
    checkout: Path
    status: PortableSessionStatus
    operation: PortableWorkflowOperation
    launch_settings: PortableLaunchSettings
    planning_thread_id: str | None
    planning_settings: PortablePlanningSettings | None
    prd_path: Path | None
    issues_index_path: Path | None
    activity_summary: str
    created_at: float
    updated_at: float

    @property
    def launch(self) -> PortableSessionLaunch:
        return PortableSessionLaunch(
            session_id=self.session_id,
            checkout=self.checkout,
            operation=self.operation,
            arguments=self.arguments,
        )

    @property
    def arguments(self) -> tuple[str, ...]:
        return self.launch_settings.to_arguments(
            checkout=self.checkout,
            prd_path=self.prd_path,
            issues_index_path=self.issues_index_path,
            operation=self.operation,
        )


@dataclass(frozen=True)
class PortableResumeCandidate:
    candidate_id: str
    project_id: str
    checkout: Path
    prd_path: Path
    issues_index_path: Path
    completed_issues: int
    pending_issues: int
    total_issues: int
    active_issue: str | None
    active_status: str | None
    active_stage: str | None
    updated_at: float


class PortableResumeArtifactsSource(Protocol):
    prd_path: Path
    issues_index: Path


class PortableResumeCandidateSource(Protocol):
    artifacts: PortableResumeArtifactsSource
    completed_issues: int
    pending_issues: int
    total_issues: int
    active_issue: str | None
    active_status: str | None
    active_stage: str | None
    updated_at: float


def portable_session_catalog_path(
    *,
    environment: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    values = os.environ if environment is None else environment
    active_platform = os.name if platform is None else platform
    user_home = Path.home() if home is None else home
    if active_platform == "nt":
        state_root = values.get("LOCALAPPDATA")
        if state_root:
            return Path(state_root) / "DevLoop" / "state" / CATALOG_FILENAME
        return user_home / "AppData" / "Local" / "DevLoop" / "state" / CATALOG_FILENAME
    state_root = values.get("XDG_STATE_HOME")
    if state_root:
        return Path(state_root) / "devloop" / CATALOG_FILENAME
    return user_home / ".local" / "state" / "devloop" / CATALOG_FILENAME


class PortableSessionCatalog:
    """Own machine-local Portable Saved Project and session discovery state."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or portable_session_catalog_path()).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create_session(
        self,
        launch: PortableSessionLaunch,
        planning_settings: PortablePlanningSettings | None = None,
    ) -> PortableCatalogSession:
        _validate_session_id(launch.session_id)
        checkout = launch.checkout.resolve()
        if not checkout.is_dir():
            raise ValueError(f"Portable session checkout does not exist: {checkout}")
        project_id = str(uuid.uuid5(uuid.NAMESPACE_URL, checkout.as_uri()))
        settings_json = (
            _serialize_planning_settings(planning_settings)
            if planning_settings is not None
            else None
        )
        launch_settings = PortableLaunchSettings.from_arguments(
            launch.arguments,
            operation=launch.operation,
            checkout=checkout,
        )
        arguments_json = json.dumps(
            launch_settings.to_dict(),
            separators=(",", ":"),
        )
        timestamp = time.time()
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO saved_projects (
                        project_id, checkout, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(checkout) DO UPDATE SET
                        updated_at = excluded.updated_at
                    """,
                    (project_id, str(checkout), timestamp, timestamp),
                )
                connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, project_id, status, operation, arguments_json,
                        planning_settings_json, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        launch.session_id,
                        project_id,
                        PortableSessionStatus.READY.value,
                        launch.operation.value,
                        arguments_json,
                        settings_json,
                        timestamp,
                        timestamp,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError(
                f"Portable session already exists: {launch.session_id}"
            ) from error
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error
        return self.get_session(launch.session_id)

    def create_session_with_lease(
        self,
        launch: PortableSessionLaunch,
        *,
        owner_id: str,
        process_id: int | None = None,
        planning_settings: PortablePlanningSettings | None = None,
    ) -> PortableCatalogSession:
        """Atomically register a selected checkout, session, and live owner."""
        _validate_session_id(launch.session_id)
        _validate_owner_id(owner_id)
        checkout = launch.checkout.resolve()
        if not checkout.is_dir():
            raise ValueError(f"Portable session checkout does not exist: {checkout}")
        active_process_id = os.getpid() if process_id is None else process_id
        _validate_process_id(active_process_id)
        project_id = str(uuid.uuid5(uuid.NAMESPACE_URL, checkout.as_uri()))
        launch_settings_json = json.dumps(
            PortableLaunchSettings.from_arguments(
                launch.arguments,
                operation=launch.operation,
                checkout=checkout,
            ).to_dict(),
            separators=(",", ":"),
        )
        planning_settings_json = (
            _serialize_planning_settings(planning_settings)
            if planning_settings is not None
            else None
        )
        timestamp = time.time()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM worktree_leases WHERE checkout = ?",
                    (str(checkout),),
                ).fetchone()
                if existing is not None:
                    raise PortableWorktreeLeaseConflict(_lease_from_row(existing))
                connection.execute(
                    """
                    INSERT INTO saved_projects (
                        project_id, checkout, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(checkout) DO UPDATE SET
                        updated_at = excluded.updated_at
                    """,
                    (project_id, str(checkout), timestamp, timestamp),
                )
                connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, project_id, status, operation, arguments_json,
                        planning_settings_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        launch.session_id,
                        project_id,
                        PortableSessionStatus.READY.value,
                        launch.operation.value,
                        launch_settings_json,
                        planning_settings_json,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO worktree_leases (
                        checkout, session_id, owner_id, process_id,
                        acquired_at, heartbeat_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(checkout),
                        launch.session_id,
                        owner_id,
                        active_process_id,
                        timestamp,
                        timestamp,
                    ),
                )
        except PortableWorktreeLeaseConflict:
            raise
        except sqlite3.IntegrityError as error:
            raise ValueError(
                f"Portable session already exists: {launch.session_id}"
            ) from error
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error
        return self.get_session(launch.session_id)

    def acquire_session_lease(
        self,
        session_id: str,
        *,
        owner_id: str,
        process_id: int | None = None,
    ) -> PortableWorktreeLease:
        _validate_session_id(session_id)
        _validate_owner_id(owner_id)
        active_process_id = os.getpid() if process_id is None else process_id
        _validate_process_id(active_process_id)
        timestamp = time.time()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                session = connection.execute(
                    """
                    SELECT p.checkout
                    FROM sessions AS s
                    JOIN saved_projects AS p ON p.project_id = s.project_id
                    WHERE s.session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise KeyError(f"Unknown portable session: {session_id}")
                existing = connection.execute(
                    "SELECT * FROM worktree_leases WHERE checkout = ?",
                    (session["checkout"],),
                ).fetchone()
                if existing is not None:
                    lease = _lease_from_row(existing)
                    if lease.session_id == session_id and lease.owner_id == owner_id:
                        return lease
                    raise PortableWorktreeLeaseConflict(lease)
                connection.execute(
                    """
                    INSERT INTO worktree_leases (
                        checkout, session_id, owner_id, process_id,
                        acquired_at, heartbeat_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session["checkout"],
                        session_id,
                        owner_id,
                        active_process_id,
                        timestamp,
                        timestamp,
                    ),
                )
        except (KeyError, PortableWorktreeLeaseConflict):
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error
        lease = self.get_worktree_lease(Path(session["checkout"]))
        assert lease is not None
        return lease

    def get_worktree_lease(self, checkout: Path) -> PortableWorktreeLease | None:
        canonical_checkout = checkout.resolve()
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM worktree_leases WHERE checkout = ?",
                    (str(canonical_checkout),),
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog read failed: {error}"
            ) from error
        return _lease_from_row(row) if row is not None else None

    def release_worktree_lease(self, session_id: str, *, owner_id: str) -> bool:
        _validate_session_id(session_id)
        _validate_owner_id(owner_id)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    DELETE FROM execution_claims
                    WHERE session_id = ? AND owner_id = ?
                    """,
                    (session_id, owner_id),
                )
                connection.execute(
                    """
                    DELETE FROM execution_requests
                    WHERE session_id = ? AND owner_id = ?
                    """,
                    (session_id, owner_id),
                )
                cursor = connection.execute(
                    """
                    DELETE FROM worktree_leases
                    WHERE session_id = ? AND owner_id = ?
                    """,
                    (session_id, owner_id),
                )
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error
        return cursor.rowcount == 1

    def rollback_session_start(self, session_id: str, *, owner_id: str) -> None:
        """Remove a newly claimed session whose worker never started."""
        _validate_session_id(session_id)
        _validate_owner_id(owner_id)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                lease = connection.execute(
                    """
                    SELECT owner_id FROM worktree_leases
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if lease is None:
                    raise PortableSessionCatalogError(
                        "Portable session start rollback has no active lease."
                    )
                if lease["owner_id"] != owner_id:
                    raise PortableSessionCatalogError(
                        "Portable session start rollback does not own its lease."
                    )
                connection.execute(
                    """
                    DELETE FROM worktree_leases
                    WHERE session_id = ? AND owner_id = ?
                    """,
                    (session_id, owner_id),
                )
                connection.execute(
                    "DELETE FROM execution_claims WHERE session_id = ?",
                    (session_id,),
                )
                connection.execute(
                    "DELETE FROM execution_requests WHERE session_id = ?",
                    (session_id,),
                )
                connection.execute(
                    "DELETE FROM sessions WHERE session_id = ?",
                    (session_id,),
                )
        except PortableSessionCatalogError:
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error

    def bind_or_create_session(
        self,
        launch: PortableSessionLaunch,
    ) -> PortableCatalogSession:
        """Atomically bind one session identity to its selected checkout."""
        _validate_session_id(launch.session_id)
        checkout = launch.checkout.resolve()
        if not checkout.is_dir():
            raise ValueError(f"Portable session checkout does not exist: {checkout}")
        project_id = str(uuid.uuid5(uuid.NAMESPACE_URL, checkout.as_uri()))
        launch_settings_json = json.dumps(
            PortableLaunchSettings.from_arguments(
                launch.arguments,
                operation=launch.operation,
                checkout=checkout,
            ).to_dict(),
            separators=(",", ":"),
        )
        timestamp = time.time()
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO saved_projects (
                        project_id, checkout, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(checkout) DO UPDATE SET
                        updated_at = excluded.updated_at
                    """,
                    (project_id, str(checkout), timestamp, timestamp),
                )
                connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, project_id, status, operation, arguments_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        project_id = excluded.project_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        launch.session_id,
                        project_id,
                        PortableSessionStatus.READY.value,
                        launch.operation.value,
                        launch_settings_json,
                        timestamp,
                        timestamp,
                    ),
                )
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error
        return self.get_session(launch.session_id)

    def get_session(self, session_id: str) -> PortableCatalogSession:
        _validate_session_id(session_id)
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT s.*, p.checkout
                    FROM sessions AS s
                    JOIN saved_projects AS p ON p.project_id = s.project_id
                    WHERE s.session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog read failed: {error}"
            ) from error
        if row is None:
            raise KeyError(f"Unknown portable session: {session_id}")
        return _session_from_row(row)

    def save_planning_thread(self, session_id: str, thread_id: str) -> None:
        _validate_session_id(session_id)
        _validate_thread_id(thread_id)
        self._update_session(
            session_id,
            "planning_thread_id = ?, updated_at = ?",
            (thread_id, time.time()),
        )

    def bind_session_checkout(
        self,
        session_id: str,
        checkout: Path,
        *,
        owner_id: str | None = None,
        prd_path: Path | None = None,
        issues_index_path: Path | None = None,
        prepare_checkout: Callable[[], None] | None = None,
    ) -> None:
        if (prd_path is None) != (issues_index_path is None):
            raise ValueError(
                "Portable workflow transfer requires both PRD and issue-index pointers."
            )
        if owner_id is not None:
            self._transfer_session_lease(
                session_id,
                checkout,
                owner_id=owner_id,
                prd_path=prd_path,
                issues_index_path=issues_index_path,
                prepare_checkout=prepare_checkout,
            )
            return
        if prepare_checkout is not None:
            raise ValueError(
                "Portable checkout preparation requires the active worktree lease."
            )
        if prd_path is not None:
            raise ValueError(
                "Portable workflow pointers can change only with the active "
                "worktree lease."
            )
        record = self.get_session(session_id)
        self.bind_or_create_session(
            PortableSessionLaunch(
                session_id=session_id,
                checkout=checkout,
                operation=record.operation,
                arguments=record.arguments,
            )
        )

    def _transfer_session_lease(
        self,
        session_id: str,
        checkout: Path,
        *,
        owner_id: str,
        prd_path: Path | None,
        issues_index_path: Path | None,
        prepare_checkout: Callable[[], None] | None,
    ) -> None:
        _validate_session_id(session_id)
        _validate_owner_id(owner_id)
        canonical_checkout = checkout.resolve()
        if not canonical_checkout.is_dir():
            raise ValueError(
                f"Portable session checkout does not exist: {canonical_checkout}"
            )
        canonical_prd: Path | None = None
        canonical_issues: Path | None = None
        if prd_path is not None and issues_index_path is not None:
            canonical_prd = prd_path.resolve()
            canonical_issues = issues_index_path.resolve()
            if (
                not canonical_prd.is_relative_to(canonical_checkout)
                or not canonical_issues.is_relative_to(canonical_checkout)
            ):
                raise ValueError(
                    "Transferred workflow pointers must belong to the selected "
                    "checkout."
                )
        project_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, canonical_checkout.as_uri())
        )
        timestamp = time.time()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                owned = connection.execute(
                    """
                    SELECT * FROM worktree_leases
                    WHERE session_id = ? AND owner_id = ?
                    """,
                    (session_id, owner_id),
                ).fetchone()
                if owned is None:
                    raise PortableSessionCatalogError(
                        "Portable session cannot change checkout without its "
                        "active worktree lease."
                    )
                conflict = connection.execute(
                    """
                    SELECT * FROM worktree_leases
                    WHERE checkout = ? AND session_id <> ?
                    """,
                    (str(canonical_checkout), session_id),
                ).fetchone()
                if conflict is not None:
                    raise PortableWorktreeLeaseConflict(_lease_from_row(conflict))
                connection.execute(
                    """
                    UPDATE worktree_leases
                    SET checkout = ?, heartbeat_at = ?
                    WHERE session_id = ? AND owner_id = ?
                    """,
                    (
                        str(canonical_checkout),
                        timestamp,
                        session_id,
                        owner_id,
                    ),
                )
                if prepare_checkout is not None:
                    prepare_checkout()
                if (
                    canonical_prd is not None
                    and canonical_issues is not None
                    and (
                        not canonical_prd.is_file()
                        or not canonical_issues.is_file()
                    )
                ):
                    raise ValueError(
                        "Transferred workflow pointers must reference existing files."
                    )
                connection.execute(
                    """
                    INSERT INTO saved_projects (
                        project_id, checkout, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(checkout) DO UPDATE SET
                        updated_at = excluded.updated_at
                    """,
                    (
                        project_id,
                        str(canonical_checkout),
                        timestamp,
                        timestamp,
                    ),
                )
                if canonical_prd is None or canonical_issues is None:
                    connection.execute(
                        """
                        UPDATE sessions
                        SET project_id = ?, updated_at = ?
                        WHERE session_id = ?
                        """,
                        (project_id, timestamp, session_id),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE sessions
                        SET project_id = ?, prd_path = ?, issues_index_path = ?,
                            updated_at = ?
                        WHERE session_id = ?
                        """,
                        (
                            project_id,
                            str(canonical_prd),
                            str(canonical_issues),
                            timestamp,
                            session_id,
                        ),
                    )
        except (PortableSessionCatalogError, PortableWorktreeLeaseConflict):
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error

    def publish_workflow(
        self,
        session_id: str,
        *,
        prd_path: Path,
        issues_index_path: Path,
        activity_summary: str,
    ) -> None:
        canonical_prd = prd_path.resolve()
        canonical_issues = issues_index_path.resolve()
        if not canonical_prd.is_file() or not canonical_issues.is_file():
            raise ValueError("Published workflow pointers must reference existing files.")
        bounded_summary = redact_persisted_evidence(activity_summary)[:500]
        self._update_session(
            session_id,
            """
            prd_path = ?, issues_index_path = ?, activity_summary = ?,
            planning_thread_id = NULL,
            planning_settings_json = NULL, updated_at = ?
            """,
            (
                str(canonical_prd),
                str(canonical_issues),
                bounded_summary,
                time.time(),
            ),
        )

    def discover_resume_candidates(
        self,
        discoverer: Callable[[Path], Iterable[PortableResumeCandidateSource]],
    ) -> tuple[PortableResumeCandidate, ...]:
        candidates: list[PortableResumeCandidate] = []
        for project in self.list_saved_projects():
            if not project.checkout.is_dir():
                continue
            for candidate in discoverer(project.checkout):
                artifacts = candidate.artifacts
                prd_path = artifacts.prd_path.resolve()
                issues_index = artifacts.issues_index.resolve()
                candidates.append(
                    PortableResumeCandidate(
                        candidate_id=str(
                            uuid.uuid5(uuid.NAMESPACE_URL, prd_path.as_uri())
                        ),
                        project_id=project.project_id,
                        checkout=project.checkout,
                        prd_path=prd_path,
                        issues_index_path=issues_index,
                        completed_issues=candidate.completed_issues,
                        pending_issues=candidate.pending_issues,
                        total_issues=candidate.total_issues,
                        active_issue=candidate.active_issue,
                        active_status=candidate.active_status,
                        active_stage=getattr(candidate, "active_stage", None),
                        updated_at=candidate.updated_at,
                    )
                )
        return tuple(
            sorted(
                candidates,
                key=lambda candidate: (
                    candidate.updated_at,
                    candidate.prd_path.name.casefold(),
                ),
                reverse=True,
            )
        )

    def save_planning_settings(
        self,
        session_id: str,
        settings: PortablePlanningSettings,
    ) -> None:
        self._update_session(
            session_id,
            "planning_settings_json = ?, updated_at = ?",
            (_serialize_planning_settings(settings), time.time()),
        )

    def update_session_status(
        self,
        session_id: str,
        status: PortableSessionStatus,
        *,
        activity_summary: str = "",
    ) -> None:
        if not isinstance(status, PortableSessionStatus):
            raise ValueError("Portable session status must be a known lifecycle value.")
        self._update_session(
            session_id,
            """
            status = ?, activity_summary = ?, updated_at = ?
            """,
            (
                status.value,
                redact_persisted_evidence(activity_summary)[:500],
                time.time(),
            ),
        )

    def get_concurrency_limit(self) -> int:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT integer_value
                    FROM catalog_settings
                    WHERE setting_key = ?
                    """,
                    (_CONCURRENCY_LIMIT_SETTING_KEY,),
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog read failed: {error}"
            ) from error
        if row is None:
            raise PortableSessionCatalogError(
                "Portable Session Catalog concurrency setting is missing."
            )
        return _validate_concurrency_limit(row["integer_value"])

    def set_concurrency_limit(self, limit: int) -> None:
        validated = _validate_concurrency_limit(limit)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE catalog_settings
                    SET integer_value = ?
                    WHERE setting_key = ?
                    """,
                    (validated, _CONCURRENCY_LIMIT_SETTING_KEY),
                )
                if cursor.rowcount != 1:
                    raise PortableSessionCatalogError(
                        "Portable Session Catalog concurrency setting is missing."
                    )
        except PortableSessionCatalogError:
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error

    def request_execution_capacity(
        self,
        session_id: str,
        *,
        owner_id: str,
        process_id: int | None = None,
    ) -> bool:
        """Atomically acquire execution capacity or retain the session's fair queue place."""
        _validate_session_id(session_id)
        _validate_owner_id(owner_id)
        active_process_id = os.getpid() if process_id is None else process_id
        _validate_process_id(active_process_id)
        timestamp = time.time()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                session = connection.execute(
                    "SELECT status FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise KeyError(f"Unknown portable session: {session_id}")
                status = PortableSessionStatus(session["status"])
                if status.terminal or status is PortableSessionStatus.UNAVAILABLE:
                    raise ValueError(
                        "Portable session cannot request execution capacity from "
                        f"{status.value}."
                    )
                lease = connection.execute(
                    """
                    SELECT owner_id
                    FROM worktree_leases
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if lease is None or lease["owner_id"] != owner_id:
                    raise PortableSessionCatalogError(
                        "Portable execution capacity request does not own the "
                        "session worktree lease."
                    )
                claim = connection.execute(
                    "SELECT owner_id FROM execution_claims WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if claim is not None:
                    if claim["owner_id"] != owner_id:
                        raise PortableSessionCatalogError(
                            "Portable execution capacity is owned by another "
                            "application."
                        )
                    connection.execute(
                        """
                        UPDATE sessions
                        SET status = ?, updated_at = ?
                        WHERE session_id = ?
                        """,
                        (
                            PortableSessionStatus.RUNNING.value,
                            timestamp,
                            session_id,
                        ),
                    )
                    return True
                request = connection.execute(
                    """
                    SELECT owner_id
                    FROM execution_requests
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if request is not None and request["owner_id"] != owner_id:
                    raise PortableSessionCatalogError(
                        "Portable execution request is owned by another application."
                    )
                if request is None:
                    next_order = connection.execute(
                        """
                        SELECT COALESCE(MAX(queue_order), 0) + 1
                        FROM execution_requests
                        """
                    ).fetchone()[0]
                    connection.execute(
                        """
                        INSERT INTO execution_requests (
                            session_id, owner_id, process_id, queue_order, requested_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            owner_id,
                            active_process_id,
                            next_order,
                            timestamp,
                        ),
                    )
                limit = connection.execute(
                    """
                    SELECT integer_value
                    FROM catalog_settings
                    WHERE setting_key = ?
                    """,
                    (_CONCURRENCY_LIMIT_SETTING_KEY,),
                ).fetchone()
                if limit is None:
                    raise PortableSessionCatalogError(
                        "Portable Session Catalog concurrency setting is missing."
                    )
                active_count = connection.execute(
                    "SELECT COUNT(*) FROM execution_claims"
                ).fetchone()[0]
                oldest_request = connection.execute(
                    """
                    SELECT session_id
                    FROM execution_requests
                    ORDER BY queue_order, session_id
                    LIMIT 1
                    """
                ).fetchone()
                granted = (
                    active_count < _validate_concurrency_limit(limit["integer_value"])
                    and oldest_request is not None
                    and oldest_request["session_id"] == session_id
                )
                if granted:
                    connection.execute(
                        "DELETE FROM execution_requests WHERE session_id = ?",
                        (session_id,),
                    )
                    connection.execute(
                        """
                        INSERT INTO execution_claims (
                            session_id, owner_id, process_id, acquired_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (session_id, owner_id, active_process_id, timestamp),
                    )
                connection.execute(
                    """
                    UPDATE sessions
                    SET status = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        (
                            PortableSessionStatus.RUNNING.value
                            if granted
                            else PortableSessionStatus.QUEUED.value
                        ),
                        timestamp,
                        session_id,
                    ),
                )
                return granted
        except (KeyError, PortableSessionCatalogError, ValueError):
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error

    def owns_execution_capacity(
        self,
        session_id: str,
        *,
        owner_id: str,
        process_id: int | None = None,
    ) -> bool:
        """Return whether this exact live process owns both session leases."""
        _validate_session_id(session_id)
        _validate_owner_id(owner_id)
        active_process_id = os.getpid() if process_id is None else process_id
        _validate_process_id(active_process_id)
        try:
            with self._connection() as connection:
                owned = connection.execute(
                    """
                    SELECT 1
                    FROM worktree_leases AS worktree
                    JOIN execution_claims AS execution
                      ON execution.session_id = worktree.session_id
                     AND execution.owner_id = worktree.owner_id
                     AND execution.process_id = worktree.process_id
                    WHERE worktree.session_id = ?
                      AND worktree.owner_id = ?
                      AND worktree.process_id = ?
                    """,
                    (session_id, owner_id, active_process_id),
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog read failed: {error}"
            ) from error
        return owned is not None

    def release_execution_capacity(
        self,
        session_id: str,
        *,
        owner_id: str,
        status: PortableSessionStatus,
        activity_summary: str = "",
    ) -> bool:
        """Atomically release a slot and persist the session's inactive status."""
        _validate_session_id(session_id)
        _validate_owner_id(owner_id)
        if not isinstance(status, PortableSessionStatus):
            raise ValueError("Portable session status must be a known lifecycle value.")
        if status in {
            PortableSessionStatus.RUNNING,
            PortableSessionStatus.PAUSING,
        }:
            raise ValueError(
                "Releasing execution capacity requires an inactive session status."
            )
        timestamp = time.time()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                session = connection.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise KeyError(f"Unknown portable session: {session_id}")
                foreign_claim = connection.execute(
                    """
                    SELECT owner_id
                    FROM execution_claims
                    WHERE session_id = ? AND owner_id <> ?
                    """,
                    (session_id, owner_id),
                ).fetchone()
                foreign_request = connection.execute(
                    """
                    SELECT owner_id
                    FROM execution_requests
                    WHERE session_id = ? AND owner_id <> ?
                    """,
                    (session_id, owner_id),
                ).fetchone()
                if foreign_claim is not None or foreign_request is not None:
                    raise PortableSessionCatalogError(
                        "Portable execution capacity is owned by another application."
                    )
                cursor = connection.execute(
                    """
                    DELETE FROM execution_claims
                    WHERE session_id = ? AND owner_id = ?
                    """,
                    (session_id, owner_id),
                )
                connection.execute(
                    """
                    DELETE FROM execution_requests
                    WHERE session_id = ? AND owner_id = ?
                    """,
                    (session_id, owner_id),
                )
                connection.execute(
                    """
                    UPDATE sessions
                    SET status = ?, activity_summary = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        status.value,
                        redact_persisted_evidence(activity_summary)[:500],
                        timestamp,
                        session_id,
                    ),
                )
                return cursor.rowcount == 1
        except (KeyError, PortableSessionCatalogError):
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error

    def list_sessions(self) -> tuple[PortableCatalogSession, ...]:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT s.*, p.checkout
                    FROM sessions AS s
                    JOIN saved_projects AS p ON p.project_id = s.project_id
                    ORDER BY s.updated_at DESC, s.session_id
                    """
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog read failed: {error}"
            ) from error
        return tuple(_session_from_row(row) for row in rows)

    def list_saved_projects(self) -> tuple[PortableSavedProject, ...]:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT project_id, checkout, created_at, updated_at
                    FROM saved_projects
                    ORDER BY updated_at DESC, checkout
                    """
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog read failed: {error}"
            ) from error
        return tuple(
            PortableSavedProject(
                project_id=row["project_id"],
                checkout=Path(row["checkout"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        )

    def _update_session(
        self,
        session_id: str,
        assignments: str,
        values: tuple[object, ...],
    ) -> None:
        _validate_session_id(session_id)
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    f"UPDATE sessions SET {assignments} WHERE session_id = ?",
                    (*values, session_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(f"Unknown portable session: {session_id}")
        except KeyError:
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error

    def _initialize(self) -> None:
        try:
            with self._connection() as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > CATALOG_SCHEMA_VERSION:
                    raise PortableSessionCatalogError(
                        "Portable Session Catalog schema version "
                        f"{version} is newer than supported version "
                        f"{CATALOG_SCHEMA_VERSION}."
                    )
                if version < 0:
                    raise PortableSessionCatalogError(
                        "Portable Session Catalog has unsupported schema version "
                        f"{version}."
                    )
                if version == 0:
                    connection.executescript(
                        """
                        BEGIN IMMEDIATE;
                        CREATE TABLE saved_projects (
                            project_id TEXT PRIMARY KEY
                                CHECK (length(project_id) BETWEEN 1 AND 128),
                            checkout TEXT NOT NULL UNIQUE
                                CHECK (length(checkout) BETWEEN 1 AND 4096),
                            created_at REAL NOT NULL
                                CHECK (typeof(created_at) IN ('integer', 'real')
                                    AND created_at >= 0),
                            updated_at REAL NOT NULL
                                CHECK (typeof(updated_at) IN ('integer', 'real')
                                    AND updated_at >= 0)
                        );
                        CREATE TABLE sessions (
                            session_id TEXT PRIMARY KEY
                                CHECK (length(session_id) BETWEEN 1 AND 128),
                            project_id TEXT NOT NULL
                                REFERENCES saved_projects(project_id),
                            status TEXT NOT NULL CHECK (
                                status IN (
                                    'READY', 'QUEUED', 'RUNNING',
                                    'WAITING_FOR_INPUT', 'PAUSING', 'PAUSED',
                                    'INTERRUPTED', 'COMPLETED', 'FAILED',
                                    'CANCELLED', 'UNAVAILABLE'
                                )
                            ),
                            operation TEXT NOT NULL
                                CHECK (operation IN ('PLANNING', 'DELIVERY')),
                            arguments_json TEXT NOT NULL
                                CHECK (length(arguments_json) BETWEEN 2 AND 4096),
                            planning_thread_id TEXT CHECK (
                                planning_thread_id IS NULL
                                OR length(planning_thread_id) = 36
                            ),
                            planning_settings_json TEXT CHECK (
                                planning_settings_json IS NULL
                                OR length(planning_settings_json) BETWEEN 2 AND 4096
                            ),
                            prd_path TEXT CHECK (
                                prd_path IS NULL
                                OR length(prd_path) BETWEEN 1 AND 4096
                            ),
                            issues_index_path TEXT CHECK (
                                issues_index_path IS NULL
                                OR length(issues_index_path) BETWEEN 1 AND 4096
                            ),
                            activity_summary TEXT NOT NULL DEFAULT ''
                                CHECK (length(activity_summary) <= 500),
                            created_at REAL NOT NULL
                                CHECK (typeof(created_at) IN ('integer', 'real')
                                    AND created_at >= 0),
                            updated_at REAL NOT NULL
                                CHECK (typeof(updated_at) IN ('integer', 'real')
                                    AND updated_at >= 0)
                        );
                        CREATE TABLE worktree_leases (
                            checkout TEXT PRIMARY KEY
                                CHECK (length(checkout) BETWEEN 1 AND 4096),
                            session_id TEXT NOT NULL UNIQUE
                                REFERENCES sessions(session_id),
                            owner_id TEXT NOT NULL
                                CHECK (length(owner_id) BETWEEN 1 AND 128),
                            process_id INTEGER NOT NULL
                                CHECK (typeof(process_id) = 'integer'
                                    AND process_id > 0),
                            acquired_at REAL NOT NULL
                                CHECK (typeof(acquired_at) IN ('integer', 'real')
                                    AND acquired_at >= 0),
                            heartbeat_at REAL NOT NULL
                                CHECK (typeof(heartbeat_at) IN ('integer', 'real')
                                    AND heartbeat_at >= 0)
                        );
                        CREATE TABLE IF NOT EXISTS catalog_settings (
                            setting_key TEXT PRIMARY KEY
                                CHECK (length(setting_key) BETWEEN 1 AND 128),
                            integer_value INTEGER NOT NULL
                                CHECK (typeof(integer_value) = 'integer')
                        );
                        INSERT OR IGNORE INTO catalog_settings (
                            setting_key, integer_value
                        ) VALUES ('session_concurrency_limit', 2);
                        CREATE TABLE IF NOT EXISTS execution_requests (
                            session_id TEXT PRIMARY KEY
                                REFERENCES sessions(session_id),
                            owner_id TEXT NOT NULL
                                CHECK (length(owner_id) BETWEEN 1 AND 128),
                            process_id INTEGER NOT NULL
                                CHECK (typeof(process_id) = 'integer'
                                    AND process_id > 0),
                            queue_order INTEGER NOT NULL UNIQUE
                                CHECK (typeof(queue_order) = 'integer'
                                    AND queue_order > 0),
                            requested_at REAL NOT NULL
                                CHECK (typeof(requested_at) IN ('integer', 'real')
                                    AND requested_at >= 0)
                        );
                        CREATE TABLE IF NOT EXISTS execution_claims (
                            session_id TEXT PRIMARY KEY
                                REFERENCES sessions(session_id),
                            owner_id TEXT NOT NULL
                                CHECK (length(owner_id) BETWEEN 1 AND 128),
                            process_id INTEGER NOT NULL
                                CHECK (typeof(process_id) = 'integer'
                                    AND process_id > 0),
                            acquired_at REAL NOT NULL
                                CHECK (typeof(acquired_at) IN ('integer', 'real')
                                    AND acquired_at >= 0)
                        );
                        PRAGMA user_version = 3;
                        """
                    )
                if version == 1:
                    connection.executescript(
                        """
                        BEGIN IMMEDIATE;
                        CREATE TABLE worktree_leases (
                            checkout TEXT PRIMARY KEY
                                CHECK (length(checkout) BETWEEN 1 AND 4096),
                            session_id TEXT NOT NULL UNIQUE
                                REFERENCES sessions(session_id),
                            owner_id TEXT NOT NULL
                                CHECK (length(owner_id) BETWEEN 1 AND 128),
                            process_id INTEGER NOT NULL
                                CHECK (typeof(process_id) = 'integer'
                                    AND process_id > 0),
                            acquired_at REAL NOT NULL
                                CHECK (typeof(acquired_at) IN ('integer', 'real')
                                    AND acquired_at >= 0),
                            heartbeat_at REAL NOT NULL
                                CHECK (typeof(heartbeat_at) IN ('integer', 'real')
                                    AND heartbeat_at >= 0)
                        );
                        PRAGMA user_version = 2;
                        """
                    )
                    version = 2
                if version == 2:
                    connection.executescript(
                        """
                        BEGIN IMMEDIATE;
                        CREATE TABLE IF NOT EXISTS catalog_settings (
                            setting_key TEXT PRIMARY KEY
                                CHECK (length(setting_key) BETWEEN 1 AND 128),
                            integer_value INTEGER NOT NULL
                                CHECK (typeof(integer_value) = 'integer')
                        );
                        INSERT OR IGNORE INTO catalog_settings (
                            setting_key, integer_value
                        ) VALUES ('session_concurrency_limit', 2);
                        CREATE TABLE IF NOT EXISTS execution_requests (
                            session_id TEXT PRIMARY KEY
                                REFERENCES sessions(session_id),
                            owner_id TEXT NOT NULL
                                CHECK (length(owner_id) BETWEEN 1 AND 128),
                            process_id INTEGER NOT NULL
                                CHECK (typeof(process_id) = 'integer'
                                    AND process_id > 0),
                            queue_order INTEGER NOT NULL UNIQUE
                                CHECK (typeof(queue_order) = 'integer'
                                    AND queue_order > 0),
                            requested_at REAL NOT NULL
                                CHECK (typeof(requested_at) IN ('integer', 'real')
                                    AND requested_at >= 0)
                        );
                        CREATE TABLE IF NOT EXISTS execution_claims (
                            session_id TEXT PRIMARY KEY
                                REFERENCES sessions(session_id),
                            owner_id TEXT NOT NULL
                                CHECK (length(owner_id) BETWEEN 1 AND 128),
                            process_id INTEGER NOT NULL
                                CHECK (typeof(process_id) = 'integer'
                                    AND process_id > 0),
                            acquired_at REAL NOT NULL
                                CHECK (typeof(acquired_at) IN ('integer', 'real')
                                    AND acquired_at >= 0)
                        );
                        PRAGMA user_version = 3;
                        """
                    )
                self._validate_schema(connection)
                self._validate_records(connection)
        except PortableSessionCatalogError:
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog is corrupt or unreadable: {error}"
            ) from error

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL").fetchone()
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise PortableSessionCatalogError(
                f"Portable Session Catalog integrity check failed: {integrity}"
            )
        foreign_key_violation = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchone()
        if foreign_key_violation is not None:
            raise PortableSessionCatalogError(
                "Portable Session Catalog foreign key check failed."
            )
        expected_tables = {
            "saved_projects",
            "sessions",
            "worktree_leases",
            "catalog_settings",
            "execution_requests",
            "execution_claims",
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if tables != expected_tables:
            raise PortableSessionCatalogError(
                "Portable Session Catalog schema is incompatible."
            )
        expected_columns = {
            "saved_projects": (
                ("project_id", "TEXT", 0, 1),
                ("checkout", "TEXT", 1, 0),
                ("created_at", "REAL", 1, 0),
                ("updated_at", "REAL", 1, 0),
            ),
            "sessions": (
                ("session_id", "TEXT", 0, 1),
                ("project_id", "TEXT", 1, 0),
                ("status", "TEXT", 1, 0),
                ("operation", "TEXT", 1, 0),
                ("arguments_json", "TEXT", 1, 0),
                ("planning_thread_id", "TEXT", 0, 0),
                ("planning_settings_json", "TEXT", 0, 0),
                ("prd_path", "TEXT", 0, 0),
                ("issues_index_path", "TEXT", 0, 0),
                ("activity_summary", "TEXT", 1, 0),
                ("created_at", "REAL", 1, 0),
                ("updated_at", "REAL", 1, 0),
            ),
            "worktree_leases": (
                ("checkout", "TEXT", 0, 1),
                ("session_id", "TEXT", 1, 0),
                ("owner_id", "TEXT", 1, 0),
                ("process_id", "INTEGER", 1, 0),
                ("acquired_at", "REAL", 1, 0),
                ("heartbeat_at", "REAL", 1, 0),
            ),
            "catalog_settings": (
                ("setting_key", "TEXT", 0, 1),
                ("integer_value", "INTEGER", 1, 0),
            ),
            "execution_requests": (
                ("session_id", "TEXT", 0, 1),
                ("owner_id", "TEXT", 1, 0),
                ("process_id", "INTEGER", 1, 0),
                ("queue_order", "INTEGER", 1, 0),
                ("requested_at", "REAL", 1, 0),
            ),
            "execution_claims": (
                ("session_id", "TEXT", 0, 1),
                ("owner_id", "TEXT", 1, 0),
                ("process_id", "INTEGER", 1, 0),
                ("acquired_at", "REAL", 1, 0),
            ),
        }
        for table, expected in expected_columns.items():
            actual = tuple(
                (
                    row["name"],
                    row["type"].upper(),
                    row["notnull"],
                    row["pk"],
                )
                for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if actual != expected:
                raise PortableSessionCatalogError(
                    "Portable Session Catalog schema is incompatible."
                )
        foreign_keys = tuple(
            (
                row["table"],
                row["from"],
                row["to"],
                row["on_update"],
                row["on_delete"],
            )
            for row in connection.execute("PRAGMA foreign_key_list(sessions)")
        )
        if foreign_keys != (
            ("saved_projects", "project_id", "project_id", "NO ACTION", "NO ACTION"),
        ):
            raise PortableSessionCatalogError(
                "Portable Session Catalog schema is incompatible."
            )
        lease_foreign_keys = tuple(
            (
                row["table"],
                row["from"],
                row["to"],
                row["on_update"],
                row["on_delete"],
            )
            for row in connection.execute(
                "PRAGMA foreign_key_list(worktree_leases)"
            )
        )
        if lease_foreign_keys != (
            ("sessions", "session_id", "session_id", "NO ACTION", "NO ACTION"),
        ):
            raise PortableSessionCatalogError(
                "Portable Session Catalog schema is incompatible."
            )
        for table in ("execution_requests", "execution_claims"):
            capacity_foreign_keys = tuple(
                (
                    row["table"],
                    row["from"],
                    row["to"],
                    row["on_update"],
                    row["on_delete"],
                )
                for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            )
            if capacity_foreign_keys != (
                ("sessions", "session_id", "session_id", "NO ACTION", "NO ACTION"),
            ):
                raise PortableSessionCatalogError(
                    "Portable Session Catalog schema is incompatible."
                )
        required_constraints = {
            "saved_projects": (
                "check(length(project_id)between1and128)",
                "checkouttextnotnullunique",
                "check(length(checkout)between1and4096)",
                "check(typeof(created_at)in('integer','real')andcreated_at>=0)",
                "check(typeof(updated_at)in('integer','real')andupdated_at>=0)",
            ),
            "sessions": (
                "check(length(session_id)between1and128)",
                "statusin('ready','queued','running'",
                "'interrupted','completed','failed','cancelled','unavailable'",
                "check(operationin('planning','delivery'))",
                "check(length(arguments_json)between2and4096)",
                "orlength(planning_thread_id)=36)",
                "orlength(planning_settings_json)between2and4096)",
                "orlength(prd_path)between1and4096)",
                "orlength(issues_index_path)between1and4096)",
                "check(length(activity_summary)<=500)",
                "check(typeof(created_at)in('integer','real')andcreated_at>=0)",
                "check(typeof(updated_at)in('integer','real')andupdated_at>=0)",
            ),
            "worktree_leases": (
                "check(length(checkout)between1and4096)",
                "session_idtextnotnullunique",
                "check(length(owner_id)between1and128)",
                "check(typeof(process_id)='integer'andprocess_id>0)",
                "check(typeof(acquired_at)in('integer','real')andacquired_at>=0)",
                "check(typeof(heartbeat_at)in('integer','real')andheartbeat_at>=0)",
            ),
            "catalog_settings": (
                "check(length(setting_key)between1and128)",
                "check(typeof(integer_value)='integer')",
            ),
            "execution_requests": (
                "check(length(owner_id)between1and128)",
                "check(typeof(process_id)='integer'andprocess_id>0)",
                "queue_orderintegernotnullunique",
                "check(typeof(queue_order)='integer'andqueue_order>0)",
                "check(typeof(requested_at)in('integer','real')andrequested_at>=0)",
            ),
            "execution_claims": (
                "check(length(owner_id)between1and128)",
                "check(typeof(process_id)='integer'andprocess_id>0)",
                "check(typeof(acquired_at)in('integer','real')andacquired_at>=0)",
            ),
        }
        for table, fragments in required_constraints.items():
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            normalized_sql = re.sub(r"\s+", "", row["sql"].lower())
            if any(fragment not in normalized_sql for fragment in fragments):
                raise PortableSessionCatalogError(
                    "Portable Session Catalog schema is incompatible."
                )

    @staticmethod
    def _validate_records(connection: sqlite3.Connection) -> None:
        try:
            for row in connection.execute(
                "SELECT project_id, checkout, created_at, updated_at FROM saved_projects"
            ):
                _validate_project_id(row["project_id"])
                _validate_catalog_path(row["checkout"])
                _validate_timestamp(row["created_at"])
                _validate_timestamp(row["updated_at"])
            rows = connection.execute(
                """
                SELECT s.*, p.checkout
                FROM sessions AS s
                JOIN saved_projects AS p ON p.project_id = s.project_id
                """
            ).fetchall()
            for row in rows:
                _session_from_row(row)
            for row in connection.execute("SELECT * FROM worktree_leases"):
                _lease_from_row(row)
            concurrency_row = connection.execute(
                """
                SELECT integer_value
                FROM catalog_settings
                WHERE setting_key = ?
                """,
                (_CONCURRENCY_LIMIT_SETTING_KEY,),
            ).fetchone()
            if concurrency_row is None:
                raise PortableSessionCatalogError(
                    "Portable Session Catalog concurrency setting is missing."
                )
            _validate_concurrency_limit(concurrency_row["integer_value"])
            for row in connection.execute("SELECT * FROM execution_requests"):
                _validate_session_id(row["session_id"])
                _validate_owner_id(row["owner_id"])
                _validate_process_id(row["process_id"])
                if (
                    isinstance(row["queue_order"], bool)
                    or not isinstance(row["queue_order"], int)
                    or row["queue_order"] <= 0
                ):
                    raise ValueError("Portable execution queue order is invalid.")
                _validate_timestamp(row["requested_at"])
            for row in connection.execute("SELECT * FROM execution_claims"):
                _validate_session_id(row["session_id"])
                _validate_owner_id(row["owner_id"])
                _validate_process_id(row["process_id"])
                _validate_timestamp(row["acquired_at"])
        except PortableSessionCatalogError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError) as error:
            raise PortableSessionCatalogError(
                "Portable Session Catalog contains an invalid session record."
            ) from error


def active_portable_catalog_session(
    *,
    environment: Mapping[str, str] | None = None,
) -> tuple[PortableSessionCatalog, PortableCatalogSession | None, bool] | None:
    """Load the catalog session projected into the current worker process."""
    values = os.environ if environment is None else environment
    catalog_path = values.get(PORTABLE_SESSION_CATALOG_ENV)
    session_id = values.get(PORTABLE_SESSION_ID_ENV)
    if not catalog_path or not session_id:
        return None
    catalog = PortableSessionCatalog(Path(catalog_path))
    try:
        record = catalog.get_session(session_id)
    except KeyError:
        record = None
    restore_requested = values.get(PORTABLE_SESSION_RESTORE_ENV) == "1"
    if restore_requested and record is None:
        raise RuntimeError(
            f"Portable Session Catalog has no resumable session {session_id!r}."
        )
    return catalog, record, restore_requested


def active_process_owns_portable_execution(
    *,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Verify catalog-backed execution ownership projected into this process."""
    values = os.environ if environment is None else environment
    catalog_path = values.get(PORTABLE_SESSION_CATALOG_ENV)
    session_id = values.get(PORTABLE_SESSION_ID_ENV)
    owner_id = values.get(PORTABLE_SESSION_OWNER_ID_ENV)
    if not catalog_path or not session_id or not owner_id:
        return False
    path = Path(catalog_path)
    if not path.is_file():
        return False
    try:
        return PortableSessionCatalog(path).owns_execution_capacity(
            session_id,
            owner_id=owner_id,
        )
    except (OSError, PortableSessionCatalogError, ValueError):
        return False


def bind_active_catalog_session_checkout(
    checkout: Path,
    *,
    environment: Mapping[str, str] | None = None,
    prd_path: Path | None = None,
    issues_index_path: Path | None = None,
    prepare_checkout: Callable[[], None] | None = None,
) -> PortableCatalogSession | None:
    """Atomically move the active session, lease, and workflow to a checkout."""
    values = os.environ if environment is None else environment
    active_session = active_portable_catalog_session(environment=values)
    if active_session is None:
        return None
    catalog, record, _restore_requested = active_session
    if record is None:
        session_id = values.get(PORTABLE_SESSION_ID_ENV, "")
        raise RuntimeError(
            f"Portable Session Catalog has no active session {session_id!r}."
        )
    catalog.bind_session_checkout(
        record.session_id,
        checkout,
        owner_id=values.get(PORTABLE_SESSION_OWNER_ID_ENV),
        prd_path=prd_path,
        issues_index_path=issues_index_path,
        prepare_checkout=prepare_checkout,
    )
    return catalog.get_session(record.session_id)


def _session_from_row(row: sqlite3.Row) -> PortableCatalogSession:
    try:
        _validate_session_id(row["session_id"])
        _validate_project_id(row["project_id"])
        _validate_catalog_path(row["checkout"])
        _validate_timestamp(row["created_at"])
        _validate_timestamp(row["updated_at"])
        arguments_text = row["arguments_json"]
        _validate_catalog_text(arguments_text, maximum_length=4096)
        launch_settings_value = json.loads(arguments_text)
        if not isinstance(launch_settings_value, dict):
            raise ValueError
        thread_id = row["planning_thread_id"]
        if thread_id is not None:
            _validate_thread_id(thread_id)
        settings_text = row["planning_settings_json"]
        if settings_text is not None:
            _validate_catalog_text(settings_text, maximum_length=4096)
            settings_value = json.loads(settings_text)
            if not isinstance(settings_value, dict):
                raise PortableSessionCatalogError(
                    "Portable planning settings are corrupt."
                )
            settings = PortablePlanningSettings.from_mapping(settings_value)
        else:
            settings = None
        activity_summary = row["activity_summary"]
        _validate_catalog_text(
            activity_summary,
            maximum_length=500,
            allow_empty=True,
        )
        prd_text = row["prd_path"]
        issues_text = row["issues_index_path"]
        if prd_text is not None:
            _validate_catalog_path(prd_text)
        if issues_text is not None:
            _validate_catalog_path(issues_text)
        return PortableCatalogSession(
            session_id=row["session_id"],
            project_id=row["project_id"],
            checkout=Path(row["checkout"]),
            status=PortableSessionStatus(row["status"]),
            operation=PortableWorkflowOperation(row["operation"]),
            launch_settings=PortableLaunchSettings.from_mapping(
                launch_settings_value
            ),
            planning_thread_id=thread_id,
            planning_settings=settings,
            prd_path=Path(prd_text) if prd_text else None,
            issues_index_path=(
                Path(issues_text) if issues_text else None
            ),
            activity_summary=activity_summary,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise PortableSessionCatalogError(
            "Portable Session Catalog contains an invalid session record."
        ) from error


def _lease_from_row(row: sqlite3.Row) -> PortableWorktreeLease:
    try:
        _validate_catalog_path(row["checkout"])
        _validate_session_id(row["session_id"])
        _validate_owner_id(row["owner_id"])
        _validate_process_id(row["process_id"])
        _validate_timestamp(row["acquired_at"])
        _validate_timestamp(row["heartbeat_at"])
        return PortableWorktreeLease(
            checkout=Path(row["checkout"]),
            session_id=row["session_id"],
            owner_id=row["owner_id"],
            process_id=row["process_id"],
            acquired_at=row["acquired_at"],
            heartbeat_at=row["heartbeat_at"],
        )
    except (TypeError, ValueError) as error:
        raise PortableSessionCatalogError(
            "Portable Session Catalog contains an invalid worktree lease."
        ) from error


def _validate_session_id(session_id: str) -> None:
    if _SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise ValueError(
            "Portable session identity must contain 1-128 letters, digits, "
            "periods, underscores, or hyphens."
        )


def _validate_thread_id(thread_id: str) -> None:
    if not isinstance(thread_id, str):
        raise ValueError("Planning thread identity must be a bounded UUID.")
    try:
        parsed = uuid.UUID(thread_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("Planning thread identity must be a bounded UUID.") from error
    if str(parsed) != thread_id or len(thread_id) > 64:
        raise ValueError("Planning thread identity must be a bounded UUID.")


def _validate_owner_id(owner_id: str) -> None:
    if (
        not isinstance(owner_id, str)
        or not owner_id
        or len(owner_id) > 128
        or _SESSION_ID_PATTERN.fullmatch(owner_id) is None
    ):
        raise ValueError(
            "Portable lease owner identity must contain 1-128 letters, digits, "
            "periods, underscores, or hyphens."
        )


def _validate_process_id(process_id: int) -> None:
    if (
        isinstance(process_id, bool)
        or not isinstance(process_id, int)
        or process_id <= 0
    ):
        raise ValueError("Portable lease process identity must be a positive integer.")


def _validate_concurrency_limit(limit: int) -> int:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not (
            MINIMUM_PORTABLE_SESSION_CONCURRENCY_LIMIT
            <= limit
            <= MAXIMUM_PORTABLE_SESSION_CONCURRENCY_LIMIT
        )
    ):
        raise ValueError(
            "Portable session concurrency limit must be an integer from 1 through 64."
        )
    return limit


def _validate_project_id(project_id: str) -> None:
    try:
        parsed = uuid.UUID(project_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("Portable project identity must be a UUID.") from error
    if str(parsed) != project_id:
        raise ValueError("Portable project identity must be a UUID.")


def _validate_catalog_path(value: str) -> None:
    _validate_catalog_text(value, maximum_length=4096)
    if not Path(value).is_absolute():
        raise ValueError("Portable catalog paths must be absolute.")


def _validate_catalog_text(
    value: str,
    *,
    maximum_length: int,
    allow_empty: bool = False,
) -> None:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value) > maximum_length
        or "\x00" in value
    ):
        raise ValueError("Portable catalog text is invalid or oversized.")


def _validate_timestamp(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("Portable catalog timestamp is invalid.")


def _validate_bounded_secret_free_text(
    value: str,
    *,
    field_name: str,
    maximum_length: int,
) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum_length
        or "\n" in value
        or "\r" in value
        or redact_persisted_evidence(value) != value
    ):
        raise ValueError(f"{field_name} must be bounded secret-free text.")


def _required_text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError
    return item


def _serialize_planning_settings(settings: PortablePlanningSettings) -> str:
    settings_value = settings.to_dict()
    PortablePlanningSettings.from_mapping(settings_value)
    return json.dumps(settings_value, separators=(",", ":"))


def _required_number(value: Mapping[str, Any], key: str) -> float:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, (int, float)):
        raise ValueError
    return float(item)


def _required_bool(value: Mapping[str, Any], key: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise ValueError
    return item
