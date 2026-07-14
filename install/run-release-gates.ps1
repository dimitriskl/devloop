param([switch]$RealBackend)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root
$releaseTemp = Join-Path ([System.IO.Path]::GetTempPath()) "devloop-codexcli-release-$PID"
$env:UV_CACHE_DIR = Join-Path $releaseTemp "uv-cache"
$baseTemp = Join-Path $releaseTemp "pytest"

function Invoke-Native {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$File failed with exit code $LASTEXITCODE"
    }
}

function Assert-Command {
    param([string]$Name)
    if ($null -eq (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required release command is unavailable: $Name"
    }
}

function Clear-ReleaseArchives {
    if (-not (Test-Path -LiteralPath "dist")) {
        return
    }
    Get-ChildItem -LiteralPath "dist" -File |
        Where-Object {
            $_.Name -like "devloop_codexcli-*.whl" -or
            $_.Name -like "devloop_codexcli-*.tar.gz"
        } |
        Remove-Item -Force
}

Assert-Command "uv"
Assert-Command "pipx"
Assert-Command "codex"
if ($RealBackend) {
    Invoke-Native codex @("login", "status")
}

Invoke-Native uv @("sync", "--locked")
Invoke-Native uv @("run", "ruff", "check", "--no-cache", ".")
Invoke-Native uv @("run", "mypy", "--cache-dir", (Join-Path $releaseTemp "mypy"))
Invoke-Native uv @("run", "pytest", "-q", "-m", "not integration", "--basetemp=$baseTemp")
Clear-ReleaseArchives
Invoke-Native uv @("build", "--sdist", "--wheel", "--out-dir", "dist")
Invoke-Native uv @("run", "python", "install/verify-release.py", "--dist", "dist")
$wheels = @(Get-ChildItem -LiteralPath "dist" -File -Filter "devloop_codexcli-0.1.0-*.whl")
if ($wheels.Count -ne 1) {
    throw "Expected exactly one verified v0.1.0 wheel."
}
$wheel = $wheels[0]
$python = (& uv run python -c "import sys; print(sys.executable)") | Select-Object -Last 1
if ($LASTEXITCODE -ne 0 -or -not $python) {
    throw "Unable to resolve the locked Python interpreter."
}
Invoke-Native uv @(
    "tool", "install", "--force", "--python", $python.Trim(), $wheel.FullName
)
Invoke-Native codexcli @("--help")
Invoke-Native codexcli @("doctor", "--help")
Invoke-Native codexcli @("run", "--help")
Invoke-Native uv @("tool", "uninstall", "devloop-codexcli")
Invoke-Native pipx @("install", "--force", $wheel.FullName)
Invoke-Native pipx @("runpip", "devloop-codexcli", "show", "devloop-codexcli")
Invoke-Native codexcli @("doctor", "--help")
Invoke-Native codexcli @("run", "--help")

if ($RealBackend) {
    $env:DEVLOOP_REAL_APP_SERVER = "1"
    $env:DEVLOOP_REAL_ANALYSIS = "1"
    $env:DEVLOOP_REAL_DEVELOPMENT = "1"
    $env:DEVLOOP_REAL_REVIEW_QA = "1"
    $env:DEVLOOP_REAL_REWORK = "1"
    $env:DEVLOOP_REAL_SCHEDULER = "1"
    $env:DEVLOOP_REAL_RECOVERY = "1"
    $env:DEVLOOP_REAL_UI = "1"
    Invoke-Native codexcli @("doctor", "--repo", $root)
    Invoke-Native uv @(
        "run", "pytest", "-q", "-m", "integration",
        "--basetemp=$(Join-Path $releaseTemp 'pytest-real')"
    )
}

Write-Output "PASS v0.1.0 release gates"
