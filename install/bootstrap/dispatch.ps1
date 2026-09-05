[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('devloop', 'devloop-plan', 'update', 'uninstall')]
    [string] $Command,

    [string] $ArgumentsJson = '[]'
)

$ErrorActionPreference = 'Stop'
$InstallRoot = Split-Path -Parent $PSScriptRoot
try { $RemainingArgs = @($ArgumentsJson | ConvertFrom-Json) }
catch { throw 'Dev Loop bootstrap received invalid encoded arguments.' }

function Get-DevLoopPython {
    foreach ($candidate in @('python', 'python3', 'py')) {
        if ($null -eq (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
        try {
            & $candidate -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' *> $null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        }
        catch { continue }
    }
    throw 'Dev Loop bootstrap requires Python 3.10+.'
}

$python = Get-DevLoopPython
$verifier = Join-Path $PSScriptRoot 'verify.py'
if (-not (Test-Path -LiteralPath $verifier -PathType Leaf)) {
    throw 'Dev Loop bootstrap verifier is missing.'
}
if ($Command -eq 'uninstall') {
    $transaction = Join-Path $PSScriptRoot 'transaction.py'
    $transactionId = (& $python $transaction begin $InstallRoot uninstall --owner-pid $PID --protocol 2).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Dev Loop bootstrap could not acquire install lock.' }
    $transactionArgs = @($transaction, 'uninstall', $InstallRoot, $transactionId, '--protocol', '2')
    $keepCapabilities = $false
    for ($index = 0; $index -lt $RemainingArgs.Count; $index++) {
        switch -CaseSensitive ($RemainingArgs[$index]) {
            '-KeepSkills' { $keepCapabilities = $true }
            '-BinDir' { $index++ }
            '-CodexSkillsPath' { $transactionArgs += @('--skills-destination', $RemainingArgs[++$index]) }
            '-CodexAgentsPath' { $transactionArgs += @('--agents-destination', $RemainingArgs[++$index]) }
            default { throw "Dev Loop bootstrap received an unsupported uninstall option: $($RemainingArgs[$index])" }
        }
    }
    if ($keepCapabilities) { $transactionArgs += '--keep-capabilities' }
    else {
        if ('--skills-destination' -notin $transactionArgs) { $transactionArgs += @('--skills-destination', (Join-Path $env:USERPROFILE '.codex\skills')) }
        if ('--agents-destination' -notin $transactionArgs) { $transactionArgs += @('--agents-destination', (Join-Path $env:USERPROFILE '.codex\agents')) }
    }
    & $python @transactionArgs
    exit $LASTEXITCODE
}
$verifyArguments = @($verifier, $InstallRoot)
if ($Command -eq 'update') { $verifyArguments += '--update-driver' }
$releaseRoot = (& $python @verifyArguments)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($releaseRoot)) {
    throw 'Dev Loop bootstrap release verification failed.'
}
$target = switch ($Command) {
    'devloop' { Join-Path $releaseRoot 'bin\devloop.ps1' }
    'devloop-plan' { Join-Path $releaseRoot 'bin\devloop-plan.ps1' }
    'update' { Join-Path $releaseRoot 'install\devloop.ps1' }
    'uninstall' { Join-Path $releaseRoot 'install\uninstall-devloop.ps1' }
}
if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
    throw "Dev Loop bootstrap command is missing from the current release: $target"
}
if ($Command -in @('update', 'uninstall')) {
    & pwsh -NoProfile -ExecutionPolicy Bypass -File $target -InstallDir $InstallRoot @RemainingArgs
}
else {
    & pwsh -NoProfile -ExecutionPolicy Bypass -File $target @RemainingArgs
}
exit $LASTEXITCODE
