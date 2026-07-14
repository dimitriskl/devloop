from __future__ import annotations

from pathlib import Path


def test_release_gates_use_external_temp_and_build_only_current_archives() -> None:
    root = Path(__file__).parents[2]
    powershell = (root / "install" / "run-release-gates.ps1").read_text(encoding="utf-8")
    shell = (root / "install" / "run-release-gates.sh").read_text(encoding="utf-8")

    assert ".uv-cache-release" not in powershell
    assert ".uv-cache-release" not in shell
    assert "[System.IO.Path]::GetTempPath()" in powershell
    assert "${TMPDIR:-/tmp}" in shell
    assert 'Assert-Command "uv"' in powershell
    assert 'Assert-Command "pipx"' in powershell
    assert 'Assert-Command "codex"' in powershell
    assert 'Invoke-Native codex @("login", "status")' in powershell
    assert "for required_command in uv pipx codex" in shell
    assert "codex login status" in shell
    assert '"ruff", "check", "--no-cache", "."' in powershell
    assert "ruff check --no-cache ." in shell
    assert '"mypy", "--cache-dir", (Join-Path $releaseTemp "mypy")' in powershell
    assert 'mypy --cache-dir "$release_temp/mypy"' in shell
    assert powershell.index("Clear-ReleaseArchives") < powershell.index(
        'Invoke-Native uv @("build"'
    )
    assert shell.index("rm -f dist/devloop_codexcli-*.whl") < shell.index(
        "uv build --sdist"
    )
    assert '$env:DEVLOOP_REAL_UI = "1"' in powershell
    assert "export DEVLOOP_REAL_UI=1" in shell
    assert '$wheel.FullName' in powershell
    assert 'uv tool install --force "$wheel"' in shell
    assert 'pipx install --force "$wheel"' in shell
    assert powershell.count('Invoke-Native codexcli @("doctor", "--help")') == 2
    assert powershell.count('Invoke-Native codexcli @("run", "--help")') == 2
    assert 'Invoke-Native uv @("tool", "uninstall", "devloop-codexcli")' in powershell
    assert shell.count("codexcli doctor --help") == 2
    assert shell.count("codexcli run --help") == 2
    assert "uv tool uninstall devloop-codexcli" in shell
    assert 'Invoke-Native codexcli @("doctor", "--repo", $root)' in powershell
    assert 'codexcli doctor --repo "$root"' in shell
