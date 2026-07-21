# How To Use Dev Loop

```text
+------------------------------------------------------------+
|  ____  _______     __  _     ___   ___  ____              |
| |  _ \| ____\ \   / / | |   / _ \ / _ \|  _ \             |
| | | | |  _|  \ \ / /  | |  | | | | | | | |_) |            |
| | |_| | |___  \ V /   | |__| |_| | |_| |  __/             |
| |____/|_____|  \_/    |_____\___/ \___/|_|                |
|                                                            |
|    [ ANALYSIS ] => [ BUILD ] => [ REVIEW ] => [ QA ]      |
+------------------------------------------------------------+
```

Dev Loop is a portable local runner that turns a PRD and Markdown issue pack
into a repeatable Codex implementation loop. It has two entrypoints:

- `devloop-plan` starts from an idea, runs the planning chat, creates PRD and
  issue artifacts, then hands off to development.
- `devloop` runs implementation, senior review, and QA from an existing PRD and
  issue index.

These wrappers are the **Portable Dev Loop** product documented here. The same
repository also contains a separate installable Textual application named
`codexcli`. It has different commands, persistence, UI, and workflow internals;
it is not started or resumed by these wrappers. See `product-boundaries.md` for
the authoritative module and command map.

## 1. Install And Verify

Required tools:

- Python 3.10 or later.
- Codex CLI installed and authenticated.
- Git.
- .NET SDK only if the target repo or optional SQL diagnostics MCP needs it.

Verify the wrappers:

```powershell
.\bin\devloop-plan.ps1 --help
.\bin\devloop.ps1 --help
```

On Ubuntu or macOS:

```bash
chmod +x ./bin/devloop-plan.sh ./bin/devloop.sh
./bin/devloop-plan.sh --help
./bin/devloop.sh --help
```

For new-machine setup, use `docs/new-pc-setup.md` and the platform install
notes in `docs/install-windows.md`, `docs/install-ubuntu.md`, or
`docs/install-macos.md`.

## 2. Choose The Right Entrypoint

Use `devloop-plan` when you have an idea, bug, screenshot, or rough request and
want Dev Loop to create the PRD and issues.

Use `devloop` when the PRD and issue pack already exist, usually as:

```text
prd/<change-name>/<change-name>.md
prd/<change-name>/issues/README.md
prd/<change-name>/issues/0001-something.md
```

The issue `README.md` must contain real Markdown links to local issue files.
Bare filenames in backticks are not enough for selection.

## 3. Plan Then Build

Windows:

```powershell
.\bin\devloop-plan.ps1 --repo C:\path\to\project
```

Ubuntu or macOS:

```bash
./bin/devloop-plan.sh --repo /path/to/project
```

Useful planning flags:

- `--repo <path>` sets the target project checkout.
- `--prd <file-or-folder>` resumes an existing PRD and skips planning.
- `--goal "<text>"` seeds the first planning message.
- `--codex <command>` chooses the Codex executable.
- `--sandbox <mode>` passes the Codex sandbox mode. Default:
  `workspace-write`.
- `--approval-policy <mode>` sets planning approvals: `never`, `on-request`,
  `untrusted`, or `on-failure`. Default: `never`.

If `--repo` is omitted, Dev Loop asks for the target checkout. It remembers the
last valid target in the user config folder. If the selected folder does not
exist, Dev Loop can create it and initialize Git after asking.

When no `--goal` or `--prd` is supplied, startup opens a replacing terminal menu:

1. **Start a new change**.
2. **Resume an unfinished PRD**.
3. **Workflow options** — opens the same Workflow Editor as `/options`.
4. **Exit** — leave planning without starting a session.

Each submenu replaces the previous screen instead of appending to it. The resume
list contains only issue packs with unfinished issues. Each entry
shows completed and remaining counts, the active issue/status when a loop-state
file exists, and last activity. Selecting one opens the normal development
handoff; completed issues remain skipped and an interrupted active issue resumes
at its next unfinished coder, reviewer, or QA gate.

The planning session runs in the `analysis` stage. It uses the planning skills
to clarify the request, write the PRD, and write the issue pack. Dev Loop watches
the target checkout. When the PRD and issue index exist, it shows the
development handoff screen.

## 4. Stage Pipeline

The built-in workflow currently follows this Primary Path:

```text
analysis => development => Security Review => Final Review => qa
```

This is a configurable workflow, not a fixed phase list. The Workflow Editor
can add, duplicate, remove, rename, or reorder instances, including multiple
instances of the same Step Type. Each instance keeps its own identity,
execution settings, capabilities, guidance, runtime state, and attempt history.

Press Enter on the development summary to start with the shown defaults. Use
`/run-options` to adjust this launch before starting, or `/options` to edit the
User Workflow Default for Future Runs.

Inside the Workflow Editor, select a Codex-backed step and use `model`,
`reasoning`, or `fast` to choose its execution settings. Choices are filtered
by the live Codex Model Catalog. `retry-catalog` retries temporary discovery
failures; any stale cache is labeled display-only and cannot authorize a run.
Run startup refreshes the catalog and stops with the exact step and unsupported
setting instead of silently falling back. Fast Off is passed explicitly, so a
global Codex `/fast` setting cannot change the saved step choice.
Use `budget` to edit the selected step's separate timeout and checkpoint
inactivity deadline. Budget changes never alter model, reasoning, or Fast.
When preflight fails interactively, choose `/options` to repair a future-run
default or `retry-catalog` to retry live discovery before execution.

Use `route` to edit any supported outcome. A route can target an existing
step, create a branch step, insert a step before the current destination, or
terminate explicitly. The text graph preview updates after each draft change.
Use `advanced` to inspect typed Input Ports and current Port Bindings, then
`bind` to select a compatible upstream output or clear a binding. Dev Loop
auto-binds only when exactly one executable compatible producer exists.
`apply` remains blocked while a start, successful terminal path, route, scope,
or required binding is invalid. `delete` previews every affected transition
and binding and never cascades into downstream deletion; repair any unresolved
references explicitly before applying.

The shared dashboard lists every configured instance separately. Workflow and
current-Issue rows are separated, completed timers freeze, rework time
accumulates on the same row, and the active line includes model, reasoning,
Fast, elapsed time, event freshness, and safe activity. Interactive terminals
reuse one bounded region; redirected output is append-only. `NO_COLOR=1`
disables color, and terminals that cannot encode Unicode use ASCII markers
without losing status labels.

## 5. Planning Chat Commands

Inside `devloop-plan`, these commands are available:

| Command | Meaning |
| --- | --- |
| Alt+V | Attach a clipboard screenshot in a real interactive terminal. |
| `/paste` | Attach a clipboard screenshot when Alt+V is unavailable. |
| `/options` | Open the Workflow Editor for future-run defaults and capability choices. |
| `/resume` | List unfinished PRDs and continue the selected development handoff. |
| `/status` | Show the stage banner, artifacts, and current selection. |
| `/done` | Detect PRD/issues now or enter artifact paths manually. |
| `/help` | Print chat help. |
| `/quit` | Stop planning without starting development. |

Screenshots saved during planning are linked from the PRD or issues when they
matter for implementation.

## 6. Run An Existing PRD

Run the first pending issue:

```powershell
.\bin\devloop.ps1 --prd E:\repo\prd\feature\feature.md --issues E:\repo\prd\feature\issues\README.md
```

Run every pending or blocked issue:

```powershell
.\bin\devloop.ps1 --prd E:\repo\prd\feature\feature.md --issues E:\repo\prd\feature\issues\README.md --all
```

Start from a specific issue:

```powershell
.\bin\devloop.ps1 --prd E:\repo\prd\feature\feature.md --issues E:\repo\prd\feature\issues\README.md --start-issue 0004
```

Preview prompts without invoking Codex or modifying issues:

```powershell
.\bin\devloop.ps1 --prd E:\repo\prd\feature\feature.md --issues E:\repo\prd\feature\issues\README.md --dry-run --no-worktree
```

The Linux/macOS command is the same shape with `./bin/devloop.sh` and Unix
paths.

## 7. `devloop` Flags

Core inputs:

- `--prd <file>` is the parent PRD Markdown file.
- `--issues <README.md>` is the issue index.
- `--preset <json>` selects role agents and skills. Relative paths resolve from
  the Dev Loop bundle. Default: `presets/generic-minimal.json`.

Issue selection and dependencies:

- `--all` runs all dependency-ready blocked or unfinished issues.
- `--start-issue <number-or-prefix>` starts at an issue number or filename
  prefix. Preflight rejects a selection that omits an unfinished prerequisite.
- `--max-passes <n>` controls coder/review/QA correction passes per issue.
  Default: `3`.

Declare dependencies only with local Markdown links inside `## Blocked by` in
the dependent issue. The issue index controls deterministic priority among
ready issues, not graph edges. Preflight rejects unknown, out-of-pack,
duplicate, self-referential, or cyclic dependencies before Codex starts.

Scheduling and Blocker Resolution:

- A prerequisite is satisfied only when its complete configured workflow is
  `COMPLETED`.
- `WAITING_ON_DEPENDENCY` issues do not invoke Codex or spend attempt budget.
- Independent dependency-ready work continues when another branch blocks.
- Blocker Resolution starts only after no ready issue has unused normal budget.
- `--blocked-retry-rounds <n>` lowers the default five additional passes per
  ready blocker; values above five are capped at five.
- `--blocked-retry-max-passes` is retained for command compatibility. Every
  Blocker Resolution attempt is exactly one workflow pass.
- `--no-blocked-retry` disables Blocker Resolution.

Codex execution:

- `--codex <command>` chooses the Codex executable.
- `--sandbox <mode>` sets Codex sandbox mode. Default: `workspace-write`.
- `--approval-policy <mode>` sets Codex approvals: `never`, `on-request`,
  `untrusted`, or `on-failure`. Default: `never`.

Worktrees:

- `--create-worktree` creates or reuses a dedicated implementation worktree.
- `--no-worktree` runs in the current checkout.
- `--worktree-path <path>` sets the implementation worktree path.
- `--branch-name <name>` sets the implementation branch name.
- `--non-interactive` prevents prompts for missing worktree decisions.

Self-improvement wiki:

- `--self-improvement-wiki` reads and updates the wiki. This is the default.
- `--no-self-improvement-wiki` disables wiki reading and update.
- `--self-improvement-wiki-path <bundle-relative-path>` chooses another wiki
  folder.
- `--self-improvement-max-lessons <n>` limits durable lessons added after a run.
  Default: `5`.

## 8. Worktree Behavior

Interactive development defaults to a dedicated implementation worktree. Dev
Loop asks for the worktree parent path, worktree folder name, and branch name
when needed. If the same final worktree path is already registered on the
requested branch, rerunning the command reuses it. Dev Loop also reuses an
existing Git checkout even when its current branch differs from the newly typed
branch prompt, and if a previous partial attempt already created the branch, it
runs `git worktree add` with the existing branch instead of trying to create it
again.
Branch names are normalized before Git runs, so a friendly name like
`Reset Queue` becomes `Reset-Queue`.
When you enter a worktree parent path, Dev Loop remembers it and suggests it as
the default parent next time.

Dev Loop does not push. After a successful run, it asks whether to merge the
implementation branch or worktree into another branch. It skips automatic merge
when the source or target checkout has uncommitted changes.

Use `--no-worktree` for small or already-isolated runs where you want changes in
the current checkout.

## 9. State, Logs, And Completion

During development, Dev Loop writes state beside the issue index:

- `README.loop.md`
- `README.loop.state.json`
- `.loop.logs/`

For PRD-folder runs, it also writes:

- `devloop.status.md`
- `devloop.status.json`

Each new run stores an immutable resolved `devloop.portable-workflow/v2`
snapshot and canonical hash in the JSON state. Editing Future Runs never
changes that snapshot. The same state keeps generic Step Runtime States and an
ordered Step Attempt Record for every execution, so duplicate reviews,
changes-requested rework, interruption, and resume remain inspectable. Portable
workflow schema v1 is intentionally rejected; repair or recreate an old local
default in `/options` rather than expecting migration or compatibility mode.
When preflight finds a schema-v1 or malformed schema-v2 User Workflow Default,
`/options` opens a fail-closed recovery mode instead of loading the rejected
content as a draft. Use `reset-workflow` and then `apply` to atomically replace
it with the built-in v2 default. `cancel` leaves the stored configuration
unchanged.

Dependency scheduler state is stored in the same JSON file. It includes ready
and waiting projections, normal attempts, per-issue additional-pass counters,
and the active scheduling reservation. Rerunning the same command resumes that
reservation without double charging it. Usage exhaustion, authentication
failure, or Codex service unavailability pauses the entire run as `RUN PAUSED`;
restore the backend condition and rerun the same command to continue the exact
issue, workflow step, pass, scheduling phase, and Blocker Resolution round.

Completed issue files are updated in place with `Completed: [x]`, checked
acceptance criteria, and implementation notes. Completed issues are skipped on
future runs. `--all` continues only blocked or unfinished issues.

## 10. Skills, Agents, And Presets

Bundled skills live under `skills/codex/`. Agent reference files live under
`agents/codex/`. The runner can read these bundled copies directly, so global
installation is optional.

Use `/options` during planning to open the Workflow Editor, then enter
`capabilities` to search and toggle Skills and Agent References for the selected
Step Instance, reset its component defaults, or install new capabilities from
GitHub. Duplicate component types can therefore use different profiles.
Required capabilities are locked with the component-contract reason. Use
`guidance` to add bounded multiline Step Guidance; secrets are redacted before
Apply and the displayed precedence keeps contracts, permissions, execution
policy, safety, and output requirements authoritative. GitHub installs accept
repository URLs with an optional `#subpath`, then copy approved skill folders
and agent Markdown files into the bundle without overwriting existing names.

The default implementation preset is `presets/generic-minimal.json`, which runs
coder, senior review, and QA roles with the bundled guidance.

## 11. Self-Improvement Wiki

Real runs read and update `docs/devloop-self-improvement/wiki/` by default. The
wiki is for durable lessons only: user workflow preferences, reusable repo
patterns, recurring bugs, verification lessons, and environment fixes.

Do not store secrets, raw logs, connection strings, or one-off debug dumps in
the wiki. Dry runs do not update it.

## 12. Optional SQL Diagnostics MCP

The optional read-only SQL diagnostics MCP server is under
`mcp/sql_diagnostics/`. Build it only when needed:

```powershell
.\install\build-sql-mcp.ps1
```

Use `docs/mcp-setup.md` for the Codex config snippet and local setup notes.

## 13. Troubleshooting

Common checks:

- Python missing: install Python 3.10+ and make sure `python --version` works.
- Codex missing: run `codex --version` and authenticate Codex CLI.
- No issues selected: confirm the issue README contains real Markdown links and
  the linked files exist.
- No visible progress: inspect `README.loop.md`, `README.loop.state.json`, and
  `.loop.logs/`.
- Worktree creation failed: run `git worktree list` and check whether the path
  or branch already exists.
- Wiki update failed: the run result still stands; inspect the compiler stderr
  under `docs/devloop-self-improvement/.compiler-runs/`.

See `docs/troubleshooting.md` for more specific failure modes.
