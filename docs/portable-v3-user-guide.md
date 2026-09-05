# Portable Dev Loop v3 User Guide

Portable Dev Loop v3 is generation **v3**, software version **0.3.1**. It is the
`devloop-plan` and `devloop` product; the separately installed CodexCLI remains
version `0.2.1` and uses different commands and state.

Check the portable version without starting work:

```powershell
.\bin\devloop-plan.ps1 --version
.\bin\devloop.ps1 --version
```

## Sessions and saved projects

An interactive start opens the permanent **Sessions** tab and does not start a
worker. A Portable Saved Project is one canonical Git checkout or worktree.
Choose Resume to start unfinished work, or `+` to select a saved checkout,
register an existing checkout, or create a distinct Git worktree. Two sessions
may use different worktrees from one repository, but a worktree lease prevents
two live sessions from using the same canonical checkout.

Each opened workflow has its own tab. Closing a workflow tab hides only the
view; the session remains available from Sessions. Completed sessions move to
History until explicitly forgotten. Forget removes catalog metadata only and
never removes project files, PRDs, issues, logs, branches, or worktrees.

## Concurrency and lifecycle controls

The machine-wide execution limit defaults to two. Change it from **Options**.
Extra sessions remain `QUEUED`; `PAUSED` and `WAITING_FOR_INPUT` sessions do not
consume capacity.

- **Pause** reaches a durable checkpoint, releases capacity, and keeps the
  worktree association for Resume.
- **Force Stop** interrupts the worker and preserves partial files, diagnostics,
  and the last durable checkpoint.
- **Cancel** is an explicit terminal action, separate from hiding a tab.
- Application exit asks once, pauses running sessions where possible, stops all
  workers, and leaves no background daemon.

Missing or moved worktrees appear as `UNAVAILABLE`. Use **Relink** with the moved
canonical checkout or **Forget** the catalog record without touching project
data. Ambiguous leases are not force-reclaimed; inspect the reported owner and
confirm the other Dev Loop process has stopped before retrying.

## Plain Mode

`--plain`, redirected input, and redirected output retain the deterministic
append-only interface. Plain Mode runs one foreground workflow while using the
same saved-project registration, worktree lease, and machine-wide concurrency
rules as the Application Shell.

## Updating from 0.2.1

Run the same platform installer against the same install directory. The stable
`bin` and `install` command paths dispatch through an atomic current-release
pointer; release payloads and isolated runtimes are immutable and retained
side-by-side. The updater can therefore run through its installed `pwsh -File`
or Bash path without replacing its running file or renaming its containing
tree. The prior release remains available through validation, adoption,
activation, retry, and rollback. See the
[side-by-side install layout](portable-v3-install-layout.md).

To select the previously current release after a successful update, run the
installed updater with `-Rollback` on Windows or `--rollback` on Linux/macOS.
Rollback verifies both releases, switches only the atomic pointer, and keeps
both payloads available.

On first successful v3 start, Dev Loop reads but does not rewrite the existing
planner configuration. It adopts the last confirmed canonical target and only
its Git-related worktrees containing unfinished portable workflows. PRDs,
issues, loop state, logs, branches, and worktrees remain unchanged, so the
older runner can still read project-local state after rollback. Additional
projects can be adopted explicitly later.

Version 0.2.1 did not persist a planning-only conversation after its process
ended. V3 cannot reconstruct an already-lost pre-PRD conversation; it can adopt
PRD-backed unfinished work.

Uninstall removes verified managed releases and unchanged bootstrap assets,
restores entrypoints displaced from a legacy checkout, and removes only
unchanged installer-copied capabilities. It preserves legacy checkout edits,
user configuration, the machine catalog, project data, and personally modified
bootstrap, skill, or agent files.
See [Troubleshooting](troubleshooting.md) for recovery guidance.
