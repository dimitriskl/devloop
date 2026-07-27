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

from .portable_sessions import (
    PortableSessionLaunch,
    PortableSessionStatus,
    PortableWorkflowOperation,
)
from .redaction import redact_persisted_evidence

CATALOG_SCHEMA_VERSION = 1
CATALOG_FILENAME = "portable-sessions.sqlite3"
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_APPROVAL_POLICIES = frozenset({"never", "on-request", "untrusted", "on-failure"})
_SANDBOX_MODES = frozenset({"read-only", "workspace-write", "danger-full-access"})


class PortableSessionCatalogError(RuntimeError):
    """Raised when the machine catalog cannot be read safely."""


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
        for field_name, value in (
            ("timeout", self.timeout_seconds),
            ("checkpoint deadline", self.checkpoint_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(
                    f"Portable planning {field_name} must be a positive number."
                )

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
        except (TypeError, ValueError) as error:
            raise PortableSessionCatalogError(
                "Portable planning settings are corrupt."
            ) from error


@dataclass(frozen=True)
class PortableLaunchSettings:
    codex_launcher: str = "codex"
    sandbox: str = "workspace-write"
    approval_policy: str = "never"
    native_editor: bool = False

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

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_arguments(
        self,
        *,
        checkout: Path,
        prd_path: Path | None,
    ) -> tuple[str, ...]:
        arguments: list[str] = [
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
    def from_arguments(cls, arguments: Iterable[str]) -> PortableLaunchSettings:
        values = tuple(arguments)
        selected: dict[str, object] = {}
        index = 0
        value_options = {
            "--codex": "codex_launcher",
            "--sandbox": "sandbox",
            "--approval-policy": "approval_policy",
        }
        ignored_value_options = {"--repo", "--prd", "--goal"}
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
        return cls(**selected)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PortableLaunchSettings:
        expected_keys = {
            "codex_launcher",
            "sandbox",
            "approval_policy",
            "native_editor",
        }
        if set(value) != expected_keys:
            raise PortableSessionCatalogError(
                "Portable launch settings are corrupt."
            )
        try:
            return cls(
                codex_launcher=_required_text(value, "codex_launcher"),
                sandbox=_required_text(value, "sandbox"),
                approval_policy=_required_text(value, "approval_policy"),
                native_editor=_required_bool(value, "native_editor"),
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
            json.dumps(planning_settings.to_dict(), separators=(",", ":"))
            if planning_settings is not None
            else None
        )
        launch_settings = PortableLaunchSettings.from_arguments(launch.arguments)
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
            PortableLaunchSettings.from_arguments(launch.arguments).to_dict(),
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

    def bind_session_checkout(self, session_id: str, checkout: Path) -> None:
        record = self.get_session(session_id)
        self.bind_or_create_session(
            PortableSessionLaunch(
                session_id=session_id,
                checkout=checkout,
                operation=record.operation,
                arguments=record.arguments,
            )
        )

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
            (json.dumps(settings.to_dict(), separators=(",", ":")), time.time()),
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
                        PRAGMA user_version = 1;
                        COMMIT;
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
        expected_tables = {"saved_projects", "sessions"}
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
        except PortableSessionCatalogError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError) as error:
            raise PortableSessionCatalogError(
                "Portable Session Catalog contains an invalid session record."
            ) from error


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
        settings = (
            PortablePlanningSettings.from_mapping(json.loads(settings_text))
            if settings_text is not None
            else None
        )
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
