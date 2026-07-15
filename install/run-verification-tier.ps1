param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("fast", "vertical", "release")]
    [string]$Tier,
    [switch]$UseExistingArtifacts
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root
$evidenceDir = Join-Path $root ".release-evidence"
$resultLog = Join-Path $evidenceDir "windows-$Tier.log"
$manifest = Join-Path $evidenceDir "windows-$Tier.json"

function Invoke-Native {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$File failed with exit code $LASTEXITCODE"
    }
}

if ($env:DEVLOOP_GATE_CAPTURE -ne "1") {
    New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null
    $started = Get-Date
    $env:DEVLOOP_GATE_CAPTURE = "1"
    $succeeded = $true
    try {
        & $PSCommandPath -Tier $Tier -UseExistingArtifacts:$UseExistingArtifacts `
            *>&1 | Tee-Object -FilePath $resultLog
        if ($LASTEXITCODE -ne 0) { $succeeded = $false }
    }
    catch {
        $_ | Out-String | Tee-Object -FilePath $resultLog -Append
        $succeeded = $false
    }
    finally {
        Remove-Item Env:DEVLOOP_GATE_CAPTURE -ErrorAction SilentlyContinue
    }
    $duration = [int][Math]::Round(((Get-Date) - $started).TotalMilliseconds)
    $status = if ($succeeded) { "PASSED" } else { "FAILED" }
    $arguments = @(
        "run", "codexcli-gate",
        "--tier", $Tier,
        "--repo", $root,
        "--output", $manifest,
        "--gate-id", "windows-$Tier",
        "--status", $status,
        "--duration-ms", "$duration",
        "--result-log", $resultLog
    )
    if ($Tier -eq "release") {
        Get-ChildItem -LiteralPath "dist" -File -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -like "devloop_codexcli-0.1.0-*.whl" -or
                $_.Name -eq "devloop_codexcli-0.1.0.tar.gz"
            } |
            ForEach-Object { $arguments += @("--artifact", $_.FullName) }
    }
    Invoke-Native uv $arguments
    if (-not $succeeded) { exit 1 }
    exit 0
}

$releaseTemp = Join-Path ([System.IO.Path]::GetTempPath()) "devloop-codexcli-$Tier-$PID"
$env:UV_CACHE_DIR = Join-Path $releaseTemp "uv-cache"
switch ($Tier) {
    "fast" {
        Invoke-Native uv @("sync", "--locked")
        Invoke-Native uv @("run", "ruff", "check", "--no-cache", ".")
        Invoke-Native uv @("run", "mypy", "--cache-dir", (Join-Path $releaseTemp "mypy"))
        Invoke-Native uv @(
            "run", "pytest", "-q", "-m", "not integration",
            "--basetemp=$(Join-Path $releaseTemp 'pytest-fast')"
        )
    }
    "vertical" {
        Invoke-Native codex @("login", "status")
        $env:DEVLOOP_REAL_VERTICAL = "1"
        Invoke-Native uv @("sync", "--locked")
        Invoke-Native uv @("run", "codexcli", "doctor", "--repo", $root)
        Invoke-Native uv @(
            "run", "pytest", "-q", "-m", "integration",
            "tests/codexcli/test_real_vertical_workflow.py",
            "--basetemp=$(Join-Path $releaseTemp 'pytest-vertical')"
        )
    }
    "release" {
        & (Join-Path $PSScriptRoot "run-release-gates.ps1") `
            -RealBackend -UseExistingArtifacts:$UseExistingArtifacts
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}
