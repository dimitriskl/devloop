$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root
$env:DEVLOOP_REAL_RECOVERY = "1"
$baseTemp = ".tmp-pytest-release-recovery-$PID"
uv run pytest -q tests/codexcli/test_real_recovery.py --basetemp=$baseTemp
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
