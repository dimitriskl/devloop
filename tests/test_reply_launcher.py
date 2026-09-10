"""Run both real PowerShell wrappers with an argument-capturing Python module."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from devloop.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]


def _literal(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@pytest.mark.parametrize("reply", [True, False])
def test_reply_launcher_preserves_argument_boundaries(tmp_path: Path, reply: bool) -> None:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    bundle = tmp_path / "bundle with spaces"
    (bundle / "bin").mkdir(parents=True)
    for relative in ("resume-feedback.ps1", "bin/devloop.ps1"):
        shutil.copyfile(ROOT / relative, bundle / relative)
    package = bundle / "src/devloop"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "Path(os.environ['DEVLOOP_ARGUMENT_CAPTURE']).write_text("
        "json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    launcher = bundle / "test-launch.ps1"
    launcher.write_text(
        "function Join-Path { param($Path, $ChildPath)\n"
        "if ($ChildPath -eq '.venv\\Scripts\\python.exe') { "
        f"return {_literal(Path(sys.executable))} }}\n"
        "Microsoft.PowerShell.Management\\Join-Path $Path $ChildPath\n}\n"
        f"& {_literal(bundle / 'resume-feedback.ps1')} @args\n",
        encoding="utf-8",
    )
    capture = bundle / "arguments.json"
    command = [shell, "-NoProfile", "-NonInteractive", "-File", str(launcher)]
    if reply:
        command.append("-Reply")
    result = subprocess.run(
        command, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
        env={**os.environ, "DEVLOOP_ARGUMENT_CAPTURE": str(capture)},
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    arguments = json.loads(capture.read_text(encoding="utf-8"))
    assert arguments == [
        "--prd",
        r"E:\LocalCode\eConnectorV2\prd\dynamic-query-delta-timestamp"
        r"\dynamic-query-delta-timestamp.md",
        *(["--reply"] if reply else []),
    ]
    assert build_parser().parse_args(arguments).reply is reply
