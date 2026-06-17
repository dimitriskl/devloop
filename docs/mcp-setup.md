# MCP Setup

The loop itself does not require MCP servers. The bundle includes optional MCP
setup files for repositories that benefit from SQL diagnostics or framework
documentation lookup.

## SQL Diagnostics MCP

Source is copied to:

```text
mcp/sql_diagnostics/
```

Build it:

```powershell
.\install\build-sql-mcp.ps1
```

Create a local config from the example:

```powershell
.\install\build-sql-mcp.ps1 -CreateLocalConfig
```

Edit `appsettings.local.json` with local read-only SQL connection strings. Do
not commit or share the real config file.

Add the matching snippet from:

```text
mcp/templates/codex-config-snippet.windows.toml
```

to your Codex `config.toml`, then replace the placeholder paths.

Ubuntu/Linux:

```bash
chmod +x ./install/build-sql-mcp.sh
./install/build-sql-mcp.sh --create-local-config
```

## Context7

The target application skills ask agents to use Context7 when it is available. This
bundle does not vendor Context7. Install it from its official instructions and
add a local Codex MCP server entry. A placeholder template is provided at:

```text
mcp/templates/context7-placeholder.toml
```

