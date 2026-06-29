from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence


def run_captured_text(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        capture_output=True,
        check=False,
    )


def output_text(value: str | None) -> str:
    return value or ""
