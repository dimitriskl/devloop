from __future__ import annotations

import argparse
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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from verify import (
    MANIFEST_SCHEMA,
    POINTER_FIELDS,
    POINTER_SCHEMA,
    TRANSACTION_SCHEMA,
    _directory_fingerprint,
    _git,
    _plain,
    _plain_ancestors,
    _tracked_fingerprint,
    _validate_release_content,
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


@dataclass(frozen=True)
class UninstallPlan:
    releases: tuple[Path, ...]
    manifests: tuple[Path, ...]
    remove_assets: tuple[Path, ...]
    restore_assets: tuple[tuple[Path, Path], ...]
    metadata: tuple[Path, ...]


def _lock_path(install: Path) -> Path:
    return install.parent / f".{install.name}.install-lock"


def _process_start(pid: int) -> str | None:
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return None
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            try:
                exit_code = wintypes.DWORD()
                if not ctypes.windll.kernel32.GetExitCodeProcess(
                    process, ctypes.byref(exit_code)
                ) or exit_code.value != 259:
                    return None
                if not ctypes.windll.kernel32.GetProcessTimes(
                    process,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
                return str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
        except (AttributeError, OSError):
            return None
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.is_file():
        try:
            return proc_stat.read_text(encoding="utf-8").split()[21]
        except (OSError, IndexError):
            return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return "alive"


def _owner_identity(requested_pid: int | None = None) -> tuple[int, str]:
    owner_pid = os.getppid() if requested_pid is None else requested_pid
    owner_start = _process_start(owner_pid)
    if owner_start is None:
        raise RuntimeError("cannot establish stable installer process identity")
    return owner_pid, owner_start


def _read_lock(install: Path) -> dict[str, object]:
    path = _lock_path(install)
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
    return value


def _lock_owner_alive(lock: dict[str, object]) -> bool:
    if lock["owner_host"] != socket.gethostname():
        return True
    pid = lock["owner_pid"]
    start = lock["owner_start"]
    return isinstance(pid, int) and isinstance(start, str) and _process_start(pid) == start


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


def begin(install: Path, operation: str, owner_pid: int | None = None) -> str:
    install = install.absolute()
    lock_path = _lock_path(install)
    transaction_id = str(uuid.uuid4())
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
        shutil.rmtree(lock_path, onerror=_remove_read_only)
    owner_pid, owner_start = _owner_identity(owner_pid)
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
        _verified_legacy_layout(install)
        if (install / "bootstrap" / "install-transaction.json").exists():
            raise RuntimeError("legacy installation transaction must be completed before migration")
    except Exception:
        _release_lock(install, transaction_id)
        raise
    return transaction_id


def _release_lock(install: Path, transaction_id: str) -> None:
    _assert_lock(install, transaction_id)
    shutil.rmtree(_lock_path(install), onerror=_remove_read_only)


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


def _atomic_json(path: Path, value: dict[str, object]) -> None:
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


def _remove_read_only(function: object, path: str, _: object) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)  # type: ignore[operator]


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


def _validate_layout(value: object, *, pending: bool = False) -> dict[str, object]:
    fields = PENDING_LAYOUT_FIELDS if pending else LAYOUT_FIELDS
    if not isinstance(value, dict) or set(value) != fields or value.get("version") != 2:
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
    return value


def _validate_legacy_layout(value: object) -> dict[str, object]:
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
    return value


def _verified_legacy_layout(install: Path) -> dict[str, object]:
    layout_path = install / "bootstrap" / "layout.json"
    layout = _validate_legacy_layout(json.loads(layout_path.read_text(encoding="utf-8")))
    for relative, expected in layout["assets"].items():  # type: ignore[union-attr]
        target = install / _safe_relative(relative)
        _plain_ancestors(target, install)
        if not target.is_file() or _sha256(target) != expected:
            raise RuntimeError(f"legacy stable bootstrap asset was modified: {relative}")
    for relative in layout["legacy_backups"]:  # type: ignore[union-attr]
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
        _verified_legacy_layout(install)
        _validate_layout(json.loads(pending_path.read_text(encoding="utf-8")), pending=True)
        return
    layout = _validate_layout(raw_layout)
    pending = _validate_layout(
        json.loads(pending_path.read_text(encoding="utf-8")), pending=True
    )
    committed = {key: pending[key] for key in LAYOUT_FIELDS}
    if committed != layout or set(pending["published"]) != ASSET_PATHS or set(
        pending["backups_ready"]
    ) != set(layout["legacy_backups"]):
        raise RuntimeError("pending bootstrap publication does not match committed layout")
    for relative, expected in layout["assets"].items():  # type: ignore[union-attr]
        target = install / _safe_relative(relative)
        if not target.is_file() or _sha256(target) != expected:
            raise RuntimeError(f"stable bootstrap asset was modified: {relative}")
    pending_path.unlink()


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
    legacy_layout: dict[str, object] | None = None
    if layout_path.exists():
        raw_layout = json.loads(layout_path.read_text(encoding="utf-8"))
        if isinstance(raw_layout, dict) and raw_layout.get("version") == 1:
            legacy_layout = _verified_legacy_layout(install)
            if (install / "bootstrap" / "install-transaction.json").exists():
                raise RuntimeError("legacy installation transaction must be completed before migration")
        else:
            layout = _validate_layout(raw_layout)
            for relative, expected in layout["assets"].items():  # type: ignore[union-attr]
                target = install / _safe_relative(relative)
                _plain_ancestors(target, install)
                if not target.is_file() or _sha256(target) != expected:
                    raise RuntimeError(f"stable bootstrap asset was modified: {relative}")
            _reconcile_committed_pending_layout(install)
            return
    if legacy_layout is not None:
        backups = {
            relative: _sha256(
                install / "bootstrap" / "legacy-assets" / _safe_relative(relative)
            )
            for relative in legacy_layout["legacy_backups"]  # type: ignore[union-attr]
        }
    else:
        backups = {
            target: _sha256(install / target)
            for _, target in ASSETS
            if (install / target).is_file()
        }
    if pending_path.exists():
        pending = _validate_layout(
            json.loads(pending_path.read_text(encoding="utf-8")), pending=True
        )
        if pending["assets"] != desired or pending["legacy_backups"] != backups:
            raise RuntimeError("bootstrap publication candidate changed")
    else:
        pending = {
            "version": 2,
            "assets": desired,
            "legacy_backups": backups,
            "backups_ready": [],
            "published": [],
        }
        _atomic_json(pending_path, pending)
    backup_root = install / "bootstrap" / "legacy-assets"
    for relative, expected in pending["legacy_backups"].items():  # type: ignore[union-attr]
        backup = backup_root / _safe_relative(relative)
        target = install / relative
        _plain_ancestors(target, install)
        _plain_ancestors(backup.parent, install)
        if relative not in pending["backups_ready"]:  # type: ignore[operator]
            backup.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists() and _sha256(backup) != expected:
                raise RuntimeError(f"legacy backup was modified: {relative}")
            if not backup.exists():
                shutil.copy2(target, backup)
            pending["backups_ready"].append(relative)  # type: ignore[union-attr]
            _atomic_json(pending_path, pending)
            if os.environ.get("DEVLOOP_TEST_INTERRUPT_BOOTSTRAP_AFTER_BACKUP") == "1":
                os._exit(93)
    for source_relative, target_relative in ASSETS:
        source = template / source_relative
        target = install / target_relative
        if target_relative in pending["published"]:  # type: ignore[operator]
            if not target.is_file() or _sha256(target) != desired[target_relative]:
                raise RuntimeError(f"published bootstrap asset was modified: {target_relative}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or _sha256(target) != desired[target_relative]:
            temporary = target.with_name(f"{target.name}.publish-{uuid.uuid4().hex}")
            shutil.copy2(source, temporary)
            if target.suffix in {".sh", ""}:
                temporary.chmod(temporary.stat().st_mode | 0o111)
            os.replace(temporary, target)
        if (
            os.environ.get("DEVLOOP_TESTING") == "1"
            and os.environ.get("DEVLOOP_TEST_INTERRUPT_BOOTSTRAP_AFTER_ASSET") == target_relative
        ):
            os._exit(94)
        pending["published"].append(target_relative)  # type: ignore[union-attr]
        _atomic_json(pending_path, pending)
    layout = {key: pending[key] for key in LAYOUT_FIELDS}
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
    if release.exists():
        raise RuntimeError("canonical release already exists without a transaction")
    previous = previous_previous = None
    current_path = install / "bootstrap" / "current.json"
    if current_path.exists():
        state = read_pointer_state(install)
        verify_pointer(install, state.current, "current")
        if state.previous is not None:
            verify_pointer(install, state.previous, "previous")
        previous, previous_previous = state.current, state.previous
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
            shutil.rmtree(candidate, onerror=_remove_read_only)
        _validate_journal_release(install, journal)
        _write_journal(install, journal, "release_ready")
        phase = "release_ready"
    if phase in {"release_ready", "adopted"}:
        _validate_journal_release(install, journal)
        return "NEEDS_ADOPTION" if phase == "release_ready" else "READY_TO_SWITCH", release
    if phase in {"switched", "committed"}:
        state = read_pointer_state(install)
        if state.current["commit"] != journal["commit"]:
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
        if current_path.exists():
            raise RuntimeError("authoritative pointer changed since transaction prepare")
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
    expected_verifier_hash = layout["assets"]["bootstrap/verify.py"]  # type: ignore[index]
    if _sha256(verifier) != expected_verifier_hash:
        raise RuntimeError("mandatory release verifier was modified; nothing was removed")
    state = read_pointer_state(install)
    verify_pointer(install, state.current, "current")
    if state.previous is not None:
        verify_pointer(install, state.previous, "previous")
    releases: list[Path] = []
    manifests: list[Path] = []
    release_names: set[str] = set()
    for release in releases_root.iterdir():
        _plain_ancestors(release, install)
        if not release.is_dir() or COMMIT_PATTERN.fullmatch(release.name) is None:
            raise RuntimeError(f"unmanaged release path blocks uninstall: {release}")
        verify_release(install, release.name)
        release_names.add(release.name)
        releases.append(release)
        manifests.append(bootstrap / f"release-{release.name}.json")
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
    for relative, expected in layout["assets"].items():  # type: ignore[union-attr]
        target = install / _safe_relative(relative)
        _plain_ancestors(target.parent, install)
        if target.exists():
            _plain_ancestors(target, install)
            if not target.is_file():
                raise RuntimeError(f"bootstrap asset is not a plain file: {relative}")
            if _sha256(target) == expected:
                remove_assets.append(target)
    for relative, expected in layout["legacy_backups"].items():  # type: ignore[union-attr]
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
    staging = _uninstall_staging(install, transaction_id)
    staging.mkdir()
    shutil.copy2(install / "bootstrap" / "transaction.py", staging / "transaction.py")
    shutil.copy2(install / "bootstrap" / "verify.py", staging / "verify.py")
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
    _write_uninstall_journal(path, journal)
    return path, journal


def _read_uninstall_journal(
    install: Path, transaction_id: str
) -> tuple[Path, dict[str, object]]:
    path = _uninstall_staging(install, transaction_id) / "journal.json"
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
    if _uninstall_plan_hash(value["actions"]) != value["plan_hash"]:
        raise RuntimeError("uninstall journal action plan changed after preflight")
    layout_path = install / "bootstrap" / "layout.json"
    if layout_path.exists() and _sha256(layout_path) != value["layout_hash"]:
        raise RuntimeError("uninstall layout changed after preflight")
    staging = path.parent
    _plain_ancestors(staging, install.parent)
    for action in value["actions"]:
        if not isinstance(action, dict):
            raise RuntimeError("uninstall journal action is invalid")
        kind = action.get("kind")
        if kind in {
            "remove_release",
            "remove_manifest",
            "remove_pointer",
            "remove_asset",
            "remove_metadata",
        }:
            target = Path(str(action.get("path"))).absolute()
            if target == install or not target.is_relative_to(install):
                raise RuntimeError("uninstall journal action escapes the install root")
        elif kind == "restore_legacy":
            source = Path(str(action.get("source"))).absolute()
            target = Path(str(action.get("target"))).absolute()
            if not source.is_relative_to(
                install / "bootstrap" / "legacy-assets"
            ) or not target.is_relative_to(install):
                raise RuntimeError("uninstall legacy restore action is not owned")
        elif kind == "stage_capability":
            source = Path(str(action.get("source"))).absolute()
            staged = Path(str(action.get("staged"))).absolute()
            if not source.is_relative_to(
                install / "releases"
            ) or not staged.is_relative_to(staging):
                raise RuntimeError("uninstall capability stage action is not owned")
        elif kind == "cleanup_capability":
            staged = Path(str(action.get("staged"))).absolute()
            if not staged.is_relative_to(staging):
                raise RuntimeError("uninstall capability action is not owned")
        else:
            raise RuntimeError("uninstall journal action is unsupported")
    return path, value


def _execute_uninstall_action(action: dict[str, object]) -> None:
    kind = action["kind"]
    if kind == "remove_release":
        path = Path(str(action["path"]))
        if path.exists():
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
        if not staged.exists():
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, staged)
    elif kind == "cleanup_capability":
        source = Path(str(action["staged"]))
        destination = Path(str(action["destination"]))
        for item in source.rglob("*"):
            if not item.is_file():
                continue
            target = destination / item.relative_to(source)
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
    for raw_action in actions:
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
        raw_action["state"] = "before"
        _write_uninstall_journal(journal_path, journal)
        _uninstall_interrupt(action_id, "before")
        try:
            _execute_uninstall_action(raw_action)
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
