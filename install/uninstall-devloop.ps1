# Remove verified installer-owned bootstrap, releases, and unchanged capabilities.

[CmdletBinding()]
param(
    [string] $InstallDir = $(if ($env:DEVLOOP_INSTALL_DIR) { $env:DEVLOOP_INSTALL_DIR } else { Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }),
    [string] $BinDir,
    [string] $CodexSkillsPath = "$env:USERPROFILE\.codex\skills",
    [string] $CodexAgentsPath = "$env:USERPROFILE\.codex\agents",
    [switch] $KeepSkills,
    [switch] $Help
)

$ErrorActionPreference = 'Stop'
$null = $BinDir
if ($Help) {
    Write-Host 'Usage: uninstall-devloop.ps1 [-InstallDir PATH] [-KeepSkills]'
    Write-Host 'Core release removal is preflighted and committed before capability cleanup.'
    return
}
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$root = [IO.Path]::GetPathRoot($InstallDir)
if ($InstallDir -ieq $root.TrimEnd('\')) { throw 'devloop-uninstall: refusing filesystem root' }
$transaction = Join-Path $InstallDir 'bootstrap\transaction.py'
if (-not (Test-Path -LiteralPath $transaction -PathType Leaf)) {
    throw 'devloop-uninstall: stable layout manifest is missing; nothing was removed'
}
$python = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    if ($null -eq (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
    try {
        & $candidate -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' *> $null
        if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
    }
    catch { continue }
}
if ($null -eq $python) { throw 'devloop-uninstall: Python 3.10+ is required' }
$transactionId = (& $python $transaction begin $InstallDir uninstall --owner-pid $PID --protocol 2).Trim()
if ($LASTEXITCODE -ne 0) { throw 'devloop-uninstall: could not acquire install lock' }
$arguments = @($transaction, 'uninstall', $InstallDir, $transactionId, '--protocol', '2')
if ($KeepSkills) { $arguments += '--keep-capabilities' }
else {
    $arguments += @('--skills-destination', $CodexSkillsPath)
    $arguments += @('--agents-destination', $CodexAgentsPath)
}
& $python @arguments
if ($LASTEXITCODE -eq 95) { [Environment]::Exit(95) }
if ($LASTEXITCODE -ne 0) { throw 'devloop-uninstall: core uninstall failed; no capability cleanup ran' }
Write-Host 'Dev Loop managed bootstrap and immutable releases were removed.'
Write-Host 'Portable catalog, project data, and legacy checkout content were preserved.'
