"""Disposable, explicitly opted-in installation fixtures; never a release gate."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_FIXTURE = ROOT / "tests" / "fixtures" / "portable_bootstrap_604"
HISTORICAL_COMMIT = "6048556f0f279cb54f4d1afa00f764227049eb8f"
HISTORICAL_PATHS = frozenset(
    {
        "install/bootstrap/bin/devloop-plan.ps1", "install/bootstrap/bin/devloop-plan.sh",
        "install/bootstrap/bin/devloop.ps1", "install/bootstrap/bin/devloop.sh",
        "install/bootstrap/dispatch.ps1", "install/bootstrap/dispatch.sh",
        "install/bootstrap/install/devloop.ps1", "install/bootstrap/install/devloop.sh",
        "install/bootstrap/install/uninstall-devloop.ps1",
        "install/bootstrap/install/uninstall-devloop.sh",
        "install/bootstrap/transaction.py", "install/bootstrap/verify.py",
        "install/devloop.ps1", "install/devloop.sh", "install/uninstall-devloop.ps1",
        "install/uninstall-devloop.sh", "portable-release.json", "src/devloop/portable_release.py",
    }
)
SOURCE_PATHS = (
    "src", "bin", "install", ".gitignore", "portable-release.json", "requirements-portable.lock",
)
SESSION_MARKER = "portable-test-session.json"
_session_root: Path | None = None
_fixture_roots: tuple[Path, ...] = ()
_operator_enabled = False


def validate_workspace_path(path: Path, *, allow_existing: bool = False) -> Path:
    """Reject aliases, reparse points and anything at/outside the actual checkout."""
    absolute = Path(os.path.abspath(path))
    if absolute == ROOT or not absolute.is_relative_to(ROOT):
        raise ValueError(f"Test path must be below the workspace: {path}")
    current = ROOT
    for part in absolute.relative_to(ROOT).parts:
        current /= part
        if current.exists() or current.is_symlink():
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"Test path contains an alias/reparse point: {current}")
    if absolute.resolve() != absolute:
        raise ValueError(f"Test path does not resolve literally: {path}")
    if not allow_existing and absolute.exists():
        raise ValueError(f"Test path must be a fresh, nonexistent leaf: {absolute}")
    return absolute


def configure_session(root: Path, *, operator_enabled: bool, basetemp: Path | None = None) -> None:
    global _session_root, _fixture_roots, _operator_enabled
    root = validate_workspace_path(root, allow_existing=True)
    if not root.is_dir() or not (root / SESSION_MARKER).is_file():
        raise ValueError("Test session must have its fresh workspace ownership marker")
    _session_root, _operator_enabled = root, operator_enabled
    _fixture_roots = (root,) if basetemp is None else (root, validate_workspace_path(basetemp))


def require_operator_install() -> None:
    if not _operator_enabled:
        raise RuntimeError(
            "Installation tests are operator-only. Run pytest with --run-operator-install "
            "and -m operator_install in a separate operator terminal."
        )


def session_root() -> Path:
    if _session_root is None:
        raise RuntimeError("A configured disposable pytest session is required")
    return validate_workspace_path(_session_root, allow_existing=True)


def require_fixture_path(path: Path, *, allow_existing: bool = True) -> Path:
    absolute = validate_workspace_path(path, allow_existing=allow_existing)
    session_root()
    if not any(absolute != root and absolute.is_relative_to(root) for root in _fixture_roots):
        raise ValueError(f"Fixture path is outside this disposable test session: {path}")
    return absolute


@contextmanager
def workspace_directory() -> Iterator[str]:
    """Retain fresh fixture leaves for evidence; never recursively delete a reused path."""
    require_operator_install()
    yield str(fresh_fixture_directory())


def fresh_fixture_directory() -> Path:
    """Create one validated session leaf and retain it; usable by source safety tests."""
    directory = require_fixture_path(
        session_root() / ("fixture-" + uuid.uuid4().hex), allow_existing=False,
    )
    directory.mkdir()
    return require_fixture_path(directory)


def isolated_environment(root: Path, source: Mapping[str, str] | None = None) -> dict[str, str]:
    root = require_fixture_path(root)
    environment = {
        key: value for key, value in (os.environ if source is None else source).items()
        if not key.upper().startswith(("GIT_", "DEVLOOP_", "PYTHON", "CODEX_"))
        and key.upper() not in {"BASH_ENV", "ENV", "CDPATH"}
    }
    paths = {
        "HOME": root / "home", "USERPROFILE": root / "home",
        "APPDATA": root / "configuration", "LOCALAPPDATA": root / "state",
        "XDG_CONFIG_HOME": root / "configuration", "XDG_DATA_HOME": root / "state",
        "XDG_STATE_HOME": root / "state",
        "XDG_CACHE_HOME": root / "cache", "CODEX_HOME": root / "home" / ".codex",
        "TMP": root / "temporary", "TEMP": root / "temporary", "TMPDIR": root / "temporary",
    }
    for name, directory in paths.items():
        require_fixture_path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        environment[name] = str(directory)
    hooks = require_fixture_path(root / "empty-git-hooks")
    templates = require_fixture_path(root / "empty-git-template")
    hooks.mkdir(exist_ok=True)
    templates.mkdir(exist_ok=True)
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_TEMPLATE_DIR": str(templates),
        "GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(hooks), "GIT_CONFIG_KEY_1": "commit.gpgSign",
        "GIT_CONFIG_VALUE_1": "false", "GIT_CONFIG_KEY_2": "tag.gpgSign",
        "GIT_CONFIG_VALUE_2": "false", "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never", "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "DEVLOOP_TESTING": "1",
    })
    return environment


class OperatorInstallTestCase(unittest.TestCase):
    pytestmark = pytest.mark.operator_install

    @classmethod
    def setUpClass(cls) -> None:
        require_operator_install()

    def setUp(self) -> None:
        context = workspace_directory()
        root = Path(context.__enter__())
        self.addCleanup(context.__exit__, None, None, None)
        patch = mock.patch.dict(os.environ, isolated_environment(root), clear=True)
        patch.start()
        self.addCleanup(patch.stop)


def git(root: Path, *arguments: str) -> str:
    """Only mutate a private fixture repository with empty config/templates/hooks."""
    require_operator_install()
    require_fixture_path(root)
    has_repository = (root / ".git").exists()
    if has_repository:
        require_fixture_path(root / ".git")
        if not (root / ".git").is_dir():
            raise ValueError("Fixture Git metadata must be a private directory")
        for name in ("config", "index", "objects", "refs"):
            require_fixture_path(root / ".git" / name)
    environment = isolated_environment(session_root() / "git-environment")
    if has_repository:
        scope = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--show-toplevel",
             "--absolute-git-dir", "--git-common-dir", "--git-path", "index"],
            env=environment, capture_output=True, text=True, check=False,
        )
        expected = [root, root / ".git", root / ".git", root / ".git" / "index"]
        if scope.returncode or [Path(line) for line in scope.stdout.splitlines()] != expected:
            raise ValueError("Git scope escapes the private fixture checkout")
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-C", str(root),
         *arguments], env=environment, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def copy_current_source(destination: Path, paths: tuple[str, ...] = SOURCE_PATHS) -> dict[str, str]:
    """Copy enumerated current bytes, including uncommitted source, excluding ignored artifacts."""
    require_operator_install()
    require_fixture_path(destination, allow_existing=False)
    sources = read_current_source(paths)
    inventory = {name: hashlib.sha256(raw).hexdigest() for name, raw in sources.items()}
    destination.mkdir(parents=True)
    for name, raw in sources.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        shutil.copymode(ROOT / name, target)
    if any((ROOT / name).read_bytes() != raw for name, raw in sources.items()):
        raise ValueError("Current source changed while creating the fixture; discard this evidence")
    return inventory


def read_current_source(paths: tuple[str, ...] = SOURCE_PATHS) -> dict[str, bytes]:
    """Read the same current inventory for fixture copying and reviewed gate identity."""
    environment = isolated_environment(session_root() / "source-environment")
    result = subprocess.run(
        ["git", "-c", "core.excludesFile=" + os.devnull, "-C", str(ROOT), "ls-files", "-z",
         "--cached", "--others", "--exclude-standard", "--", *paths],
        env=environment, capture_output=True, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    names = sorted(set(name.decode("utf-8") for name in result.stdout.split(b"\0") if name))
    if not names:
        raise ValueError("Current-source inventory is empty")
    sources: dict[str, bytes] = {}
    for name in names:
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name:
            raise ValueError(f"Invalid current-source path: {name}")
        source = validate_workspace_path(ROOT / name, allow_existing=True)
        if not source.exists():  # A tracked deletion is absent from the current source snapshot.
            continue
        if not source.is_file():
            raise ValueError(f"Current-source inventory contains a non-file: {name}")
        if (any(part in {"__pycache__", ".venv", ".git"} for part in relative.parts)
                or relative.suffix in {".pyc", ".pyo"}):
            raise ValueError(f"Generated/runtime artifact in source inventory: {name}")
        raw = source.read_bytes()
        sources[name] = raw
    return sources


def historical_overlay(fixture: Path = HISTORICAL_FIXTURE) -> dict[str, bytes]:
    """Validate inert JSON completely; no import, materialization or historical execution."""
    manifest = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))
    sources = json.loads((fixture / "sources.json").read_text(encoding="utf-8"))
    expected = {
        "fixture_format": 1, "scope": "historical-bootstrap-overlay",
        "historical_commit": HISTORICAL_COMMIT, "historical_commit_available": False,
        "full_historical_tree": False, "source_encoding": "utf-8", "source_newlines": "LF",
    }
    if any(type(manifest.get(key)) is not type(value) or manifest.get(key) != value
           for key, value in expected.items()):
        raise ValueError("Historical fixture identity does not match the frozen overlay")
    if set(sources) != HISTORICAL_PATHS or set(manifest["files"]) != HISTORICAL_PATHS:
        raise ValueError("Historical overlay must contain the exact 18-file inventory")
    decoded: dict[str, bytes] = {}
    for name, text in sources.items():
        if not isinstance(text, str) or "\r" in text or text.startswith("\ufeff"):
            raise ValueError(f"Historical source must be UTF-8/LF without BOM: {name}")
        raw = text.encode("utf-8")
        record = manifest["files"][name]
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        if (len(raw) != record["bytes"] or hashlib.sha256(raw).hexdigest() != record["sha256"]
                or blob != record["git_blob_sha1"]):
            raise ValueError(f"Historical source failed byte/hash validation: {name}")
        decoded[name] = raw
    return decoded


def create_source_repository(root: Path, *, historical: bool = False) -> Path:
    require_operator_install()
    require_fixture_path(root)
    overlay = historical_overlay() if historical else {}
    remote = root / ("historical-bootstrap-current-support" if historical else "remote")
    inventory = copy_current_source(remote)
    for name, raw in overlay.items():
        target = remote / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    shim = remote / "src" / "textual.py"
    if shim.exists():
        raise ValueError("Runtime shim would overwrite current source")
    shim.write_bytes(b'__version__ = "8.2.8"\n')
    provenance = {
        "scope": "historical-bootstrap-overlay-with-current-checkout-support" if historical
        else "current-checkout-source-fixture",
        "source_checkout": str(ROOT), "current_source_sha256": inventory,
        "historical_commit_available": False if historical else None,
        "full_historical_tree": False,
        "historical_overlay_sha256": {
            name: hashlib.sha256(raw).hexdigest() for name, raw in overlay.items()
        },
        "runtime": "DEVLOOP_TESTING=1; Textual version stub; no real dependency installation",
        "runtime_shim_sha256": hashlib.sha256(shim.read_bytes()).hexdigest(),
    }
    (root / f"{remote.name}-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8",
    )
    git(remote, "init", "--initial-branch=main")
    git(remote, "config", "user.email", "devloop@example.invalid")
    git(remote, "config", "user.name", "Dev Loop Tests")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "historical overlay with current support" if historical
        else "current checkout source fixture")
    provenance["fixture_commit"] = git(remote, "rev-parse", "HEAD")
    (root / f"{remote.name}-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8",
    )
    return remote


def fixture_environment(root: Path) -> dict[str, str]:
    environment = isolated_environment(root)
    tools = root / "tools"
    tools.mkdir(exist_ok=True)
    if os.name == "nt":
        (tools / "python.cmd").write_text(
            f'@echo off\r\n"{sys.executable}" %*\r\n', encoding="utf-8",
        )
    environment["PATH"] = os.pathsep.join(
        (str(tools), str(Path(sys.executable).parent), environment["PATH"])
    )
    return environment
