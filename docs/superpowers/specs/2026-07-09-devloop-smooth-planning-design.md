# Dev Loop Smooth Planning Experience — Design

Date: 2026-07-09
Status: Approved pending user spec review

## Problem

`devloop-plan` currently embeds the full Codex TUI as a blocking subprocess for
the analysis (grill-with-docs) stage. Consequences:

- The user must type `/quit` or press Ctrl+C to leave Codex before the wrapper
  can continue — a jarring, non-obvious step.
- No always-visible indication of which stage the run is in.
- Agent/skill choices are hardcoded in the planning prompt and preset JSON.
- Screenshot paste depends on the Codex TUI (Alt+V) and is unavailable in the
  rest of the flow.
- The self-improvement wiki is optional per question, though it should always
  be used.

## Goals

1. One continuous terminal session from analysis through qa — no forced exits.
2. Always-visible stage pipeline: `analysis → development → review → qa`.
3. Codex-CLI-like interaction: chat turns, **Alt+V** screenshot paste (as in
   Claude Code / Codex CLI), slash commands.
3a. Development context discipline: every coder/reviewer/qa session starts
   clean (no planning-chat carryover); issues are self-contained and sized
   for the context window.
4. Agent/skill selection via `/options`, backed by drop-in files in
   `agents/codex/` and `skills/codex/`, with embedded defaults when the user
   selects nothing.
5. Skills/agents installable from GitHub URLs.
6. Self-improvement wiki always on in this flow (read during analysis and
   development; updated post-run).
7. Identical behavior via both wrappers: `bin/devloop-plan.ps1` and
   `bin/devloop-plan.sh` (thin forwarders kept in sync).
8. Zero new runtime dependencies — Python 3.10+ stdlib only.

## Non-Goals

- Replacing the `devloop` batch runner or its issue/state formats.
- Reimplementing Codex TUI features beyond the essentials listed here.
- Changing role prompts (`prompts/coder.md`, `reviewer.md`, `qa.md`) beyond
  what stage banners require (they require nothing).
- Removing `cli.py`'s `--no-self-improvement-wiki` flag (kept for standalone
  and CI use; the interactive flow simply always enables the wiki).

## Architecture

Chosen approach: **devloop-owned chat loop over `codex exec` / `codex exec
resume` with plain streaming output and filesystem artifact detection**
(Approach A). Verified against Codex CLI 0.143.0, which provides
`codex exec`, `codex exec resume <id> | --last`, and `-i/--image <file>`.

Rejected alternatives:

- Keep embedding the Codex TUI: cannot remove the exit step or render a
  status banner while another process owns the terminal.
- Parse `codex exec --json` JSONL events: precise, but version-fragile across
  Codex releases and requires re-rendering all output.
- `resume --last` without id capture: risks cross-talk with concurrent Codex
  sessions.

### Flow

```
devloop-plan (ps1/sh → python -m devloop.interactive_runner)
  1. Repo + branch selection            (existing logic, unchanged)
  2. ANALYSIS — chat loop
       banner: devloop · analysis ● → development → review → qa
       prompt: [analysis] ›
       turn 1: codex exec … <planning prompt>   (capture session id)
       turn N: codex exec resume <id> [-i img…] <user message>
       after each turn: rescan prd/ via find_artifacts
  3. Artifacts detected → banner flips to DEVELOPMENT
       one-screen summary (issues, worktree path, branch, wiki: always on)
       Enter = start; /options = adjust
  4. Dev runner invoked in-process (devloop.cli.main), per issue:
       coder   → banner DEVELOPMENT · issue n/m · pass k
       reviewer→ banner REVIEW
       qa      → banner QA
     wiki read by all roles; post-run wiki update always runs
```

### Stage transitions

| From → To | Trigger |
|---|---|
| ANALYSIS → DEVELOPMENT | PRD + `issues/README.md` detected after a turn (or `/done`), then Enter on summary |
| DEVELOPMENT → REVIEW | coder PASS for current issue |
| REVIEW → QA | reviewer PASS |
| QA → DEVELOPMENT | next pending issue begins (`issue n/m` context) |
| QA → done | last issue completes → wiki update → final summary |

## Components

### New modules (`src/devloop/`)

| Module | Responsibility | Key interface |
|---|---|---|
| `chat_loop.py` | REPL: prompt, turn dispatch, session-id capture (header parse, `resume --last` fallback), slash-command routing, artifact rescan per turn | `run_planning_chat(repo_root, bundle_root, goal, selection) -> PlanningArtifacts \| None` |
| `lineeditor.py` | Raw-mode line reader with **Alt+V** image-paste hook (as in Claude Code / Codex CLI), backspace, left/right/home/end, up/down history. POSIX: `termios`/`tty`, Alt+V = `ESC v`. Windows: VT input mode via `ctypes` (`ENABLE_VIRTUAL_TERMINAL_INPUT`), same `ESC v` sequence. Non-TTY or raw-mode failure → plain `input()` fallback with `/paste` | `read_line(prompt, on_paste_image) -> str` |
| `statusui.py` | `Stage` enum, pipeline banner rendering, stage-prefixed prompts; shared by chat loop and `cli.py` | `render_banner(stage, context)`, `stage_prompt(stage)` |
| `clipboard.py` | Clipboard-image capture → temp PNG. Windows: PowerShell `Get-Clipboard -Format Image`; Linux: `wl-paste` then `xclip`; macOS: `pngpaste` then AppleScript | `capture_clipboard_image(dest_dir) -> Path \| None` |
| `catalog.py` | Discover agents (`agents/codex/*.md`) and skills (`skills/codex/*/SKILL.md`); mark embedded defaults; hold current selection | `discover(bundle_root) -> Catalog`; `Selection` dataclass |
| `github_install.py` | `git clone --depth 1` to temp dir, locate skill/agent files, confirm with user, atomic move into target directory | `install_from_github(url, catalog) -> InstallResult` |

### Modified files

- `interactive_runner.py`
  - Keeps repo/branch selection and artifact resolution.
  - Replaces `run_codex_planning_session` with the chat loop.
  - Planning prompt rebuilt: skills list rendered from `Selection`
    (defaults = grill-with-docs, domain-modeling, to-prd, to-issues), adds the
    self-improvement wiki path, removes all "ask the user to exit"
    instructions; Codex is told to report artifact paths in its final message
    and continue chatting.
  - Dev-parameter Q&A collapses into summary + Enter handoff
    (wiki question deleted; wiki always enabled).
  - Invokes `devloop.cli.main()` in-process instead of spawning
    `devloop.ps1`/`devloop.sh`.
- `cli.py` — role transitions render stage banners via `statusui`
  (DEVELOPMENT/REVIEW/QA with `issue n/m · pass k`). No behavioral change.
- `bin/devloop-plan.ps1` / `bin/devloop-plan.sh` — remain thin forwarders;
  any new flags/help text mirrored in both.
- Docs — README, `docs/interactive-runner.md`, `docs/skills-and-agents.md`
  (drop-in extension + GitHub install), `docs/usage.md`.

### Slash commands

| Command | Action |
|---|---|
| **Alt+V** | Primary paste shortcut (matches Claude Code / Codex CLI): capture clipboard image; show `[image N attached]`; attach to next turn via `-i` |
| `/paste` | Fallback for terminals where Alt+V cannot be captured (non-TTY, raw mode unavailable); same behavior |
| `/options` | Menu: default agents (per-role agent/skill from catalog), planning skills, add from GitHub, dev-parameter defaults (worktree/branch/start-issue) |
| `/status` | Reprint banner + artifact paths + selection summary |
| `/done` | Force artifact detection; fallback to manual path entry (`ask_existing_file`) |
| `/help` | List commands |
| `/quit` | Abort the run (never required to advance) |

Image file paths appearing in a chat message are auto-detected and attached.

### Clean-context development sessions (invariant)

The planning chat's `resume` chain exists **only** during ANALYSIS and ends at
the handoff. Every development invocation (coder, reviewer, qa — per issue,
per pass) starts a **clean `codex exec` session with no inherited context**,
exactly as `codex_runner.py` does today (plain `exec`, never `resume`). The
full context window of each development session is reserved for the issue at
hand.

Consequence: the issue pack must carry everything a clean session needs. The
planning prompt instructs Codex (reinforcing the `to-issues` skill) that each
issue file must be **self-contained**:

- Goal, acceptance criteria, and verification steps stated in the issue
  itself — never "as discussed above" or references to the planning chat.
- Concrete pointers: relevant file paths, the PRD path, and the specific PRD
  sections that apply — summarized in the issue, not duplicated wholesale.
- Sized to respect the context window: one thin vertical slice per issue; if
  the required context outgrows a comfortable single read, the issue must be
  split rather than compressed.
- Screenshots and images referenced during planning that matter for
  implementation are saved into the PRD folder and linked by path from the
  relevant issue, so clean sessions can load them.

### Selection persistence

`/options` choices persist in the existing plan-state JSON
(`%APPDATA%\DevLoop\devloop-plan.json` or `~/.config/devloop/devloop-plan.json`)
so they become sticky defaults across runs. Enter always accepts defaults.

## Error Handling

- Codex turn nonzero exit: message shown; loop stays at prompt; user retries,
  rephrases, or `/quit`s. The wrapper never crashes.
- Session id not captured: fall back to `codex exec resume --last`; if that
  fails, start a fresh session with a one-line continuation note.
- Clipboard empty / no image tool available: friendly one-liner naming the
  missing tool; no attachment.
- Raw keyboard mode unavailable (piped stdin, unsupported console): line
  editor silently degrades to plain `input()` and prints a one-time hint that
  `/paste` replaces Alt+V in this terminal.
- Artifacts never detected: `/done` → manual path entry.
- GitHub install failure (bad URL, git missing, no SKILL.md/agent .md found):
  clear message; temp dir discarded; target directories untouched
  (temp-then-atomic-move).
- Ctrl+C in chat loop: caught; "Abort planning? [y/N]". Ctrl+C during
  development: unchanged (state file preserves progress; reruns continue).

## Testing

Stdlib `unittest` in a new `tests/` folder (no new dependencies):

- `catalog`: fixture dirs; defaults marking; tolerance of missing dirs.
- Session-id parsing: sample outputs incl. absence → fallback path.
- Artifact detection: temp `prd/` layouts (regression coverage for existing logic).
- `lineeditor`: synthetic key-sequence tests (`ESC v` → paste hook fires;
  arrows/backspace/history editing; raw-mode-unavailable → `input()` fallback).
- `statusui`: banner snapshots per stage; `NO_COLOR`/non-TTY mode.
- `clipboard` / `github_install`: injected command runner; assert per-OS argv.
- Manual smoke matrix: Windows PowerShell + Windows Terminal, Ubuntu bash,
  both wrappers, real Codex session end-to-end.

## Extensibility (GitHub-facing)

- Drop-in: new agent = one `.md` in `agents/codex/`; new skill = folder with
  `SKILL.md` in `skills/codex/`; both auto-discovered by `/options`.
- Install from GitHub: `/options → add from GitHub` accepts a repo URL
  (optionally `#subpath`), clones shallow to temp, shows what will be
  installed, confirms, moves atomically.
- Docs describe the stage pipeline, module layout, and extension points so
  the repo is understandable and forkable.
