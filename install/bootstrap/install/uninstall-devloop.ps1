[CmdletBinding()]
param(
    [string] $InstallDir,
    [string] $BinDir,
    [string] $CodexSkillsPath,
    [string] $CodexAgentsPath,
    [switch] $KeepSkills,
    [switch] $Help
)
$ErrorActionPreference = 'Stop'
$arguments = @()
foreach ($entry in @(
    @('-BinDir', $BinDir),
    @('-CodexSkillsPath', $CodexSkillsPath),
    @('-CodexAgentsPath', $CodexAgentsPath)
)) {
    if (-not [string]::IsNullOrWhiteSpace($entry[1])) { $arguments += $entry }
}
if ($KeepSkills) { $arguments += '-KeepSkills' }
if ($Help) { $arguments += '-Help' }
& (Join-Path (Split-Path -Parent $PSScriptRoot) 'bootstrap\dispatch.ps1') -Command uninstall -ArgumentsJson ($arguments | ConvertTo-Json -Compress)
exit $LASTEXITCODE
