from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypedDict, cast, overload

from verify import (
    GIT_ENTRY_DIRECTORY,
    GIT_ENTRY_FILE,
    GIT_OWNERSHIP_SCHEMA,
    MANIFEST_SCHEMA,
    POINTER_FIELDS,
    POINTER_SCHEMA,
    TRANSACTION_SCHEMA,
    _directory_fingerprint,
    _git,
    _git_directory_entries,
    _git_directory_fingerprint,
    _git_entries_fingerprint,
    _git_ownership_path,
    _plain,
    _plain_ancestors,
    _read_git_ownership,
    _tracked_fingerprint,
    _validate_release_content,
    _verify_git_ownership,
    read_pointer_state,
    verify,
    verify_pointer,
    verify_release,
)

ASSETS = (
    ("dispatch.ps1", "bootstrap/dispatch.ps1"),
    ("dispatch.sh", "bootstrap/dispatch.sh"),
    ("verify.py", "bootstrap/verify.py"),
    ("transaction.py", "bootstrap/transaction.py"),
    ("bin/devloop.ps1", "bin/devloop.ps1"),
    ("bin/devloop-plan.ps1", "bin/devloop-plan.ps1"),
    ("bin/devloop.sh", "bin/devloop.sh"),
    ("bin/devloop-plan.sh", "bin/devloop-plan.sh"),
    ("install/devloop.ps1", "install/devloop.ps1"),
    ("install/uninstall-devloop.ps1", "install/uninstall-devloop.ps1"),
    ("install/devloop.sh", "install/devloop.sh"),
    ("install/uninstall-devloop.sh", "install/uninstall-devloop.sh"),
)
ASSET_PATHS = frozenset(target for _, target in ASSETS)
HASH_PATTERN = re.compile(r"[0-9A-F]{64}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
PHASES = frozenset(
    {
        "journaled",
        "owned",
        "manifested",
        "prepared",
        "release_ready",
        "adopted",
        "switched",
        "committed",
    }
)
JOURNAL_FIELDS = POINTER_FIELDS | {
    "version",
    "transaction_id",
    "install_root",
    "candidate_path",
    "phase",
    "previous_pointer",
    "previous_previous_pointer",
}
OWNERSHIP_FIELDS = {"version", "transaction_id", "candidate_name", "commit"}
GIT_REMOVAL_PROOF_SCHEMA = 1
GIT_REMOVAL_PROOF_FIELDS = {
    "version", "transaction_id", "install_root", "commit", "git_fingerprint", "entries",
}
GIT_REMOVAL_PROOF_PREFIX = "git-entries-"
GIT_REMOVAL_PROOF_MISSING = (
    "Git ownership removal proof missing; preserve and restore owned data"
)
LAYOUT_FIELDS = {"version", "assets", "legacy_backups"}
PENDING_LAYOUT_FIELDS = LAYOUT_FIELDS | {"backups_ready", "published"}
BOOTSTRAP_PROTOCOL = 2
LOCK_SCHEMA = 1
TRANSACTION_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
LOCK_FIELDS = {
    "version",
    "transaction_id",
    "owner_pid",
    "owner_start",
    "owner_host",
    "operation",
    "candidate_name",
}
COMPATIBILITY_FIELDS = frozenset(
    {
        "bootstrap_protocol_min",
        "bootstrap_protocol_max",
        "pointer_schema_min",
        "pointer_schema_max",
        "manifest_schema_min",
        "manifest_schema_max",
        "transaction_schema_min",
        "transaction_schema_max",
    }
)
UNINSTALL_EVIDENCE_NAME = "action-evidence.json"
UNINSTALL_METADATA = frozenset({"layout.json", "install-transaction.json", "layout.pending.json"})
CAPABILITY_PATHS = ("skills/codex", "agents/codex")
UNINSTALL_ACTION_FIELDS = {
    **dict.fromkeys(
        ("remove_release", "remove_manifest", "remove_pointer", "remove_asset", "remove_metadata"),
        frozenset({"id", "kind", "state", "path"}),
    ),
    "restore_legacy": frozenset({"id", "kind", "state", "source", "target"}),
    "stage_capability": frozenset({"id", "kind", "state", "source", "staged", "destination"}),
    "cleanup_capability": frozenset({"id", "kind", "state", "staged", "destination"}),
}


@dataclass(frozen=True)
class UninstallPlan:
    releases: tuple[Path, ...]
    manifests: tuple[Path, ...]
    remove_assets: tuple[Path, ...]
    restore_assets: tuple[tuple[Path, Path], ...]
    metadata: tuple[Path, ...]
    release_inventories: tuple[tuple[Path, dict[str, str | None]], ...] = ()


class ProcessState(Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProcessIdentity:
    state: ProcessState
    start: str | None = None


class Layout(TypedDict):
    version: int
    assets: dict[str, str]
    legacy_backups: dict[str, str]


class PendingLayout(Layout):
    backups_ready: list[str]
    published: list[str]
    backup_staging: dict[str, str]


class LegacyLayout(TypedDict):
    version: int
    assets: dict[str, str]
    legacy_backups: list[str]


def _lock_path(install: Path) -> Path:
    return install.parent / f".{install.name}.install-lock"


def _plain_full_ancestry(path: Path) -> None:
    for location in (path, *path.parents):
        try:
            status = location.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(status.st_mode) or (
            getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise RuntimeError(f"reparse or symbolic link rejected: {location}")
        if location != path and not stat.S_ISDIR(status.st_mode):
            raise RuntimeError(f"ancestor is not a plain directory: {location}")


def _inspect_process(pid: int) -> ProcessIdentity:
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
            kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.c_void_p] * 4
            process = kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                state = ProcessState.DEAD if ctypes.get_last_error() == 87 else ProcessState.UNKNOWN
                return ProcessIdentity(state)
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(
                    process, ctypes.byref(exit_code)
                ):
                    return ProcessIdentity(ProcessState.UNKNOWN)
                if exit_code.value != 259:
                    return ProcessIdentity(ProcessState.DEAD)
                if not kernel32.GetProcessTimes(
                    process,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return ProcessIdentity(ProcessState.UNKNOWN)
                return ProcessIdentity(
                    ProcessState.ALIVE,
                    str((creation.dwHighDateTime << 32) | creation.dwLowDateTime),
                )
            finally:
                kernel32.CloseHandle(process)
        except (AttributeError, OSError):
            return ProcessIdentity(ProcessState.UNKNOWN)
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.is_file():
        try:
            # The process name may itself contain spaces or parentheses.
            fields = proc_stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            return ProcessIdentity(ProcessState.ALIVE, fields[19])
        except (OSError, IndexError):
            return ProcessIdentity(ProcessState.UNKNOWN)
    try:
        os.kill(pid, 0)
    except OSError as error:
        state = ProcessState.DEAD if error.errno == errno.ESRCH else ProcessState.UNKNOWN
        return ProcessIdentity(state)
    return ProcessIdentity(ProcessState.ALIVE, "alive")


def _process_start(pid: int) -> str | None:
    return _inspect_process(pid).start


@contextmanager
def _lock_guard(install: Path) -> Iterator[None]:
    # Keep the inode permanently: unlinking an advisory lock allows two owners
    # to lock different inodes bearing the same path. OS locks die with the process.
    path = install.parent / f".{install.name}.install-guard"
    _plain_full_ancestry(path)
    with path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("install lock is being changed concurrently") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _owner_identity(requested_pid: int | None = None) -> tuple[int, str]:
    owner_pid = os.getppid() if requested_pid is None else requested_pid
    owner_start = _process_start(owner_pid)
    if owner_start is None:
        raise RuntimeError("cannot establish stable installer process identity")
    return owner_pid, owner_start


def _read_lock(install: Path) -> dict[str, object]:
    path = _lock_path(install)
    _plain_full_ancestry(path / "owner.json")
    value = json.loads((path / "owner.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != LOCK_FIELDS or value.get("version") != 1:
        raise RuntimeError("install lock has an unsupported schema")
    transaction_id = value["transaction_id"]
    if (
        not isinstance(transaction_id, str)
        or TRANSACTION_ID_PATTERN.fullmatch(transaction_id) is None
    ):
        raise RuntimeError("install lock transaction identity is invalid")
    if value["operation"] not in {"install", "rollback", "uninstall"}:
        raise RuntimeError("install lock operation is invalid")
    if (
        type(value["owner_pid"]) is not int or value["owner_pid"] <= 0
        or not isinstance(value["owner_start"], str) or not value["owner_start"]
        or not isinstance(value["owner_host"], str) or not value["owner_host"]
        or value["candidate_name"] != f".{install.name}.candidate-{transaction_id}"
    ):
        raise RuntimeError("install lock owner identity is invalid")
    return value


def _lock_owner_alive(lock: dict[str, object]) -> bool:
    if lock["owner_host"] != socket.gethostname():
        return True
    pid = lock["owner_pid"]
    start = lock["owner_start"]
    assert isinstance(pid, int) and isinstance(start, str)
    identity = _inspect_process(pid)
    if identity.state is ProcessState.DEAD:
        return False
    if identity.state is ProcessState.UNKNOWN or identity.start == "alive":
        return True
    return identity.start == start


def _assert_lock(install: Path, transaction_id: str, operation: str | None = None) -> None:
    lock = _read_lock(install)
    if lock["transaction_id"] != transaction_id:
        raise RuntimeError("install lock transaction identity does not match")
    if operation is not None and lock["operation"] != operation:
        raise RuntimeError("install lock operation does not match command")


def _raw_journal_transaction_id(install: Path) -> str | None:
    path = install / "bootstrap" / "install-transaction.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    transaction_id = value.get("transaction_id") if isinstance(value, dict) else None
    if (
        not isinstance(transaction_id, str)
        or TRANSACTION_ID_PATTERN.fullmatch(transaction_id) is None
    ):
        raise RuntimeError("installation journal transaction identity is invalid")
    return transaction_id


def begin(
    install: Path, operation: str, owner_pid: int | None = None,
    *, recovery_transaction_id: str | None = None,
) -> str:
    install = install.absolute()
    with _lock_guard(install):
        return _begin_locked(install, operation, owner_pid, recovery_transaction_id)


def _remove_lock(install: Path, expected: dict[str, object]) -> None:
    path = _lock_path(install)
    if _read_lock(install) != expected or {p.name for p in path.iterdir()} != {"owner.json"}:
        raise RuntimeError("install lock changed during reclamation")
    (path / "owner.json").unlink()
    path.rmdir()


def _begin_locked(
    install: Path, operation: str, owner_pid: int | None,
    recovery_transaction_id: str | None,
) -> str:
    owner_pid, owner_start = _owner_identity(owner_pid)
    lock_path = _lock_path(install)
    transaction_id = recovery_transaction_id or str(uuid.uuid4())
    if recovery_transaction_id is not None:
        if operation != "uninstall":
            raise RuntimeError("only uninstall can resume an external recovery journal")
        _read_uninstall_journal(install, recovery_transaction_id)
    if lock_path.exists():
        lock = _read_lock(install)
        if _lock_owner_alive(lock):
            raise RuntimeError(
                f"install is busy with healthy owner {lock['owner_pid']} transaction "
                f"{lock['transaction_id']}"
            )
        stale_id = str(lock["transaction_id"])
        uninstall_journal = (
            install.parent / f".{install.name}.uninstall-{stale_id}" / "journal.json"
        )
        pending_id = _raw_journal_transaction_id(install)
        if pending_id is not None:
            transaction_id = pending_id
        elif operation == "uninstall" and uninstall_journal.is_file():
            transaction_id = stale_id
        else:
            candidate = install.parent / str(lock["candidate_name"])
            expected = f".{install.name}.candidate-{stale_id}"
            if (
                candidate.name == expected
                and candidate.parent == install.parent
                and candidate.exists()
            ):
                _plain_ancestors(candidate, install.parent)
                shutil.rmtree(candidate, onerror=_remove_read_only)
        _remove_lock(install, lock)
    candidate_name = f".{install.name}.candidate-{transaction_id}"
    staging = install.parent / f".{install.name}.lock-staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        _atomic_json(
            staging / "owner.json",
            {
                "version": LOCK_SCHEMA,
                "transaction_id": transaction_id,
                "owner_pid": owner_pid,
                "owner_start": owner_start,
                "owner_host": socket.gethostname(),
                "operation": operation,
                "candidate_name": candidate_name,
            },
        )
        try:
            staging.rename(lock_path)
        except FileExistsError as error:
            raise RuntimeError("install lock was acquired concurrently") from error
    finally:
        if staging.exists():
            shutil.rmtree(staging, onerror=_remove_read_only)
    _reconcile_committed_pending_layout(install)
    return transaction_id


def begin_legacy_migration(install: Path, owner_pid: int | None = None) -> str:
    install = install.absolute()
    transaction_id = begin(install, "install", owner_pid)
    try:
        pending = install / "bootstrap" / "layout.pending.json"
        if pending.exists():
            _reconcile_committed_pending_layout(install)
        else:
            _verified_legacy_layout(install)
        if (install / "bootstrap" / "install-transaction.json").exists():
            raise RuntimeError("legacy installation transaction must be completed before migration")
    except Exception:
        _release_lock(install, transaction_id)
        raise
    return transaction_id


def _release_lock(install: Path, transaction_id: str) -> None:
    with _lock_guard(install):
        _assert_lock(install, transaction_id)
        _remove_lock(install, _read_lock(install))


def abort(install: Path, transaction_id: str) -> None:
    install = install.absolute()
    _assert_lock(install, transaction_id, "install")
    if _raw_journal_transaction_id(install) is not None:
        raise RuntimeError("cannot abort a durable installation transaction")
    candidate = install.parent / f".{install.name}.candidate-{transaction_id}"
    if candidate.exists():
        _plain_ancestors(candidate, install.parent)
        shutil.rmtree(candidate, onerror=_remove_read_only)
    _release_lock(install, transaction_id)


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _plain_ancestors(path.parent, path.parent)
    temporary = path.with_name(f"{path.name}.next-{uuid.uuid4().hex}")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, separators=(",", ":"), sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(50):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            time.sleep(0.02)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _flush_file(path: Path) -> None:
    # Windows FlushFileBuffers requires a write-capable handle; never truncate the file.
    with path.open("r+b" if os.name == "nt" else "rb") as stream:
        os.fsync(stream.fileno())


def _flush_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _remove_read_only(function: Callable[[str], object], path: str, _: object) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _interrupt(phase: str, exit_code: int = 91) -> None:
    if (
        os.environ.get("DEVLOOP_TESTING") == "1"
        and os.environ.get("DEVLOOP_TEST_INTERRUPT_AFTER_PHASE") == phase
    ):
        os._exit(exit_code)


def _write_journal(install: Path, journal: dict[str, object], phase: str) -> None:
    journal["phase"] = phase
    _atomic_json(install / "bootstrap" / "install-transaction.json", journal)
    _interrupt(phase)


def _pointer_from_evidence(evidence: dict[str, object]) -> dict[str, object]:
    return {field: evidence[field] for field in POINTER_FIELDS}


def _release_evidence(root: Path, commit: str) -> dict[str, object]:
    _plain(root)
    if _git(root, "rev-parse", "HEAD").strip() != commit:
        raise RuntimeError("release commit changed during validation")
    try:
        _git(root, "diff", "--quiet", "--")
    except RuntimeError as error:
        raise RuntimeError("release has tracked changes") from error
    try:
        _git(root, "diff", "--cached", "--quiet", "--")
    except RuntimeError as error:
        raise RuntimeError("release has staged changes") from error
    _validate_release_content(root)
    return {
        "commit": commit,
        "release_path": f"releases/{commit}",
        "tracked_fingerprint": _tracked_fingerprint(root),
        "runtime_fingerprint": _directory_fingerprint(root / ".venv"),
    }


def _release_manifest(evidence: dict[str, object], release: Path) -> dict[str, object]:
    metadata = json.loads((release / "portable-release.json").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or not COMPATIBILITY_FIELDS.issubset(metadata):
        raise RuntimeError("release compatibility metadata is incomplete")
    compatibility = {field: metadata[field] for field in COMPATIBILITY_FIELDS}
    return {
        "version": MANIFEST_SCHEMA,
        **evidence,
        **compatibility,
    }


def _safe_relative(value: object, allowed: frozenset[str] = ASSET_PATHS) -> str:
    if not isinstance(value, str) or "\\" in value:
        raise RuntimeError("layout asset path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or ".." in path.parts or value not in allowed:
        raise RuntimeError("layout asset path is not canonical or owned")
    return value


def _validate_hash(value: object, description: str) -> str:
    if not isinstance(value, str) or HASH_PATTERN.fullmatch(value) is None:
        raise RuntimeError(f"{description} hash is invalid")
    return value


@overload
def _validate_layout(value: object, *, pending: Literal[True]) -> PendingLayout: ...


@overload
def _validate_layout(value: object, *, pending: Literal[False] = False) -> Layout: ...


def _validate_layout(value: object, *, pending: bool = False) -> Layout | PendingLayout:
    fields = PENDING_LAYOUT_FIELDS if pending else LAYOUT_FIELDS
    accepted_fields = (fields, fields | {"backup_staging"}) if pending else (fields,)
    if (
        not isinstance(value, dict) or set(value) not in accepted_fields
        or value.get("version") != 2
    ):
        raise RuntimeError("bootstrap layout has an unsupported schema")
    assets = value["assets"]
    backups = value["legacy_backups"]
    if not isinstance(assets, dict) or set(assets) != ASSET_PATHS:
        raise RuntimeError("bootstrap layout asset allowlist is incomplete")
    if not isinstance(backups, dict) or not set(backups).issubset(ASSET_PATHS):
        raise RuntimeError("bootstrap layout legacy backup allowlist is invalid")
    for relative, digest in assets.items():
        _safe_relative(relative)
        _validate_hash(digest, "bootstrap asset")
    for relative, digest in backups.items():
        _safe_relative(relative)
        _validate_hash(digest, "legacy backup")
    if pending:
        value = {"backup_staging": {}, **value}
        staging = value["backup_staging"]
        if not isinstance(staging, dict) or not set(staging).issubset(backups):
            raise RuntimeError("bootstrap backup staging ownership is invalid")
        for relative, name in staging.items():
            if not isinstance(name, str) or re.fullmatch(
                re.escape(PurePosixPath(relative).name) + r"\.backup-[0-9a-f]{32}", name,
            ) is None:
                raise RuntimeError("bootstrap backup staging path is invalid")
        for key in ("backups_ready", "published"):
            entries = value[key]
            if not isinstance(entries, list) or len(entries) != len(set(entries)):
                raise RuntimeError(f"bootstrap publication {key} is invalid")
            for relative in entries:
                _safe_relative(relative)
        if not set(value["backups_ready"]).issubset(backups):
            raise RuntimeError("bootstrap publication backup state is invalid")
        if not set(value["published"]).issubset(assets):
            raise RuntimeError("bootstrap publication asset state is invalid")
    if pending:
        return cast(PendingLayout, value)
    return cast(Layout, value)


def _validate_legacy_layout(value: object) -> LegacyLayout:
    if not isinstance(value, dict) or set(value) != LAYOUT_FIELDS or value.get("version") != 1:
        raise RuntimeError("legacy bootstrap layout has an unsupported schema")
    assets = value["assets"]
    backups = value["legacy_backups"]
    if not isinstance(assets, dict) or set(assets) != ASSET_PATHS:
        raise RuntimeError("legacy bootstrap layout asset allowlist is incomplete")
    if (
        not isinstance(backups, list)
        or len(backups) != len(set(backups))
        or not set(backups).issubset(ASSET_PATHS)
    ):
        raise RuntimeError("legacy bootstrap backup allowlist is invalid")
    for relative, digest in assets.items():
        _safe_relative(relative)
        _validate_hash(digest, "legacy bootstrap asset")
    for relative in backups:
        _safe_relative(relative)
    return cast(LegacyLayout, value)


def _verified_legacy_layout(install: Path) -> LegacyLayout:
    layout_path = install / "bootstrap" / "layout.json"
    layout = _validate_legacy_layout(json.loads(layout_path.read_text(encoding="utf-8")))
    for relative, expected in layout["assets"].items():
        target = install / _safe_relative(relative)
        _plain_ancestors(target, install)
        if not target.is_file() or _sha256(target) != expected:
            raise RuntimeError(f"legacy stable bootstrap asset was modified: {relative}")
    for relative in layout["legacy_backups"]:
        backup = install / "bootstrap" / "legacy-assets" / _safe_relative(relative)
        _plain_ancestors(backup, install)
        if not backup.is_file():
            raise RuntimeError(f"legacy bootstrap backup is missing: {relative}")
    return layout


def _reconcile_committed_pending_layout(install: Path) -> None:
    layout_path = install / "bootstrap" / "layout.json"
    pending_path = install / "bootstrap" / "layout.pending.json"
    if not layout_path.is_file() or not pending_path.exists():
        return
    raw_layout = json.loads(layout_path.read_text(encoding="utf-8"))
    if isinstance(raw_layout, dict) and raw_layout.get("version") == 1:
        legacy = _validate_legacy_layout(raw_layout)
        pending = _validate_layout(
            json.loads(pending_path.read_text(encoding="utf-8")), pending=True,
        )
        _validate_pending_publication(install, pending, legacy)
        return
    layout = _validate_layout(raw_layout)
    pending = _validate_layout(
        json.loads(pending_path.read_text(encoding="utf-8")), pending=True
    )
    committed = _committed_layout(pending)
    if committed != layout or set(pending["published"]) != ASSET_PATHS or set(
        pending["backups_ready"]
    ) != set(layout["legacy_backups"]):
        raise RuntimeError("pending bootstrap publication does not match committed layout")
    for relative, expected in layout["assets"].items():
        target = install / _safe_relative(relative)
        if not target.is_file() or _sha256(target) != expected:
            raise RuntimeError(f"stable bootstrap asset was modified: {relative}")
    pending_path.unlink()


def _committed_layout(pending: PendingLayout) -> Layout:
    return {"version": pending["version"], "assets": pending["assets"],
            "legacy_backups": pending["legacy_backups"]}


def _validate_pending_publication(
    install: Path, pending: PendingLayout, legacy: LegacyLayout | None,
) -> None:
    originals = legacy["assets"] if legacy is not None else pending["legacy_backups"]
    if legacy is not None and set(pending["legacy_backups"]) != set(legacy["legacy_backups"]):
        raise RuntimeError("bootstrap publication legacy backup set changed")
    for relative, desired in pending["assets"].items():
        target = install / relative
        _plain_full_ancestry(target)
        if not target.exists():
            if relative in originals or relative in pending["published"]:
                raise RuntimeError(f"bootstrap publication asset is missing: {relative}")
            continue
        if not target.is_file():
            raise RuntimeError(f"bootstrap publication asset is not a file: {relative}")
        digest = _sha256(target)
        allowed = (
            {desired} if relative in pending["published"] else {desired, originals.get(relative)}
        )
        if digest not in allowed:
            raise RuntimeError(f"bootstrap publication asset was modified: {relative}")
    for relative, expected in pending["legacy_backups"].items():
        backup = install / "bootstrap" / "legacy-assets" / relative
        _plain_full_ancestry(backup)
        staged_name = pending["backup_staging"].get(relative)
        if staged_name is not None:
            staged = backup.with_name(staged_name)
            _plain_full_ancestry(staged)
            if staged.exists() and not staged.is_file():
                raise RuntimeError(f"legacy backup staging is not a file: {relative}")
        if backup.exists():
            if not backup.is_file() or _sha256(backup) != expected:
                raise RuntimeError(f"legacy backup was modified: {relative}")
        elif legacy is not None or relative in pending["backups_ready"]:
            raise RuntimeError(f"legacy backup is missing: {relative}")
        else:
            target = install / relative
            if not target.is_file() or _sha256(target) != expected:
                raise RuntimeError(f"original bootstrap asset is unavailable: {relative}")


def _publish_legacy_backup(
    target: Path, backup: Path, expected: str, relative: str,
    pending_path: Path, pending: PendingLayout,
) -> None:
    if backup.exists():
        if not backup.is_file() or _sha256(backup) != expected:
            raise RuntimeError(f"legacy backup was modified: {relative}")
        _flush_file(backup)
        _flush_directory(backup.parent)
        staged_name = pending["backup_staging"].get(relative)
        if staged_name is not None:
            staged = backup.with_name(staged_name)
            _plain_full_ancestry(staged)
            if staged.exists():
                if not staged.is_file():
                    raise RuntimeError(f"legacy backup staging is not a file: {relative}")
                staged.unlink()
                _flush_directory(backup.parent)
        return
    if not target.is_file() or _sha256(target) != expected:
        raise RuntimeError(f"original bootstrap asset was modified: {relative}")
    staged_name = pending["backup_staging"].get(relative)
    if staged_name is None:
        temporary = backup.with_name(f"{backup.name}.backup-{uuid.uuid4().hex}")
        # Reserve before recording ownership. A crash before the checkpoint
        # leaves an unowned empty file, which recovery never reuses or removes.
        with temporary.open("xb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        pending["backup_staging"][relative] = temporary.name
        _atomic_json(pending_path, pending)
    else:
        temporary = backup.with_name(staged_name)
    _plain_full_ancestry(temporary)
    shutil.copy2(target, temporary)
    if _sha256(temporary) != expected or _sha256(target) != expected:
        raise RuntimeError(f"original bootstrap asset changed during backup: {relative}")
    _flush_file(temporary)
    # Hard-link publication is atomic and exclusive on both supported platforms;
    # unlike replace(), it cannot overwrite a concurrently created final backup.
    try:
        os.link(temporary, backup)
    except FileExistsError:
        raise
    except OSError as error:
        raise RuntimeError(
            "filesystem cannot atomically publish legacy backup; "
            "original and owned staging were preserved"
        ) from error
    _flush_directory(backup.parent)
    temporary.unlink()
    _flush_directory(backup.parent)


def _initialize_bootstrap(install: Path, candidate: Path) -> None:
    template = candidate / "install" / "bootstrap"
    desired = {target: _sha256(template / source) for source, target in ASSETS}
    for source, _ in ASSETS:
        _plain_ancestors(template / source, candidate)
    for relative in ("bootstrap", "bootstrap/legacy-assets", "releases", "bin", "install"):
        path = install / relative
        if path.exists():
            _plain_ancestors(path, install)
        else:
            path.mkdir(parents=True, exist_ok=True)
    layout_path = install / "bootstrap" / "layout.json"
    pending_path = install / "bootstrap" / "layout.pending.json"
    pending: PendingLayout | None = None
    if pending_path.exists():
        _plain_ancestors(pending_path, Path(install.anchor))
        pending = _validate_layout(
            json.loads(pending_path.read_text(encoding="utf-8")), pending=True,
        )
        if pending["assets"] != desired:
            raise RuntimeError("bootstrap publication candidate changed")
    legacy_layout: LegacyLayout | None = None
    if layout_path.exists():
        raw_layout = json.loads(layout_path.read_text(encoding="utf-8"))
        if isinstance(raw_layout, dict) and raw_layout.get("version") == 1:
            legacy_layout = (
                _validate_legacy_layout(raw_layout) if pending is not None
                else _verified_legacy_layout(install)
            )
            if (install / "bootstrap" / "install-transaction.json").exists():
                raise RuntimeError(
                    "legacy installation transaction must be completed before migration"
                )
        else:
            layout = _validate_layout(raw_layout)
            for relative, expected in layout["assets"].items():
                target = install / _safe_relative(relative)
                _plain_ancestors(target, install)
                if not target.is_file() or _sha256(target) != expected:
                    raise RuntimeError(f"stable bootstrap asset was modified: {relative}")
            _reconcile_committed_pending_layout(install)
            return
    if pending is not None:
        _validate_pending_publication(install, pending, legacy_layout)
        backups = pending["legacy_backups"]
    elif legacy_layout is not None:
        backups = {
            relative: _sha256(
                install / "bootstrap" / "legacy-assets" / _safe_relative(relative)
            )
            for relative in legacy_layout["legacy_backups"]
        }
    else:
        backups = {
            target: _sha256(install / target)
            for _, target in ASSETS
            if (install / target).is_file()
        }
    if pending is None:
        pending = {
            "version": 2,
            "assets": desired,
            "legacy_backups": backups,
            "backups_ready": [],
            "published": [],
            "backup_staging": {},
        }
        _atomic_json(pending_path, pending)
    _validate_pending_publication(install, pending, legacy_layout)
    backup_root = install / "bootstrap" / "legacy-assets"
    for relative, expected in pending["legacy_backups"].items():
        backup = backup_root / _safe_relative(relative)
        target = install / relative
        _plain_ancestors(target, install)
        _plain_ancestors(backup.parent, install)
        if relative not in pending["backups_ready"]:
            backup.parent.mkdir(parents=True, exist_ok=True)
            _publish_legacy_backup(target, backup, expected, relative, pending_path, pending)
            pending["backups_ready"].append(relative)
            _atomic_json(pending_path, pending)
            if os.environ.get("DEVLOOP_TEST_INTERRUPT_BOOTSTRAP_AFTER_BACKUP") == "1":
                os._exit(93)
    for source_relative, target_relative in ASSETS:
        source = template / source_relative
        target = install / target_relative
        if target_relative in pending["published"]:
            if not target.is_file() or _sha256(target) != desired[target_relative]:
                raise RuntimeError(f"published bootstrap asset was modified: {target_relative}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or _sha256(target) != desired[target_relative]:
            temporary = target.with_name(f"{target.name}.publish-{uuid.uuid4().hex}")
            shutil.copy2(source, temporary)
            if target.suffix in {".sh", ""}:
                temporary.chmod(temporary.stat().st_mode | 0o111)
            if _sha256(temporary) != desired[target_relative]:
                raise RuntimeError(
                    f"bootstrap source changed during publication: {target_relative}"
                )
            _flush_file(temporary)
            os.replace(temporary, target)
        _flush_file(target)
        _flush_directory(target.parent)
        if (
            os.environ.get("DEVLOOP_TESTING") == "1"
            and os.environ.get("DEVLOOP_TEST_INTERRUPT_BOOTSTRAP_AFTER_ASSET") == target_relative
        ):
            os._exit(94)
        pending["published"].append(target_relative)
        _atomic_json(pending_path, pending)
    layout = _committed_layout(pending)
    _atomic_json(layout_path, layout)
    pending_path.unlink()


def _ownership_path(install: Path, transaction_id: str) -> Path:
    return install / "bootstrap" / f"candidate-{transaction_id}.json"


def _validate_candidate_path(install: Path, value: object, transaction_id: str) -> Path:
    expected = install.parent / f".{install.name}.candidate-{transaction_id}"
    if not isinstance(value, str) or value != str(expected):
        raise RuntimeError("candidate path is not owned by this transaction")
    candidate = Path(value)
    if not candidate.is_absolute() or str(candidate) != value:
        raise RuntimeError("candidate path is not canonical and absolute")
    for path in (candidate, *candidate.parents):
        try:
            status = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(status.st_mode) or (
            getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise RuntimeError(f"candidate path contains a reparse or symbolic link: {path}")
        if not stat.S_ISDIR(status.st_mode):
            raise RuntimeError(f"candidate path is not a plain directory: {path}")
    if candidate.parent.resolve(strict=True) != candidate.parent:
        raise RuntimeError("candidate path parent is not canonical")
    return candidate


def _validate_candidate_ownership(
    install: Path, journal: dict[str, object], candidate: Path
) -> None:
    ownership = json.loads(
        _ownership_path(install, str(journal["transaction_id"])).read_text(encoding="utf-8")
    )
    if not isinstance(ownership, dict) or set(ownership) != OWNERSHIP_FIELDS:
        raise RuntimeError("candidate ownership evidence has an unsupported schema")
    if ownership != {
        "version": 1,
        "transaction_id": journal["transaction_id"],
        "candidate_name": candidate.name,
        "commit": journal["commit"],
    }:
        raise RuntimeError("candidate ownership evidence does not match the journal")


def _journal(install: Path) -> dict[str, object] | None:
    path = install / "bootstrap" / "install-transaction.json"
    if not path.exists():
        return None
    _plain_ancestors(path, install)
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or set(value) != JOURNAL_FIELDS
        or value.get("version") != TRANSACTION_SCHEMA
    ):
        raise RuntimeError("installation journal has an unsupported schema")
    commit = value["commit"]
    if not isinstance(commit, str) or COMMIT_PATTERN.fullmatch(commit) is None:
        raise RuntimeError("installation journal has an invalid commit")
    if Path(str(value["install_root"])) != install:
        raise RuntimeError("installation journal root mismatch")
    if value["release_path"] != f"releases/{commit}":
        raise RuntimeError("installation journal release path is not owned")
    if value["phase"] not in PHASES:
        raise RuntimeError("installation journal phase is unsupported")
    for field in ("tracked_fingerprint", "runtime_fingerprint"):
        _validate_hash(value[field], "installation journal")
    transaction_id = value["transaction_id"]
    if (
        not isinstance(transaction_id, str)
        or TRANSACTION_ID_PATTERN.fullmatch(transaction_id) is None
    ):
        raise RuntimeError("installation journal transaction identity is invalid")
    candidate = _validate_candidate_path(install, value["candidate_path"], transaction_id)
    if value["phase"] != "journaled":
        _validate_candidate_ownership(install, value, candidate)
    return value


def publish(install: Path, candidate: Path, transaction_id: str) -> None:
    install = install.absolute()
    candidate = candidate.absolute()
    _assert_lock(install, transaction_id, "install")
    candidate = _validate_candidate_path(install, str(candidate), transaction_id)
    _initialize_bootstrap(install, candidate)


def prepare(install: Path, candidate: Path, commit: str, transaction_id: str) -> Path:
    install = install.absolute()
    candidate = candidate.absolute()
    _assert_lock(install, transaction_id, "install")
    candidate = _validate_candidate_path(install, str(candidate), transaction_id)
    evidence = _release_evidence(candidate, commit)
    if not (install / "bootstrap" / "layout.json").is_file():
        raise RuntimeError("stable bootstrap must be published before prepare")
    _initialize_bootstrap(install, candidate)
    if _journal(install) is not None:
        raise RuntimeError("an unfinished installation transaction requires recovery")
    release = install / "releases" / commit
    previous = previous_previous = None
    current_path = install / "bootstrap" / "current.json"
    if current_path.exists():
        state = read_pointer_state(install)
        verify_pointer(install, state.current, "current")
        if state.previous is not None:
            verify_pointer(install, state.previous, "previous")
        previous, previous_previous = state.current, state.previous
    if release.exists():
        verify_release(install, commit)
        _verify_git_ownership(install, release, required=True, commit=commit)
        retained = _release_evidence(release, commit)
        if any(retained[field] != evidence[field] for field in ("commit", "tracked_fingerprint")):
            raise RuntimeError("retained release does not match requested candidate")
        # The candidate is still owned solely by the live install lock, exactly
        # as in abort(). Discard it before publishing a journal for the retained
        # release: its newly-built runtime need not match the retained runtime.
        _validate_candidate_path(install, str(candidate), transaction_id)
        if _release_evidence(candidate, commit) != evidence:
            raise RuntimeError("candidate changed before retained release activation")
        shutil.rmtree(candidate, onerror=_remove_read_only)
        _atomic_json(_ownership_path(install, transaction_id), {
            "version": 1, "transaction_id": transaction_id,
            "candidate_name": candidate.name, "commit": commit,
        })
        _write_journal(install, {
            "version": TRANSACTION_SCHEMA, "transaction_id": transaction_id,
            "install_root": str(install), "candidate_path": str(candidate),
            **retained, "previous_pointer": previous,
            "previous_previous_pointer": previous_previous,
        }, "release_ready")
        return release
    git_ownership = {
        "version": GIT_OWNERSHIP_SCHEMA,
        "commit": commit,
        "git_fingerprint": _git_directory_fingerprint(candidate),
    }
    git_ownership_path = _git_ownership_path(install, commit)
    _plain_ancestors(git_ownership_path, install)
    if git_ownership_path.exists():
        # A crash before the first journal write may leave this receipt. Never
        # replace earlier ownership evidence with today's candidate contents.
        _verify_git_ownership(install, candidate, required=True, commit=commit)
    else:
        _atomic_json(git_ownership_path, git_ownership)
    journal = {
        "version": TRANSACTION_SCHEMA,
        "transaction_id": transaction_id,
        "install_root": str(install),
        "candidate_path": str(candidate),
        **evidence,
        "previous_pointer": previous,
        "previous_previous_pointer": previous_previous,
        "phase": "prepared",
    }
    _write_journal(install, journal, "journaled")
    ownership = {
        "version": 1,
        "transaction_id": transaction_id,
        "candidate_name": candidate.name,
        "commit": commit,
    }
    _atomic_json(_ownership_path(install, transaction_id), ownership)
    _write_journal(install, journal, "owned")
    _atomic_json(
        install / "bootstrap" / f"release-{commit}.json",
        _release_manifest(evidence, candidate),
    )
    _write_journal(install, journal, "manifested")
    _write_journal(install, journal, "prepared")
    if (
        os.environ.get("DEVLOOP_TESTING") == "1"
        and os.environ.get("DEVLOOP_TEST_MUTATE_PREPARED_CANDIDATE") == "1"
    ):
        with (candidate / "release-marker").open("a") as stream:
            stream.write("tampered\n")
        actual = _release_evidence(candidate, commit)
        if any(actual[field] != journal[field] for field in POINTER_FIELDS):
            raise RuntimeError("prepared candidate fingerprint mismatch")
    candidate.rename(release)
    _interrupt("candidate_moved")
    _write_journal(install, journal, "release_ready")
    return release


def _validate_journal_release(install: Path, journal: dict[str, object]) -> Path:
    release = install / str(journal["release_path"])
    verify_release(install, str(journal["commit"]))
    evidence = _release_evidence(release, str(journal["commit"]))
    for field in POINTER_FIELDS:
        if evidence[field] != journal[field]:
            raise RuntimeError("immutable release fingerprint mismatch")
    return release


def recover(install: Path, transaction_id: str) -> tuple[str, Path | None]:
    install = install.absolute()
    _assert_lock(install, transaction_id, "install")
    journal = _journal(install)
    if journal is None:
        return "NONE", None
    if journal["transaction_id"] != transaction_id:
        raise RuntimeError("installation journal transaction identity does not match")
    release = install / str(journal["release_path"])
    phase = str(journal["phase"])
    candidate = Path(str(journal["candidate_path"]))
    for location in (candidate, release):
        if location.exists():
            _verify_git_ownership(install, location, commit=str(journal["commit"]))
    if phase == "journaled":
        if not candidate.is_dir():
            raise RuntimeError("journaled transaction has no candidate")
        evidence = _release_evidence(candidate, str(journal["commit"]))
        if any(evidence[field] != journal[field] for field in POINTER_FIELDS):
            raise RuntimeError("journaled candidate fingerprint mismatch")
        ownership = {
            "version": 1,
            "transaction_id": transaction_id,
            "candidate_name": candidate.name,
            "commit": journal["commit"],
        }
        _atomic_json(_ownership_path(install, transaction_id), ownership)
        _write_journal(install, journal, "owned")
        phase = "owned"
    if phase == "owned":
        evidence = _release_evidence(candidate, str(journal["commit"]))
        _atomic_json(
            install / "bootstrap" / f"release-{journal['commit']}.json",
            _release_manifest(evidence, candidate),
        )
        _write_journal(install, journal, "manifested")
        phase = "manifested"
    if phase == "manifested":
        _write_journal(install, journal, "prepared")
        phase = "prepared"
    if phase == "prepared":
        locations = [path for path in (candidate, release) if path.exists()]
        if not locations:
            raise RuntimeError("prepared transaction has no candidate or canonical release")
        for location in locations:
            evidence = _release_evidence(location, str(journal["commit"]))
            for field in POINTER_FIELDS:
                if evidence[field] != journal[field]:
                    raise RuntimeError("prepared candidate fingerprint mismatch")
        if candidate.exists() and not release.exists():
            candidate.rename(release)
        elif candidate.exists() and release.exists():
            _verify_git_ownership(
                install, candidate, required=True, commit=str(journal["commit"])
            )
            shutil.rmtree(candidate, onerror=_remove_read_only)
        _validate_journal_release(install, journal)
        _write_journal(install, journal, "release_ready")
        phase = "release_ready"
    if phase in {"release_ready", "adopted"}:
        _validate_journal_release(install, journal)
        return "NEEDS_ADOPTION" if phase == "release_ready" else "READY_TO_SWITCH", release
    if phase in {"switched", "committed"}:
        state = read_pointer_state(install)
        if (
            state.current != _pointer_from_evidence(journal)
            or state.previous != journal["previous_pointer"]
        ):
            raise RuntimeError("switched journal disagrees with authoritative pointer state")
        verify(install)
        _finish_transaction(install, journal)
        _release_lock(install, transaction_id)
        return "COMPLETE", release
    raise RuntimeError("installation journal phase is unsupported")


def _finish_transaction(install: Path, journal: dict[str, object]) -> None:
    journal_path = install / "bootstrap" / "install-transaction.json"
    if journal_path.exists():
        journal_path.unlink()
    ownership = _ownership_path(install, str(journal["transaction_id"]))
    if ownership.exists():
        ownership.unlink()


def _assert_pointer_baseline(
    install: Path, journal: dict[str, object], new_pointer: dict[str, object]
) -> None:
    current_path = install / "bootstrap" / "current.json"
    expected_current = journal["previous_pointer"]
    expected_previous = journal["previous_previous_pointer"]
    if expected_current is None:
        if expected_previous is not None:
            raise RuntimeError("fresh installation journal has an invalid previous pointer")
        if not current_path.exists():
            return
    state = read_pointer_state(install)
    if state.current == new_pointer and state.previous == expected_current:
        return
    if state.current != expected_current or state.previous != expected_previous:
        raise RuntimeError("authoritative pointer changed since transaction prepare")


def commit(install: Path, transaction_id: str) -> None:
    install = install.absolute()
    _assert_lock(install, transaction_id, "install")
    journal = _journal(install)
    if journal is None:
        raise RuntimeError("installation journal is missing")
    if journal["transaction_id"] != transaction_id:
        raise RuntimeError("installation journal transaction identity does not match")
    _validate_journal_release(install, journal)
    if journal["phase"] == "release_ready":
        _write_journal(install, journal, "adopted")
    new_pointer = _pointer_from_evidence(journal)
    current_path = install / "bootstrap" / "current.json"
    _assert_pointer_baseline(install, journal, new_pointer)
    previous = journal["previous_pointer"]
    if (
        os.environ.get("DEVLOOP_TESTING") == "1"
        and os.environ.get("DEVLOOP_TEST_MUTATE_CANDIDATE_AFTER_VALIDATION") == "1"
    ):
        with (install / str(journal["release_path"]) / "release-marker").open("a") as stream:
            stream.write("tampered\n")
    _validate_journal_release(install, journal)
    if (
        previous is not None
        and os.environ.get("DEVLOOP_TESTING") == "1"
        and os.environ.get("DEVLOOP_TEST_MUTATE_CURRENT_AFTER_FINAL_CHECK") == "1"
    ):
        if not isinstance(previous, dict):
            raise RuntimeError("installation journal previous pointer is invalid")
        (install / str(previous["release_path"]) / "operator-race.bin").write_bytes(b"race\x00\xff")
    _atomic_json(
        current_path, {"version": POINTER_SCHEMA, "current": new_pointer, "previous": previous}
    )
    old_previous = install / "bootstrap" / "previous.json"
    if old_previous.exists():
        old_previous.unlink()
    _interrupt("pointer_swapped")
    _write_journal(install, journal, "switched")
    _write_journal(install, journal, "committed")
    verify(install)
    _finish_transaction(install, journal)
    _release_lock(install, transaction_id)


def rollback(install: Path, transaction_id: str) -> None:
    install = install.absolute()
    _assert_lock(install, transaction_id, "rollback")
    if _journal(install) is not None:
        raise RuntimeError("finish the pending update before rollback")
    state = read_pointer_state(install)
    if state.previous is None:
        raise RuntimeError("no previous release is available")
    verify_pointer(install, state.current, "current")
    verify_pointer(install, state.previous, "previous")
    _atomic_json(
        install / "bootstrap" / "current.json",
        {"version": POINTER_SCHEMA, "current": state.previous, "previous": state.current},
    )
    legacy_previous = install / "bootstrap" / "previous.json"
    if legacy_previous.exists():
        legacy_previous.unlink()
    _release_lock(install, transaction_id)


def _uninstall_plan(install: Path) -> UninstallPlan:
    install = install.resolve(strict=True)
    _plain_ancestors(install.parent, Path(install.anchor))
    _plain_ancestors(install, install)
    bootstrap = install / "bootstrap"
    releases_root = install / "releases"
    _plain_ancestors(bootstrap, install)
    _plain_ancestors(releases_root, install)
    verifier = bootstrap / "verify.py"
    if not verifier.is_file():
        raise RuntimeError("mandatory release verifier is missing; nothing was removed")
    layout_path = bootstrap / "layout.json"
    _plain_ancestors(layout_path, install)
    layout = _validate_layout(json.loads(layout_path.read_text(encoding="utf-8")))
    expected_verifier_hash = layout["assets"]["bootstrap/verify.py"]
    if _sha256(verifier) != expected_verifier_hash:
        raise RuntimeError("mandatory release verifier was modified; nothing was removed")
    state = read_pointer_state(install)
    verify_pointer(install, state.current, "current")
    if state.previous is not None:
        verify_pointer(install, state.previous, "previous")
    releases: list[Path] = []
    release_inventories: list[tuple[Path, dict[str, str | None]]] = []
    manifests: list[Path] = []
    release_names: set[str] = set()
    for release in releases_root.iterdir():
        _plain_ancestors(release, install)
        if not release.is_dir() or COMMIT_PATTERN.fullmatch(release.name) is None:
            raise RuntimeError(f"unmanaged release path blocks uninstall: {release}")
        inventory = _content_inventory(release)
        verify_release(install, release.name)
        _verify_git_ownership(install, release, required=True)
        _check_inventory(release, inventory, partial=False)
        release_inventories.append((release, inventory))
        release_names.add(release.name)
        releases.append(release)
        manifests.append(bootstrap / f"release-{release.name}.json")
        manifests.append(_git_ownership_path(install, release.name))
    manifest_names: set[str] = set()
    for path in bootstrap.glob("release-*.json"):
        _plain_ancestors(path, install)
        match = re.fullmatch(r"release-([0-9a-f]{40})\.json", path.name)
        if match is None:
            raise RuntimeError(f"non-canonical release ownership manifest: {path.name}")
        manifest_names.add(match.group(1))
    if manifest_names != release_names:
        raise RuntimeError("release ownership manifests do not exactly match managed releases")
    backup_root = bootstrap / "legacy-assets"
    remove_assets: list[Path] = []
    restore_assets: list[tuple[Path, Path]] = []
    for relative, expected in layout["assets"].items():
        target = install / _safe_relative(relative)
        _plain_ancestors(target.parent, install)
        if target.exists():
            _plain_ancestors(target, install)
            if not target.is_file():
                raise RuntimeError(f"bootstrap asset is not a plain file: {relative}")
            if _sha256(target) == expected:
                remove_assets.append(target)
    for relative, expected in layout["legacy_backups"].items():
        backup = backup_root / _safe_relative(relative)
        target = install / relative
        _plain_ancestors(backup, install)
        if not backup.is_file() or _sha256(backup) != expected:
            raise RuntimeError(f"legacy backup is missing or modified: {relative}")
        if target in remove_assets:
            restore_assets.append((backup, target))
    metadata = [layout_path, install / "bootstrap" / "current.json"]
    for name in ("previous.json", "install-transaction.json", "layout.pending.json"):
        path = bootstrap / name
        if path.exists():
            _plain_ancestors(path, install)
            metadata.append(path)
    return UninstallPlan(
        tuple(releases),
        tuple(manifests),
        tuple(remove_assets),
        tuple(restore_assets),
        tuple(metadata),
        tuple(release_inventories),
    )


def _uninstall_staging(install: Path, transaction_id: str) -> Path:
    return install.parent / f".{install.name}.uninstall-{transaction_id}"


def _uninstall_interrupt(action_id: str, position: str) -> None:
    requested = os.environ.get("DEVLOOP_TEST_INTERRUPT_UNINSTALL")
    if os.environ.get("DEVLOOP_TESTING") == "1" and requested == f"{position}:{action_id}":
        os._exit(95)


def _uninstall_actions(
    install: Path,
    plan: UninstallPlan,
    staging: Path,
    skills_destination: Path | None,
    agents_destination: Path | None,
    keep_capabilities: bool,
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []

    def add(kind: str, **values: object) -> None:
        actions.append(
            {"id": f"{len(actions):03d}-{kind}", "kind": kind, "state": "pending", **values}
        )

    if not keep_capabilities:
        state = read_pointer_state(install)
        release = verify_pointer(install, state.current, "current")
        for name, destination in (
            ("skills/codex", skills_destination),
            ("agents/codex", agents_destination),
        ):
            source = release / name
            if destination is not None and source.is_dir():
                add(
                    "stage_capability",
                    source=str(source),
                    staged=str(staging / name),
                    destination=str(destination.absolute()),
                )
    for release in plan.releases:
        add("remove_release", path=str(release))
    for manifest in plan.manifests:
        add("remove_manifest", path=str(manifest))
    pointer_paths = {
        install / "bootstrap" / "current.json",
        install / "bootstrap" / "previous.json",
    }
    for path in plan.metadata:
        if path in pointer_paths:
            add("remove_pointer", path=str(path))
    restore_targets = {target for _, target in plan.restore_assets}
    delayed: list[dict[str, object]] = []
    retained_until_commit = {
        "bootstrap/dispatch.ps1",
        "bootstrap/dispatch.sh",
        "bootstrap/transaction.py",
        "install/uninstall-devloop.ps1",
        "install/uninstall-devloop.sh",
    }
    for target in plan.remove_assets:
        if target in restore_targets:
            continue
        action = {"path": str(target)}
        if target.relative_to(install).as_posix() in retained_until_commit:
            delayed.append({"kind": "remove_asset", **action})
        else:
            add("remove_asset", **action)
    for backup, target in plan.restore_assets:
        values = {"source": str(backup), "target": str(target)}
        if target.relative_to(install).as_posix() in retained_until_commit:
            delayed.append({"kind": "restore_legacy", **values})
        else:
            add("restore_legacy", **values)
    for path in plan.metadata:
        if path not in pointer_paths:
            add("remove_metadata", path=str(path))
    for action in tuple(actions):
        if action["kind"] == "stage_capability":
            add(
                "cleanup_capability",
                staged=action["staged"],
                destination=action["destination"],
            )
    for action in delayed:
        add(str(action.pop("kind")), **action)
    return actions


def _write_uninstall_journal(path: Path, journal: dict[str, object]) -> None:
    _atomic_json(path, journal)


def _uninstall_plan_hash(actions: list[dict[str, object]]) -> str:
    normalized = [{**action, "state": "pending"} for action in actions]
    encoded = json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest().upper()


def _canonical_action_path(value: object, root: Path) -> Path:
    if not isinstance(value, str):
        raise RuntimeError("uninstall action path is not a string")
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts or path == root:
        raise RuntimeError("uninstall action path is not canonical")
    if not path.is_relative_to(root):
        raise RuntimeError("uninstall action path is outside its owned root")
    _plain_full_ancestry(path)
    return path


def _validate_action_paths(
    install: Path, staging: Path, action: dict[str, Any], index: int,
) -> None:
    kind = action.get("kind")
    if (
        not isinstance(kind, str) or kind not in UNINSTALL_ACTION_FIELDS
        or set(action) != UNINSTALL_ACTION_FIELDS[kind]
        or action.get("id") != f"{index:03d}-{kind}"
        or action.get("state") not in {"pending", "before", "after"}
    ):
        raise RuntimeError("uninstall journal action has an unsupported schema or identity")
    if "path" in action:
        target = _canonical_action_path(action["path"], install)
        relative = target.relative_to(install).as_posix()
        valid = {
            "remove_release": re.fullmatch(r"releases/[0-9a-f]{40}", relative) is not None,
            "remove_manifest": re.fullmatch(
                r"bootstrap/(release|git-ownership)-[0-9a-f]{40}\.json", relative,
            ) is not None,
            "remove_pointer": relative in {"bootstrap/current.json", "bootstrap/previous.json"},
            "remove_asset": relative in ASSET_PATHS,
            "remove_metadata": relative in {f"bootstrap/{name}" for name in UNINSTALL_METADATA},
        }
        if not valid[kind]:
            raise RuntimeError("uninstall action target is not in its canonical allowlist")
    elif kind == "restore_legacy":
        target = _canonical_action_path(action["target"], install)
        relative = _safe_relative(target.relative_to(install).as_posix())
        source = _canonical_action_path(action["source"], install)
        if source != install / "bootstrap" / "legacy-assets" / relative:
            raise RuntimeError("uninstall legacy restore action is not owned")
    else:
        staged = _canonical_action_path(action["staged"], staging)
        relative = staged.relative_to(staging).as_posix()
        if relative not in CAPABILITY_PATHS:
            raise RuntimeError("uninstall capability staging path is not owned")
        destination = Path(str(action["destination"]))
        _canonical_action_path(action["destination"], Path(destination.anchor))
        if kind == "stage_capability":
            source = _canonical_action_path(action["source"], install)
            if re.fullmatch(
                rf"releases/[0-9a-f]{{40}}/{re.escape(relative)}",
                source.relative_to(install).as_posix(),
            ) is None:
                raise RuntimeError("uninstall capability source is not owned")


def _content_inventory(root: Path) -> dict[str, str | None]:
    _plain_full_ancestry(root)
    inventory: dict[str, str | None] = {}
    pending = [root]
    while pending:
        path = pending.pop()
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode) or (
            getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise RuntimeError("uninstall content contains a reparse or symbolic link")
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(status.st_mode):
            inventory[relative] = None
            pending.extend(path.iterdir())
        elif stat.S_ISREG(status.st_mode):
            inventory[relative] = _sha256(path)
        else:
            raise RuntimeError("uninstall content contains a special filesystem entry")
    return inventory


def _check_inventory(path: Path, expected: object, *, partial: bool) -> None:
    if not isinstance(expected, dict) or "." not in expected:
        raise RuntimeError("uninstall original content evidence is missing")
    for relative, digest in expected.items():
        if (
            not isinstance(relative, str) or "\\" in relative
            or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts
            or PurePosixPath(relative).as_posix() != relative
        ):
            raise RuntimeError("uninstall original content evidence path is invalid")
        if digest is not None:
            _validate_hash(digest, "uninstall content")
    actual = _content_inventory(path) if path.exists() else {}
    if (not partial and actual != expected) or any(
        relative not in expected or expected[relative] != digest
        for relative, digest in actual.items()
    ):
        raise RuntimeError(f"uninstall content changed after preflight: {path}")


def _publish_uninstall_evidence(
    install: Path, staging: Path, journal: dict[str, Any],
    *, release_inventories: Mapping[Path, dict[str, str | None]] | None = None,
) -> None:
    layout = _validate_layout(json.loads((install / "bootstrap/layout.json").read_text()))
    records: dict[str, object] = {}
    for index, action in enumerate(journal["actions"]):
        _validate_action_paths(install, staging, action, index)
        kind = action["kind"]
        if kind == "remove_release":
            release = Path(action["path"])
            if release_inventories is None or release not in release_inventories:
                raise RuntimeError("uninstall release has no ownership-bound preflight inventory")
            original = release_inventories[release]
            _check_inventory(release, original, partial=False)
            records[action["id"]] = original
        elif "path" in action:
            records[action["id"]] = _content_inventory(Path(action["path"]))
        elif kind == "restore_legacy":
            records[action["id"]] = {
                "source": _content_inventory(Path(action["source"])),
                "target": _content_inventory(Path(action["target"])),
            }
        elif kind == "stage_capability":
            records[action["id"]] = _content_inventory(Path(action["source"]))
        else:
            source_action = next(
                item for item in journal["actions"]
                if item["kind"] == "stage_capability" and item["staged"] == action["staged"]
                and item["destination"] == action["destination"]
            )
            records[action["id"]] = records[source_action["id"]]
    _atomic_json(staging / UNINSTALL_EVIDENCE_NAME, {
        "version": 1, "transaction_id": journal["transaction_id"],
        "install_root": str(install), "layout_hash": journal["layout_hash"],
        "plan_hash": journal["plan_hash"], "layout": layout, "actions": records,
    })


def _uninstall_evidence(
    install: Path, staging: Path, journal: dict[str, Any],
) -> dict[str, Any]:
    path = staging / UNINSTALL_EVIDENCE_NAME
    _plain_full_ancestry(path)
    if not path.is_file():
        raise RuntimeError("uninstall original content evidence is missing; preserve owned data")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "transaction_id", "install_root", "layout_hash",
                         "plan_hash", "layout", "actions"}
        or value["version"] != 1
        or any(value[field] != journal[field] for field in
               ("transaction_id", "install_root", "layout_hash", "plan_hash"))
        or not isinstance(value["actions"], dict)
        or set(value["actions"]) != {item["id"] for item in journal["actions"]}
    ):
        raise RuntimeError("uninstall original content evidence does not match the journal")
    _validate_layout(value["layout"])
    return value


def _validate_action_content(
    install: Path, action: dict[str, Any], evidence: dict[str, Any],
) -> None:
    if action["state"] == "after":
        return
    expected = evidence["actions"][action["id"]]
    kind = action["kind"]
    if "path" in action:
        path = Path(action["path"])
        if kind == "remove_asset":
            relative = path.relative_to(install).as_posix()
            if expected != {".": evidence["layout"]["assets"][relative]}:
                raise RuntimeError("uninstall asset evidence does not match the owned layout")
        _check_inventory(path, expected, partial=action["state"] == "before")
    elif kind == "restore_legacy":
        source, target = Path(action["source"]), Path(action["target"])
        relative = target.relative_to(install).as_posix()
        expected_source = {".": evidence["layout"]["legacy_backups"].get(relative)}
        expected_target = {".": evidence["layout"]["assets"][relative]}
        if expected != {"source": expected_source, "target": expected_target}:
            raise RuntimeError("uninstall legacy evidence does not match the owned layout")
        if source.exists():
            _check_inventory(source, expected_source, partial=False)
            _check_inventory(target, expected_target, partial=action["state"] == "before")
        elif action["state"] == "before":
            _check_inventory(target, expected_source, partial=False)
        else:
            raise RuntimeError("uninstall legacy backup is missing")
    elif kind == "stage_capability":
        _check_inventory(Path(action["source"]), expected, partial=False)
        staged = Path(action["staged"])
        if staged.exists():
            _check_inventory(staged, expected, partial=action["state"] == "before")
    else:
        _check_inventory(Path(action["staged"]), expected, partial=False)


def _new_uninstall_journal(
    install: Path,
    transaction_id: str,
    skills_destination: Path | None,
    agents_destination: Path | None,
    keep_capabilities: bool,
) -> tuple[Path, dict[str, object]]:
    plan = _uninstall_plan(install)
    layout_path = install / "bootstrap" / "layout.json"
    layout_hash = _sha256(layout_path)
    layout = _validate_layout(json.loads(layout_path.read_text(encoding="utf-8")))
    recovery_sources = (
        ("bootstrap/transaction.py", "transaction.py"),
        ("bootstrap/verify.py", "verify.py"),
        ("install/uninstall-devloop.ps1", "uninstall-devloop.ps1"),
        ("install/uninstall-devloop.sh", "uninstall-devloop.sh"),
    )
    for relative, _ in recovery_sources:
        source = install / relative
        _plain_full_ancestry(source)
        if not source.is_file() or _sha256(source) != layout["assets"][relative]:
            raise RuntimeError(
                f"uninstall recovery prerequisite is missing or modified: {relative}"
            )
    staging = _uninstall_staging(install, transaction_id)
    staging.mkdir()
    for relative, name in recovery_sources:
        copied = staging / name
        shutil.copy2(install / relative, copied)
        _flush_file(copied)
    actions = _uninstall_actions(
        install,
        plan,
        staging,
        skills_destination,
        agents_destination,
        keep_capabilities,
    )
    journal = {
        "version": 1,
        "transaction_id": transaction_id,
        "install_root": str(install),
        "layout_hash": layout_hash,
        "plan_hash": _uninstall_plan_hash(actions),
        "status": "executing",
        "actions": actions,
    }
    path = staging / "journal.json"
    _publish_uninstall_evidence(
        install, staging, journal, release_inventories=dict(plan.release_inventories),
    )
    _write_uninstall_journal(path, journal)
    return path, journal


def _validate_uninstall_retry_options(
    install: Path, staging: Path, journal: dict[str, Any], *,
    keep_capabilities: bool, skills_destination: Path | None,
    agents_destination: Path | None, install_root: Path | None, bin_directory: Path | None,
) -> None:
    if install_root is not None and install_root.absolute() != install:
        raise RuntimeError("uninstall retry install directory differs from the bound plan")
    if bin_directory is not None:
        raise RuntimeError("uninstall retry cannot change bin directory; use the bound plan")
    capability_actions = [action for action in journal["actions"]
                          if action["kind"] == "stage_capability"]
    if keep_capabilities and capability_actions:
        raise RuntimeError("uninstall retry preservation option conflicts with the bound plan")
    for relative, requested in (
        ("skills/codex", skills_destination), ("agents/codex", agents_destination),
    ):
        if requested is None:
            continue
        destinations = {action["destination"] for action in capability_actions
                        if action["staged"] == str(staging / relative)}
        if destinations != {str(requested.absolute())}:
            raise RuntimeError("uninstall retry capability destination differs from the bound plan")


def resume_uninstall(
    staging: Path, owner_pid: int | None = None, *, keep_capabilities: bool = False,
    skills_destination: Path | None = None, agents_destination: Path | None = None,
    install_root: Path | None = None, bin_directory: Path | None = None,
) -> None:
    staging = staging.absolute()
    _plain_full_ancestry(staging)
    raw = json.loads((staging / "journal.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("uninstall recovery journal must be an object")
    transaction_id = raw.get("transaction_id")
    journal_install_root = raw.get("install_root")
    if (
        not isinstance(transaction_id, str)
        or TRANSACTION_ID_PATTERN.fullmatch(transaction_id) is None
        or not isinstance(journal_install_root, str)
    ):
        raise RuntimeError("uninstall recovery identity is invalid")
    install = Path(journal_install_root)
    if not install.is_absolute() or str(install) != journal_install_root or (
        staging != _uninstall_staging(install, transaction_id)
    ):
        raise RuntimeError("uninstall recovery location is not owned")
    _, journal = _read_uninstall_journal(install, transaction_id)
    _validate_uninstall_retry_options(
        install, staging, journal, keep_capabilities=keep_capabilities,
        skills_destination=skills_destination, agents_destination=agents_destination,
        install_root=install_root, bin_directory=bin_directory,
    )
    acquired = begin(
        install, "uninstall", owner_pid, recovery_transaction_id=transaction_id,
    )
    if acquired != transaction_id:
        _release_lock(install, acquired)
        raise RuntimeError("uninstall recovery lock does not identify this journal")
    uninstall(install, transaction_id, None, None, True)


def _read_uninstall_journal(
    install: Path, transaction_id: str
) -> tuple[Path, dict[str, object]]:
    path = _uninstall_staging(install, transaction_id) / "journal.json"
    _plain_full_ancestry(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or set(value) != {
            "version",
            "transaction_id",
            "install_root",
            "layout_hash",
            "plan_hash",
            "status",
            "actions",
        }
        or value.get("version") != 1
        or value.get("transaction_id") != transaction_id
        or value.get("install_root") != str(install)
        or not isinstance(value.get("actions"), list)
    ):
        raise RuntimeError("uninstall journal has an unsupported schema")
    _validate_hash(value["layout_hash"], "uninstall layout")
    _validate_hash(value["plan_hash"], "uninstall plan")
    staging = path.parent
    unfinished = False
    started = False
    for index, action in enumerate(value["actions"]):
        if not isinstance(action, dict):
            raise RuntimeError("uninstall journal action is invalid")
        _validate_action_paths(install, staging, action, index)
        if action["state"] == "after":
            if unfinished:
                raise RuntimeError("uninstall action checkpoints are out of order")
        else:
            if action["state"] == "before" and (unfinished or started):
                raise RuntimeError("uninstall action checkpoints are out of order")
            unfinished = True
            started = started or action["state"] == "before"
    if value.get("status") not in {"executing", "committed"}:
        raise RuntimeError("uninstall journal status is unsupported")
    if value["status"] == "committed" and unfinished:
        raise RuntimeError("committed uninstall has unfinished actions")
    if _uninstall_plan_hash(value["actions"]) != value["plan_hash"]:
        raise RuntimeError("uninstall journal action plan changed after preflight")
    layout_path = install / "bootstrap" / "layout.json"
    if layout_path.exists() and _sha256(layout_path) != value["layout_hash"]:
        raise RuntimeError("uninstall layout changed after preflight")
    evidence = _uninstall_evidence(install, staging, value)
    for action in value["actions"]:
        if action["kind"] == "cleanup_capability" and any(
            item["kind"] == "stage_capability" and item["staged"] == action["staged"]
            and item["state"] != "after" for item in value["actions"]
        ):
            continue
        _validate_action_content(install, action, evidence)
    return path, value


def _git_removal_proof(
    install: Path, release: Path, transaction_id: str, state: str,
) -> tuple[Path, dict[str, object]]:
    if (
        TRANSACTION_ID_PATTERN.fullmatch(transaction_id) is None
        or release.parent != install / "releases"
        or COMMIT_PATTERN.fullmatch(release.name) is None
        or state not in {"pending", "before"}
    ):
        raise RuntimeError("Git ownership removal identity or state is invalid")
    _plain_ancestors(release, install)
    receipt = _read_git_ownership(install, release.name, required=True)
    assert receipt is not None
    path = _uninstall_staging(install, transaction_id) / (
        f"{GIT_REMOVAL_PROOF_PREFIX}{release.name}.json"
    )
    _plain_ancestors(path, install.parent)
    identity = {
        "version": GIT_REMOVAL_PROOF_SCHEMA,
        "transaction_id": transaction_id, "install_root": str(install),
        "commit": release.name, "git_fingerprint": receipt["git_fingerprint"],
    }
    for location in (path, path.parent, install.parent):
        try:
            status = location.lstat()
        except FileNotFoundError:
            if location == path:
                continue
            raise
        plain_kind = stat.S_ISREG if location == path else stat.S_ISDIR
        if not plain_kind(status.st_mode) or (
            getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise RuntimeError("Git ownership removal proof requires a plain path and ancestry")
    if path.parent.resolve(strict=True) != path.parent:
        raise RuntimeError("Git ownership removal proof staging path is not canonical")
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict) or set(value) != GIT_REMOVAL_PROOF_FIELDS
            or type(value.get("version")) is not int
            or any(value[key] != expected for key, expected in identity.items())
        ):
            raise RuntimeError("Git ownership removal proof has an invalid schema or identity")
        entries = _parse_git_removal_entries(value["entries"])
        if _git_entries_fingerprint(entries) != receipt["git_fingerprint"]:
            raise RuntimeError("Git ownership removal proof does not match original ownership")
        try:
            (release / ".git").lstat()
        except FileNotFoundError:
            current: list[tuple[str, str, str]] = []
        else:
            current = _git_directory_entries(release)
        if not set(current).issubset(entries) or (state == "pending" and current != entries):
            raise RuntimeError("Git ownership survivors changed; release must be preserved")
        return path, value
    # Reconstruct an entry proof only from the exact original prepare-time hash.
    # A partial old attempt without this proof cannot establish omitted entries.
    try:
        entries = _git_directory_entries(release)
    except FileNotFoundError as error:
        raise RuntimeError(GIT_REMOVAL_PROOF_MISSING) from error
    if _git_entries_fingerprint(entries) != receipt["git_fingerprint"]:
        raise RuntimeError(GIT_REMOVAL_PROOF_MISSING)
    return path, {**identity, "entries": entries}


def _parse_git_removal_entries(value: object) -> list[tuple[str, str, str]]:
    if not isinstance(value, list):
        raise RuntimeError("Git ownership removal proof entries must be a list")
    entries: list[tuple[str, str, str]] = []
    seen: dict[str, str] = {}
    for entry in value:
        if not isinstance(entry, list) or len(entry) != 3 or not all(
            isinstance(field, str) for field in entry
        ):
            raise RuntimeError("Git ownership removal proof entry is invalid")
        relative, kind, digest = entry
        relative_path = PurePosixPath(relative)
        if (
            not relative or "\\" in relative or relative_path.is_absolute()
            or relative != relative_path.as_posix() or ".." in relative_path.parts
            or relative == "." or relative in seen
            or kind not in {GIT_ENTRY_FILE, GIT_ENTRY_DIRECTORY}
            or (kind == GIT_ENTRY_DIRECTORY and digest != "")
            or (kind == GIT_ENTRY_FILE and re.fullmatch(r"[0-9a-f]{64}", digest) is None)
        ):
            raise RuntimeError("Git ownership removal proof entry is not canonical")
        seen[relative] = kind
        entries.append((relative, kind, digest))
    if entries != sorted(entries) or any(
        seen.get(parent.as_posix()) != GIT_ENTRY_DIRECTORY
        for relative in seen for parent in PurePosixPath(relative).parents
        if parent != PurePosixPath(".")
    ):
        raise RuntimeError("Git ownership removal proof inventory is not canonical")
    return entries


def _execute_uninstall_action(action: dict[str, object], transaction_id: str) -> None:
    kind = action["kind"]
    if kind == "remove_release":
        path = Path(str(action["path"]))
        if path.exists():
            _plain_ancestors(path, path.parent.parent)
            proof_path, _ = _git_removal_proof(
                path.parent.parent, path, transaction_id, str(action["state"])
            )
            if not proof_path.is_file():
                raise RuntimeError("Git ownership removal proof must be durable before deletion")
            shutil.rmtree(path, onerror=_remove_read_only)
    elif kind in {"remove_manifest", "remove_pointer", "remove_asset", "remove_metadata"}:
        path = Path(str(action["path"]))
        if path.exists():
            path.unlink()
    elif kind == "restore_legacy":
        source = Path(str(action["source"]))
        target = Path(str(action["target"]))
        if source.exists():
            if target.exists():
                target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
    elif kind == "stage_capability":
        source = Path(str(action["source"]))
        staged = Path(str(action["staged"]))
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, staged, dirs_exist_ok=True)
    elif kind == "cleanup_capability":
        source = Path(str(action["staged"]))
        destination = Path(str(action["destination"]))
        for item in source.rglob("*"):
            if not item.is_file():
                continue
            target = destination / item.relative_to(source)
            _plain_ancestors(target, Path(target.anchor))
            if not target.is_file():
                continue
            if _sha256(item) == _sha256(target):
                target.unlink()
            else:
                print(f"Kept modified capability: {target}")
            if os.environ.get("DEVLOOP_TEST_CAPABILITY_FAILURE") == "1":
                raise OSError("injected capability cleanup failure")
    else:
        raise RuntimeError("uninstall journal action is unsupported")


def _validate_uninstall_staging(
    staging: Path, journal: dict[str, Any], evidence: dict[str, Any],
) -> None:
    allowed = {".", "journal.json", UNINSTALL_EVIDENCE_NAME}
    for name, relative in (
        ("transaction.py", "bootstrap/transaction.py"),
        ("verify.py", "bootstrap/verify.py"),
        ("uninstall-devloop.ps1", "install/uninstall-devloop.ps1"),
        ("uninstall-devloop.sh", "install/uninstall-devloop.sh"),
    ):
        allowed.add(name)
        _check_inventory(staging / name, {".": evidence["layout"]["assets"][relative]},
                         partial=False)
    for action in journal["actions"]:
        if action["kind"] == "remove_release":
            commit = Path(action["path"]).name
            allowed.add(f"{GIT_REMOVAL_PROOF_PREFIX}{commit}.json")
        elif action["kind"] == "stage_capability":
            root = Path(action["staged"])
            expected = evidence["actions"][action["id"]]
            _check_inventory(root, expected, partial=False)
            allowed.add(root.parent.relative_to(staging).as_posix())
            allowed.update((root / relative).relative_to(staging).as_posix()
                           for relative in expected)
    if not set(_content_inventory(staging)).issubset(allowed):
        raise RuntimeError("uninstall recovery staging contains unexpected data; preserve it")


def uninstall_layout(install: Path, transaction_id: str) -> None:
    uninstall(install, transaction_id, None, None, True)


def uninstall(
    install: Path,
    transaction_id: str,
    skills_destination: Path | None,
    agents_destination: Path | None,
    keep_capabilities: bool,
) -> None:
    install = install.absolute()
    _assert_lock(install, transaction_id, "uninstall")
    staging = _uninstall_staging(install, transaction_id)
    if (staging / "journal.json").is_file():
        journal_path, journal = _read_uninstall_journal(install, transaction_id)
    else:
        journal_path, journal = _new_uninstall_journal(
            install,
            transaction_id,
            skills_destination,
            agents_destination,
            keep_capabilities,
        )
    actions = journal["actions"]
    assert isinstance(actions, list)
    evidence = _uninstall_evidence(install, staging, journal)
    launcher = staging / ("uninstall-devloop.ps1" if os.name == "nt" else "uninstall-devloop.sh")
    print(f'devloop-uninstall: recovery entrypoint: "{launcher}"', flush=True)
    for index, raw_action in enumerate(actions):
        if not isinstance(raw_action, dict) or set(raw_action) - {
            "id",
            "kind",
            "state",
            "path",
            "source",
            "target",
            "staged",
            "destination",
        }:
            raise RuntimeError("uninstall journal action is invalid")
        if raw_action.get("state") == "after":
            continue
        if (
            raw_action["kind"] == "cleanup_capability"
            and os.environ.get("DEVLOOP_TEST_INTERRUPT_AFTER_CORE_UNINSTALL") == "1"
        ):
            try:
                (install / "releases").rmdir()
            except OSError:
                pass
            os._exit(95)
        action_id = str(raw_action["id"])
        _validate_action_paths(install, staging, raw_action, index)
        _validate_action_content(install, raw_action, evidence)
        if raw_action["kind"] == "remove_release":
            release = Path(str(raw_action["path"]))
            if release.exists():
                proof_path, proof = _git_removal_proof(
                    install, release, transaction_id, str(raw_action["state"])
                )
                if not proof_path.exists():
                    _atomic_json(proof_path, proof)
        raw_action["state"] = "before"
        _write_uninstall_journal(journal_path, journal)
        _uninstall_interrupt(action_id, "before")
        try:
            _validate_action_paths(install, staging, raw_action, index)
            _validate_action_content(install, raw_action, evidence)
            _execute_uninstall_action(raw_action, transaction_id)
        except OSError as error:
            if raw_action["kind"] != "cleanup_capability":
                raise
            print(f"devloop-uninstall: capability cleanup warning: {error}", file=sys.stderr)
            try:
                (install / "releases").rmdir()
            except OSError:
                pass
            return
        raw_action["state"] = "after"
        _write_uninstall_journal(journal_path, journal)
        _uninstall_interrupt(action_id, "after")
    journal["status"] = "committed"
    _write_uninstall_journal(journal_path, journal)
    for path in (
        install / "releases",
        install / "bootstrap" / "legacy-assets",
        install / "bootstrap",
        install / "bin",
        install / "install",
    ):
        try:
            path.rmdir()
        except OSError:
            pass
    _validate_uninstall_staging(staging, journal, evidence)
    _release_lock(install, transaction_id)
    shutil.rmtree(staging, onerror=_remove_read_only)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    begin_parser = sub.add_parser("begin")
    begin_parser.add_argument("install", type=Path)
    begin_parser.add_argument("operation", choices=("install", "rollback", "uninstall"))
    begin_parser.add_argument("--owner-pid", type=int)
    begin_parser.add_argument("--protocol", type=int, required=True)
    legacy_parser = sub.add_parser("begin-legacy-migration")
    legacy_parser.add_argument("install", type=Path)
    legacy_parser.add_argument("--owner-pid", type=int)
    legacy_parser.add_argument("--protocol", type=int, required=True)
    publish_parser = sub.add_parser("publish")
    publish_parser.add_argument("install", type=Path)
    publish_parser.add_argument("candidate", type=Path)
    publish_parser.add_argument("transaction_id")
    publish_parser.add_argument("--protocol", type=int, required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("install", type=Path)
    prepare_parser.add_argument("candidate", type=Path)
    prepare_parser.add_argument("commit")
    prepare_parser.add_argument("transaction_id")
    prepare_parser.add_argument("--protocol", type=int, required=True)
    for name in ("recover", "commit", "rollback", "abort"):
        command_parser = sub.add_parser(name)
        command_parser.add_argument("install", type=Path)
        command_parser.add_argument("transaction_id")
        command_parser.add_argument("--protocol", type=int, required=True)
    uninstall_parser = sub.add_parser("uninstall")
    uninstall_parser.add_argument("install", type=Path)
    uninstall_parser.add_argument("transaction_id")
    uninstall_parser.add_argument("--protocol", type=int, required=True)
    uninstall_parser.add_argument("--skills-destination", type=Path)
    uninstall_parser.add_argument("--agents-destination", type=Path)
    uninstall_parser.add_argument("--keep-capabilities", action="store_true")
    resume_parser = sub.add_parser("resume-uninstall")
    resume_parser.add_argument("staging", type=Path)
    resume_parser.add_argument("--owner-pid", type=int)
    resume_parser.add_argument("--protocol", type=int, required=True)
    resume_parser.add_argument("--keep-capabilities", action="store_true")
    resume_parser.add_argument("--skills-destination", type=Path)
    resume_parser.add_argument("--agents-destination", type=Path)
    resume_parser.add_argument("--install-root", type=Path)
    resume_parser.add_argument("--bin-directory", type=Path)
    args = parser.parse_args()
    if args.protocol != BOOTSTRAP_PROTOCOL:
        raise RuntimeError(
            "bootstrap protocol mismatch: "
            f"requested {args.protocol}, supported {BOOTSTRAP_PROTOCOL}"
        )
    if args.command == "begin":
        print(begin(args.install, args.operation, args.owner_pid))
    elif args.command == "begin-legacy-migration":
        print(begin_legacy_migration(args.install, args.owner_pid))
    elif args.command == "publish":
        publish(args.install, args.candidate, args.transaction_id)
    elif args.command == "prepare":
        print(prepare(args.install, args.candidate, args.commit, args.transaction_id))
    elif args.command == "recover":
        action, release = recover(args.install, args.transaction_id)
        print(f"{action}\t{release or ''}")
    elif args.command == "commit":
        commit(args.install, args.transaction_id)
    elif args.command == "uninstall":
        uninstall(
            args.install,
            args.transaction_id,
            args.skills_destination,
            args.agents_destination,
            args.keep_capabilities,
        )
    elif args.command == "abort":
        abort(args.install, args.transaction_id)
    elif args.command == "resume-uninstall":
        resume_uninstall(
            args.staging, args.owner_pid, keep_capabilities=args.keep_capabilities,
            skills_destination=args.skills_destination, agents_destination=args.agents_destination,
            install_root=args.install_root, bin_directory=args.bin_directory,
        )
    else:
        rollback(args.install, args.transaction_id)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"devloop-install: error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
