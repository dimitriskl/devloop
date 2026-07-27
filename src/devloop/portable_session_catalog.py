from __future__ import annotations

import json
import os
import re
import sqlite3
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
                or value <= 0
            ):
                raise ValueError(
                    f"Portable planning {field_name} must be a positive number."
                )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PortablePlanningSettings:
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
    arguments: tuple[str, ...]
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
        arguments_json = json.dumps(list(launch.arguments), separators=(",", ":"))
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO saved_projects (
                        project_id, checkout, created_at, updated_at
                    ) VALUES (?, ?, unixepoch('subsec'), unixepoch('subsec'))
                    ON CONFLICT(checkout) DO UPDATE SET
                        updated_at = excluded.updated_at
                    """,
                    (project_id, str(checkout)),
                )
                connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, project_id, status, operation, arguments_json,
                        planning_settings_json, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, unixepoch('subsec'), unixepoch('subsec')
                    )
                    """,
                    (
                        launch.session_id,
                        project_id,
                        PortableSessionStatus.READY.value,
                        launch.operation.value,
                        arguments_json,
                        settings_json,
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
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("Planning thread identity must be non-empty text.")
        self._update_session(
            session_id,
            "planning_thread_id = ?, updated_at = unixepoch('subsec')",
            (thread_id,),
        )

    def bind_session_checkout(self, session_id: str, checkout: Path) -> None:
        _validate_session_id(session_id)
        canonical_checkout = checkout.resolve()
        if not canonical_checkout.is_dir():
            raise ValueError(
                f"Portable session checkout does not exist: {canonical_checkout}"
            )
        project_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, canonical_checkout.as_uri())
        )
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO saved_projects (
                        project_id, checkout, created_at, updated_at
                    ) VALUES (?, ?, unixepoch('subsec'), unixepoch('subsec'))
                    ON CONFLICT(checkout) DO UPDATE SET
                        updated_at = excluded.updated_at
                    """,
                    (project_id, str(canonical_checkout)),
                )
                cursor = connection.execute(
                    """
                    UPDATE sessions
                    SET project_id = ?, updated_at = unixepoch('subsec')
                    WHERE session_id = ?
                    """,
                    (project_id, session_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(f"Unknown portable session: {session_id}")
        except KeyError:
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
        arguments_json = json.dumps(
            ["--prd", str(canonical_prd)],
            separators=(",", ":"),
        )
        self._update_session(
            session_id,
            """
            prd_path = ?, issues_index_path = ?, activity_summary = ?,
            arguments_json = ?, planning_thread_id = NULL,
            planning_settings_json = NULL, updated_at = unixepoch('subsec')
            """,
            (
                str(canonical_prd),
                str(canonical_issues),
                bounded_summary,
                arguments_json,
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
            "planning_settings_json = ?, updated_at = unixepoch('subsec')",
            (json.dumps(settings.to_dict(), separators=(",", ":")),),
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
            status = ?, activity_summary = ?, updated_at = unixepoch('subsec')
            """,
            (
                status.value,
                redact_persisted_evidence(activity_summary)[:500],
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
                if version == 0:
                    connection.executescript(
                        """
                        BEGIN IMMEDIATE;
                        CREATE TABLE saved_projects (
                            project_id TEXT PRIMARY KEY,
                            checkout TEXT NOT NULL UNIQUE,
                            created_at REAL NOT NULL,
                            updated_at REAL NOT NULL
                        );
                        CREATE TABLE sessions (
                            session_id TEXT PRIMARY KEY,
                            project_id TEXT NOT NULL
                                REFERENCES saved_projects(project_id),
                            status TEXT NOT NULL,
                            operation TEXT NOT NULL,
                            arguments_json TEXT NOT NULL,
                            planning_thread_id TEXT,
                            planning_settings_json TEXT,
                            prd_path TEXT,
                            issues_index_path TEXT,
                            activity_summary TEXT NOT NULL DEFAULT '',
                            created_at REAL NOT NULL,
                            updated_at REAL NOT NULL
                        );
                        PRAGMA user_version = 1;
                        COMMIT;
                        """
                    )
                self._validate_schema(connection)
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
        expected_tables = {"saved_projects", "sessions"}
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not expected_tables.issubset(tables):
            raise PortableSessionCatalogError(
                "Portable Session Catalog schema is incomplete."
            )


def _session_from_row(row: sqlite3.Row) -> PortableCatalogSession:
    try:
        arguments_value = json.loads(row["arguments_json"])
        if not isinstance(arguments_value, list) or not all(
            isinstance(argument, str) for argument in arguments_value
        ):
            raise ValueError
        settings_text = row["planning_settings_json"]
        settings = (
            PortablePlanningSettings.from_mapping(json.loads(settings_text))
            if settings_text is not None
            else None
        )
        return PortableCatalogSession(
            session_id=row["session_id"],
            project_id=row["project_id"],
            checkout=Path(row["checkout"]),
            status=PortableSessionStatus(row["status"]),
            operation=PortableWorkflowOperation(row["operation"]),
            arguments=tuple(arguments_value),
            planning_thread_id=row["planning_thread_id"],
            planning_settings=settings,
            prd_path=Path(row["prd_path"]) if row["prd_path"] else None,
            issues_index_path=(
                Path(row["issues_index_path"]) if row["issues_index_path"] else None
            ),
            activity_summary=row["activity_summary"],
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
