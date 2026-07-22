# Portable Application Shell

Status: implemented; installer-created runtime validation remains a release gate.

Target product: devloop-plan + devloop only. This design does not change or
merge the separately installed CodexCLI application.

## Outcome

An interactive Dev Loop session is one full-screen terminal application from
startup until exit. Planning, resume, workflow editing, handoff, development,
review, QA, blocker resolution, self-improvement, errors, and completion all
appear inside one persistent outer frame. The terminal transcript never grows
while the application is running.

The same workflow remains usable in automation through Portable Plain Mode.

## Non-Negotiable Invariants

1. Exactly one application owns the interactive terminal and alternate screen.
2. The outer header, two-pane body, status row, and action bar remain mounted
   for the complete session.
3. A view transition replaces the current content; it never appends another
   panel or prompt.
4. Moving the left-pane selection immediately refreshes the right-pane
   Portable Selection Preview.
5. Planning, workflow, state, worktree, Codex, scheduler, and post-run code do
   not call terminal input or output directly.
6. Codex and repository commands run outside the Textual UI thread and publish
   typed progress back into the shell.
7. Raw command and agent output is captured in durable logs and never escapes
   below the application.
8. Existing PRD files, issue packs, workflow definitions, resume behavior, and
   loop-state formats remain authoritative.
9. Portable Dev Loop never imports CodexCLI UI, App Server, Run Store, or
   Workflow Run state.
10. Non-TTY and explicit plain execution emits no cursor movement, color
    control, or alternate-screen sequences.

## Standard Screen

    ┌ Dev Loop ─ context path ─ project ─ branch/worktree ───────────────┐
    │ Navigation / selection       │ Selected details / active work      │
    │                               │                                     │
    │ > highlighted item           │ Immediate selection preview         │
    │   next item                  │                                     │
    │   next item                  ├ Activity ───────────────────────────┤
    │                               │ Latest meaningful running activity  │
    ├───────────────────────────────┴─────────────────────────────────────┤
    │ Status: workflow · issue · step · attempt · elapsed · health       │
    ├─────────────────────────────────────────────────────────────────────┤
    │ F1 Help  F2 Primary  F3 View  F4 Logs  F5 Context  F9 Actions     │
    └─────────────────────────────────────────────────────────────────────┘

The frame is stable; labels and pane content change with context. The activity
region is part of the right pane and may collapse when nothing is running.

### Layout Rules

- Minimum supported application size is 80 columns by 24 rows.
- At 80 columns, the left pane is 28 columns and the right pane receives the
  remaining width. Wider terminals use approximately 35 percent for the left
  pane, capped at 42 columns.
- Below the minimum size, the mounted shell shows a Terminal Too Small view
  with the required and current dimensions. It does not fall back to printing.
- Lists and documents scroll only inside their panes. Terminal history does
  not scroll.
- The highlighted row stays visible as a list moves. Selection and internal
  scroll positions survive help, log, and confirmation overlays.
- Long values use an ellipsis in summaries and open in a bounded viewer for
  their complete content.
- Borders and labels use ASCII when Unicode box drawing is unavailable.
- State is never communicated by color alone.

## Portable View Catalog

| Portable View | Left pane | Right pane |
| --- | --- | --- |
| Target project | Recent/default targets and actions | Highlighted path, Git status, validation, and create/init choices |
| Startup | New change, resume, workflow options, exit | Explanation and consequences of the highlighted action |
| Resume | Unfinished PRDs | Selected PRD paths, counts, active issue, saved status, and last activity |
| Planning | Workflow position and discovered artifacts | Composer, agent response, artifacts, and bounded activity |
| Branch/worktree | Available strategies or choices | Selected checkout, branch, worktree path, and validation |
| Workflow Editor | Workflow Steps | Selected settings, ports, routes, capabilities, guidance, and validation |
| Catalog/capabilities | Searchable capability list | Highlighted capability details and selection state |
| Development handoff | Launch options | PRD, issue selection, workspace, branch, workflow snapshot, and wiki policy |
| Running workflow | Workflow Steps and Issues | Selected Issue/Step details, attempts, result, and live Portable Activity Feed |
| Blocker resolution | Blocked and dependency-waiting Issues | Retry budget, dependency chain, attempt history, and current work |
| Post-run tasks | Finalization tasks including wiki update | Current task activity, outcome, and diagnostic link |
| Completion review | Rerun unfinished issues (when available), exit | Explicit finished state, completed/remaining counts, per-issue outcomes, loop-state path, and selected next action |

Help, action menus, confirmations, free-form input, errors, and the log viewer
open as overlays inside the same application. They never replace the shell or
write beneath it.

Direct devloop invocation enters the same shell at the handoff, preflight, or
running-workflow view appropriate to its arguments and saved state.

## Interaction Contract

### Global Keys

| Key | Behavior |
| --- | --- |
| Up/Down | Move within the focused finite list and refresh the preview |
| Left/Right | Navigate a tree/tab or change pane-local horizontal position |
| Tab/Shift+Tab | Move focus between visible panes and controls |
| Enter | Open or confirm the highlighted action |
| Esc | Close an overlay or select the explicit Back/Cancel path |
| F1 | Contextual help |
| F2 | Contextual primary action such as Apply or Start |
| F3 | Toggle the contextual alternate view |
| F4 | Open the filtered log viewer |
| F5 | Contextual secondary action such as Add or Retry |
| F9 | Open the complete contextual Actions menu |
| Ctrl+C | Open the context-aware stop dialog while work is running |

F10 is not required because desktop terminals may reserve it. Exit is an
explicit launcher/final-view action and may also use Ctrl+Q when no destructive
operation is pending.

### Choice And Input Rules

- Every finite interactive choice is an arrow-key list. TTY users never type a
  number or a word such as cancel.
- Every subordinate list contains an explicit Back or Cancel item, and Esc
  selects that path.
- Free-form text uses a dedicated composer or input overlay with its prompt,
  current context, validation, and shortcuts visible.
- Planning retains multiline editing, history, clipboard-image attachment, and
  Alt+V behavior.
- Searchable lists filter as the user types without losing the current preview.
- Mouse interaction may be supported but is never required.

## Activity And Logs

The Portable Activity Feed answers what Dev Loop is doing now. It is not a raw
event dump.

- Repeated low-value events are coalesced. For example, many repository-command
  events become one running command entry plus its terminal outcome.
- Each visible activity item identifies its Workflow Step, Issue when
  applicable, attempt, action, elapsed time, and current outcome.
- Spinner and elapsed time update in place.
- A failure remains visible until acknowledged or superseded by an explicit
  retry.
- The latest important event is visible in the normal view. F4 opens complete
  durable logs filtered to the selected Issue, Step, attempt, or post-run task.
- The log viewer supports follow mode, scrolling, search, copy, and switching
  between summary, stdout, stderr, and structured events when available.
- Codex JSONL, Git output, worktree commands, validation commands, and
  self-improvement output are captured. No child process inherits the
  application's terminal output streams.
- Existing sanitization and secret-redaction rules apply before activity or
  persisted evidence is displayed.

The self-improvement wiki update is presented as a bounded post-run task. Its
repeated tool events are summarized, and invalid structured output becomes an
actionable result inside the final view rather than a scrolling error block.

## Background Work And Terminal Lifecycle

- Textual owns all rendering and input on its application thread.
- Blocking orchestration and subprocess work runs through background workers.
- Workers publish typed events through a thread-safe queue; they never mutate
  widgets directly.
- UI actions emit typed intents to a controller. Widgets do not perform
  workflow, Git, state, or Codex operations.
- Ctrl+C opens a stop dialog instead of terminating the renderer. Available
  choices reflect what the current operation can safely do.
- Recoverable failures appear in an error overlay with Retry, Details, Back, or
  Exit actions as appropriate.
- An unhandled error restores the terminal before reporting one concise startup
  or crash message. Normal interactive exit restores the previous terminal
  screen without printing the application's transcript.

## Presentation Architecture

    devloop-plan / devloop wrappers
                    │
             UI mode resolver
              ┌─────┴─────┐
              │           │
       Textual adapter   Plain adapter
              └─────┬─────┘
                    │
       framework-neutral presentation port
                    │
      portable controller and existing services
                    │
       workflow / state / Codex / Git / wiki

### Contracts

The framework-neutral presentation boundary contains:

- Immutable Portable Shell and Portable View models.
- Typed user intents for navigation, selection, editing, confirmation, start,
  retry, stop, and exit.
- Typed activity events for lifecycle changes, commands, Codex work,
  scheduling, persistence, post-run tasks, notices, and failures.
- Input requests containing prompt, validation, sensitivity, default, and
  cancellation behavior.
- A cancellation-aware operation handle for background work.

The durable workflow and loop-state models remain unchanged. Presentation
models are projections and are never persisted as a second source of truth.

### Recommended Module Ownership

- src/devloop/portable_presentation.py owns framework-neutral models, intents,
  events, and presentation protocols.
- src/devloop/portable_controller.py owns transitions between Portable Views
  and coordinates existing portable services.
- src/devloop/portable_ui/ owns the Textual app, shell, widgets, overlays,
  theme, and view adapters.
- src/devloop/plain_ui.py owns Portable Plain Mode.
- src/devloop/ui/ remains CodexCLI-only and is never imported by portable UI
  code.

interactive_runner.py and cli.py remain entrypoint/orchestration seams while
their direct printing and prompting moves behind the presentation port.
chat_loop.py, worktree.py, codex_runner.py, workflow_editor.py, and post-run
logic publish events or return results instead of owning terminal behavior.

The existing Workflow Progress Dashboard projection may be adapted into a
Portable View model. Its ANSI cursor writer is not used by the Textual adapter;
safe append-only formatting may remain available to Portable Plain Mode.

## UI Mode Selection

The launchers select one UI mode for the complete process lifetime. On Windows
they use `[Console]::IsInputRedirected` and `[Console]::IsOutputRedirected`; on
Linux and macOS they use the shell TTY checks. An interactive console selects
the full-screen shell. Direct Python entry-point calls fall back to Python's TTY
and terminal-capability checks.

Portable Plain Mode is selected when:

- --plain is provided to devloop-plan or devloop;
- input or output is redirected or piped; or
- the environment cannot support interactive terminal control.

An interactive --plain run may use line-oriented prompts. A non-interactive run
must receive required values through arguments/configuration and fails clearly
instead of waiting for input. --non-interactive remains an execution choice and
does not itself disable the full-screen shell. --help prints ordinary CLI help
without launching the application.

Plain output is append-only, sanitized, stable enough for logs, and free of
animation, raw-key menus, or control sequences. It reports the same statuses
and final exit code as the full-screen shell. `--plain` always overrides the
launcher-selected application mode.

## Installation And Startup

The Windows, Linux, and macOS installers create and maintain a bundle-local
.venv from a committed exact dependency lock. The launchers always use that
interpreter for installed commands.

- Textual and its transitive dependencies are pinned and verified after install
  or update.
- Installation prepares a replacement environment and validates it before
  making it active, so a failed dependency update does not destroy the last
  working runtime.
- No dependency is installed into the user's global Python environment.
- Launchers do not print the logo before application startup. Branding is a
  launcher Portable View inside the shell.
- Startup performs no surprise package download. A missing or damaged runtime
  produces one repair instruction directing the operator to rerun the
  installer.
- Development checkouts may use their project environment, but release
  validation must exercise the installer-created environment.

## Visual Style

The initial theme is Midnight Commander-inspired rather than an exact clone:

- dark blue application background;
- cyan/white focused selections;
- a strong fixed border and pane divider;
- green success, yellow warning, and red error accents;
- high-contrast focus markers and explicit text labels;
- NO_COLOR and monochrome-safe behavior.

The theme is centralized in the Portable UI package. Workflow code never emits
style markup or color names.

## Migration Plan

The public default must not expose a half-converted interface. Development may
use a temporary internal opt-in until all interactive paths have parity.

1. Runtime foundation
   - Add the exact portable dependency lock and isolated installer runtime.
   - Add --plain and the UI mode resolver.
   - Define presentation models, intents, events, and the plain adapter.
2. Shell tracer
   - Add the persistent Textual shell, standard layout, theme, help, log, error,
     input, confirmation, and terminal-too-small overlays.
   - Convert target selection, startup, and resume end to end.
3. Configuration
   - Convert branch/worktree choices, Workflow Editor, route/model pickers,
     capabilities, guidance, and every finite menu.
4. Planning
   - Convert the composer, planning chat, artifacts, slash commands, clipboard
     attachment, preflight, and live Codex activity.
5. Handoff
   - Convert saved-status display, run options, worktree preparation, workflow
     snapshot validation, and direct devloop startup.
6. Execution
   - Convert issue scheduling, development, review, QA, requested-change loops,
     dependency waiting, blocker resolution, timers, and attempt results.
7. Completion
   - Convert self-improvement, final results, merge choices, stop behavior,
     failures, and terminal restoration.
8. Cutover and cleanup
   - Pass parity and real-terminal gates on Windows and Linux.
   - Make Textual the interactive default.
   - Remove obsolete interactive ANSI menus, cursor writers, line prompts, and
     direct terminal writes while retaining Portable Plain Mode.
   - Update user documentation and screenshots to the implemented interface.

Each migration slice must be vertically usable and tested, but the released
interactive default changes only after the complete path no longer falls back
to line output.

## Verification Gates

### Automated

- Unit-test controllers, view models, action availability, selection
  preservation, activity coalescing, and mode selection without Textual.
- Run every Portable View through Textual's headless test driver.
- Exercise keyboard navigation, overlays, back/cancel behavior, stop dialogs,
  and immediate selection previews.
- Verify layouts at 80x24, 100x30, and a wide terminal.
- Verify the Terminal Too Small view during live resize and recovery.
- Verify that background events update the mounted view without blocking input.
- Verify complete stdout/stderr capture from Codex, Git, worktree, validation,
  and self-improvement subprocesses.
- Fail tests if converted application services perform direct terminal reads or
  writes.
- Verify Portable Plain Mode contains no ANSI control sequences and preserves
  status/exit-code parity.
- Keep the small --dry-run --no-worktree issue-pack gate and inspect generated
  loop state and logs.
- Test fresh install, update, failed dependency refresh, and runtime repair
  guidance.
- Verify terminal restoration after normal exit, cancellation, worker failure,
  and unhandled exception.

### Real Terminals

- Windows PowerShell: startup, arrow navigation, resize, Alt+V, a dry run, a
  short real run, interruption, resume, and clean exit.
- Linux terminal: the same keyboard, resize, dry-run, interruption, resume, and
  clean-exit evidence.
- Redirected stdout/stderr: automatic Portable Plain Mode with no cursor
  movement.

Authenticated or long-running real Codex gates are operator-run and write
non-secret result logs inside the workspace for inspection.

## Acceptance Criteria

1. Invoking devloop-plan in an interactive terminal enters one alternate-screen
   application; the logo and target selection are inside it.
2. Arrow movement changes one highlight and its preview without adding terminal
   lines.
3. Every Portable View in this document uses the same outer frame.
4. Planning, Codex execution, repository commands, scheduler updates, retries,
   and wiki work update bounded panes only.
5. No raw Codex or command stream writes outside the shell.
6. F4 exposes complete filtered logs without leaving the application.
7. Direct devloop invocation uses the same shell and execution view.
8. Ctrl+C presents safe contextual choices and never leaves a damaged terminal.
9. Normal exit returns to the original prompt without dumping the application
   transcript.
10. --plain and redirected runs remain deterministic and control-sequence free.
11. Current PRD-local state and exact resume behavior remain compatible.
12. No portable module imports CodexCLI UI or Workflow Run state.
13. Every finite TTY choice is arrow-key driven with explicit Back/Cancel.
14. The shell remains responsive and consistent during all workflow and
    post-run stages.

## Non-Goals

- Replacing Codex exec with Codex App Server.
- Migrating Portable Dev Loop state into CodexCLI.
- Changing workflow, Issue, retry, worktree, or self-improvement semantics.
- Removing durable raw logs.
- Building a browser or desktop GUI.
- Reproducing Midnight Commander pixel for pixel.
- Requiring mouse input.
