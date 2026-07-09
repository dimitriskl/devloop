# Interactive Plan-To-Dev Loop Runner

`devloop-plan` is the interactive front door for a new change. It opens one
continuous session: a Codex-backed planning chat for design and local
artifact creation, then development, review, and QA against the generated PRD
and issue pack, with the current stage always visible. No exit or Ctrl+C is
ever needed between stages — devloop watches the target checkout and moves
itself into development once the PRD and issue pack exist on disk.

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

- Windows: the built-in PowerShell clipboard cmdlets — nothing extra to
  install.
- Linux: `wl-paste` (Wayland) or `xclip` (X11).
- macOS: `pngpaste` (`brew install pngpaste`).

## Slash Commands

| Command | Description |
| --- | --- |
| Alt+V | attach a screenshot from the clipboard (use `/paste` if unavailable) |
| `/paste` | attach a screenshot from the clipboard |
| `/options` | open agent/skill and development options |
| `/status` | show the stage banner, artifacts, and selection summary |
| `/done` | detect the PRD and issue pack now (or enter paths manually) |
| `/help` | show this help |
| `/quit` | abort planning (never required to continue) |

## `/options`: Agents, Skills, And GitHub Installs

Typing `/options` at any prompt opens a small menu without leaving the chat:

1. **Planning skills** — pick which bundled skills drive the planning chat
   itself (defaults to `grill-with-docs`, `domain-modeling`, `to-prd`,
   `to-issues`).
2. **Default agents & skills per role (coder / reviewer / qa)** — override
   which bundled skills and agent references each development role uses
   instead of the embedded preset defaults.
3. **Add skill or agent from GitHub** — installs new skills or agents into
   the bundle; see `docs/skills-and-agents.md` for the exact format and rules.
4. **Back** — returns to the chat and persists the current selection to the
   plan-state JSON (the same file that remembers your last target checkout),
   so future sessions reopen with your last choices.

If you customized any per-role agents or skills, devloop writes a session
preset, `devloop.session.preset.json`, into the PRD folder once planning
hands off to development, and passes it to the implementation runner as
`--preset`. Sessions with no per-role overrides skip writing a preset and use
the bundled `presets/generic-minimal.json` defaults.

## Handoff To Development

When the PRD and issue pack are detected, devloop prints the DEVELOPMENT
banner and a one-screen summary: the PRD path, issue index path, which issues
will run, the implementation worktree path (or "disabled" if you chose not to
use one), the branch name, and a line confirming the self-improvement wiki is
always on (read before development, and updated after). Press Enter to start
development immediately with those defaults. Type `/options` to change the
start issue, whether to run every pending issue, whether to use a dedicated
worktree, the worktree path, or the branch name before starting. Type `/quit`
to stop without starting development.

Development, review, and QA then run in the same terminal, in-process —
devloop calls its own implementation runner directly rather than spawning a
new process. Expect a DEVELOPMENT/REVIEW/QA banner before every role prompt,
with a context suffix such as `issue 0004 (2/6) · pass 1`, and the
self-improvement wiki update running automatically at the end of the run.

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
