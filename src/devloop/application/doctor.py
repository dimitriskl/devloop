from __future__ import annotations

import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from devloop.application.config import ApplicationConfig
from devloop.domain.doctor import (
    DoctorCheckId,
    DoctorCheckResult,
    DoctorReport,
)
from devloop.execution.app_server import AppServerClient, AppServerStatus
from devloop.infrastructure.codex import (
    CodexExecutableError,
    resolve_codex_executable,
    run_codex,
)

MINIMUM_PYTHON = (3, 10)
VERSION_COMMAND_TIMEOUT_SECONDS = 5.0
STORAGE_PROBE_TIMEOUT_SECONDS = 1.0
_VERSION = re.compile(r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+)+(?:[._+-][A-Za-z0-9.-]+)?)")


@dataclass(frozen=True)
class CommandObservation:
    succeeded: bool
    output: str = ""


def check_python(version: tuple[int, int, int]) -> DoctorCheckResult:
    rendered = ".".join(str(part) for part in version)
    if version[:2] >= MINIMUM_PYTHON:
        return DoctorCheckResult.passed(
            DoctorCheckId.PYTHON, "Python", f"Python {rendered} is supported."
        )
    return DoctorCheckResult.failed(
        DoctorCheckId.PYTHON,
        "Python",
        f"Python {rendered} is older than the supported runtime.",
        "Install Python 3.10 or newer and run the doctor again.",
    )


def check_git(
    executable: str | None,
    version_command: CommandObservation | None,
) -> DoctorCheckResult:
    if executable is None:
        return DoctorCheckResult.failed(
            DoctorCheckId.GIT,
            "Git",
            "Git is not available on PATH.",
            "Install Git, add it to PATH, and run the doctor again.",
        )
    version = extract_version(version_command.output) if version_command else None
    if version_command is None or not version_command.succeeded or version is None:
        return DoctorCheckResult.failed(
            DoctorCheckId.GIT,
            "Git",
            "The Git executable did not return a usable version.",
            "Repair or reinstall Git, then verify that 'git --version' succeeds.",
        )
    return DoctorCheckResult.passed(DoctorCheckId.GIT, "Git", f"Git {version} is available.")


def check_repository(
    *,
    exists: bool,
    is_directory: bool,
    git_available: bool,
    git_work_tree: bool,
) -> DoctorCheckResult:
    if not exists:
        return DoctorCheckResult.failed(
            DoctorCheckId.REPOSITORY,
            "Repository",
            "The requested repository does not exist.",
            "Pass --repo with the path to an existing Git repository.",
        )
    if not is_directory:
        return DoctorCheckResult.failed(
            DoctorCheckId.REPOSITORY,
            "Repository",
            "The requested repository is not a directory.",
            "Pass --repo with the path to a Git repository directory.",
        )
    if not git_available:
        return DoctorCheckResult.failed(
            DoctorCheckId.REPOSITORY,
            "Repository",
            "Repository status cannot be verified without Git.",
            "Install Git and run the doctor again.",
        )
    if not git_work_tree:
        return DoctorCheckResult.failed(
            DoctorCheckId.REPOSITORY,
            "Repository",
            "The requested directory is not inside a Git work tree.",
            "Pass --repo with a Git work tree, or initialize the directory with Git.",
        )
    return DoctorCheckResult.passed(
        DoctorCheckId.REPOSITORY,
        "Repository",
        "The requested directory is inside a Git work tree.",
    )


def check_codex_executable(executable: Path | None) -> DoctorCheckResult:
    if executable is None:
        return DoctorCheckResult.failed(
            DoctorCheckId.CODEX_EXECUTABLE,
            "Codex executable",
            "The Codex executable is not available on PATH.",
            "Install Codex CLI, add 'codex' to PATH, and run the doctor again.",
        )
    return DoctorCheckResult.passed(
        DoctorCheckId.CODEX_EXECUTABLE,
        "Codex executable",
        "The Codex executable is available on PATH.",
    )


def check_codex_version(
    executable: Path | None,
    version_command: CommandObservation | None,
) -> DoctorCheckResult:
    if executable is None:
        return DoctorCheckResult.failed(
            DoctorCheckId.CODEX_VERSION,
            "Codex version",
            "The Codex version cannot be checked because the executable is unavailable.",
            "Install Codex CLI, add 'codex' to PATH, and run the doctor again.",
        )
    version = extract_version(version_command.output) if version_command else None
    if version_command is None or not version_command.succeeded or version is None:
        return DoctorCheckResult.failed(
            DoctorCheckId.CODEX_VERSION,
            "Codex version",
            "The Codex executable did not return a usable version.",
            "Update or reinstall Codex CLI, then verify that 'codex --version' succeeds.",
        )
    return DoctorCheckResult.passed(
        DoctorCheckId.CODEX_VERSION, "Codex version", f"Codex CLI {version} is available."
    )


def check_app_server(status: AppServerStatus | None) -> DoctorCheckResult:
    if status is None:
        return DoctorCheckResult.failed(
            DoctorCheckId.APP_SERVER,
            "App Server",
            "The Codex App Server readiness probe did not complete.",
            "Update or reinstall Codex CLI, verify 'codex --version', and retry.",
        )
    return DoctorCheckResult.passed(
        DoctorCheckId.APP_SERVER,
        "App Server",
        "Initialize and account/read completed successfully.",
    )


def check_authentication(status: AppServerStatus | None) -> DoctorCheckResult:
    if status is None:
        return DoctorCheckResult.failed(
            DoctorCheckId.AUTHENTICATION,
            "Authentication",
            "Authentication readiness could not be read from the App Server.",
            "Resolve the App Server failure, then run the doctor again.",
        )
    if not status.authentication.ready:
        return DoctorCheckResult.failed(
            DoctorCheckId.AUTHENTICATION,
            "Authentication",
            "Codex requires OpenAI authentication before the runner can start.",
            "Run 'codex login', then run the doctor again.",
        )
    return DoctorCheckResult.passed(
        DoctorCheckId.AUTHENTICATION, "Authentication", "Codex authentication is ready."
    )


def check_terminal(*, stdin_is_terminal: bool, stdout_is_terminal: bool) -> DoctorCheckResult:
    if stdin_is_terminal and stdout_is_terminal:
        return DoctorCheckResult.passed(
            DoctorCheckId.TERMINAL,
            "Terminal",
            "Interactive input and output are available.",
        )
    return DoctorCheckResult.warning(
        DoctorCheckId.TERMINAL,
        "Terminal",
        "Interactive terminal input and output are required.",
        "Run CodexCLI directly in an interactive terminal without piping or redirection.",
    )


def check_storage(unavailable_locations: tuple[str, ...]) -> DoctorCheckResult:
    if not unavailable_locations:
        return DoctorCheckResult.passed(
            DoctorCheckId.STORAGE,
            "Storage",
            "Project run, user configuration, and user data storage are writable.",
        )
    locations = ", ".join(unavailable_locations)
    return DoctorCheckResult.failed(
        DoctorCheckId.STORAGE,
        "Storage",
        f"Writable access is unavailable for: {locations}.",
        f"Create these locations and grant the current user write access: {locations}.",
    )


def extract_version(output: str) -> str | None:
    """Extract a display-safe dotted version without retaining raw tool output."""

    match = _VERSION.search(output)
    if match is None:
        return None
    version = match.group(1)
    return version if len(version) <= 64 else None


def collect_doctor_report(config: ApplicationConfig) -> DoctorReport:
    """Run every readiness check in deterministic order, aggregating failures."""

    python_version = (
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )
    git_executable = _which("git")
    git_version = _run_git((git_executable, "--version")) if git_executable else None

    repository_exists, repository_is_directory = _path_kind(config.repository)
    git_work_tree = False
    if git_executable is not None and repository_is_directory:
        repository_probe = _run_git(
            (git_executable, "-C", str(config.repository), "rev-parse", "--is-inside-work-tree")
        )
        git_work_tree = (
            repository_probe.succeeded and repository_probe.output.strip().casefold() == "true"
        )

    codex_executable = _resolve_codex()
    codex_version = (
        _run_codex_version(
            codex_executable,
            cwd=config.repository if repository_is_directory else Path.cwd(),
        )
        if codex_executable is not None
        else None
    )
    app_server_status = _probe_app_server(
        codex_executable,
        timeout_seconds=config.app_server_timeout_seconds,
    )
    stdin_is_terminal, stdout_is_terminal = _terminal_capabilities()

    return DoctorReport.from_checks(
        (
            check_python(python_version),
            check_git(git_executable, git_version),
            check_repository(
                exists=repository_exists,
                is_directory=repository_is_directory,
                git_available=git_executable is not None,
                git_work_tree=git_work_tree,
            ),
            check_codex_executable(codex_executable),
            check_codex_version(codex_executable, codex_version),
            check_app_server(app_server_status),
            check_authentication(app_server_status),
            check_terminal(
                stdin_is_terminal=stdin_is_terminal,
                stdout_is_terminal=stdout_is_terminal,
            ),
            check_storage(_unavailable_storage(config)),
        )
    )


def run_doctor(config: ApplicationConfig) -> DoctorReport:
    return collect_doctor_report(config)


def print_doctor_report(report: DoctorReport, *, output: TextIO | None = None) -> None:
    from devloop.domain.doctor import render_doctor_report

    stream = sys.stdout if output is None else output
    stream.write(render_doctor_report(report) + "\n")


def _which(name: str) -> str | None:
    try:
        return shutil.which(name)
    except OSError:
        return None


def _run_git(command: tuple[str, ...]) -> CommandObservation:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=VERSION_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return CommandObservation(False)
    output = " ".join(part for part in (completed.stdout, completed.stderr) if part)
    return CommandObservation(completed.returncode == 0, output)


def _resolve_codex() -> Path | None:
    try:
        return resolve_codex_executable()
    except (CodexExecutableError, OSError):
        return None


def _run_codex_version(executable: Path, *, cwd: Path) -> CommandObservation:
    try:
        completed = run_codex(
            executable,
            ["--version"],
            cwd=cwd,
            timeout_seconds=VERSION_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return CommandObservation(False)
    output = " ".join(part for part in (completed.stdout, completed.stderr) if part)
    return CommandObservation(completed.returncode == 0, output)


def _path_kind(path: Path) -> tuple[bool, bool]:
    try:
        return path.exists(), path.is_dir()
    except OSError:
        return False, False


def _probe_app_server(
    executable: Path | None,
    *,
    timeout_seconds: float,
) -> AppServerStatus | None:
    if executable is None:
        return None
    try:
        with AppServerClient(str(executable), timeout_seconds=timeout_seconds) as client:
            return client.probe()
    except Exception:
        # Never render protocol, process, account, home, or stderr details.
        return None


def _terminal_capabilities() -> tuple[bool, bool]:
    try:
        return sys.stdin.isatty(), sys.stdout.isatty()
    except (AttributeError, OSError):
        return False, False


def _unavailable_storage(config: ApplicationConfig) -> tuple[str, ...]:
    locations = (
        ("project run storage", config.paths.run_root),
        ("user configuration storage", config.paths.user_config),
        ("user data storage", config.paths.user_data),
    )
    return tuple(label for label, path in locations if not _storage_is_writable(path))


def _storage_is_writable(path: Path) -> bool:
    results: queue.Queue[bool] = queue.Queue(maxsize=1)
    worker = threading.Thread(
        target=_run_storage_probe,
        args=(path, results),
        name="codexcli-storage-probe",
        daemon=True,
    )
    worker.start()
    try:
        return results.get(timeout=STORAGE_PROBE_TIMEOUT_SECONDS)
    except queue.Empty:
        return False


def _run_storage_probe(path: Path, results: queue.Queue[bool]) -> None:
    results.put(_probe_storage_is_writable(path))


def _probe_storage_is_writable(path: Path) -> bool:
    """Probe an existing target or its nearest parent, cleaning all probe artifacts."""

    try:
        if path.exists():
            if not path.is_dir():
                return False
            with tempfile.TemporaryFile(dir=path):
                pass
            return True

        probe_root = path.parent
        while not probe_root.exists() and probe_root.parent != probe_root:
            probe_root = probe_root.parent
        if not probe_root.is_dir():
            return False
        with tempfile.TemporaryFile(dir=probe_root):
            pass
    except OSError:
        return False
    return True
