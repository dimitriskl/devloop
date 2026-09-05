# Install or update Portable Dev Loop through an immutable side-by-side release.
[CmdletBinding()]
param(
    [string] $InstallDir,
    [string] $BinDir,
    [string] $RepoUrl = $(if ($env:DEVLOOP_REPO_URL) { $env:DEVLOOP_REPO_URL } else { 'https://github.com/dimitriskl/devloop.git' }),
    [string] $Ref = $(if ($env:DEVLOOP_REF) { $env:DEVLOOP_REF } else { 'main' }),
    [switch] $NoSkills, [switch] $NoBinLinks, [switch] $Rollback, [switch] $Help
)
$ErrorActionPreference = 'Stop'
$DefaultInstallDir = 'C:\devloop'
$BootstrapProtocol = 2
$script:CandidateDir = $null
$script:CandidateCommit = $null
$script:TransactionId = $null
$null = $BinDir
$null = $NoBinLinks

function Write-InstallLog { param([string] $Message) Write-Host "devloop-install: $Message" }
function Show-Usage {
    @'
Usage: devloop.ps1 [options]

Install or update Portable Dev Loop using immutable side-by-side releases.

Options:
  -InstallDir PATH   Stable bootstrap directory (default: C:\devloop)
  -Ref REF           Git branch, tag, or commit (default: main)
  -RepoUrl URL       Git repository URL
  -NoSkills          Skip copying bundled Codex skills and agents
  -Rollback          Atomically select the previously current release
  -Help              Show this help
'@ | Write-Host
}
function Get-DevLoopPython {
    foreach ($candidate in @('python', 'python3', 'py')) {
        if ($null -eq (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
        try {
            & $candidate -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' *> $null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch { continue }
    }
    throw 'devloop-install: error: Python 3.10+ is required.'
}
function Resolve-InstallDirectory {
    $selected = if ($InstallDir) { $InstallDir } elseif ($env:DEVLOOP_INSTALL_DIR) { $env:DEVLOOP_INSTALL_DIR } else { $DefaultInstallDir }
    try { return [IO.Path]::GetFullPath($selected).TrimEnd('\') }
    catch { throw "devloop-install: error: invalid install directory '$selected'" }
}
function Assert-PlainPath {
    param([string] $Path, [switch] $AllowMissing)
    if (-not (Test-Path -LiteralPath $Path)) { if ($AllowMissing) { return }; throw "devloop-install: error: required path is missing: $Path" }
    if (((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "devloop-install: error: refusing reparse point: $Path" }
}
function Assert-SafeInstallRoot {
    $root = [IO.Path]::GetPathRoot($InstallDir)
    if (-not $root -or $InstallDir -ieq $root.TrimEnd('\')) { throw 'devloop-install: error: refusing filesystem root as InstallDir' }
    $parent = Split-Path -Parent $InstallDir
    if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    Assert-PlainPath $parent
    Assert-PlainPath $InstallDir -AllowMissing
}
function Invoke-ReleaseCommand {
    param([string] $Python, [string] $Root, [string] $Command)
    $oldPythonPath = $env:PYTHONPATH; $oldNoBytecode = $env:PYTHONDONTWRITEBYTECODE
    try {
        $env:PYTHONPATH = Join-Path $Root 'src'; $env:PYTHONDONTWRITEBYTECODE = '1'
        & $Python -m devloop.portable_release $Command
        if ($LASTEXITCODE -ne 0) { throw "devloop-install: error: portable release $Command failed" }
    } finally { $env:PYTHONPATH = $oldPythonPath; $env:PYTHONDONTWRITEBYTECODE = $oldNoBytecode }
}
function Invoke-Bootstrap {
    param([string] $Entry, [Parameter(ValueFromRemainingArguments = $true)][object[]] $Arguments)
    & (Get-DevLoopPython) -B $Entry @Arguments --protocol $BootstrapProtocol
}
function Clone-Candidate {
    $parent = Split-Path -Parent $InstallDir; $leaf = Split-Path -Leaf $InstallDir
    $script:CandidateDir = Join-Path $parent ".${leaf}.candidate-$script:TransactionId"
    New-Item -ItemType Directory -Path $script:CandidateDir | Out-Null
    if ($env:DEVLOOP_TESTING -eq '1' -and $env:DEVLOOP_TEST_INTERRUPT_CANDIDATE_BOUNDARY -eq 'candidate_created') { [Environment]::Exit(92) }
    Write-InstallLog "Staging ref $Ref outside the stable bootstrap"
    & git clone --depth 1 --branch $Ref $RepoUrl $script:CandidateDir
    if ($LASTEXITCODE -ne 0) {
        if (Test-Path -LiteralPath $script:CandidateDir) { Remove-Item -LiteralPath $script:CandidateDir -Recurse -Force }
        & git clone --depth 1 $RepoUrl $script:CandidateDir
        if ($LASTEXITCODE -ne 0) { throw "devloop-install: error: could not clone $RepoUrl" }
        & git -C $script:CandidateDir checkout -f $Ref
        if ($LASTEXITCODE -ne 0) { throw "devloop-install: error: could not resolve ref $Ref" }
    }
    $script:CandidateCommit = (& git -C $script:CandidateDir rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $script:CandidateCommit -cnotmatch '^[0-9a-f]{40}$') { throw 'devloop-install: error: candidate commit is invalid' }
    & git -C $script:CandidateDir reset --hard $script:CandidateCommit
    & git -C $script:CandidateDir clean -ffdx
    if ($LASTEXITCODE -ne 0) { throw 'devloop-install: error: could not clean candidate' }
    if ($env:DEVLOOP_TESTING -eq '1' -and $env:DEVLOOP_TEST_INTERRUPT_CANDIDATE_BOUNDARY -eq 'candidate_cloned') { [Environment]::Exit(92) }
}
function Install-CandidateRuntime {
    $runtime = Join-Path $script:CandidateDir '.venv'
    if ($env:DEVLOOP_TESTING -eq '1') {
        Invoke-ReleaseCommand (Get-DevLoopPython) $script:CandidateDir 'validate-runtime'
        New-Item -ItemType Directory -Path $runtime | Out-Null
        [IO.File]::WriteAllText((Join-Path $runtime '.devloop-test-runtime'), $script:CandidateCommit, [Text.UTF8Encoding]::new($false)); return
    }
    $basePython = Get-DevLoopPython; & $basePython -m venv $runtime
    if ($LASTEXITCODE -ne 0) { throw 'devloop-install: error: runtime creation failed' }
    $python = Join-Path $runtime 'Scripts\python.exe'
    & $python -m pip install --disable-pip-version-check --requirement (Join-Path $script:CandidateDir 'requirements-portable.lock')
    if ($LASTEXITCODE -ne 0) { throw 'devloop-install: error: runtime dependency installation failed' }
    Invoke-ReleaseCommand $python $script:CandidateDir 'validate-runtime'
}
function Initialize-UserState {
    param([string] $ReleaseRoot)
    $python = if ($env:DEVLOOP_TESTING -eq '1') { Get-DevLoopPython } else { Join-Path $ReleaseRoot '.venv\Scripts\python.exe' }
    Write-InstallLog 'Initializing v3 user state and idempotent v0.2.1 adoption'
    Invoke-ReleaseCommand $python $ReleaseRoot 'prepare-user-state'
}
function Get-VerifiedRelease {
    $release = (& (Get-DevLoopPython) -B (Join-Path $InstallDir 'bootstrap\verify.py') $InstallDir).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'devloop-install: error: current release verification failed' }; return $release
}
function Recover-PendingTransaction {
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDir 'bootstrap\install-transaction.json') -PathType Leaf)) { return $false }
    $transaction = Join-Path $InstallDir 'bootstrap\transaction.py'
    $parts = (Invoke-Bootstrap $transaction recover $InstallDir $script:TransactionId) -split "`t", 2
    if ($LASTEXITCODE -ne 0) { throw 'devloop-install: error: pending installation recovery failed' }
    Write-InstallLog "Recovering durable installation action $($parts[0])"
    if ($parts[0] -eq 'NEEDS_ADOPTION') { Initialize-UserState $parts[1]; Invoke-Bootstrap $transaction commit $InstallDir $script:TransactionId }
    elseif ($parts[0] -eq 'READY_TO_SWITCH') { Invoke-Bootstrap $transaction commit $InstallDir $script:TransactionId }
    elseif ($parts[0] -ne 'COMPLETE') { throw "devloop-install: error: unsupported recovery action: $($parts[0])" }
    if ($LASTEXITCODE -eq 91) { [Environment]::Exit(91) }
    if ($LASTEXITCODE -ne 0) { throw 'devloop-install: error: pending installation commit failed' }; return $true
}
function Activate-Candidate {
    Invoke-Bootstrap (Join-Path $script:CandidateDir 'install\bootstrap\transaction.py') publish $InstallDir $script:CandidateDir $script:TransactionId
    if ($LASTEXITCODE -in @(93, 94)) { [Environment]::Exit($LASTEXITCODE) }
    if ($LASTEXITCODE -ne 0) { throw 'devloop-install: error: stable bootstrap publication failed' }
    $transaction = Join-Path $InstallDir 'bootstrap\transaction.py'
    $release = Invoke-Bootstrap $transaction prepare $InstallDir $script:CandidateDir $script:CandidateCommit $script:TransactionId
    if ($LASTEXITCODE -in @(91, 93, 94)) { [Environment]::Exit($LASTEXITCODE) }
    if ($LASTEXITCODE -ne 0) { throw 'devloop-install: error: candidate preparation failed' }
    $script:CandidateDir = $null; Initialize-UserState $release
    Invoke-Bootstrap $transaction commit $InstallDir $script:TransactionId
    if ($LASTEXITCODE -eq 91) { [Environment]::Exit(91) }
    if ($LASTEXITCODE -ne 0) { throw 'devloop-install: error: candidate activation failed' }
}
function Install-Capabilities {
    if ($NoSkills) { return }; $root = Get-VerifiedRelease
    try {
        if ($env:DEVLOOP_TESTING -eq '1' -and $env:DEVLOOP_TEST_CAPABILITY_FAILURE -eq '1') { throw 'injected capability installation failure' }
        & (Join-Path $root 'install\install-skills.ps1'); if ($LASTEXITCODE -ne 0) { throw 'capability installer returned a failure' }
    } catch { Write-Warning "devloop-install: capability installation warning: $($_.Exception.Message)" }
}
function Show-NextSteps {
    $root = Get-VerifiedRelease; $commit = (& git -C $root rev-parse HEAD).Trim()
    Write-Host ''; Write-Host "Portable Dev Loop is installed at $InstallDir"; Write-Host "Current immutable release: $commit"
    Write-Host "Run: & '$InstallDir\bin\devloop.ps1' --help"; Write-Host "Plan: & '$InstallDir\bin\devloop-plan.ps1' --help"
    Write-Host "Update: & '$InstallDir\install\devloop.ps1'"; Write-Host "Uninstall: & '$InstallDir\install\uninstall-devloop.ps1'"
}

if ($Help) { Show-Usage; return }
if ($null -eq (Get-Command git -ErrorAction SilentlyContinue)) { throw 'devloop-install: error: Git is required.' }
[void](Get-DevLoopPython); $InstallDir = Resolve-InstallDirectory; Assert-SafeInstallRoot
if ($Rollback) {
    $transaction = Join-Path $InstallDir 'bootstrap\transaction.py'
    $beginResult = Invoke-Bootstrap $transaction begin $InstallDir rollback --owner-pid $PID
    if ($LASTEXITCODE -ne 0) { throw 'devloop-install: error: could not acquire install lock' }
    $script:TransactionId = $beginResult.Trim()
    Invoke-Bootstrap $transaction rollback $InstallDir $script:TransactionId
    if ($LASTEXITCODE -ne 0) { throw 'devloop-install: error: rollback failed' }; Show-NextSteps; return
}
$sourceTransaction = Join-Path $PSScriptRoot 'bootstrap\transaction.py'
$transactionEntry = if (Test-Path -LiteralPath (Join-Path $InstallDir 'bootstrap\transaction.py')) { Join-Path $InstallDir 'bootstrap\transaction.py' } else { $sourceTransaction }
$beginResult = Invoke-Bootstrap $transactionEntry begin $InstallDir install --owner-pid $PID
if ($LASTEXITCODE -ne 0 -and $transactionEntry -ne $sourceTransaction) {
    $transactionEntry = $sourceTransaction
    $beginResult = Invoke-Bootstrap $transactionEntry begin-legacy-migration $InstallDir --owner-pid $PID
}
if ($LASTEXITCODE -ne 0) { throw 'devloop-install: error: could not acquire compatible install lock' }
$script:TransactionId = $beginResult.Trim()
if (Recover-PendingTransaction) { Install-Capabilities; Show-NextSteps; return }
try {
    Clone-Candidate; Install-CandidateRuntime
    $currentRoot = if (Test-Path -LiteralPath (Join-Path $InstallDir 'bootstrap\current.json')) { Get-VerifiedRelease } else { $null }
    $currentCommit = if ($currentRoot) { (& git -C $currentRoot rev-parse HEAD).Trim() } else { $null }
    if ($currentCommit -and $currentCommit -ceq $script:CandidateCommit) {
        Remove-Item -LiteralPath $script:CandidateDir -Recurse -Force; $script:CandidateDir = $null; Initialize-UserState $currentRoot
        Invoke-Bootstrap $transactionEntry abort $InstallDir $script:TransactionId
    } else { Activate-Candidate }
} catch {
    $failure = $_
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDir 'bootstrap\install-transaction.json')) -and $script:TransactionId) {
        Invoke-Bootstrap $transactionEntry abort $InstallDir $script:TransactionId 2> $null
    }
    throw $failure
}
Install-Capabilities; Show-NextSteps
