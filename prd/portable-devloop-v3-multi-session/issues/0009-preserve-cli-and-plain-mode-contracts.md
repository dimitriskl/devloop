Label: ready-for-agent

# Preserve CLI and Plain Mode Contracts

## Target Product

Product: devloop-plan + devloop

Portable Windows/Linux wrappers, command-line parsing, Plain Mode, redirected
output, shared catalog concurrency, worktree leases, and existing runner
handoff. CodexCLI commands are out of scope.

## What to build

Carry the v3 catalog and worktree safety contracts through existing explicit CLI
and Plain Mode entry points without imposing tab rendering. Existing `--repo`,
`--prd`, worktree, selection, dry-run, and runner options must retain their
meaning. A non-interactive invocation runs one foreground session while
registering its project, acquiring machine-wide capacity and its worktree lease,
and publishing deterministic append-only output.

Ensure an interactive v3 tab and a separate Plain Mode process cannot collide on
one worktree or bypass the configured concurrency limit. Preserve the existing
wrapper/bootstrap behavior on Windows and Linux.

Covers parent PRD user stories 75–79.

## Acceptance criteria

- [ ] Existing portable `--repo`, `--prd`, worktree, issue-selection, dry-run, and runner flags remain accepted with unchanged meanings.
- [ ] Plain Mode executes one foreground session without mounting tabs or emitting cursor-control sequences.
- [ ] Redirected output remains deterministic, sanitized, append-only, and suitable for durable logs.
- [ ] A Plain Mode invocation registers or updates the same Portable Session Catalog used by the interactive shell.
- [ ] Plain Mode atomically acquires machine-wide execution capacity before active work.
- [ ] Plain Mode acquires the same canonical worktree lease and fails clearly when an interactive or external session owns it.
- [ ] Interactive sessions observe a Plain Mode lease and catalog status without starting a duplicate worker.
- [ ] Dry-run does not leave a live worker, stale lease, or false running status.
- [ ] Existing wrapper bootstrap and interpreter selection remain consistent on Windows and Linux.
- [ ] Non-interactive failures return stable nonzero exit codes and actionable plain-text diagnostics.
- [ ] Existing portable CLI, resume, worktree, dry-run, redirected-output, and terminal-safety tests remain green.
- [ ] New cross-entry-point tests prove interactive/Plain Mode lease exclusion and shared concurrency.

## Blocked by

- Blocked by [Issue 0002: Resume Pre-PRD Sessions from the Machine Catalog](./0002-resume-pre-prd-sessions-from-the-machine-catalog.md)
- Blocked by [Issue 0003: Create and Lease Distinct Worktrees](./0003-create-and-lease-distinct-worktrees.md)
- Blocked by [Issue 0005: Queue Sessions with a Configurable Machine Limit](./0005-queue-sessions-with-a-configurable-machine-limit.md)
- Blocked by [Issue 0006: Pause, Stop, and Exit Sessions Safely](./0006-pause-stop-and-exit-sessions-safely.md)

## User stories addressed

- User stories 75–79

## Implementation Notes

Completed: [x]

- Issue commits: `65635e6`, `ba26b41`, and `69e132a`.
- Implementation preserves the existing planning and delivery entry points while
  routing Application and Plain Mode through the shared catalog, canonical
  worktree lease, and machine-wide execution-capacity contract. Relative launch
  arguments retain a trusted base across worker checkout changes, and a created
  worktree is normalized transactionally for later catalog reload and resume.
- Fresh independent review: PASS. The committed-tree rerun reported 65/65 for
  contract, entry-point, presentation, and worktree coverage and 77/77 for
  catalog, migration-review, and concurrency coverage. The former created-worktree
  resume blocker passed 1/1, and the former Windows SQLite cleanup race passed
  20/20 across ten fresh-process repetitions. The planning/static-wrapper group
  reported 64 passed plus one unrelated pre-existing Windows path-separator
  assertion.
- Fresh QA: PASS. QA reported 241 green executions (238 unique plus 3 repeats),
  successful `compileall`, successful PowerShell parsing and Python `--help`
  entry-point probes, and no surviving QA Python processes or SQLite artifacts.
- Platform limitation: live Linux/WSL execution was unavailable with
  `E_ACCESSDENIED`; wrapper parity was therefore static only, and the live
  cross-platform gate remains deferred to Issue 0013. No real/authenticated
  Codex or installer gate ran.
- Tooling limitation: mypy was unavailable, the full suite is not claimed, and
  repository-wide Ruff remains red from baseline findings. These limitations do
  not change the focused Issue 0009 review and QA PASS results.
