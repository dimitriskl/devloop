from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from .execution_backend_id import parse_execution_backend_id
from .issue_pack import canonical_issue_index_for_prd, find_repo_root
from .portable_launch_target import launch_creates_checkout
from .portable_sessions import (
    PortableSessionLaunch,
    PortableSessionProgress,
    PortableSessionStatus,
    PortableWorkflowOperation,
    PortableWorktreeLease,
    PortableWorktreeLeaseConflict,
)
from .portable_workflow import ExecutionBudget, FastPreference, StepExecutionSettings
from .redaction import redact_persisted_evidence
from .subprocess_utils import (
    ProcessIdentity,
    ProcessTreeIdentity,
    ProcessTreeKind,
    ProcessTreeState,
    capture_process_identity,
    process_identity_state,
    process_tree_identity_state,
)

CATALOG_SCHEMA_VERSION = 8
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
class _CatalogIndex:
    name: str
    sql: str
    list_metadata: tuple[object, ...]
    xinfo: tuple[tuple[object, ...], ...]


def _user_index_snapshot(
    connection: sqlite3.Connection,
    table: str = "worktree_leases",
) -> tuple[_CatalogIndex, ...]:
    indexes: list[_CatalogIndex] = []
    for row in connection.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'index'
            AND tbl_name = ?
            AND sql IS NOT NULL
        ORDER BY name
        """,
        (table,),
    ):
        name = row["name"]
        sql = row["sql"]
        list_metadata = connection.execute(
            """
            SELECT name, \"unique\", origin, partial
            FROM pragma_index_list(?)
            WHERE name = ?
            """,
            (table, name),
        ).fetchone()
        if not isinstance(name, str) or not isinstance(sql, str) or list_metadata is None:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog {table} index metadata is unreadable."
            )
        indexes.append(
            _CatalogIndex(
                name=name,
                sql=sql,
                list_metadata=tuple(list_metadata),
                xinfo=tuple(
                    tuple(metadata)
                    for metadata in connection.execute(
                        """
                        SELECT seqno, cid, name, desc, coll, key
                        FROM pragma_index_xinfo(?)
                        ORDER BY seqno
                        """,
                        (name,),
                    )
                ),
            )
        )
    return tuple(indexes)


def _rebuild_sessions_for_v7(connection: sqlite3.Connection) -> None:
    indexes = _user_index_snapshot(connection, "sessions")
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(sessions)")
    }
    connection.execute("PRAGMA legacy_alter_table = ON")
    try:
        connection.execute("ALTER TABLE sessions RENAME TO sessions_v6")
        connection.execute(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY
                    CHECK (length(session_id) BETWEEN 1 AND 128),
                project_id TEXT NOT NULL REFERENCES saved_projects(project_id),
                status TEXT NOT NULL CHECK (status IN (
                    'READY', 'QUEUED', 'RUNNING', 'WAITING_FOR_INPUT', 'PAUSING',
                    'PAUSED', 'INTERRUPTED', 'COMPLETED', 'FAILED', 'CANCELLED',
                    'UNAVAILABLE'
                )),
                operation TEXT NOT NULL CHECK (operation IN ('PLANNING', 'DELIVERY')),
                arguments_json TEXT NOT NULL
                    CHECK (length(arguments_json) BETWEEN 2 AND 4096),
                planning_thread_id TEXT CHECK (
                    planning_thread_id IS NULL OR length(planning_thread_id) = 36
                ),
                planning_settings_json TEXT CHECK (
                    planning_settings_json IS NULL
                    OR length(planning_settings_json) BETWEEN 2 AND 4096
                ),
                prd_path TEXT CHECK (
                    prd_path IS NULL OR length(prd_path) BETWEEN 1 AND 4096
                ),
                issues_index_path TEXT CHECK (
                    issues_index_path IS NULL
                    OR length(issues_index_path) BETWEEN 1 AND 4096
                ),
                activity_summary TEXT NOT NULL DEFAULT ''
                    CHECK (length(activity_summary) <= 500),
                worktree_available INTEGER NOT NULL DEFAULT 1
                    CHECK (worktree_available IN (0, 1)),
                unavailable_from_status TEXT CHECK (
                    unavailable_from_status IS NULL OR unavailable_from_status IN (
                        'READY', 'QUEUED', 'RUNNING', 'WAITING_FOR_INPUT', 'PAUSING',
                        'PAUSED', 'INTERRUPTED', 'COMPLETED', 'FAILED', 'CANCELLED'
                    )
                ),
                result INTEGER CHECK (
                    result IS NULL OR typeof(result) = 'integer'
                ),
                progress_stage TEXT NOT NULL DEFAULT ''
                    CHECK (length(progress_stage) <= 200),
                completed_issues INTEGER NOT NULL DEFAULT 0
                    CHECK (typeof(completed_issues) = 'integer'
                        AND completed_issues >= 0),
                total_issues INTEGER NOT NULL DEFAULT 0
                    CHECK (typeof(total_issues) = 'integer' AND total_issues >= 0),
                active_issue TEXT CHECK (
                    active_issue IS NULL OR length(active_issue) <= 128
                ),
                created_at REAL NOT NULL CHECK (
                    typeof(created_at) IN ('integer', 'real') AND created_at >= 0
                ),
                updated_at REAL NOT NULL CHECK (
                    typeof(updated_at) IN ('integer', 'real') AND updated_at >= 0
                ),
                revision INTEGER NOT NULL DEFAULT 1
                    CHECK (typeof(revision) = 'integer' AND revision > 0),
                CHECK (completed_issues <= total_issues)
            )
            """
        )
        if "worktree_available" in existing_columns:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id, project_id, status, operation, arguments_json,
                    planning_thread_id, planning_settings_json, prd_path,
                    issues_index_path, activity_summary, worktree_available,
                    unavailable_from_status, result, progress_stage,
                    completed_issues, total_issues, active_issue, created_at,
                    updated_at, revision
                )
                SELECT session_id, project_id, status, operation, arguments_json,
                    planning_thread_id, planning_settings_json, prd_path,
                    issues_index_path, activity_summary, worktree_available,
                    unavailable_from_status, result, progress_stage,
                    completed_issues, total_issues, active_issue, created_at,
                    updated_at, revision
                FROM sessions_v6
                """
            )
        else:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id, project_id, status, operation, arguments_json,
                    planning_thread_id, planning_settings_json, prd_path,
                    issues_index_path, activity_summary, created_at, updated_at, revision
                )
                SELECT session_id, project_id, status, operation, arguments_json,
                    planning_thread_id, planning_settings_json, prd_path,
                    issues_index_path, activity_summary, created_at, updated_at, revision
                FROM sessions_v6
                """
            )
            connection.execute(
                """
                UPDATE sessions
                SET unavailable_from_status = COALESCE(unavailable_from_status, 'READY'),
                    worktree_available = 0
                WHERE status = 'UNAVAILABLE'
                """
            )
            connection.execute(
                """
                UPDATE sessions SET status = unavailable_from_status
                WHERE worktree_available = 0
                """
            )
        connection.execute("DROP TABLE sessions_v6")
        for index in indexes:
            connection.execute(index.sql)
        if _user_index_snapshot(connection, "sessions") != indexes:
            raise PortableSessionCatalogError(
                "Portable Session Catalog session indexes changed during schema migration."
            )
    finally:
        connection.execute("PRAGMA legacy_alter_table = OFF")


def _sessions_requires_v7_rebuild(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchone()
    if row is None or not isinstance(row["sql"], str):
        return False
    columns = tuple(
        column["name"] for column in connection.execute("PRAGMA table_info(sessions)")
    )
    expected = (
        "session_id", "project_id", "status", "operation", "arguments_json",
        "planning_thread_id", "planning_settings_json", "prd_path",
        "issues_index_path", "activity_summary", "worktree_available",
        "unavailable_from_status", "result", "progress_stage", "completed_issues",
        "total_issues", "active_issue", "created_at", "updated_at", "revision",
    )
    schema = re.sub(r"\s+", "", row["sql"].lower())
    return set(columns) == set(expected) and (
        "check(completed_issues<=total_issues)" not in schema
    )


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
    argument_base: str | None = None

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
            ("argument base", self.argument_base),
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
        argument_base: Path | None = None,
    ) -> PortableLaunchSettings:
        values = tuple(arguments)
        selected: dict[str, object] = {
            "argument_base": str((argument_base or checkout).resolve()),
        }
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
            "--single-issue",
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
                        option_value = values[index + 1]
                        index += 2
                    else:
                        option_value = argument[len(delivery_value_option) + 1 :]
                        index += 1
                    if delivery_value_option == "--worktree-path":
                        option_path = Path(option_value).expanduser()
                        if not option_path.is_absolute():
                            option_path = checkout / option_path
                        option_value = str(option_path.resolve())
                    delivery_options.extend((delivery_value_option, option_value))
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
            if delivery_prd_path is None:
                raise ValueError("Portable delivery launch requires --prd.")
            if delivery_issues_path is None:
                delivery_issues_path = canonical_issue_index_for_prd(
                    delivery_prd_path
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
        current_keys = expected_keys | {"argument_base"}
        if frozenset(value) not in {
            frozenset(legacy_keys),
            frozenset(expected_keys),
            frozenset(current_keys),
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
            argument_base_value = value.get("argument_base")
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
            if argument_base_value is not None and not isinstance(
                argument_base_value,
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
                argument_base=argument_base_value,
            )
        except (TypeError, ValueError) as error:
            raise PortableSessionCatalogError(
                "Portable launch settings are corrupt."
            ) from error


def _relinked_launch_settings(
    settings: PortableLaunchSettings,
    *,
    old_checkout: Path,
    new_checkout: Path,
) -> PortableLaunchSettings:
    options = list(settings.delivery_options)
    for index, option in enumerate(options[:-1]):
        if option == "--worktree-path":
            options[index + 1] = _relinked_path_text(
                options[index + 1],
                old_checkout=old_checkout,
                new_checkout=new_checkout,
            )
    return replace(
        settings,
        delivery_prd_path=_relinked_optional_path_text(
            settings.delivery_prd_path,
            old_checkout=old_checkout,
            new_checkout=new_checkout,
        ),
        delivery_issues_path=_relinked_optional_path_text(
            settings.delivery_issues_path,
            old_checkout=old_checkout,
            new_checkout=new_checkout,
        ),
        delivery_options=tuple(options),
        argument_base=str(new_checkout.resolve()),
    )


def _bound_launch_settings(
    settings: PortableLaunchSettings,
    *,
    operation: PortableWorkflowOperation,
    checkout: Path,
    prd_path: Path | None,
    issues_index_path: Path | None,
) -> PortableLaunchSettings:
    options = settings.delivery_options
    if (
        operation is PortableWorkflowOperation.DELIVERY
        and prd_path is not None
        and issues_index_path is not None
        and "--create-worktree" in options
    ):
        retained: list[str] = []
        index = 0
        while index < len(options):
            option = options[index]
            if option == "--create-worktree":
                index += 1
                continue
            if option in {"--worktree-path", "--branch-name"}:
                index += 2
                continue
            retained.append(option)
            index += 1
        options = tuple(retained)
    return replace(
        settings,
        delivery_prd_path=(
            str(prd_path.resolve())
            if prd_path is not None
            else settings.delivery_prd_path
        ),
        delivery_issues_path=(
            str(issues_index_path.resolve())
            if issues_index_path is not None
            else settings.delivery_issues_path
        ),
        delivery_options=options,
        argument_base=str(checkout.resolve()),
    )


def _relinked_optional_path_text(
    value: str | None,
    *,
    old_checkout: Path,
    new_checkout: Path,
) -> str | None:
    if value is None:
        return None
    return _relinked_path_text(
        value,
        old_checkout=old_checkout,
        new_checkout=new_checkout,
    )


def _relinked_path_text(
    value: str,
    *,
    old_checkout: Path,
    new_checkout: Path,
) -> str:
    path = Path(value)
    if not path.is_absolute():
        return value
    resolved = path.resolve()
    old_root = old_checkout.resolve()
    if not resolved.is_relative_to(old_root):
        return str(resolved)
    return str(new_checkout.resolve() / resolved.relative_to(old_root))


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
    unavailable_from_status: PortableSessionStatus | None
    result: int | None
    progress: PortableSessionProgress
    created_at: float
    updated_at: float
    revision: int

    @property
    def launch(self) -> PortableSessionLaunch:
        argument_base = self._launch_argument_base()
        return PortableSessionLaunch(
            session_id=self.session_id,
            checkout=self.checkout,
            operation=self.operation,
            arguments=self.arguments,
            argument_base=argument_base,
        )

    def _launch_argument_base(self) -> Path:
        if self.launch_settings.argument_base is not None:
            return Path(self.launch_settings.argument_base).resolve()
        if self.checkout.is_dir():
            try:
                checkout_root = find_repo_root(self.checkout)
            except RuntimeError:
                checkout_root = None
            if checkout_root == self.checkout.resolve():
                return checkout_root
        source_paths = (
            self.launch_settings.delivery_prd_path,
            self.launch_settings.delivery_issues_path,
        )
        checkouts: set[Path] = set()
        for source_path in source_paths:
            if source_path is None:
                continue
            candidate = Path(source_path)
            if not candidate.is_absolute() or not candidate.is_file():
                continue
            try:
                checkouts.add(find_repo_root(candidate.parent))
            except RuntimeError:
                continue
        if len(checkouts) == 1:
            return checkouts.pop().resolve()
        raise PortableSessionCatalogError(
            "Portable launch has no trusted existing argument base."
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
class PortableRelinkReceipt:
    session_id: str
    source_checkout: Path
    source_prd_path: Path
    source_issues_index_path: Path
    target_checkout: Path
    target_prd_path: Path
    target_issues_index_path: Path
    state_sha256: str


@dataclass(frozen=True)
class PortableAdoptionSession:
    session_id: str
    checkout: Path
    prd_path: Path
    issues_index_path: Path
    completed_issues: int
    total_issues: int
    active_issue: str | None
    active_stage: str | None


@dataclass(frozen=True)
class PortableAdoptionRequest:
    receipt_id: str
    source_version: str
    configuration_path: Path
    configuration_sha256: str
    projects: tuple[Path, ...]
    sessions: tuple[PortableAdoptionSession, ...]
    unavailable_projects: tuple[Path, ...] = ()


@dataclass(frozen=True)
class PortableAdoptionReceipt:
    receipt_id: str
    schema_version: int
    source_version: str
    configuration_path: Path
    configuration_sha256: str
    created_at: float


@dataclass(frozen=True)
class PortableAdoptionCommit:
    adopted_projects: frozenset[Path]
    adopted_sessions: frozenset[str]
    receipt_created: bool


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

    def __init__(
        self,
        path: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        process_probe: Callable[[ProcessIdentity], ProcessTreeState] = (
            process_identity_state
        ),
        process_tree_probe: Callable[
            [ProcessTreeIdentity, ProcessIdentity], ProcessTreeState
        ] = process_tree_identity_state,
        lease_timeout_seconds: float = 15.0,
    ) -> None:
        if not math.isfinite(lease_timeout_seconds) or lease_timeout_seconds <= 0:
            raise ValueError("Portable lease timeout must be a positive duration.")
        self.path = (path or portable_session_catalog_path()).resolve()
        self._clock = clock
        self._process_probe = process_probe
        self._process_tree_probe = process_tree_probe
        self._lease_timeout_seconds = lease_timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def adopt_projects(self, request: PortableAdoptionRequest) -> PortableAdoptionCommit:
        """Atomically add existing project pointers and the matching receipt."""
        _validate_catalog_text(request.receipt_id, maximum_length=128)
        _validate_catalog_text(request.source_version, maximum_length=32)
        _validate_catalog_path(str(request.configuration_path.resolve()))
        if not re.fullmatch(r"[0-9a-f]{64}", request.configuration_sha256):
            raise ValueError("Portable adoption configuration hash is invalid.")
        projects = tuple(dict.fromkeys(path.resolve() for path in request.projects))
        unavailable_projects = tuple(
            dict.fromkeys(path.resolve() for path in request.unavailable_projects)
        )
        unavailable_set = frozenset(unavailable_projects)
        if unavailable_set.difference(projects):
            raise ValueError("Unavailable adoption projects must be registered projects.")
        sessions = tuple(request.sessions)
        project_set = frozenset(projects)
        for session in sessions:
            _validate_session_id(session.session_id)
            checkout = session.checkout.resolve()
            prd_path = session.prd_path.resolve()
            issues_path = session.issues_index_path.resolve()
            if checkout not in project_set:
                raise ValueError("Portable adoption session has no registered project.")
            if not prd_path.is_relative_to(checkout) or not issues_path.is_relative_to(checkout):
                raise ValueError("Portable adoption workflow pointers must belong to the checkout.")
            if not prd_path.is_file() or not issues_path.is_file():
                raise ValueError(
                    "Portable adoption workflow pointers must reference existing files."
                )
        timestamp = self._clock()
        adopted_projects: set[Path] = set()
        adopted_sessions: set[str] = set()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for checkout in projects:
                    project_id = str(uuid.uuid5(uuid.NAMESPACE_URL, checkout.as_uri()))
                    cursor = connection.execute(
                        """
                        INSERT INTO saved_projects (
                            project_id, checkout, created_at, updated_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(checkout) DO NOTHING
                        """,
                        (project_id, str(checkout), timestamp, timestamp),
                    )
                    if cursor.rowcount == 1:
                        adopted_projects.add(checkout)
                    stored = connection.execute(
                        "SELECT project_id FROM saved_projects WHERE checkout = ?",
                        (str(checkout),),
                    ).fetchone()
                    if stored is None or stored["project_id"] != project_id:
                        raise PortableSessionCatalogError(
                            "Portable adoption project identity conflicts with the catalog."
                        )
                for session in sessions:
                    checkout = session.checkout.resolve()
                    project_id = str(uuid.uuid5(uuid.NAMESPACE_URL, checkout.as_uri()))
                    launch_settings = PortableLaunchSettings.from_arguments(
                        ("--prd", str(session.prd_path.resolve())),
                        operation=PortableWorkflowOperation.PLANNING,
                        checkout=checkout,
                        argument_base=checkout,
                    )
                    cursor = connection.execute(
                        """
                        INSERT INTO sessions (
                            session_id, project_id, status, operation, arguments_json,
                            prd_path, issues_index_path, activity_summary,
                            progress_stage, completed_issues, total_issues, active_issue,
                            created_at, updated_at, revision
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        ON CONFLICT(session_id) DO NOTHING
                        """,
                        (
                            session.session_id,
                            project_id,
                            PortableSessionStatus.READY.value,
                            PortableWorkflowOperation.PLANNING.value,
                            json.dumps(launch_settings.to_dict(), separators=(",", ":")),
                            str(session.prd_path.resolve()),
                            str(session.issues_index_path.resolve()),
                            "Adopted unfinished project workflow",
                            session.active_stage or "",
                            session.completed_issues,
                            session.total_issues,
                            session.active_issue,
                            timestamp,
                            timestamp,
                        ),
                    )
                    if cursor.rowcount == 1:
                        adopted_sessions.add(session.session_id)
                        connection.execute(
                            """
                            INSERT INTO session_revision_counters (session_id, revision)
                            VALUES (?, 1)
                            """,
                            (session.session_id,),
                        )
                    stored = connection.execute(
                        """
                        SELECT project_id, prd_path, issues_index_path
                        FROM sessions WHERE session_id = ?
                        """,
                        (session.session_id,),
                    ).fetchone()
                    if (
                        stored is None
                        or stored["project_id"] != project_id
                        or Path(stored["prd_path"]).resolve() != session.prd_path.resolve()
                        or Path(stored["issues_index_path"]).resolve()
                        != session.issues_index_path.resolve()
                    ):
                        raise PortableSessionCatalogError(
                            "Portable adoption session identity conflicts with the catalog."
                        )
                for checkout in unavailable_projects:
                    project_id = str(uuid.uuid5(uuid.NAMESPACE_URL, checkout.as_uri()))
                    session_id = str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"{checkout.as_uri()}#v0.2.1-unavailable")
                    )
                    launch_settings = PortableLaunchSettings.from_arguments(
                        ("--repo", str(checkout)),
                        operation=PortableWorkflowOperation.PLANNING,
                        checkout=checkout,
                        argument_base=checkout,
                    )
                    cursor = connection.execute(
                        """
                        INSERT INTO sessions (
                            session_id, project_id, status, operation, arguments_json,
                            activity_summary, worktree_available, unavailable_from_status,
                            created_at, updated_at, revision
                        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 1)
                        ON CONFLICT(session_id) DO NOTHING
                        """,
                        (
                            session_id,
                            project_id,
                            PortableSessionStatus.READY.value,
                            PortableWorkflowOperation.PLANNING.value,
                            json.dumps(launch_settings.to_dict(), separators=(",", ":")),
                            "Imported checkout is unavailable; Relink or Forget",
                            PortableSessionStatus.READY.value,
                            timestamp,
                            timestamp,
                        ),
                    )
                    if cursor.rowcount == 1:
                        adopted_sessions.add(session_id)
                        connection.execute(
                            """
                            INSERT INTO session_revision_counters (session_id, revision)
                            VALUES (?, 1)
                            """,
                            (session_id,),
                        )
                    stored = connection.execute(
                        """
                        SELECT project_id, worktree_available, prd_path, issues_index_path
                        FROM sessions WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    if (
                        stored is None
                        or stored["project_id"] != project_id
                        or stored["worktree_available"] != 0
                        or stored["prd_path"] is not None
                        or stored["issues_index_path"] is not None
                    ):
                        raise PortableSessionCatalogError(
                            "Portable unavailable adoption identity conflicts with the catalog."
                        )
                receipt_cursor = connection.execute(
                    """
                    INSERT INTO project_adoption_receipts (
                        receipt_id, schema_version, source_version, configuration_path,
                        configuration_sha256, created_at
                    ) VALUES (?, 1, ?, ?, ?, ?)
                    ON CONFLICT(receipt_id) DO NOTHING
                    """,
                    (
                        request.receipt_id,
                        request.source_version,
                        str(request.configuration_path.resolve()),
                        request.configuration_sha256,
                        timestamp,
                    ),
                )
                stored_receipt = connection.execute(
                    """
                    SELECT schema_version, source_version, configuration_path,
                        configuration_sha256
                    FROM project_adoption_receipts WHERE receipt_id = ?
                    """,
                    (request.receipt_id,),
                ).fetchone()
                if (
                    stored_receipt is None
                    or stored_receipt["schema_version"] != 1
                    or stored_receipt["source_version"] != request.source_version
                    or Path(stored_receipt["configuration_path"]).resolve()
                    != request.configuration_path.resolve()
                    or stored_receipt["configuration_sha256"]
                    != request.configuration_sha256
                ):
                    raise PortableSessionCatalogError(
                        "Portable adoption receipt identity conflicts with the catalog."
                    )
        except PortableSessionCatalogError:
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error
        return PortableAdoptionCommit(
            adopted_projects=frozenset(adopted_projects),
            adopted_sessions=frozenset(adopted_sessions),
            receipt_created=receipt_cursor.rowcount == 1,
        )

    def list_adoption_receipts(self) -> tuple[PortableAdoptionReceipt, ...]:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM project_adoption_receipts
                    ORDER BY created_at, receipt_id
                    """
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog read failed: {error}"
            ) from error
        return tuple(
            PortableAdoptionReceipt(
                receipt_id=row["receipt_id"],
                schema_version=row["schema_version"],
                source_version=row["source_version"],
                configuration_path=Path(row["configuration_path"]),
                configuration_sha256=row["configuration_sha256"],
                created_at=row["created_at"],
            )
            for row in rows
        )

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
            checkout=launch.argument_base or checkout,
            argument_base=launch.argument_base,
        )
        arguments_json = json.dumps(
            launch_settings.to_dict(),
            separators=(",", ":"),
        )
        timestamp = self._clock()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
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
                revision = self._next_session_revision(
                    connection,
                    launch.session_id,
                )
                connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, project_id, status, operation, arguments_json,
                        planning_settings_json, created_at, updated_at, revision
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                        revision,
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
        process_identity: ProcessIdentity | None = None,
        worker_generation: int | None = None,
        planning_settings: PortablePlanningSettings | None = None,
    ) -> PortableCatalogSession:
        """Atomically register a selected checkout, session, and live owner."""
        _validate_session_id(launch.session_id)
        _validate_owner_id(owner_id)
        checkout = launch.checkout.resolve()
        if not checkout.is_dir() and not launch_creates_checkout(launch, checkout):
            raise ValueError(f"Portable session checkout does not exist: {checkout}")
        identity = _resolve_process_identity(process_id, process_identity)
        active_process_id = identity.pid if identity is not None else process_id
        if active_process_id is None:
            active_process_id = os.getpid()
        _validate_process_id(active_process_id)
        _validate_worker_generation(worker_generation)
        project_id = str(uuid.uuid5(uuid.NAMESPACE_URL, checkout.as_uri()))
        launch_settings_json = json.dumps(
            PortableLaunchSettings.from_arguments(
                launch.arguments,
                operation=launch.operation,
                checkout=launch.argument_base or checkout,
                argument_base=launch.argument_base,
            ).to_dict(),
            separators=(",", ":"),
        )
        planning_settings_json = (
            _serialize_planning_settings(planning_settings)
            if planning_settings is not None
            else None
        )
        timestamp = self._clock()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM worktree_leases WHERE checkout = ?",
                    (str(checkout),),
                ).fetchone()
                if existing is not None:
                    self._reclaim_expired_lease(
                        connection,
                        _lease_from_row(existing),
                        now=timestamp,
                    )
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
                revision = self._next_session_revision(
                    connection,
                    launch.session_id,
                )
                connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, project_id, status, operation, arguments_json,
                        planning_settings_json, created_at, updated_at, revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        revision,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO worktree_leases (
                        checkout, session_id, owner_id, process_id,
                        acquired_at, heartbeat_at, process_start_fingerprint,
                        worker_generation, worker_process_id,
                        worker_process_start_fingerprint, process_tree_kind,
                        process_tree_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(checkout),
                        launch.session_id,
                        owner_id,
                        active_process_id,
                        timestamp,
                        timestamp,
                        identity.creation_time if identity is not None else None,
                        worker_generation,
                        active_process_id if worker_generation is not None else None,
                        (
                            identity.creation_time
                            if identity is not None and worker_generation is not None
                            else None
                        ),
                        (
                            ProcessTreeKind.ROOT_PROCESS.value
                            if worker_generation is not None
                            else None
                        ),
                        active_process_id if worker_generation is not None else None,
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
        process_identity: ProcessIdentity | None = None,
        worker_generation: int | None = None,
    ) -> PortableWorktreeLease:
        _validate_session_id(session_id)
        _validate_owner_id(owner_id)
        identity = _resolve_process_identity(process_id, process_identity)
        active_process_id = identity.pid if identity is not None else process_id
        if active_process_id is None:
            active_process_id = os.getpid()
        _validate_process_id(active_process_id)
        _validate_worker_generation(worker_generation)
        timestamp = self._clock()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                session = connection.execute(
                    """
                    SELECT p.checkout, s.status, s.worktree_available
                    FROM sessions AS s
                    JOIN saved_projects AS p ON p.project_id = s.project_id
                    WHERE s.session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise KeyError(f"Unknown portable session: {session_id}")
                if session["worktree_available"] == 0:
                    raise ValueError(
                        "Portable session is unavailable; Relink its saved worktree first."
                    )
                existing = connection.execute(
                    "SELECT * FROM worktree_leases WHERE checkout = ?",
                    (session["checkout"],),
                ).fetchone()
                if existing is not None:
                    lease = _lease_from_row(existing)
                    if (
                        lease.session_id == session_id
                        and lease.owner_id == owner_id
                        and (
                            identity is None
                            or (
                                lease.process_id == identity.pid
                                and lease.process_start_fingerprint
                                == identity.creation_time
                                and lease.worker_generation == worker_generation
                            )
                        )
                    ):
                        return lease
                    self._reclaim_expired_lease(
                        connection,
                        lease,
                        now=timestamp,
                    )
                connection.execute(
                    """
                    INSERT INTO worktree_leases (
                        checkout, session_id, owner_id, process_id,
                        acquired_at, heartbeat_at, process_start_fingerprint,
                        worker_generation, worker_process_id,
                        worker_process_start_fingerprint, process_tree_kind,
                        process_tree_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session["checkout"],
                        session_id,
                        owner_id,
                        active_process_id,
                        timestamp,
                        timestamp,
                        identity.creation_time if identity is not None else None,
                        worker_generation,
                        active_process_id if worker_generation is not None else None,
                        (
                            identity.creation_time
                            if identity is not None and worker_generation is not None
                            else None
                        ),
                        (
                            ProcessTreeKind.ROOT_PROCESS.value
                            if worker_generation is not None
                            else None
                        ),
                        active_process_id if worker_generation is not None else None,
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

    def renew_worktree_lease(
        self,
        session_id: str,
        *,
        owner_id: str,
        process_identity: ProcessIdentity,
        worker_generation: int,
        heartbeat_at: float | None = None,
    ) -> PortableWorktreeLease:
        """Renew only an exact live worker lease; session state is untouched."""
        _validate_session_id(session_id)
        _validate_owner_id(owner_id)
        _validate_process_identity(process_identity)
        _validate_worker_generation(worker_generation, required=True)
        timestamp = self._clock() if heartbeat_at is None else heartbeat_at
        _validate_timestamp(timestamp)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE worktree_leases
                    SET heartbeat_at = ?
                    WHERE session_id = ?
                      AND owner_id = ?
                      AND worker_process_id = ?
                      AND worker_process_start_fingerprint = ?
                      AND worker_generation = ?
                    """,
                    (
                        timestamp,
                        session_id,
                        owner_id,
                        process_identity.pid,
                        process_identity.creation_time,
                        worker_generation,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PortableSessionCatalogError(
                        "Portable worker heartbeat does not own the exact "
                        "session lease generation."
                    )
                row = connection.execute(
                    "SELECT * FROM worktree_leases WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                assert row is not None
        except PortableSessionCatalogError:
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error
        return _lease_from_row(row)

    def bind_worktree_lease_worker(
        self,
        session_id: str,
        *,
        owner_id: str,
        owner_process_identity: ProcessIdentity,
        process_tree_identity: ProcessTreeIdentity,
        worker_generation: int,
    ) -> PortableWorktreeLease:
        """Bind the exact worker tree to its unchanged supervisor bootstrap."""
        _validate_session_id(session_id)
        _validate_owner_id(owner_id)
        _validate_process_identity(owner_process_identity)
        _validate_process_tree_identity(process_tree_identity)
        _validate_worker_generation(worker_generation, required=True)
        timestamp = self._clock()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE worktree_leases
                    SET worker_generation = ?, worker_process_id = ?,
                        worker_process_start_fingerprint = ?,
                        process_tree_kind = ?, process_tree_id = ?,
                        heartbeat_at = ?
                    WHERE session_id = ? AND owner_id = ?
                      AND process_id = ?
                      AND process_start_fingerprint = ?
                    """,
                    (
                        worker_generation,
                        process_tree_identity.root.pid,
                        process_tree_identity.root.creation_time,
                        process_tree_identity.kind.value,
                        process_tree_identity.tree_id,
                        timestamp,
                        session_id,
                        owner_id,
                        owner_process_identity.pid,
                        owner_process_identity.creation_time,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PortableSessionCatalogError(
                        "Portable worker cannot bind an unowned session lease."
                    )
                row = connection.execute(
                    "SELECT * FROM worktree_leases WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                assert row is not None
        except PortableSessionCatalogError:
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error
        return _lease_from_row(row)

    def _reclaim_expired_lease(
        self,
        connection: sqlite3.Connection,
        lease: PortableWorktreeLease,
        *,
        now: float,
    ) -> None:
        """Reclaim one lease only from a confirmed-dead exact process identity."""
        if now - lease.heartbeat_at <= self._lease_timeout_seconds:
            raise PortableWorktreeLeaseConflict(
                lease,
                reason="the renewable heartbeat has not expired",
            )
        fingerprint = lease.process_start_fingerprint
        if fingerprint is None:
            raise PortableWorktreeLeaseConflict(
                lease,
                reason="stable owner process identity is unavailable",
            )
        owner_identity = ProcessIdentity(
            pid=lease.process_id,
            creation_time=fingerprint,
        )
        if lease.worker_generation is None:
            active_state = self._process_probe(owner_identity)
            active_label = "owner process"
        else:
            worker_identity = lease.worker_process_identity
            tree_identity = lease.process_tree_identity
            if worker_identity is None or tree_identity is None:
                raise PortableWorktreeLeaseConflict(
                    lease,
                    reason="exact worker process-tree identity is unavailable",
                )
            active_state = self._process_tree_probe(
                tree_identity,
                owner_identity,
            )
            active_label = "worker process tree"
        if active_state is ProcessTreeState.RUNNING:
            raise PortableWorktreeLeaseConflict(
                lease,
                reason=f"the exact {active_label} is still running",
            )
        if active_state is ProcessTreeState.UNKNOWN:
            raise PortableWorktreeLeaseConflict(
                lease,
                reason=f"{active_label} liveness is ambiguous",
            )
        connection.execute(
            "DELETE FROM execution_claims WHERE session_id = ?",
            (lease.session_id,),
        )
        connection.execute(
            "DELETE FROM execution_requests WHERE session_id = ?",
            (lease.session_id,),
        )
        connection.execute(
            """
            UPDATE sessions
            SET status = ?,
                unavailable_from_status = CASE
                    WHEN worktree_available = 0 THEN ?
                    ELSE unavailable_from_status
                END,
                activity_summary = ?, updated_at = ?,
                revision = revision + 1
            WHERE session_id = ?
            """,
            (
                PortableSessionStatus.INTERRUPTED.value,
                PortableSessionStatus.INTERRUPTED.value,
                "Worker owner confirmed dead",
                now,
                lease.session_id,
            ),
        )
        self._session_revision(connection, lease.session_id)
        cursor = connection.execute(
            """
            DELETE FROM worktree_leases
            WHERE checkout = ?
              AND session_id = ?
              AND owner_id = ?
              AND process_id = ?
              AND process_start_fingerprint = ?
              AND heartbeat_at = ?
            """,
            (
                str(lease.checkout),
                lease.session_id,
                lease.owner_id,
                lease.process_id,
                fingerprint,
                lease.heartbeat_at,
            ),
        )
        if cursor.rowcount != 1:
            raise PortableWorktreeLeaseConflict(lease)

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
                checkout=launch.argument_base or checkout,
                argument_base=launch.argument_base,
            ).to_dict(),
            separators=(",", ":"),
        )
        timestamp = time.time()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
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
                revision = self._next_session_revision(
                    connection,
                    launch.session_id,
                )
                connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, project_id, status, operation, arguments_json,
                        created_at, updated_at, revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        project_id = excluded.project_id,
                        updated_at = excluded.updated_at,
                        revision = excluded.revision
                    """,
                    (
                        launch.session_id,
                        project_id,
                        PortableSessionStatus.READY.value,
                        launch.operation.value,
                        launch_settings_json,
                        timestamp,
                        timestamp,
                        revision,
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

    def save_planning_thread(self, session_id: str, thread_id: str) -> int:
        _validate_session_id(session_id)
        _validate_thread_id(thread_id)
        return self._update_session(
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
    ) -> int:
        if (prd_path is None) != (issues_index_path is None):
            raise ValueError(
                "Portable workflow transfer requires both PRD and issue-index pointers."
            )
        if owner_id is not None:
            return self._transfer_session_lease(
                session_id,
                checkout,
                owner_id=owner_id,
                prd_path=prd_path,
                issues_index_path=issues_index_path,
                prepare_checkout=prepare_checkout,
            )
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
        return self.bind_or_create_session(
            PortableSessionLaunch(
                session_id=session_id,
                checkout=checkout,
                operation=record.operation,
                arguments=record.arguments,
            )
        ).revision

    def _transfer_session_lease(
        self,
        session_id: str,
        checkout: Path,
        *,
        owner_id: str,
        prd_path: Path | None,
        issues_index_path: Path | None,
        prepare_checkout: Callable[[], None] | None,
    ) -> int:
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
                session = connection.execute(
                    """
                    SELECT operation, arguments_json
                    FROM sessions
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise KeyError(f"Unknown portable session: {session_id}")
                try:
                    launch_settings_value = json.loads(session["arguments_json"])
                except (TypeError, ValueError) as error:
                    raise PortableSessionCatalogError(
                        "Portable launch settings are corrupt."
                    ) from error
                if not isinstance(launch_settings_value, dict):
                    raise PortableSessionCatalogError(
                        "Portable launch settings are corrupt."
                    )
                operation = PortableWorkflowOperation(session["operation"])
                launch_settings = PortableLaunchSettings.from_mapping(
                    launch_settings_value
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
                bound_launch_settings_json = json.dumps(
                    _bound_launch_settings(
                        launch_settings,
                        operation=operation,
                        checkout=canonical_checkout,
                        prd_path=canonical_prd,
                        issues_index_path=canonical_issues,
                    ).to_dict(),
                    separators=(",", ":"),
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
                    cursor = connection.execute(
                        """
                        UPDATE sessions
                        SET project_id = ?, arguments_json = ?, updated_at = ?,
                            revision = revision + 1
                        WHERE session_id = ?
                        """,
                        (
                            project_id,
                            bound_launch_settings_json,
                            timestamp,
                            session_id,
                        ),
                    )
                else:
                    cursor = connection.execute(
                        """
                        UPDATE sessions
                        SET project_id = ?, prd_path = ?, issues_index_path = ?,
                            arguments_json = ?, updated_at = ?, revision = revision + 1
                        WHERE session_id = ?
                        """,
                        (
                            project_id,
                            str(canonical_prd),
                            str(canonical_issues),
                            bound_launch_settings_json,
                            timestamp,
                            session_id,
                        ),
                    )
                if cursor.rowcount != 1:
                    raise KeyError(f"Unknown portable session: {session_id}")
                revision = self._session_revision(connection, session_id)
        except (KeyError, PortableSessionCatalogError, PortableWorktreeLeaseConflict):
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error
        return revision

    def publish_workflow(
        self,
        session_id: str,
        *,
        prd_path: Path,
        issues_index_path: Path,
        activity_summary: str,
    ) -> int:
        canonical_prd = prd_path.resolve()
        canonical_issues = issues_index_path.resolve()
        if not canonical_prd.is_file() or not canonical_issues.is_file():
            raise ValueError("Published workflow pointers must reference existing files.")
        bounded_summary = redact_persisted_evidence(activity_summary)[:500]
        return self._update_session(
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
        self.mark_missing_worktrees_unavailable()
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

    def mark_missing_worktrees_unavailable(self) -> tuple[str, ...]:
        """Retain missing saved checkouts as explicit, non-runnable records."""
        from .worktree import find_git_checkout

        timestamp = self._clock()
        unavailable: list[str] = []
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                projects = connection.execute(
                    "SELECT project_id, checkout FROM saved_projects"
                ).fetchall()
                for project in projects:
                    checkout = Path(project["checkout"])
                    if checkout.is_dir():
                        git_checkout = find_git_checkout(checkout)
                        if (
                            git_checkout is not None
                            and git_checkout.repo_root.resolve() == checkout.resolve()
                        ):
                            continue
                    rows = connection.execute(
                        """
                        SELECT session_id FROM sessions
                        WHERE project_id = ? AND worktree_available = 1
                        """,
                        (project["project_id"],),
                    ).fetchall()
                    for row in rows:
                        session_id = row["session_id"]
                        lease_row = connection.execute(
                            "SELECT * FROM worktree_leases WHERE session_id = ?",
                            (session_id,),
                        ).fetchone()
                        if lease_row is not None:
                            try:
                                self._reclaim_expired_lease(
                                    connection,
                                    _lease_from_row(lease_row),
                                    now=timestamp,
                                )
                            except PortableWorktreeLeaseConflict:
                                pass
                        remaining_lease = connection.execute(
                            "SELECT 1 FROM worktree_leases WHERE session_id = ?",
                            (session_id,),
                        ).fetchone()
                        if remaining_lease is None:
                            connection.execute(
                                "DELETE FROM execution_requests WHERE session_id = ?",
                                (session_id,),
                            )
                            connection.execute(
                                "DELETE FROM execution_claims WHERE session_id = ?",
                                (session_id,),
                            )
                        connection.execute(
                            """
                            UPDATE sessions SET worktree_available = 0,
                                unavailable_from_status = status,
                                activity_summary = ?, updated_at = ?,
                                revision = revision + 1
                            WHERE session_id = ?
                            """,
                            (
                                "Saved worktree is unavailable; Relink or Forget",
                                timestamp,
                                session_id,
                            ),
                        )
                        self._session_revision(connection, session_id)
                        unavailable.append(session_id)
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error
        return tuple(unavailable)

    def relink_unavailable_session(
        self,
        session_id: str,
        checkout: Path,
    ) -> tuple[str, ...]:
        """Atomically relink one unavailable saved-project group after validation."""
        _validate_session_id(session_id)
        canonical_checkout = checkout.expanduser().resolve()
        from .worktree import find_git_checkout

        git_checkout = find_git_checkout(canonical_checkout)
        if git_checkout is None or git_checkout.repo_root.resolve() != canonical_checkout:
            raise ValueError(
                "Relink requires an existing canonical Git checkout root: "
                f"{canonical_checkout}"
            )
        selected = self.get_session(session_id)
        if selected.status is not PortableSessionStatus.UNAVAILABLE:
            raise ValueError("Relink is available only for an UNAVAILABLE session.")
        project_sessions = tuple(
            record
            for record in self.list_sessions()
            if record.project_id == selected.project_id
        )
        existing_receipts = {
            record.session_id: self.get_relink_receipt(record.session_id)
            for record in project_sessions
        }
        remapped = {
            record.session_id: self._validate_relinked_workflow(
                record,
                old_checkout=selected.checkout,
                new_checkout=canonical_checkout,
                existing_receipt=existing_receipts[record.session_id],
            )
            for record in project_sessions
        }
        target_project_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, canonical_checkout.as_uri())
        )
        timestamp = self._clock()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    """
                    SELECT project_id, worktree_available, revision
                    FROM sessions WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if (
                    current is None
                    or current["project_id"] != selected.project_id
                    or current["worktree_available"] != 0
                    or current["revision"] != selected.revision
                ):
                    raise PortableSessionCatalogError(
                        "Unavailable session changed while Relink was being validated; retry."
                    )
                leased = connection.execute(
                    """
                    SELECT session_id FROM worktree_leases
                    WHERE session_id IN (
                        SELECT session_id FROM sessions WHERE project_id = ?
                    ) LIMIT 1
                    """,
                    (selected.project_id,),
                ).fetchone()
                if leased is not None:
                    raise PortableSessionCatalogError(
                        "Relink cannot change a saved project with an active worktree lease."
                    )
                connection.execute(
                    """
                    INSERT INTO saved_projects (
                        project_id, checkout, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(checkout) DO UPDATE SET updated_at = excluded.updated_at
                    """,
                    (
                        target_project_id,
                        str(canonical_checkout),
                        timestamp,
                        timestamp,
                    ),
                )
                target = connection.execute(
                    "SELECT project_id FROM saved_projects WHERE checkout = ?",
                    (str(canonical_checkout),),
                ).fetchone()
                assert target is not None
                target_project_id = target["project_id"]
                for record in project_sessions:
                    prd_path, issues_path, receipt = remapped[record.session_id]
                    launch_settings = _relinked_launch_settings(
                        record.launch_settings,
                        old_checkout=selected.checkout,
                        new_checkout=canonical_checkout,
                    )
                    cursor = connection.execute(
                        """
                        UPDATE sessions
                        SET project_id = ?, prd_path = ?, issues_index_path = ?,
                            arguments_json = ?,
                            worktree_available = 1,
                            unavailable_from_status = NULL, updated_at = ?,
                            revision = revision + 1
                        WHERE session_id = ? AND project_id = ?
                          AND worktree_available = 0
                          AND revision = ?
                        """,
                        (
                            target_project_id,
                            str(prd_path) if prd_path is not None else None,
                            str(issues_path) if issues_path is not None else None,
                            json.dumps(
                                launch_settings.to_dict(),
                                separators=(",", ":"),
                            ),
                            timestamp,
                            record.session_id,
                            selected.project_id,
                            record.revision,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise PortableSessionCatalogError(
                            "Saved project sessions changed while Relink was being "
                            "committed; retry."
                        )
                    self._session_revision(connection, record.session_id)
                    if receipt is not None:
                        connection.execute(
                            """
                            INSERT INTO session_relink_receipts (
                                session_id, source_checkout, source_prd_path,
                                source_issues_index_path, target_checkout,
                                target_prd_path, target_issues_index_path,
                                state_sha256
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(session_id) DO UPDATE SET
                                source_checkout = excluded.source_checkout,
                                source_prd_path = excluded.source_prd_path,
                                source_issues_index_path = excluded.source_issues_index_path,
                                target_checkout = excluded.target_checkout,
                                target_prd_path = excluded.target_prd_path,
                                target_issues_index_path = excluded.target_issues_index_path,
                                state_sha256 = excluded.state_sha256
                            """,
                            (
                                receipt.session_id,
                                str(receipt.source_checkout),
                                str(receipt.source_prd_path),
                                str(receipt.source_issues_index_path),
                                str(receipt.target_checkout),
                                str(receipt.target_prd_path),
                                str(receipt.target_issues_index_path),
                                receipt.state_sha256,
                            ),
                        )
                if selected.project_id != target_project_id:
                    connection.execute(
                        "DELETE FROM saved_projects WHERE project_id = ?",
                        (selected.project_id,),
                    )
        except PortableSessionCatalogError:
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error
        return tuple(record.session_id for record in project_sessions)

    def get_relink_receipt(self, session_id: str) -> PortableRelinkReceipt | None:
        _validate_session_id(session_id)
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM session_relink_receipts WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog read failed: {error}"
            ) from error
        if row is None:
            return None
        return PortableRelinkReceipt(
            session_id=row["session_id"],
            source_checkout=Path(row["source_checkout"]),
            source_prd_path=Path(row["source_prd_path"]),
            source_issues_index_path=Path(row["source_issues_index_path"]),
            target_checkout=Path(row["target_checkout"]),
            target_prd_path=Path(row["target_prd_path"]),
            target_issues_index_path=Path(row["target_issues_index_path"]),
            state_sha256=row["state_sha256"],
        )

    def clear_relink_receipt(self, session_id: str) -> None:
        _validate_session_id(session_id)
        try:
            with self._connection() as connection:
                connection.execute(
                    "DELETE FROM session_relink_receipts WHERE session_id = ?",
                    (session_id,),
                )
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error

    def forget_session(self, session_id: str) -> None:
        """Delete one completed or unavailable catalog record, never project files."""
        _validate_session_id(session_id)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                session = connection.execute(
                    """
                    SELECT project_id, status, worktree_available
                    FROM sessions WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise KeyError(f"Unknown portable session: {session_id}")
                status = PortableSessionStatus(session["status"])
                if (
                    status is not PortableSessionStatus.COMPLETED
                    and session["worktree_available"] != 0
                ):
                    raise ValueError(
                        "Forget is available only for completed History or "
                        "UNAVAILABLE records."
                    )
                lease = connection.execute(
                    "SELECT 1 FROM worktree_leases WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                claim = connection.execute(
                    "SELECT 1 FROM execution_claims WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                request = connection.execute(
                    "SELECT 1 FROM execution_requests WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if lease is not None or claim is not None or request is not None:
                    raise PortableSessionCatalogError(
                        "Forget cannot remove metadata while execution ownership exists."
                    )
                connection.execute(
                    "DELETE FROM sessions WHERE session_id = ?",
                    (session_id,),
                )
                connection.execute(
                    "DELETE FROM session_revision_counters WHERE session_id = ?",
                    (session_id,),
                )
                connection.execute(
                    """
                    DELETE FROM saved_projects
                    WHERE project_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM sessions WHERE project_id = ?
                      )
                    """,
                    (session["project_id"], session["project_id"]),
                )
        except (KeyError, PortableSessionCatalogError, ValueError):
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error

    @staticmethod
    def _validate_relinked_workflow(
        record: PortableCatalogSession,
        *,
        old_checkout: Path,
        new_checkout: Path,
        existing_receipt: PortableRelinkReceipt | None,
    ) -> tuple[Path | None, Path | None, PortableRelinkReceipt | None]:
        if record.prd_path is None and record.issues_index_path is None:
            return None, None, None
        if record.prd_path is None or record.issues_index_path is None:
            raise ValueError(
                f"Relink cannot validate incomplete workflow pointers for {record.session_id}."
            )
        try:
            prd_relative = record.prd_path.resolve().relative_to(old_checkout.resolve())
            issues_relative = record.issues_index_path.resolve().relative_to(
                old_checkout.resolve()
            )
        except ValueError as error:
            raise ValueError(
                f"Relink cannot map workflow pointers outside the saved checkout for "
                f"{record.session_id}."
            ) from error
        prd_path = (new_checkout / prd_relative).resolve()
        issues_path = (new_checkout / issues_relative).resolve()
        if not prd_path.is_file() or not issues_path.is_file():
            raise ValueError(
                f"Relink requires the referenced PRD and Issue Index for "
                f"{record.session_id}: {prd_path} ; {issues_path}"
            )
        from .issue_pack import parse_issue_index

        issues = parse_issue_index(issues_path)
        if not issues:
            raise ValueError(
                f"Relink requires an authoritative non-empty Issue Index for "
                f"{record.session_id}: {issues_path}"
            )
        loop_state_path = issues_path.with_name(f"{issues_path.stem}.loop.state.json")
        receipt: PortableRelinkReceipt | None = None
        state_paths = (loop_state_path, prd_path.parent / "devloop.status.json")
        for state_path in state_paths:
            if not state_path.is_file():
                continue
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(
                    f"Relink cannot validate authoritative workflow state: {state_path}"
                ) from error
            if not isinstance(state, dict):
                raise ValueError(
                    f"Relink requires workflow state to be a JSON object: {state_path}"
                )
            if state_path == loop_state_path:
                from .state import LoopStateWriter

                source_checkout = old_checkout.resolve()
                source_prd = record.prd_path.resolve()
                source_issues = record.issues_index_path.resolve()
                state_sha256 = hashlib.sha256(state_path.read_bytes()).hexdigest()
                if existing_receipt is not None:
                    if (
                        existing_receipt.target_checkout.resolve() != source_checkout
                        or existing_receipt.target_prd_path.resolve() != source_prd
                        or existing_receipt.target_issues_index_path.resolve() != source_issues
                        or existing_receipt.state_sha256 != state_sha256
                    ):
                        raise ValueError(
                            "Relink cannot validate the previous exact relocation receipt."
                        )
                    source_checkout = existing_receipt.source_checkout.resolve()
                    source_prd = existing_receipt.source_prd_path.resolve()
                    source_issues = existing_receipt.source_issues_index_path.resolve()
                LoopStateWriter(issues_path).durable_scheduling_checkpoint(
                    repo_root=new_checkout,
                    prd_path=prd_path,
                    relocated_from_repo_root=source_checkout,
                    relocated_from_prd_path=source_prd,
                    relocated_from_issues_index_path=source_issues,
                    relocated_state_sha256=state_sha256,
                )
                receipt = PortableRelinkReceipt(
                    session_id=record.session_id,
                    source_checkout=source_checkout,
                    source_prd_path=source_prd,
                    source_issues_index_path=source_issues,
                    target_checkout=new_checkout.resolve(),
                    target_prd_path=prd_path,
                    target_issues_index_path=issues_path,
                    state_sha256=state_sha256,
                )
        return prd_path, issues_path, receipt

    def save_planning_settings(
        self,
        session_id: str,
        settings: PortablePlanningSettings,
    ) -> int:
        return self._update_session(
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
    ) -> int:
        if not isinstance(status, PortableSessionStatus):
            raise ValueError("Portable session status must be a known lifecycle value.")
        return self._update_session(
            session_id,
            """
            status = ?, activity_summary = ?, unavailable_from_status = CASE
                WHEN worktree_available = 0 THEN ? ELSE NULL END,
            updated_at = ?
            """,
            (
                status.value,
                redact_persisted_evidence(activity_summary)[:500],
                status.value,
                time.time(),
            ),
        )

    def update_session_summary(
        self,
        session_id: str,
        *,
        status: PortableSessionStatus,
        result: int | None,
        progress: PortableSessionProgress,
        activity_summary: str = "",
    ) -> int:
        if not isinstance(status, PortableSessionStatus):
            raise ValueError("Portable session status must be a known lifecycle value.")
        if result is not None and (
            not isinstance(result, int) or isinstance(result, bool)
        ):
            raise ValueError("Portable session result must be an integer or null.")
        if not isinstance(progress, PortableSessionProgress):
            raise ValueError("Portable session progress must be structured progress.")
        if (
            progress.completed_issues < 0
            or progress.total_issues < 0
            or progress.completed_issues > progress.total_issues
        ):
            raise ValueError("Portable session progress counts are invalid.")
        _validate_catalog_text(progress.stage, maximum_length=200, allow_empty=True)
        if progress.active_issue is not None:
            _validate_catalog_text(
                progress.active_issue,
                maximum_length=128,
                allow_empty=True,
            )
        return self._update_session(
            session_id,
            """
            status = ?, result = ?, progress_stage = ?, completed_issues = ?,
            total_issues = ?, active_issue = ?, activity_summary = ?,
            unavailable_from_status = CASE
                WHEN worktree_available = 0 THEN ? ELSE NULL END,
            updated_at = ?
            """,
            (
                status.value,
                result,
                progress.stage,
                progress.completed_issues,
                progress.total_issues,
                progress.active_issue,
                redact_persisted_evidence(activity_summary)[:500],
                status.value,
                self._clock(),
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
        granted, _revision = self.request_execution_capacity_with_revision(
            session_id,
            owner_id=owner_id,
            process_id=process_id,
        )
        return granted

    def request_execution_capacity_with_revision(
        self,
        session_id: str,
        *,
        owner_id: str,
        process_id: int | None = None,
    ) -> tuple[bool, int]:
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
                    """
                    SELECT status, worktree_available
                    FROM sessions WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise KeyError(f"Unknown portable session: {session_id}")
                status = PortableSessionStatus(session["status"])
                if session["worktree_available"] == 0:
                    raise ValueError(
                        "Portable session cannot request execution capacity from "
                        "UNAVAILABLE."
                    )
                if status.terminal:
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
                        SET status = ?, updated_at = ?,
                            revision = revision + 1
                        WHERE session_id = ?
                        """,
                        (
                            PortableSessionStatus.RUNNING.value,
                            timestamp,
                            session_id,
                        ),
                    )
                    return True, self._session_revision(connection, session_id)
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
                    SET status = ?, updated_at = ?,
                        revision = revision + 1
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
                return granted, self._session_revision(connection, session_id)
        except (KeyError, PortableSessionCatalogError, ValueError):
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error

    def enqueue_execution_capacity(
        self,
        session_id: str,
        *,
        owner_id: str,
        process_id: int | None = None,
    ) -> int:
        """Atomically retain a fair queue place without acquiring a free slot."""
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
                    SELECT status, worktree_available
                    FROM sessions WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise KeyError(f"Unknown portable session: {session_id}")
                status = PortableSessionStatus(session["status"])
                if session["worktree_available"] == 0:
                    raise ValueError(
                        "Portable session cannot queue execution capacity from "
                        "UNAVAILABLE."
                    )
                if status.terminal:
                    raise ValueError(
                        "Portable session cannot queue execution capacity from "
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
                    """
                    SELECT owner_id
                    FROM execution_claims
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if claim is not None:
                    raise PortableSessionCatalogError(
                        "Portable session must release execution capacity before "
                        "returning to the queue."
                    )
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
                connection.execute(
                    """
                    UPDATE sessions
                    SET status = ?, updated_at = ?,
                        revision = revision + 1
                    WHERE session_id = ?
                    """,
                    (
                        PortableSessionStatus.QUEUED.value,
                        timestamp,
                        session_id,
                    ),
                )
                revision = self._session_revision(connection, session_id)
        except (KeyError, PortableSessionCatalogError, ValueError):
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error
        return revision

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
        released, _revision = self.release_execution_capacity_with_revision(
            session_id,
            owner_id=owner_id,
            status=status,
            activity_summary=activity_summary,
        )
        return released

    def release_execution_capacity_with_revision(
        self,
        session_id: str,
        *,
        owner_id: str,
        status: PortableSessionStatus,
        activity_summary: str = "",
    ) -> tuple[bool, int]:
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
                    SET status = ?, activity_summary = ?,
                        unavailable_from_status = CASE
                            WHEN worktree_available = 0 THEN ? ELSE NULL
                        END,
                        updated_at = ?,
                        revision = revision + 1
                    WHERE session_id = ?
                    """,
                    (
                        status.value,
                        redact_persisted_evidence(activity_summary)[:500],
                        status.value,
                        timestamp,
                        session_id,
                    ),
                )
                return (
                    cursor.rowcount == 1,
                    self._session_revision(connection, session_id),
                )
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
    ) -> int:
        _validate_session_id(session_id)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    (
                        f"UPDATE sessions SET {assignments}, "
                        "revision = revision + 1 WHERE session_id = ?"
                    ),
                    (*values, session_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(f"Unknown portable session: {session_id}")
                revision = self._session_revision(connection, session_id)
        except KeyError:
            raise
        except sqlite3.DatabaseError as error:
            raise PortableSessionCatalogError(
                f"Portable Session Catalog write failed: {error}"
            ) from error
        return revision

    @staticmethod
    def _session_revision(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> int:
        row = connection.execute(
            "SELECT revision FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown portable session: {session_id}")
        revision = _validate_revision(row["revision"])
        connection.execute(
            """
            INSERT INTO session_revision_counters (session_id, revision)
            VALUES (?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                revision = excluded.revision
            """,
            (session_id, revision),
        )
        return revision

    @staticmethod
    def _next_session_revision(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> int:
        connection.execute(
            """
            INSERT INTO session_revision_counters (session_id, revision)
            VALUES (?, 1)
            ON CONFLICT(session_id) DO UPDATE SET
                revision = session_revision_counters.revision + 1
            """,
            (session_id,),
        )
        row = connection.execute(
            """
            SELECT revision
            FROM session_revision_counters
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        assert row is not None
        return _validate_revision(row["revision"])

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
                repair_weak_v7 = (
                    version == 7 and _sessions_requires_v7_rebuild(connection)
                )
                if 0 < version <= 6 or repair_weak_v7:
                    connection.execute("PRAGMA foreign_keys = OFF")
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
                            worktree_available INTEGER NOT NULL DEFAULT 1
                                CHECK (worktree_available IN (0, 1)),
                            unavailable_from_status TEXT CHECK (
                                unavailable_from_status IS NULL OR
                                unavailable_from_status IN (
                                    'READY', 'QUEUED', 'RUNNING',
                                    'WAITING_FOR_INPUT', 'PAUSING', 'PAUSED',
                                    'INTERRUPTED', 'COMPLETED', 'FAILED', 'CANCELLED'
                                )
                            ),
                            result INTEGER CHECK (
                                result IS NULL OR typeof(result) = 'integer'
                            ),
                            progress_stage TEXT NOT NULL DEFAULT ''
                                CHECK (length(progress_stage) <= 200),
                            completed_issues INTEGER NOT NULL DEFAULT 0
                                CHECK (typeof(completed_issues) = 'integer'
                                    AND completed_issues >= 0),
                            total_issues INTEGER NOT NULL DEFAULT 0
                                CHECK (typeof(total_issues) = 'integer'
                                    AND total_issues >= 0),
                            active_issue TEXT CHECK (
                                active_issue IS NULL OR length(active_issue) <= 128
                            ),
                            created_at REAL NOT NULL
                                CHECK (typeof(created_at) IN ('integer', 'real')
                                    AND created_at >= 0),
                            updated_at REAL NOT NULL
                                CHECK (typeof(updated_at) IN ('integer', 'real')
                                    AND updated_at >= 0),
                            revision INTEGER NOT NULL DEFAULT 1
                                CHECK (typeof(revision) = 'integer'
                                    AND revision > 0),
                            CHECK (completed_issues <= total_issues)
                        );
                        CREATE TABLE session_revision_counters (
                            session_id TEXT PRIMARY KEY
                                CHECK (length(session_id) BETWEEN 1 AND 128),
                            revision INTEGER NOT NULL
                                CHECK (typeof(revision) = 'integer'
                                    AND revision > 0)
                        );
                        CREATE TABLE session_relink_receipts (
                            session_id TEXT PRIMARY KEY
                                REFERENCES sessions(session_id) ON DELETE CASCADE,
                            source_checkout TEXT NOT NULL
                                CHECK (length(source_checkout) BETWEEN 1 AND 4096),
                            source_prd_path TEXT NOT NULL
                                CHECK (length(source_prd_path) BETWEEN 1 AND 4096),
                            source_issues_index_path TEXT NOT NULL
                                CHECK (length(source_issues_index_path) BETWEEN 1 AND 4096),
                            target_checkout TEXT NOT NULL
                                CHECK (length(target_checkout) BETWEEN 1 AND 4096),
                            target_prd_path TEXT NOT NULL
                                CHECK (length(target_prd_path) BETWEEN 1 AND 4096),
                            target_issues_index_path TEXT NOT NULL
                                CHECK (length(target_issues_index_path) BETWEEN 1 AND 4096),
                            state_sha256 TEXT NOT NULL
                                CHECK (length(state_sha256) = 64)
                        );
                        CREATE TABLE IF NOT EXISTS project_adoption_receipts (
                            receipt_id TEXT PRIMARY KEY
                                CHECK (length(receipt_id) BETWEEN 1 AND 128),
                            schema_version INTEGER NOT NULL CHECK (schema_version = 1),
                            source_version TEXT NOT NULL
                                CHECK (length(source_version) BETWEEN 1 AND 32),
                            configuration_path TEXT NOT NULL
                                CHECK (length(configuration_path) BETWEEN 1 AND 4096),
                            configuration_sha256 TEXT NOT NULL
                                CHECK (length(configuration_sha256) = 64),
                            created_at REAL NOT NULL
                                CHECK (typeof(created_at) IN ('integer', 'real')
                                    AND created_at >= 0)
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
                                    AND heartbeat_at >= 0),
                            process_start_fingerprint INTEGER
                                CHECK (process_start_fingerprint IS NULL
                                    OR (typeof(process_start_fingerprint) = 'integer'
                                        AND process_start_fingerprint > 0)),
                            worker_generation INTEGER
                                CHECK (worker_generation IS NULL
                                    OR (typeof(worker_generation) = 'integer'
                                        AND worker_generation > 0)),
                            worker_process_id INTEGER
                                CHECK (worker_process_id IS NULL
                                    OR (typeof(worker_process_id) = 'integer'
                                        AND worker_process_id > 0)),
                            worker_process_start_fingerprint INTEGER
                                CHECK (worker_process_start_fingerprint IS NULL
                                    OR (typeof(worker_process_start_fingerprint) = 'integer'
                                        AND worker_process_start_fingerprint > 0)),
                            process_tree_kind TEXT CHECK (
                                process_tree_kind IS NULL OR process_tree_kind IN (
                                    'ROOT_PROCESS', 'POSIX_PROCESS_GROUP',
                                    'WINDOWS_KILL_ON_CLOSE_JOB'
                                )
                            ),
                            process_tree_id INTEGER
                                CHECK (process_tree_id IS NULL
                                    OR (typeof(process_tree_id) = 'integer'
                                        AND process_tree_id > 0)),
                            CHECK (
                                (
                                    worker_process_id IS NULL
                                    AND worker_process_start_fingerprint IS NULL
                                    AND process_tree_kind IS NULL
                                    AND process_tree_id IS NULL
                                ) OR (
                                    worker_generation IS NOT NULL
                                    AND worker_process_id IS NOT NULL
                                    AND worker_process_start_fingerprint IS NOT NULL
                                    AND process_tree_kind IS NOT NULL
                                    AND process_tree_id IS NOT NULL
                                    AND process_tree_id = worker_process_id
                                )
                            )
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
                        PRAGMA user_version = 8;
                        """
                    )
                    version = 8
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
                    version = 3
                if version == 3:
                    columns = {
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA table_info(sessions)"
                        )
                    }
                    if not connection.in_transaction:
                        connection.execute("BEGIN IMMEDIATE")
                    if "revision" not in columns:
                        connection.execute(
                            """
                            ALTER TABLE sessions
                                ADD COLUMN revision INTEGER NOT NULL DEFAULT 1
                                CHECK (typeof(revision) = 'integer'
                                    AND revision > 0)
                            """
                        )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS session_revision_counters (
                            session_id TEXT PRIMARY KEY
                                CHECK (length(session_id) BETWEEN 1 AND 128),
                            revision INTEGER NOT NULL
                                CHECK (typeof(revision) = 'integer'
                                    AND revision > 0)
                        )
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO session_revision_counters (
                            session_id, revision
                        )
                        SELECT session_id, revision
                        FROM sessions
                        WHERE 1
                        ON CONFLICT(session_id) DO UPDATE SET
                            revision = MAX(
                                session_revision_counters.revision,
                                excluded.revision
                            )
                        """
                    )
                    connection.execute("PRAGMA user_version = 4")
                    version = 4
                if version == 4:
                    columns = {
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA table_info(worktree_leases)"
                        )
                    }
                    if not connection.in_transaction:
                        connection.execute("BEGIN IMMEDIATE")
                    if "process_start_fingerprint" not in columns:
                        connection.execute(
                            """
                            ALTER TABLE worktree_leases
                            ADD COLUMN process_start_fingerprint INTEGER
                                CHECK (process_start_fingerprint IS NULL
                                    OR (typeof(process_start_fingerprint) = 'integer'
                                        AND process_start_fingerprint > 0))
                            """
                        )
                    if "worker_generation" not in columns:
                        connection.execute(
                            """
                            ALTER TABLE worktree_leases
                            ADD COLUMN worker_generation INTEGER
                                CHECK (worker_generation IS NULL
                                    OR (typeof(worker_generation) = 'integer'
                                        AND worker_generation > 0))
                            """
                        )
                    connection.execute("PRAGMA user_version = 5")
                    version = 5
                if version == 5:
                    columns = {
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA table_info(worktree_leases)"
                        )
                    }
                    if not connection.in_transaction:
                        connection.execute("BEGIN IMMEDIATE")
                    additions = {
                        "worker_process_id": (
                            "INTEGER CHECK (worker_process_id IS NULL OR "
                            "(typeof(worker_process_id) = 'integer' AND "
                            "worker_process_id > 0))"
                        ),
                        "worker_process_start_fingerprint": (
                            "INTEGER CHECK (worker_process_start_fingerprint IS NULL OR "
                            "(typeof(worker_process_start_fingerprint) = 'integer' AND "
                            "worker_process_start_fingerprint > 0))"
                        ),
                        "process_tree_kind": (
                            "TEXT CHECK (process_tree_kind IS NULL OR "
                            "process_tree_kind IN ('ROOT_PROCESS', "
                            "'POSIX_PROCESS_GROUP', 'WINDOWS_KILL_ON_CLOSE_JOB'))"
                        ),
                        "process_tree_id": (
                            "INTEGER CHECK (process_tree_id IS NULL OR "
                            "(typeof(process_tree_id) = 'integer' AND "
                            "process_tree_id > 0))"
                        ),
                    }
                    for column, declaration in additions.items():
                        if column not in columns:
                            connection.execute(
                                f"ALTER TABLE worktree_leases ADD COLUMN {column} "
                                f"{declaration}"
                            )
                    lease_schema_row = connection.execute(
                        """
                        SELECT sql FROM sqlite_master
                        WHERE type = 'table' AND name = 'worktree_leases'
                        """
                    ).fetchone()
                    lease_schema = re.sub(
                        r"\s+",
                        "",
                        (
                            lease_schema_row["sql"].lower()
                            if lease_schema_row is not None
                            else ""
                        ),
                    )
                    if "process_tree_id=worker_process_id" not in lease_schema:
                        lease_indexes = _user_index_snapshot(connection)
                        connection.execute(
                            "ALTER TABLE worktree_leases RENAME TO worktree_leases_v5"
                        )
                        connection.execute(
                            """
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
                                        AND heartbeat_at >= 0),
                                process_start_fingerprint INTEGER
                                    CHECK (process_start_fingerprint IS NULL
                                        OR (typeof(process_start_fingerprint) = 'integer'
                                            AND process_start_fingerprint > 0)),
                                worker_generation INTEGER
                                    CHECK (worker_generation IS NULL
                                        OR (typeof(worker_generation) = 'integer'
                                            AND worker_generation > 0)),
                                worker_process_id INTEGER
                                    CHECK (worker_process_id IS NULL
                                        OR (typeof(worker_process_id) = 'integer'
                                            AND worker_process_id > 0)),
                                worker_process_start_fingerprint INTEGER
                                    CHECK (worker_process_start_fingerprint IS NULL
                                        OR (typeof(worker_process_start_fingerprint) = 'integer'
                                            AND worker_process_start_fingerprint > 0)),
                                process_tree_kind TEXT CHECK (
                                    process_tree_kind IS NULL OR process_tree_kind IN (
                                        'ROOT_PROCESS', 'POSIX_PROCESS_GROUP',
                                        'WINDOWS_KILL_ON_CLOSE_JOB'
                                    )
                                ),
                                process_tree_id INTEGER
                                    CHECK (process_tree_id IS NULL
                                        OR (typeof(process_tree_id) = 'integer'
                                            AND process_tree_id > 0)),
                                CHECK (
                                    (
                                        worker_process_id IS NULL
                                        AND worker_process_start_fingerprint IS NULL
                                        AND process_tree_kind IS NULL
                                        AND process_tree_id IS NULL
                                    ) OR (
                                        worker_generation IS NOT NULL
                                        AND worker_process_id IS NOT NULL
                                        AND worker_process_start_fingerprint IS NOT NULL
                                        AND process_tree_kind IS NOT NULL
                                        AND process_tree_id IS NOT NULL
                                        AND process_tree_id = worker_process_id
                                    )
                                )
                            )
                            """
                        )
                        connection.execute(
                            """
                            INSERT INTO worktree_leases (
                                checkout, session_id, owner_id, process_id,
                                acquired_at, heartbeat_at,
                                process_start_fingerprint, worker_generation,
                                worker_process_id,
                                worker_process_start_fingerprint,
                                process_tree_kind, process_tree_id
                            )
                            SELECT checkout, session_id, owner_id, process_id,
                                acquired_at, heartbeat_at,
                                process_start_fingerprint, worker_generation,
                                worker_process_id,
                                worker_process_start_fingerprint,
                                process_tree_kind, process_tree_id
                            FROM worktree_leases_v5
                            """
                        )
                        connection.execute("DROP TABLE worktree_leases_v5")
                        for index in lease_indexes:
                            connection.execute(index.sql)
                        if _user_index_snapshot(connection) != lease_indexes:
                            raise PortableSessionCatalogError(
                                "Portable Session Catalog worktree indexes changed "
                                "during schema migration."
                            )
                    connection.execute("PRAGMA user_version = 6")
                    version = 6
                if version == 6:
                    if not connection.in_transaction:
                        connection.execute("BEGIN IMMEDIATE")
                    _rebuild_sessions_for_v7(connection)
                    connection.execute("PRAGMA user_version = 7")
                    version = 7
                elif repair_weak_v7:
                    connection.execute("BEGIN IMMEDIATE")
                    _rebuild_sessions_for_v7(connection)
                if version == 7:
                    if not connection.in_transaction:
                        connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS project_adoption_receipts (
                            receipt_id TEXT PRIMARY KEY
                                CHECK (length(receipt_id) BETWEEN 1 AND 128),
                            schema_version INTEGER NOT NULL CHECK (schema_version = 1),
                            source_version TEXT NOT NULL
                                CHECK (length(source_version) BETWEEN 1 AND 32),
                            configuration_path TEXT NOT NULL
                                CHECK (length(configuration_path) BETWEEN 1 AND 4096),
                            configuration_sha256 TEXT NOT NULL
                                CHECK (length(configuration_sha256) = 64),
                            created_at REAL NOT NULL
                                CHECK (typeof(created_at) IN ('integer', 'real')
                                    AND created_at >= 0)
                        )
                        """
                    )
                    connection.execute("PRAGMA user_version = 8")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_relink_receipts (
                        session_id TEXT PRIMARY KEY
                            REFERENCES sessions(session_id) ON DELETE CASCADE,
                        source_checkout TEXT NOT NULL
                            CHECK (length(source_checkout) BETWEEN 1 AND 4096),
                        source_prd_path TEXT NOT NULL
                            CHECK (length(source_prd_path) BETWEEN 1 AND 4096),
                        source_issues_index_path TEXT NOT NULL
                            CHECK (length(source_issues_index_path) BETWEEN 1 AND 4096),
                        target_checkout TEXT NOT NULL
                            CHECK (length(target_checkout) BETWEEN 1 AND 4096),
                        target_prd_path TEXT NOT NULL
                            CHECK (length(target_prd_path) BETWEEN 1 AND 4096),
                        target_issues_index_path TEXT NOT NULL
                            CHECK (length(target_issues_index_path) BETWEEN 1 AND 4096),
                        state_sha256 TEXT NOT NULL CHECK (length(state_sha256) = 64)
                    )
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
            "session_revision_counters",
            "session_relink_receipts",
            "project_adoption_receipts",
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
                ("worktree_available", "INTEGER", 1, 0),
                ("unavailable_from_status", "TEXT", 0, 0),
                ("result", "INTEGER", 0, 0),
                ("progress_stage", "TEXT", 1, 0),
                ("completed_issues", "INTEGER", 1, 0),
                ("total_issues", "INTEGER", 1, 0),
                ("active_issue", "TEXT", 0, 0),
                ("created_at", "REAL", 1, 0),
                ("updated_at", "REAL", 1, 0),
                ("revision", "INTEGER", 1, 0),
            ),
            "session_revision_counters": (
                ("session_id", "TEXT", 0, 1),
                ("revision", "INTEGER", 1, 0),
            ),
            "session_relink_receipts": (
                ("session_id", "TEXT", 0, 1),
                ("source_checkout", "TEXT", 1, 0),
                ("source_prd_path", "TEXT", 1, 0),
                ("source_issues_index_path", "TEXT", 1, 0),
                ("target_checkout", "TEXT", 1, 0),
                ("target_prd_path", "TEXT", 1, 0),
                ("target_issues_index_path", "TEXT", 1, 0),
                ("state_sha256", "TEXT", 1, 0),
            ),
            "project_adoption_receipts": (
                ("receipt_id", "TEXT", 0, 1),
                ("schema_version", "INTEGER", 1, 0),
                ("source_version", "TEXT", 1, 0),
                ("configuration_path", "TEXT", 1, 0),
                ("configuration_sha256", "TEXT", 1, 0),
                ("created_at", "REAL", 1, 0),
            ),
            "worktree_leases": (
                ("checkout", "TEXT", 0, 1),
                ("session_id", "TEXT", 1, 0),
                ("owner_id", "TEXT", 1, 0),
                ("process_id", "INTEGER", 1, 0),
                ("acquired_at", "REAL", 1, 0),
                ("heartbeat_at", "REAL", 1, 0),
                ("process_start_fingerprint", "INTEGER", 0, 0),
                ("worker_generation", "INTEGER", 0, 0),
                ("worker_process_id", "INTEGER", 0, 0),
                ("worker_process_start_fingerprint", "INTEGER", 0, 0),
                ("process_tree_kind", "TEXT", 0, 0),
                ("process_tree_id", "INTEGER", 0, 0),
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
        relink_foreign_keys = tuple(
            (
                row["table"],
                row["from"],
                row["to"],
                row["on_update"],
                row["on_delete"],
            )
            for row in connection.execute(
                "PRAGMA foreign_key_list(session_relink_receipts)"
            )
        )
        if relink_foreign_keys != (
            ("sessions", "session_id", "session_id", "NO ACTION", "CASCADE"),
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
                "check(worktree_availablein(0,1))",
                "unavailable_from_statusin('ready','queued','running'",
                "'completed','failed','cancelled')",
                "resultisnullortypeof(result)='integer'",
                "check(length(progress_stage)<=200)",
                "typeof(completed_issues)='integer'andcompleted_issues>=0",
                "typeof(total_issues)='integer'andtotal_issues>=0",
                "check(completed_issues<=total_issues)",
                "active_issueisnullorlength(active_issue)<=128",
                "check(typeof(created_at)in('integer','real')andcreated_at>=0)",
                "check(typeof(updated_at)in('integer','real')andupdated_at>=0)",
                "check(typeof(revision)='integer'andrevision>0)",
            ),
            "session_revision_counters": (
                "check(length(session_id)between1and128)",
                "check(typeof(revision)='integer'andrevision>0)",
            ),
            "session_relink_receipts": (
                "check(length(source_checkout)between1and4096)",
                "check(length(source_prd_path)between1and4096)",
                "check(length(source_issues_index_path)between1and4096)",
                "check(length(target_checkout)between1and4096)",
                "check(length(target_prd_path)between1and4096)",
                "check(length(target_issues_index_path)between1and4096)",
                "check(length(state_sha256)=64)",
            ),
            "project_adoption_receipts": (
                "check(length(receipt_id)between1and128)",
                "check(schema_version=1)",
                "check(length(source_version)between1and32)",
                "check(length(configuration_path)between1and4096)",
                "check(length(configuration_sha256)=64)",
                "check(typeof(created_at)in('integer','real')andcreated_at>=0)",
            ),
            "worktree_leases": (
                "check(length(checkout)between1and4096)",
                "session_idtextnotnullunique",
                "check(length(owner_id)between1and128)",
                "check(typeof(process_id)='integer'andprocess_id>0)",
                "check(typeof(acquired_at)in('integer','real')andacquired_at>=0)",
                "check(typeof(heartbeat_at)in('integer','real')andheartbeat_at>=0)",
                (
                    "check(process_start_fingerprintisnullor("
                    "typeof(process_start_fingerprint)='integer'and"
                    "process_start_fingerprint>0))"
                ),
                (
                    "check(worker_generationisnullor("
                    "typeof(worker_generation)='integer'and"
                    "worker_generation>0))"
                ),
                (
                    "check(worker_process_idisnullor("
                    "typeof(worker_process_id)='integer'and"
                    "worker_process_id>0))"
                ),
                (
                    "check(worker_process_start_fingerprintisnullor("
                    "typeof(worker_process_start_fingerprint)='integer'and"
                    "worker_process_start_fingerprint>0))"
                ),
                (
                    "process_tree_kindin('root_process','posix_process_group',"
                    "'windows_kill_on_close_job')"
                ),
                (
                    "check(process_tree_idisnullor("
                    "typeof(process_tree_id)='integer'andprocess_tree_id>0))"
                ),
                (
                    "check((worker_process_idisnulland"
                    "worker_process_start_fingerprintisnulland"
                    "process_tree_kindisnullandprocess_tree_idisnull)or("
                    "worker_generationisnotnullandworker_process_idisnotnulland"
                    "worker_process_start_fingerprintisnotnulland"
                    "process_tree_kindisnotnullandprocess_tree_idisnotnulland"
                    "process_tree_id=worker_process_id))"
                ),
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
            if not connection.in_transaction:
                # Python's legacy sqlite3 transaction mode does not start a
                # transaction for SELECT. Pin every validation query to one
                # WAL snapshot so a concurrent atomic session/counter update
                # cannot be observed half before and half after its commit.
                connection.execute("BEGIN")
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
            revisions = {
                row["session_id"]: _validate_revision(row["revision"])
                for row in connection.execute(
                    "SELECT session_id, revision FROM session_revision_counters"
                )
            }
            for session_id, _revision in revisions.items():
                _validate_session_id(session_id)
            if any(
                revisions.get(row["session_id"]) != row["revision"]
                for row in rows
            ):
                raise PortableSessionCatalogError(
                    "Portable Session Catalog session revisions are inconsistent."
                )
            for row in connection.execute("SELECT * FROM worktree_leases"):
                _lease_from_row(row)
            for row in connection.execute("SELECT * FROM project_adoption_receipts"):
                _validate_catalog_text(row["receipt_id"], maximum_length=128)
                if row["schema_version"] != 1:
                    raise ValueError("Portable adoption receipt version is invalid.")
                _validate_catalog_text(row["source_version"], maximum_length=32)
                _validate_catalog_path(row["configuration_path"])
                if not re.fullmatch(r"[0-9a-f]{64}", row["configuration_sha256"]):
                    raise ValueError("Portable adoption receipt hash is invalid.")
                _validate_timestamp(row["created_at"])
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
        _validate_revision(row["revision"])
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
        lifecycle_status = PortableSessionStatus(row["status"])
        worktree_available = row["worktree_available"]
        if worktree_available not in (0, 1):
            raise ValueError
        unavailable_from_value = row["unavailable_from_status"]
        unavailable_from_status = (
            PortableSessionStatus(unavailable_from_value)
            if unavailable_from_value is not None
            else None
        )
        if worktree_available == 0:
            if (
                unavailable_from_status is None
                or unavailable_from_status is not lifecycle_status
            ):
                raise ValueError
            status = PortableSessionStatus.UNAVAILABLE
        elif unavailable_from_status is not None:
            raise ValueError
        else:
            status = lifecycle_status
        result = row["result"]
        if result is not None and (
            not isinstance(result, int) or isinstance(result, bool)
        ):
            raise ValueError
        progress_stage = row["progress_stage"]
        _validate_catalog_text(progress_stage, maximum_length=200, allow_empty=True)
        completed_issues = row["completed_issues"]
        total_issues = row["total_issues"]
        if (
            not isinstance(completed_issues, int)
            or isinstance(completed_issues, bool)
            or not isinstance(total_issues, int)
            or isinstance(total_issues, bool)
            or completed_issues < 0
            or total_issues < 0
            or completed_issues > total_issues
        ):
            raise ValueError
        active_issue = row["active_issue"]
        if active_issue is not None:
            _validate_catalog_text(active_issue, maximum_length=128, allow_empty=True)
        return PortableCatalogSession(
            session_id=row["session_id"],
            project_id=row["project_id"],
            checkout=Path(row["checkout"]),
            status=status,
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
            unavailable_from_status=unavailable_from_status,
            result=result,
            progress=PortableSessionProgress(
                stage=progress_stage,
                completed_issues=completed_issues,
                total_issues=total_issues,
                active_issue=active_issue,
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            revision=row["revision"],
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
        process_start_fingerprint = row["process_start_fingerprint"]
        if process_start_fingerprint is not None:
            _validate_process_start_fingerprint(process_start_fingerprint)
        worker_generation = row["worker_generation"]
        _validate_worker_generation(worker_generation)
        worker_process_id = row["worker_process_id"]
        worker_process_start_fingerprint = row[
            "worker_process_start_fingerprint"
        ]
        process_tree_kind_value = row["process_tree_kind"]
        process_tree_id = row["process_tree_id"]
        if worker_process_id is not None:
            _validate_process_id(worker_process_id)
        if worker_process_start_fingerprint is not None:
            _validate_process_start_fingerprint(
                worker_process_start_fingerprint
            )
        process_tree_kind = (
            ProcessTreeKind(process_tree_kind_value)
            if process_tree_kind_value is not None
            else None
        )
        if process_tree_id is not None:
            _validate_process_id(process_tree_id)
        worker_fields = (
            worker_process_id,
            worker_process_start_fingerprint,
            process_tree_kind,
            process_tree_id,
        )
        if any(value is not None for value in worker_fields) and any(
            value is None for value in worker_fields
        ):
            raise ValueError("Portable worker process-tree identity is incomplete.")
        if all(value is not None for value in worker_fields):
            if worker_generation is None:
                raise ValueError(
                    "Portable worker process-tree identity has no generation."
                )
            assert worker_process_id is not None
            assert worker_process_start_fingerprint is not None
            assert process_tree_kind is not None
            assert process_tree_id is not None
            _validate_process_tree_identity(
                ProcessTreeIdentity(
                    root=ProcessIdentity(
                        pid=worker_process_id,
                        creation_time=worker_process_start_fingerprint,
                    ),
                    kind=process_tree_kind,
                    tree_id=process_tree_id,
                )
            )
        return PortableWorktreeLease(
            checkout=Path(row["checkout"]),
            session_id=row["session_id"],
            owner_id=row["owner_id"],
            process_id=row["process_id"],
            acquired_at=row["acquired_at"],
            heartbeat_at=row["heartbeat_at"],
            process_start_fingerprint=process_start_fingerprint,
            worker_generation=worker_generation,
            worker_process_id=worker_process_id,
            worker_process_start_fingerprint=worker_process_start_fingerprint,
            process_tree_kind=process_tree_kind,
            process_tree_id=process_tree_id,
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


def _validate_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("Portable session revision must be a positive integer.")
    return value


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


def _validate_process_start_fingerprint(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            "Portable lease process-start fingerprint must be a positive integer."
        )


def _validate_process_identity(identity: ProcessIdentity) -> None:
    if not isinstance(identity, ProcessIdentity):
        raise ValueError("Portable lease requires a stable process identity.")
    _validate_process_id(identity.pid)
    _validate_process_start_fingerprint(identity.creation_time)


def _validate_process_tree_identity(identity: ProcessTreeIdentity) -> None:
    if not isinstance(identity, ProcessTreeIdentity):
        raise ValueError("Portable worker tree identity has an invalid type.")
    _validate_process_identity(identity.root)
    if not isinstance(identity.kind, ProcessTreeKind):
        raise ValueError("Portable worker tree kind is invalid.")
    _validate_process_id(identity.tree_id)
    if identity.tree_id != identity.root.pid:
        raise ValueError(
            "Portable worker tree identity must match its root process."
        )


def _resolve_process_identity(
    process_id: int | None,
    process_identity: ProcessIdentity | None,
) -> ProcessIdentity | None:
    if process_identity is not None:
        _validate_process_identity(process_identity)
        if process_id is not None and process_id != process_identity.pid:
            raise ValueError(
                "Portable lease PID does not match its stable process identity."
            )
        return process_identity
    if process_id is not None:
        _validate_process_id(process_id)
        return None
    return capture_process_identity()


def _validate_worker_generation(
    worker_generation: int | None,
    *,
    required: bool = False,
) -> None:
    if worker_generation is None and not required:
        return
    if (
        isinstance(worker_generation, bool)
        or not isinstance(worker_generation, int)
        or worker_generation <= 0
    ):
        raise ValueError(
            "Portable worker generation must be a positive integer."
        )


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
