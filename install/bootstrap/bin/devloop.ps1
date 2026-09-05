[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]] $RemainingArgs)
$ErrorActionPreference = 'Stop'
& (Join-Path (Split-Path -Parent $PSScriptRoot) 'bootstrap\dispatch.ps1') -Command devloop -ArgumentsJson ($RemainingArgs | ConvertTo-Json -Compress)
exit $LASTEXITCODE
