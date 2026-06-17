[CmdletBinding()]
param(
    [string] $CodexSkillsPath = "$env:USERPROFILE\.codex\skills",
    [string] $ClaudeAgentsPath = "$env:USERPROFILE\.claude\agents"
)

$ErrorActionPreference = 'Stop'
$bundleRoot = Split-Path -Parent $PSScriptRoot

$codexSource = Join-Path $bundleRoot 'skills\codex'
$claudeSource = Join-Path $bundleRoot 'agents\claude'

if (-not (Test-Path -LiteralPath $codexSource)) {
    throw "Codex skills source not found: $codexSource"
}

if (-not (Test-Path -LiteralPath $claudeSource)) {
    throw "Claude agents source not found: $claudeSource"
}

New-Item -ItemType Directory -Force -Path $CodexSkillsPath | Out-Null
New-Item -ItemType Directory -Force -Path $ClaudeAgentsPath | Out-Null

Copy-Item -Path (Join-Path $codexSource '*') -Destination $CodexSkillsPath -Recurse -Force
Copy-Item -Path (Join-Path $claudeSource '*.md') -Destination $ClaudeAgentsPath -Force

Write-Host "Installed Codex skills to $CodexSkillsPath"
Write-Host "Installed Claude agents to $ClaudeAgentsPath"

