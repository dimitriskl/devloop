# Interactive Plan-To-Dev Loop Runner

`devloop-plan` is the interactive front door for a new change. It opens one
continuous session: a Codex-backed planning chat for design and local
artifact creation, then development, review, and QA against the generated PRD
and issue pack, with the current stage always visible. No exit or Ctrl+C is
ever needed between stages — devloop watches the target checkout and moves
itself into development once the PRD and issue pack exist on disk.

> Product boundary: this document covers the portable `devloop-plan` and
> `devloop` wrappers. The separately installed `codexcli` Textual application
> is not their backend and has different commands and state. See
> `product-boundaries.md`.

> The interactive interface is the persistent
> [Portable Application Shell](portable-application-shell.md). Planning,
> configuration, delivery, post-run work, errors, and completion stay inside
> one mounted two-pane Textual application. `--plain` keeps deterministic
> line-oriented behavior for redirected or diagnostic runs.

## Windows PowerShell

```powershell
& 'F:\devloop\bin\devloop-plan.ps1' --repo 'C:\LocalCode\eConnectorV2'
```

Continue an existing PRD without reopening planning:

```powershell
& 'F:\devloop\bin\devloop-plan.ps1' --prd 'C:\LocalCode\eConnectorV2\prd\example\example.md'
```

## Ubuntu Or Mac

```bash
/path/to/devloop/bin/devloop-plan.sh --repo /path/to/project
```

## Start Or Resume

After the target checkout is selected, Dev Loop opens a replacing startup menu:

- **Start a new change**
- **Resume an unfinished PRD**
- **Workflow options** (same editor as `/options`)
- **Exit**

On an interactive terminal, use Up/Down and Enter to choose an item; Esc exits
or returns to the previous screen. Redirected terminals and explicit `--plain`
runs use the line-oriented fallback.

The resume catalog scans standard
`prd/<name>/<name>.md` plus `prd/<name>/issues/README.md` packages and supported
flat local issue packs. Fully completed packs are omitted. Entries include
completion counts, the active issue and status when known, and last activity.

Selecting a PRD skips branch selection and the analysis chat, prints its saved
status, and opens the usual DEVELOPMENT handoff. Nothing starts until the user
presses Enter there. The same catalog is available with `/resume` from an
already-open planning chat.

## Stage Pipeline

Every run moves through four stages:

```
analysis -> development -> review -> qa
```

The active stage is always shown two ways: as a banner (`devloop · analysis
-> development -> review -> qa`, with the current stage highlighted) and
inside the input prompt itself, `[stage] > `. The banner reprints before
every prompt, so the current stage survives any amount of scrolled Codex or
role output above it. Set `NO_COLOR=1` to turn off the banner highlight;
consoles that cannot encode the default `●`/`○`/`→` markers automatically
fall back to plain ASCII (`*`, `.`, `->`).

## The Chat Loop

Once the target checkout and branch strategy are chosen (current branch, a
new branch, or a new worktree — see below), devloop starts the `analysis`
stage and never releases the terminal to the user. Every message you type
runs one `codex exec resume` turn against the same Codex session; the first
turn starts the session with `codex exec`, and every turn after that resumes
it, so Codex keeps full context of the conversation. Codex's own output
streams to the terminal exactly as it is produced — there is nothing to page
through afterward.

You never need to quit anything to move forward. After each turn, devloop
checks the target checkout for a matching PRD and issue pack under
`prd/<change-name>/`. As soon as both exist, it prints "PRD and issue pack
detected; continuing to development." and flips straight to the DEVELOPMENT
handoff screen automatically.

## Screenshots: Alt+V Paste

Press PrintScreen, or Win+Shift+S on Windows, to put a screenshot on the
clipboard, then press Alt+V at the chat prompt. Devloop grabs the clipboard
image, saves it to a temp folder, and inserts a `[image N attached]` marker
into the line you are typing; the image is sent alongside that message on the
next Codex turn. You can also type or paste an image file path directly into
your message (for example when referencing a screenshot already saved to
disk) — devloop detects any token that resolves to an existing image file and
attaches it the same way, no marker needed.

Alt+V needs a real interactive terminal. On piped stdin or legacy consoles
where raw-mode key reading is unavailable, devloop prints a one-time hint
("Alt+V unavailable in this terminal; use /paste instead.") and falls back to
plain line input; use the `/paste` command to attach a clipboard image in
that mode.

Clipboard capture depends on tools already present on most machines:

- Windows: the built-in PowerShell and .NET clipboard APIs — nothing extra to
  install.
- Linux: `wl-paste` (Wayland) or `xclip` (X11).
- macOS: `pngpaste` (`brew install pngpaste`).

## Slash Commands

| Command | Description |
| --- | --- |
| Alt+V | attach a screenshot from the clipboard (use `/paste` if unavailable) |
| `/paste` | attach a screenshot from the clipboard |
| `/options` | open the Workflow Editor for defaults and resumable preferences |
| `/resume` | list unfinished PRDs and continue the selected handoff |
| `/status` | show the stage banner, artifacts, and selection summary |
| `/done` | detect the PRD and issue pack now (or enter paths manually) |
| `/help` | show this help |
| `/quit` | abort planning (never required to continue) |

## `/options`: Workflow Editor And Capabilities

Typing `/options` at any planning or development-handoff prompt opens the
transactional terminal Workflow Editor. The color-aware full-screen layout keeps
Workflow Steps in the left pane, the selected step's settings in the right
pane, feedback in the window, and common shortcuts in the bottom action bar.
Use Up/Down to select a Primary Path step, Enter or F9 for the complete action
list, F1 for help, F2 to apply, F3 for the route map, F4 for technical details,
F5 to add a step, F7 for capabilities, and Esc to cancel. Number keys and the
complete-word commands below remain the fallback on limited or redirected
terminals.
It honors `NO_COLOR=1` and uses ASCII borders when box drawing is unavailable.

Every finite choice—including models, reasoning effort, Fast mode, component
types, routes, bindings, and confirmations—opens as a replacing full-screen
menu with an explicit Back or Cancel row. Esc returns from these menus. The
complete action catalog is divided into named groups so only a short list is
shown at each level. Free-form values open their own input view.

The editor shows the Primary Path and the selected step's display name,
component type, and component-owned scope. Step
Instance IDs stay hidden unless `advanced` is selected. Use a step number to
change selection. `add` appends an installed component type, while `insert`
places one at a one-based Primary Path position. `move-up`, `move-down`, and
`position` reorder the `SUCCEEDED` path without changing Step Instance IDs;
displayed positions are always renumbered from one. New required inputs are
bound automatically only when one compatible upstream output exists. Missing
or ambiguous inputs stay visible and block `apply` for deliberate repair.
When a move places a producer after one of its consumers, the editor clears
that now-unexecutable binding and leaves the input explicitly unresolved; it
never keeps or guesses a runtime-invalid source.
Use `route` to choose a supported outcome and then target an existing step,
create a new branch step, insert a step on the route, or terminate that outcome
explicitly. The live text graph preview is derived from these structured
transitions and supports loops and branch-local steps without pretending they
have a Primary Path Position. Use `advanced` to display required and optional
typed Input Ports, compatible producers, current bindings, and repair errors;
use `bind` to select a producer or clear the binding. `apply` fails closed until
all routes and required bindings form an executable workflow with a start and
a successful terminal path.
`duplicate` creates a new identity and warns about outputs that still need
consumers. `delete` first previews affected transitions and bindings, requires
an explicit confirmation, never deletes downstream steps, and repairs only an
unambiguous Primary Path success link. `type` preserves the selected identity,
name, and position while resetting type-owned settings and showing the repair
work. All three operations remain draft-only and can be reverted with `undo`.
For Codex-backed steps, `model`, `reasoning`, and `fast` edit that Step
Instance's independent Codex Execution Settings. Model names come from every
page of the live installed Codex catalog; reasoning choices and Fast are
limited to capabilities advertised for the selected model. If discovery
fails, the editor marks its last cache as stale display-only data and offers
`retry-catalog`. A fresh catalog preflight is still required before execution,
and an unavailable model, effort, or Fast choice blocks the run without
substitution. Local deterministic steps instead state that Codex settings do
not apply.
Every step also shows an independent Execution Budget. Use `budget` to set its
overall timeout and checkpoint inactivity deadline without changing its model,
reasoning effort, or Fast choice. These limits are snapshotted with the
workflow and enforced for Analysis and each development role attempt.
The checkpoint deadline remains paused while Codex reports an active repository
command, MCP call, or web search; those operations remain bounded by the overall
timeout. After the operation completes, a fresh inactivity window begins.
If a role reaches either limit, Completion Review names the exact timeout
instead of showing only exit code 124. Repository changes already written
remain in the implementation worktree; rerun the unfinished issue to continue
from that partial work. Each role prompt also states its limits so Codex can
reserve time for focused verification and its required structured response.
Previously saved exit-code-124 summaries are upgraded when displayed, and
failure details retain and wrap their full sanitized log path instead of
truncating it to 100 characters.
When unfinished issues remain, Completion Review selects `Rerun unfinished
issues` by default, so Enter renews their exhausted scheduler budgets and
continues from changes already present in the worktree. Esc still selects
`Exit Dev Loop`. Moving the highlight only previews an action; the details,
status row, and action bar explicitly say that Enter confirms it.
Starting development for a resumed PRD also renews an already exhausted
scheduler before its first projection. The explicit Start development
confirmation therefore launches the unfinished work directly instead of
immediately returning to the previous Completion Review.
Each step owns an independent capability profile. Enter `capabilities` to
search and toggle installed Skills and Agent References for the selected Step
Instance. Required capabilities remain enabled and locked with the
component-contract reason; Reset restores only that component's defaults.
Enter `guidance` to edit optional bounded multiline Step Guidance. Secret-like
values are redacted before persistence, and the editor and generated prompt
state that component contracts, execution policy, permissions, output
requirements, required capabilities, and safety boundaries outrank guidance.
Guidance marked `NEEDS_REVIEW` must be explicitly kept, edited, or cleared
before Apply.
Use `rename`, `undo`, `reset-step`, or `reset-workflow` for the remaining draft
actions. `apply` atomically replaces the User Workflow Default; `cancel`
discards the workflow and capability-selection draft. Matching model,
reasoning-effort, Fast, and capability changes are adopted before the next
attempt when unfinished work is resumed.

When an existing implementation worktree contains loop state, the editor also
shows its read-only **Current Run** configuration. Enter `current` to inspect
it and `future` to return to the editable **Workflow Default**. Applying that
draft does not change a Codex turn already in progress. Stop and rerun to use
updated model, reasoning effort, Fast, Skills, or Agent References on the next
attempt. Workflow structure, bindings, budgets, and guidance remain fixed for
the existing run and apply only to new runs.

If live catalog discovery or exact-setting validation fails before an attempt,
the preflight prompt can open `/options`, run `retry-catalog`, and revalidate
without exiting to an unavailable command surface. A resumed run can therefore
repair its execution preferences while retaining the same workflow graph and
recovery cursor.

Enter `capabilities` to open the existing capability choices without leaving
the workflow draft:

1. **Search and toggle this Step Instance's capabilities** — filter installed
   Skills and Agent References and enable or disable replaceable entries.
2. **Reset this Step Instance to component capability defaults** — restores
   required and default capabilities without changing another instance.
3. **Add skill or agent from GitHub** — installs new skills or agents into
   the bundle; see `docs/skills-and-agents.md` for the exact format and rules.
4. **Back to Workflow Editor** — returns to the workflow draft. Capability
   selections are persisted together with `apply`.

Applied capability profiles and Step Guidance are stored in the User Workflow
Default. New runs snapshot both per Step Instance. Resumed runs refresh matching
capability profiles before the next attempt, while guidance remains part of the
original run definition. Saved attempt context records the capability profile
and guidance used by that attempt.

## Handoff To Development

When the PRD and issue pack are detected, devloop prints the DEVELOPMENT
banner and a one-screen summary: the PRD path, issue index path, which issues
will run, the implementation worktree path (or "disabled" if you chose not to
use one), the branch name, and a line confirming the self-improvement wiki is
always on (read before development, and updated after). Press Enter to start
development immediately with those defaults. Type `/options` to change the
User Workflow Default or inspect a reused worktree's Current Run snapshot. Type
`/run-options` to change the start issue, whether to run every pending issue,
whether to use a dedicated worktree, the worktree parent path and folder name,
or the branch name before starting. Type `/quit` to stop without starting
development.
Friendly branch names are normalized before Git runs, so `Reset Queue` becomes
`Reset-Queue`.
Entered worktree parent paths are remembered and offered as the next default.
If a selected worktree path already exists as a Git checkout, Dev Loop continues
from that checkout instead of trying to create it again.

Development, review, and QA then run in the same terminal, in-process —
devloop calls its own implementation runner directly rather than spawning a
new process. The shared dashboard renders every configured Workflow Step
instance by display name, including two instances backed by the same component.
Workflow-scoped rows are separated from current-Issue rows. Completed status,
pass, Last Result, and total elapsed time remain frozen while the active detail
shows model, reasoning effort, Fast, a spinner, current-attempt elapsed time,
event freshness, and safe activity. Rework adds time to the same step row
without making a fresh attempt appear to have inherited that historical
duration, and without overwriting older Step Attempt Records. Long workflows
keep the active row in view.
Interactive terminals update one bounded region; redirected output is
append-only and contains no cursor movement. `NO_COLOR=1`, narrow terminals,
and non-Unicode consoles retain complete text labels. The self-improvement
wiki update runs automatically at the end of the run.

The generated PRD is expected under `prd/<prd-stem>/`. The issue pack is
expected under `prd/<prd-stem>/issues/README.md` with real Markdown links to
numbered issue files. Loop state, logs, and other PRD execution artifacts
stay under the same PRD folder.

For each PRD run, Dev Loop writes `devloop.status.json` and
`devloop.status.md` in the PRD folder. It also keeps the compatibility files
next to the issue index: `issues/README.loop.state.json` and
`issues/README.loop.md`. When `devloop-plan --prd ...` is used, the wrapper
prints that status and then starts the development handoff screen directly.
Choosing to run all pending issues continues only blocked or unfinished
issues because completed issue files are skipped by the implementation
runner.

## Clean-Context Guarantee

Development, review, and QA roles never resume the planning chat session —
each one runs a fresh `codex exec` with no memory of the planning
conversation, so its full context window is spent on the issue at hand
instead of prior back-and-forth. This is why the planning prompt requires
every issue to be self-contained: goal, acceptance criteria, verification
steps, relevant file paths, and the specific PRD sections that apply, with no
"as discussed" references back to the chat. Screenshots that matter for
implementation are saved into the PRD folder during planning and linked by
relative path from the issues that need them.
