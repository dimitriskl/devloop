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

## The runner says the PRD targets codexcli

The repository contains two separate applications. `devloop.sh` and
`devloop.ps1` intentionally refuse a PRD or linked issue whose `Target Product`
explicitly names the separate `codexcli` application. Use the correct CodexCLI
workflow, or correct the PRD and issue pack to target `devloop-plan + devloop`.
Do not remove the target merely to bypass the check; confirm the owning modules
in `docs/product-boundaries.md` first.

## The runner returns to the shell with no visible progress

Check the loop state files next to the issue README:

- `README.loop.md`
- `README.loop.state.json`
- `.loop.logs/*stderr*`

Recent versions print selected issues, role status, and blocked summaries to
the terminal. If a run still looks silent, open `README.loop.md` first.

## The Workflow Editor cannot apply a draft

Read the validation message together with the selected Step Display Name and,
under `advanced`, its Step Instance ID. Use the live graph preview to repair an
unsupported or unreachable `route`. Use `advanced` and `bind` to repair a
missing, incompatible, ambiguous, or no-longer-upstream Input Port producer.
Dev Loop chooses a producer automatically only when exactly one compatible
source is executable on every path to the consumer.

After `delete`, repair every binding sourced from the deleted step; downstream
steps are intentionally retained. After `duplicate`, bind consumers to the new
outputs only when that is deliberate. After `type`, review reset ports,
settings, capabilities, outcomes, and any preserved guidance. Guidance marked
`NEEDS_REVIEW` must be kept, edited, or cleared before `apply`.

## A portable workflow default reports schema v1 or malformed schema v2

Portable Dev Loop intentionally accepts only `devloop.portable-workflow/v2`.
There is no v1 reader, migration, or dual-write path. From planning or
implementation preflight, open `/options`; the editor enters a fail-closed
recovery mode and does not load rejected content as an editable draft. Choose
`reset-workflow` and then `apply` to atomically replace the invalid default with
the built-in v2 workflow. Choose `cancel` to leave the stored configuration
byte-for-byte unchanged. You may instead repair the local JSON outside a
running Current Run. Malformed UUIDs, duplicate names, unknown Step Types,
invalid routes, scopes, bindings, and unknown fields fail closed rather than
being ignored.

## Model discovery or execution preflight fails

Confirm the installed Codex CLI is authenticated, then use `retry-catalog` in
the Workflow Editor. A stale cache is display-only and cannot authorize a run.
If preflight names a Step Display Name and model, reasoning effort, or Fast
setting, edit that exact Future Runs step in `/options` and retry. Dev Loop does
not substitute another model, lower effort, or disable Fast silently. An
already-started Current Run is immutable; it can retry live discovery but
cannot adopt Future Runs edits.

## Dashboard rows wrap, lack color, or repeat in redirected output

Widen the terminal when possible. Narrow layouts window long workflows while
keeping the active instance visible. `NO_COLOR=1` intentionally removes color,
but PASS, FAIL, BLOCKED, WORKING, and WAITING text remains authoritative.
Redirected output is intentionally append-only, so repeated snapshots preserve
history without cursor-control sequences. If dynamic content appears unsafe or
garbled, update Dev Loop; current releases sanitize backend activity and fall
back to ASCII when the output encoding cannot represent Unicode.

## A restarted run begins again at coder pass 1

Update Dev Loop and rerun the same `--prd` and `--issues` command. Current
builds preserve `README.loop.state.json` and resume an in-progress issue at the
next unfinished role and pass. If an older build already overwrote the state
history, the runner recovers normal coder/reviewer/QA results from `.loop.logs`
when possible. Completed issue files remain skipped, including when an existing
implementation worktree is reused.

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

If the exact same worktree path is already registered on the requested branch,
rerun the same command. Current builds reuse that worktree instead of trying to
create the branch again.

If Git reports that the branch already exists but the worktree path is not
registered, either choose a new branch name or remove/rename the old branch
yourself before rerunning.

If the path is registered on a different branch, choose a different worktree path
or rerun with the branch name that matches that checkout.

## MCP does not start

Check the generated Codex config snippet paths and verify the SQL MCP builds:

```powershell
dotnet build .\mcp\sql_diagnostics\DevLoop.SqlDiagnosticsMcp.csproj -c Release
```
