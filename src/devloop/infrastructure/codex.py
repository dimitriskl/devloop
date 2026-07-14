from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


class CodexExecutableError(RuntimeError):
    pass


def resolve_codex_executable(command: str = "codex") -> Path:
    resolved = shutil.which(command)
    if resolved:
        return Path(resolved).resolve()

    candidate = Path(command).expanduser()
    if candidate.is_file():
        return candidate.resolve()

    raise CodexExecutableError(
        "Codex CLI was not found. Install and authenticate Codex before running codexcli."
    )


def executable_command(executable: Path, arguments: Sequence[str]) -> list[str]:
    command = [str(executable), *arguments]
    if not sys.platform.startswith("win") or executable.suffix.casefold() not in {".bat", ".cmd"}:
        return command

    command_line = subprocess.list2cmdline(command)
    return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command_line]


def run_codex(
    executable: Path,
    arguments: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        executable_command(executable, arguments),
        cwd=cwd,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=timeout_seconds,
    )
