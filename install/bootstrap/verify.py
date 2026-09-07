from __future__ import annotations

import hashlib
import json
import os
import re
import stat
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
GIT_OWNERSHIP_FIELDS = {"version", "commit", "git_fingerprint"}
GIT_OWNERSHIP_SCHEMA = 1
GIT_ENTRY_FILE = "file"
GIT_ENTRY_DIRECTORY = "directory"
# Repository-local selectors reported by Git's rev-parse --local-env-vars.
# Config injection variables can introduce selectors without naming them directly.
GIT_LOCAL_ENVIRONMENT = frozenset({
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_OBJECT_DIRECTORY", "GIT_DIR",
    "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX", "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR", "GIT_ATTR_SOURCE",
})


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


def _git_environment() -> dict[str, str]:
    return {
        key: value for key, value in os.environ.items()
        if key.upper() not in GIT_LOCAL_ENVIRONMENT
        and not key.upper().startswith("GIT_CONFIG")
    }


def _run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "--no-optional-locks", "-c", "diff.autoRefreshIndex=false", *args],
        capture_output=True, text=True, check=False, env=_git_environment(),
    )
    if result.returncode:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"git {' '.join(args)} validation failed: {detail}")
    return result.stdout


def _validate_git_scope(root: Path) -> None:
    root = root.absolute()
    _plain_ancestors(root / ".git", Path(root.anchor))
    if not (root / ".git").is_dir():
        raise RuntimeError("Git scope requires a private .git directory")
    paths = _run_git(
        root, "rev-parse", "--path-format=absolute", "--show-toplevel",
        "--absolute-git-dir", "--git-common-dir", "--git-path", "index",
    ).splitlines()
    expected = (root, root / ".git", root / ".git", root / ".git" / "index")
    if len(paths) != len(expected):
        raise RuntimeError("Git scope did not report all repository paths")
    for value, owned in zip(paths, expected, strict=True):
        actual = Path(value)
        if not actual.is_absolute() or actual.resolve() != owned:
            raise RuntimeError("Git scope escapes the private checkout")
        _plain_ancestors(owned, root)


def _git(root: Path, *args: str) -> str:
    _validate_git_scope(root)
    return _run_git(root, *args)


def validate_bootstrap_source(root: Path) -> None:
    """Check the staged checkout and every installer-owned bootstrap input."""
    _git(root, "diff", "--quiet", "--")
    _git(root, "diff", "--cached", "--quiet", "--")
    _validate_release_content(root)
    # Import only after checking the downloaded tracked tree; -B is required by callers.
    from transaction import ASSETS

    for relative in ("transaction.py", "verify.py", *(source for source, _ in ASSETS)):
        path = root / "install" / "bootstrap" / relative
        _plain_ancestors(path, root)
        if not path.is_file():
            raise RuntimeError(f"bootstrap source asset is missing: {relative}")
        _git(root, "ls-files", "--error-unmatch", "--", path.relative_to(root).as_posix())


def _tracked_fingerprint(root: Path) -> str:
    entries = sorted(_git(root, "ls-files", "--stage").splitlines())
    return hashlib.sha256("\n".join(entries).encode()).hexdigest().upper()


def _validate_release_content(root: Path) -> None:
    # No ignore exclusions: ignored data and empty untracked directories are not
    # installer-owned payload. Git emits raw, NUL-delimited paths with -z.
    paths = _git(root, "ls-files", "--others", "--directory", "-z").split("\0")
    if any(path and not path.startswith(".venv/") for path in paths):
        raise RuntimeError("release contains unexpected untracked content")


def _git_ownership_path(install_root: Path, commit: str) -> Path:
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise RuntimeError("Git ownership has an invalid commit")
    return install_root / "bootstrap" / f"git-ownership-{commit}.json"


def _git_directory_entries(root: Path) -> list[tuple[str, str, str]]:
    git_root = root / ".git"
    _plain_ancestors(git_root, root)
    root_status = git_root.lstat()
    if not stat.S_ISDIR(root_status.st_mode) or (
        getattr(root_status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise RuntimeError("Git ownership requires a plain .git directory")
    entries: list[tuple[str, str, str]] = []
    pending = [git_root]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            status = path.lstat()
            if stat.S_ISLNK(status.st_mode) or (
                getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise RuntimeError("Git ownership rejects reparse or symbolic links")
            relative = path.relative_to(git_root).as_posix()
            if stat.S_ISDIR(status.st_mode):
                entries.append((relative, GIT_ENTRY_DIRECTORY, ""))
                pending.append(path)
            elif stat.S_ISREG(status.st_mode):
                entries.append(
                    (relative, GIT_ENTRY_FILE, hashlib.sha256(path.read_bytes()).hexdigest())
                )
            else:
                raise RuntimeError("Git ownership rejects special filesystem entries")
    return sorted(entries)


def _git_entries_fingerprint(entries: list[tuple[str, str, str]]) -> str:
    encoded = json.dumps(sorted(entries), ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest().upper()


def _git_directory_fingerprint(root: Path) -> str:
    return _git_entries_fingerprint(_git_directory_entries(root))


def _read_git_ownership(
    install_root: Path, commit: str, *, required: bool = False,
) -> dict[str, object] | None:
    path = _git_ownership_path(install_root, commit)
    _plain_ancestors(path, install_root)
    if not path.exists():
        if required:
            raise RuntimeError(
                "Git ownership evidence is missing; legacy release must be preserved"
            )
        return None
    value = _json(path)
    if (
        set(value) != GIT_OWNERSHIP_FIELDS
        or type(value.get("version")) is not int
        or value.get("version") != GIT_OWNERSHIP_SCHEMA
        or value.get("commit") != commit
        or not isinstance(value.get("git_fingerprint"), str)
        or HASH_PATTERN.fullmatch(str(value["git_fingerprint"])) is None
    ):
        raise RuntimeError("Git ownership evidence has an unsupported schema or identity")
    return value


def _verify_git_ownership(
    install_root: Path, release: Path, *, required: bool = False, commit: str | None = None,
) -> None:
    commit = release.name if commit is None else commit
    value = _read_git_ownership(install_root, commit, required=required)
    if value is None:
        return
    if _git_directory_fingerprint(release) != value["git_fingerprint"]:
        raise RuntimeError("Git ownership fingerprint mismatch; release must be preserved")


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
    _verify_git_ownership(install_root, release)
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
    if not (
        set(current_manifest) == LEGACY_POINTER_FIELDS and current_manifest.get("version") == 1
    ):
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
        if len(sys.argv) > 2 and sys.argv[2] == "--bootstrap-source":
            validate_bootstrap_source(root)
            raise SystemExit(0)
        if len(sys.argv) > 2 and sys.argv[2] == "--update-driver":
            release = select_update_driver(root)
        else:
            release = verify_release(root, sys.argv[2]) if len(sys.argv) > 2 else verify(root)
        print(release)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"devloop-bootstrap: error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
