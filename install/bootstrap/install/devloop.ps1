[CmdletBinding()]
param(
    [string] $InstallDir,
    [string] $BinDir,
    [string] $RepoUrl,
    [string] $Ref,
    [switch] $NoSkills,
    [switch] $NoBinLinks,
    [switch] $Rollback,
    [switch] $Help
)
$ErrorActionPreference = 'Stop'
$arguments = @()
foreach ($entry in @(
    @('-BinDir', $BinDir),
    @('-RepoUrl', $RepoUrl),
    @('-Ref', $Ref)
)) {
    if (-not [string]::IsNullOrWhiteSpace($entry[1])) { $arguments += $entry }
}
if ($NoSkills) { $arguments += '-NoSkills' }
if ($NoBinLinks) { $arguments += '-NoBinLinks' }
if ($Rollback) { $arguments += '-Rollback' }
if ($Help) { $arguments += '-Help' }
& (Join-Path (Split-Path -Parent $PSScriptRoot) 'bootstrap\dispatch.ps1') -Command update -ArgumentsJson ($arguments | ConvertTo-Json -Compress)
exit $LASTEXITCODE
