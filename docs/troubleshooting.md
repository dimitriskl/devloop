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
local issue Markdown files under the same issue folder. Links outside that
folder, such as `../PRD.md`, are ignored and will not be selected as issues.

## The runner returns to the shell with no visible progress

Check the loop state files next to the issue README:

- `README.loop.md`
- `README.loop.state.json`
- `.loop.logs/*stderr*`

Recent versions print selected issues, role status, and blocked summaries to
the terminal. If a run still looks silent, open `README.loop.md` first.

## The runner crashes with FileNotFoundError under `.loop.logs`

A coder pass may delete `.loop.logs` after a reviewer asks to remove devloop
artifacts from the change set. Update devloop and rerun; newer builds recreate
the log folder before each role write and tell agents not to touch runner files.

## Codex exec fails immediately

Older devloop builds passed `-a` to `codex exec`. Codex CLI 0.140+ expects
approval policy via config instead, for example:

```bash
codex exec -c 'approval_policy="never"' ...
```

If stderr mentions `unexpected argument '-a'`, update devloop and rerun.

## Codex returns invalid JSON

The runner marks the role `BLOCKED`. Inspect `.loop.logs/*last-message*`,
`.loop.logs/*stdout*`, and `.loop.logs/*stderr*`.

## Codex output crashes with cp1253 UnicodeDecodeError

Older builds let Python decode captured Codex output with the active Windows
console code page. If stderr mentions `encodings\cp1253.py` followed by
`NoneType found`, update devloop and rerun. Captured subprocess output should be
decoded as UTF-8 with replacement.

## Self-improvement wiki update fails

The post-run self-improvement compiler is non-fatal. The issue loop result still
stands, and the failure is recorded in `README.loop.state.json` under
`self_improvement_wiki`.
Inspect
`docs/devloop-self-improvement/.compiler-runs/self-improvement-compiler.stderr.txt`
in the Dev Loop bundle and rerun with `--no-self-improvement-wiki` if you need
a run with no documentation changes.

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
