# Troubleshooting

## Python is missing

Install Python 3.10 or later. On Windows, disable the broken Microsoft Store
alias if `python --version` starts a missing app installer instead of Python.

## Codex is missing

Install Codex CLI, sign in, and verify:

```powershell
codex --version
```

## The runner cannot find issues

The `--issues` path must point to a Markdown README/index containing links to
local issue Markdown files.

## Codex returns invalid JSON

The runner marks the role `BLOCKED`. Inspect `.loop.logs/*last-message*`,
`.loop.logs/*stdout*`, and `.loop.logs/*stderr*`.

## Worktree creation fails

Run:

```powershell
git worktree list
git status --short --branch
```

Make sure the branch name is unique and the target directory does not already
contain another checkout.

## MCP does not start

Check the generated Codex config snippet paths and verify the SQL MCP builds:

```powershell
dotnet build .\mcp\sql_diagnostics\DevLoop.SqlDiagnosticsMcp.csproj -c Release
```


