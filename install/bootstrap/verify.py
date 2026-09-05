from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BOOTSTRAP_PROTOCOL = 2
POINTER_SCHEMA = 2
MANIFEST_SCHEMA = 2
TRANSACTION_SCHEMA = 2
LEGACY_POINTER_FIELDS = {
    "version",
    "commit",
    "release_path",
    "tracked_fingerprint",
    "runtime_fingerprint",
}
POINTER_FIELDS = LEGACY_POINTER_FIELDS - {"version"}
POINTER_STATE_FIELDS = {"version", "current", "previous"}
MANIFEST_FIELDS = LEGACY_POINTER_FIELDS | {
    "bootstrap_protocol_min",
    "bootstrap_protocol_max",
    "pointer_schema_min",
    "pointer_schema_max",
    "manifest_schema_min",
    "manifest_schema_max",
    "transaction_schema_min",
    "transaction_schema_max",
}
HASH_PATTERN = re.compile(r"[0-9A-F]{64}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class PointerState:
    current: dict[str, object]
    previous: dict[str, object] | None
    legacy: bool = False


def _plain(path: Path) -> None:
    if path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(path)):
        raise RuntimeError(f"reparse or symbolic link rejected: {path}")


def _plain_ancestors(path: Path, stop: Path) -> None:
    current = path
    while True:
        if current.exists():
            _plain(current)
        if current == stop:
            return
        if current.parent == current:
            raise RuntimeError(f"path is not contained by expected root: {path}")
        current = current.parent


def _json(path: Path) -> dict[str, object]:
    _plain(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"object metadata required: {path}")
    return value


def _directory_fingerprint(root: Path) -> str:
    _plain(root)
    entries: list[str] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        _plain_ancestors(path, root)
        relative = path.relative_to(root).as_posix()
        entries.append(f"{relative}={hashlib.sha256(path.read_bytes()).hexdigest().upper()}")
    return hashlib.sha256("\n".join(entries).encode()).hexdigest().upper()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"git {' '.join(args)} validation failed: {detail}")
    return result.stdout


def _tracked_fingerprint(root: Path) -> str:
    entries = sorted(_git(root, "ls-files", "--stage").splitlines())
    return hashlib.sha256("\n".join(entries).encode()).hexdigest().upper()


def _validate_release_content(root: Path) -> None:
    # No ignore exclusions: ignored data and empty untracked directories are not
    # installer-owned payload. Git emits raw, NUL-delimited paths with -z.
    paths = _git(root, "ls-files", "--others", "--directory", "-z").split("\0")
    if any(path and not path.startswith(".venv/") for path in paths):
        raise RuntimeError("release contains unexpected untracked content")


def _validate_pointer(pointer: object, name: str) -> dict[str, object]:
    if not isinstance(pointer, dict) or set(pointer) != POINTER_FIELDS:
        raise RuntimeError(f"{name} release pointer has an unsupported schema")
    commit = pointer["commit"]
    if not isinstance(commit, str) or COMMIT_PATTERN.fullmatch(commit) is None:
        raise RuntimeError(f"{name} release pointer has an invalid commit")
    if pointer["release_path"] != f"releases/{commit}":
        raise RuntimeError(f"{name} release pointer has a non-canonical release path")
    for field in ("tracked_fingerprint", "runtime_fingerprint"):
        value = pointer[field]
        if not isinstance(value, str) or HASH_PATTERN.fullmatch(value) is None:
            raise RuntimeError(f"{name} release pointer has an invalid {field}")
    return pointer


def read_pointer_state(install_root: Path) -> PointerState:
    """Read the one authoritative pointer state, accepting v1 only for migration."""
    bootstrap = install_root / "bootstrap"
    value = _json(bootstrap / "current.json")
    if set(value) == LEGACY_POINTER_FIELDS and value.get("version") == 1:
        current = _validate_pointer({field: value[field] for field in POINTER_FIELDS}, "current")
        previous_path = bootstrap / "previous.json"
        previous = None
        if previous_path.exists():
            legacy_previous = _json(previous_path)
            if set(legacy_previous) != LEGACY_POINTER_FIELDS or legacy_previous.get("version") != 1:
                raise RuntimeError("previous release pointer has an unsupported schema")
            previous = _validate_pointer(
                {field: legacy_previous[field] for field in POINTER_FIELDS}, "previous"
            )
        return PointerState(current=current, previous=previous, legacy=True)
    if set(value) != POINTER_STATE_FIELDS or value.get("version") != POINTER_SCHEMA:
        raise RuntimeError("current pointer has an unsupported schema")
    current = _validate_pointer(value["current"], "current")
    previous_value = value["previous"]
    previous = None if previous_value is None else _validate_pointer(previous_value, "previous")
    return PointerState(current=current, previous=previous)


def _validate_manifest_compatibility(manifest: dict[str, object]) -> None:
    if set(manifest) == LEGACY_POINTER_FIELDS and manifest.get("version") == 1:
        return
    if set(manifest) != MANIFEST_FIELDS or manifest.get("version") != MANIFEST_SCHEMA:
        raise RuntimeError("release manifest has an unsupported schema")
    ranges = (
        ("bootstrap_protocol", BOOTSTRAP_PROTOCOL),
        ("pointer_schema", POINTER_SCHEMA),
        ("manifest_schema", MANIFEST_SCHEMA),
        ("transaction_schema", TRANSACTION_SCHEMA),
    )
    for name, supported in ranges:
        minimum = manifest[f"{name}_min"]
        maximum = manifest[f"{name}_max"]
        if not isinstance(minimum, int) or not isinstance(maximum, int) or minimum > maximum:
            raise RuntimeError(f"release manifest has an invalid {name} compatibility range")
        if not minimum <= supported <= maximum:
            raise RuntimeError(f"release is incompatible with bootstrap {name} {supported}")


def verify_release(install_root: Path, commit: str) -> Path:
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise RuntimeError("release manifest has an invalid commit")
    bootstrap = install_root / "bootstrap"
    manifest = _json(bootstrap / f"release-{commit}.json")
    _validate_manifest_compatibility(manifest)
    if manifest["commit"] != commit:
        raise RuntimeError("release manifest has an invalid commit")
    if manifest["release_path"] != f"releases/{commit}":
        raise RuntimeError("release manifest has a non-canonical release path")
    for field in ("tracked_fingerprint", "runtime_fingerprint"):
        value = manifest[field]
        if not isinstance(value, str) or HASH_PATTERN.fullmatch(value) is None:
            raise RuntimeError(f"release manifest has an invalid {field}")
    release = install_root / "releases" / commit
    _plain_ancestors(release, install_root)
    if release.resolve(strict=True) != release:
        raise RuntimeError("current pointer does not resolve to its canonical release")
    if _git(release, "rev-parse", "HEAD").strip() != commit:
        raise RuntimeError("release commit does not match current pointer")
    try:
        _git(release, "diff", "--quiet", "--")
    except RuntimeError as error:
        raise RuntimeError("release has tracked changes") from error
    try:
        _git(release, "diff", "--cached", "--quiet", "--")
    except RuntimeError as error:
        raise RuntimeError("release has staged changes") from error
    _validate_release_content(release)
    if _tracked_fingerprint(release) != manifest["tracked_fingerprint"]:
        raise RuntimeError("tracked release fingerprint mismatch")
    if _directory_fingerprint(release / ".venv") != manifest["runtime_fingerprint"]:
        raise RuntimeError("runtime fingerprint mismatch")
    return release


def verify_pointer(install_root: Path, pointer: dict[str, object], name: str) -> Path:
    pointer = _validate_pointer(pointer, name)
    release = verify_release(install_root, str(pointer["commit"]))
    manifest = _json(install_root / "bootstrap" / f"release-{pointer['commit']}.json")
    for field in POINTER_FIELDS:
        if manifest[field] != pointer[field]:
            raise RuntimeError(f"release manifest does not match {name} pointer")
    return release


def verify(install_root: Path) -> Path:
    install_root = install_root.absolute()
    _plain_ancestors(install_root, Path(install_root.anchor))
    install_root = install_root.resolve(strict=True)
    _plain_ancestors(install_root, install_root)
    _plain_ancestors(install_root / "bootstrap", install_root)
    state = read_pointer_state(install_root)
    return verify_pointer(install_root, state.current, "current")


def select_update_driver(install_root: Path) -> Path:
    install_root = install_root.resolve(strict=True)
    state = read_pointer_state(install_root)
    current = verify_pointer(install_root, state.current, "current")
    current_manifest = _json(
        install_root / "bootstrap" / f"release-{state.current['commit']}.json"
    )
    if not (set(current_manifest) == LEGACY_POINTER_FIELDS and current_manifest.get("version") == 1):
        return current
    if state.previous is None:
        raise RuntimeError("no release driver supports the stable bootstrap protocol")
    previous = verify_pointer(install_root, state.previous, "previous")
    previous_manifest = _json(
        install_root / "bootstrap" / f"release-{state.previous['commit']}.json"
    )
    if set(previous_manifest) == LEGACY_POINTER_FIELDS and previous_manifest.get("version") == 1:
        raise RuntimeError("no release driver supports the stable bootstrap protocol")
    return previous


if __name__ == "__main__":
    try:
        root = Path(sys.argv[1]).resolve(strict=True)
        if len(sys.argv) > 2 and sys.argv[2] == "--update-driver":
            release = select_update_driver(root)
        else:
            release = verify_release(root, sys.argv[2]) if len(sys.argv) > 2 else verify(root)
        print(release)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"devloop-bootstrap: error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
