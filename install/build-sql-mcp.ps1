[CmdletBinding()]
param(
    [switch] $CreateLocalConfig
)

$ErrorActionPreference = 'Stop'
$bundleRoot = Split-Path -Parent $PSScriptRoot
$mcpRoot = Join-Path $bundleRoot 'mcp\sql_diagnostics'
$project = Join-Path $mcpRoot 'DevLoop.SqlDiagnosticsMcp.csproj'

if (-not (Test-Path -LiteralPath $project)) {
    throw "SQL diagnostics MCP project not found: $project"
}

dotnet build $project -c Release

if ($CreateLocalConfig) {
    $example = Join-Path $mcpRoot 'appsettings.local.example.json'
    $local = Join-Path $mcpRoot 'appsettings.local.json'
    if (-not (Test-Path -LiteralPath $local)) {
        Copy-Item -LiteralPath $example -Destination $local
        Write-Host "Created $local. Edit it with local read-only SQL connection strings."
    }
}


