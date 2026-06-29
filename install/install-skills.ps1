[CmdletBinding()]
param(
    [string] $CodexSkillsPath = "$env:USERPROFILE\.codex\skills",
    [string] $CodexAgentsPath = "$env:USERPROFILE\.codex\agents"
)

$ErrorActionPreference = 'Stop'
$bundleRoot = Split-Path -Parent $PSScriptRoot

$codexSource = Join-Path $bundleRoot 'skills\codex'
$codexAgentsSource = Join-Path $bundleRoot 'agents\codex'

if (-not (Test-Path -LiteralPath $codexSource)) {
    throw "Codex skills source not found: $codexSource"
}

if (-not (Test-Path -LiteralPath $codexAgentsSource)) {
    throw "Codex agent references source not found: $codexAgentsSource"
}

New-Item -ItemType Directory -Force -Path $CodexSkillsPath | Out-Null
New-Item -ItemType Directory -Force -Path $CodexAgentsPath | Out-Null

Copy-Item -Path (Join-Path $codexSource '*') -Destination $CodexSkillsPath -Recurse -Force
Copy-Item -Path (Join-Path $codexAgentsSource '*.md') -Destination $CodexAgentsPath -Force

Write-Host "Installed Codex skills to $CodexSkillsPath"
Write-Host "Installed Codex agent references to $CodexAgentsPath"
