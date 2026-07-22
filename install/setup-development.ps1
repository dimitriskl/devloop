# Prepare this development checkout without installing global commands or skills.

[CmdletBinding()]
param(
    [switch] $Help
)

$ErrorActionPreference = 'Stop'
$bundleRoot = Split-Path -Parent $PSScriptRoot

function Show-Usage {
    @'
Usage: setup-development.ps1 [-Help]

Prepare this development checkout with its isolated Python runtime. This command
does not create shortcuts, change PATH, copy global skills, or update Git.
'@ | Write-Host
}

function Get-DevLoopPython {
    foreach ($candidate in @('python', 'python3', 'py')) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -eq $command) {
            continue
        }
        try {
            & $candidate -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' *> $null
            if ($LASTEXITCODE -eq 0) {
                return $candidate
            }
        }
        catch {
            continue
        }
    }
    throw 'devloop-development: Python 3.10+ is required.'
}

function Install-DevelopmentRuntime {
    $basePython = Get-DevLoopPython
    $runtimePath = Join-Path $bundleRoot '.venv'
    $nextPath = Join-Path $bundleRoot '.venv.next'
    $previousPath = Join-Path $bundleRoot '.venv.previous'
    $lockPath = Join-Path $bundleRoot 'requirements-portable.lock'

    if (Test-Path -LiteralPath $nextPath) {
        Remove-Item -LiteralPath $nextPath -Recurse -Force
    }
    Write-Host "Preparing checkout-local runtime at $runtimePath"
    & $basePython -m venv $nextPath
    if ($LASTEXITCODE -ne 0) {
        throw 'devloop-development: could not create the replacement runtime'
    }
    $nextPython = Join-Path $nextPath 'Scripts\python.exe'
    & $nextPython -m pip install --disable-pip-version-check --requirement $lockPath
    if ($LASTEXITCODE -ne 0) {
        throw 'devloop-development: dependency installation failed'
    }
    & $nextPython -c 'import textual; raise SystemExit(0 if textual.__version__ == "8.2.8" else 1)'
    if ($LASTEXITCODE -ne 0) {
        throw 'devloop-development: runtime validation failed'
    }

    if (Test-Path -LiteralPath $previousPath) {
        Remove-Item -LiteralPath $previousPath -Recurse -Force
    }
    if (Test-Path -LiteralPath $runtimePath) {
        Move-Item -LiteralPath $runtimePath -Destination $previousPath
    }
    try {
        Move-Item -LiteralPath $nextPath -Destination $runtimePath
    }
    catch {
        if (Test-Path -LiteralPath $previousPath) {
            Move-Item -LiteralPath $previousPath -Destination $runtimePath
        }
        throw
    }
    if (Test-Path -LiteralPath $previousPath) {
        Remove-Item -LiteralPath $previousPath -Recurse -Force
    }
}

if ($Help) {
    Show-Usage
    return
}

Install-DevelopmentRuntime
Write-Host ''
Write-Host 'Development runtime ready. No global installation changes were made.'
Write-Host "Run: & '$bundleRoot\bin\devloop-plan.ps1'"
