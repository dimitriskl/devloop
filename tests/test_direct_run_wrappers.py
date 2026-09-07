"""Execute the direct launchers with a supplied runtime and disposable PRD package."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from portable_test_support import ROOT, isolated_environment


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _launcher(entrypoint: str, bundle: Path) -> list[str]:
    if entrypoint == "python":
        return [sys.executable, "-B", "-m", "devloop"]
    scripts = bundle / "bin"
    scripts.mkdir(parents=True)
    if entrypoint == "powershell":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            pytest.skip("PowerShell is unavailable")
        wrapper = scripts / "devloop.ps1"
        shutil.copyfile(ROOT / "bin/devloop.ps1", wrapper)
        # Supply only the interpreter lookup; execute the unchanged wrapper body.
        launcher = bundle / "launch.ps1"
        launcher.write_text(
            "function Join-Path { param($Path, $ChildPath)\n"
            "if ($ChildPath -eq '.venv\\Scripts\\python.exe') { "
            f"return {_powershell_literal(sys.executable)} }}\n"
            "Microsoft.PowerShell.Management\\Join-Path $Path $ChildPath\n}\n"
            f"& {_powershell_literal(str(wrapper))} @args\nexit $LASTEXITCODE\n",
            encoding="utf-8",
        )
        return [shell, "-NoProfile", "-NonInteractive", "-File", str(launcher)]
    shell = shutil.which("bash")
    if os.name == "nt":
        git = shutil.which("git")
        candidate = Path(git).parent.parent / "bin/bash.exe" if git else None
        shell = str(candidate) if candidate and candidate.is_file() else None
    if shell is None:
        pytest.skip("A local Bash runtime is unavailable")
    wrapper = scripts / "devloop.sh"
    shutil.copyfile(ROOT / "bin/devloop.sh", wrapper)
    runtime = bundle / ".venv/bin/python"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        f'#!/usr/bin/env bash\nexec {shlex.quote(Path(sys.executable).as_posix())} "$@"\n',
        encoding="utf-8",
    )
    runtime.chmod(0o755)
    return [shell, str(wrapper)]


@pytest.mark.parametrize("entrypoint", ["python", "powershell", "bash"])
def test_prd_only_launcher_uses_core_defaults(entrypoint: str, tmp_path: Path) -> None:
    environment = isolated_environment(tmp_path)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]
    )
    repository = tmp_path / "target with spaces"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        env=environment, capture_output=True, check=True,
    )
    package = repository / "prd/feature"
    issues = package / "issues"
    issues.mkdir(parents=True)
    prd = package / "feature.md"
    target = "## Target Product\n\nProduct: devloop-plan + devloop\n"
    prd.write_text("# Feature\n\n" + target, encoding="utf-8")
    links = []
    for number in (1, 2):
        name = f"{number:04d}-example.md"
        (issues / name).write_text(
            f"# Issue {number:04d}\n\n{target}\nCompleted: [ ]\n\n## Blocked by\n\nNone.\n",
            encoding="utf-8",
        )
        links.append(f"- [Issue {number:04d}](./{name})\n")
    (issues / "README.md").write_text("".join(links), encoding="utf-8")
    command = [*_launcher(entrypoint, tmp_path / "bundle"), "--prd", str(prd), "--dry-run"]
    result = subprocess.run(
        command, cwd=repository, env=environment, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=60, check=False,
    )
    (tmp_path / "launcher-output.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Selected issues: 0001, 0002" in result.stdout
    assert "self-improvement wiki update skipped for dry run" in result.stdout
    assert "Created implementation worktree" not in result.stdout
    state = json.loads((issues / "README.loop.state.json").read_text(encoding="utf-8"))
    assert (issues / "README.loop.md").is_file()
    prompts = list((issues / ".loop.logs").rglob("*.prompt.md"))
    assert prompts
    for number in ("0001", "0002"):
        assert number in state["issues"]
        assert any(f"{number}-development" in path.name for path in prompts)
    assert Path(state["repo_root"]) == repository
    assert Path(state["issues_index"]) == issues / "README.md"
    assert Path(state["prd_path"]) == prd
    assert all(str(repository) in path.read_text(encoding="utf-8") for path in prompts)
    assert any("wiki" in path.read_text(encoding="utf-8") for path in prompts)
    assert all("Completed: [ ]" in p.read_text() for p in issues.glob("*-example.md"))
