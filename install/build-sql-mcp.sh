#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MCP_ROOT="$BUNDLE_ROOT/mcp/sql_diagnostics"

dotnet build "$MCP_ROOT/DevLoop.SqlDiagnosticsMcp.csproj" -c Release

if [[ "${1:-}" == "--create-local-config" ]]; then
  if [[ ! -f "$MCP_ROOT/appsettings.local.json" ]]; then
    cp "$MCP_ROOT/appsettings.local.example.json" "$MCP_ROOT/appsettings.local.json"
    printf 'Created %s. Edit it with local read-only SQL connection strings.\n' "$MCP_ROOT/appsettings.local.json"
  fi
fi


