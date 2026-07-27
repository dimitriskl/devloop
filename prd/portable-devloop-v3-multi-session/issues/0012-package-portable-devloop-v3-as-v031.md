Label: ready-for-agent

# Package Portable Dev Loop v3 as v0.3.1

## Target Product

Product: devloop-plan + devloop

Portable version identity, Windows/Linux installer and updater behavior,
isolated runtime validation, v3 catalog adoption entrypoint, documentation, and
rollback safety. CodexCLI packaging is out of scope.

## What to build

Package the completed multi-session feature as Portable Dev Loop generation v3,
software version `0.3.1`. Update the portable version surfaces only after all
required functionality is present, and make fresh install and update establish
the same supported runtime without adding a third-party database, IPC, or
process-management dependency.

Integrate first-run catalog creation and `0.2.1` project adoption into the
Windows and Linux update experience. Preserve source checkouts and all target
project data during update, failed update, retry, uninstall, and rollback.
Document the Sessions tab, worktree rules, concurrency option, lifecycle
controls, Plain Mode compatibility, adoption behavior, and migration limit.

Covers parent PRD user stories 80, 92, and 100.

## Acceptance criteria

- [ ] Portable Dev Loop reports generation v3 and software version `0.3.1` consistently in its version API, logo, help, documentation, and release metadata.
- [ ] The version changes from `0.2.1` only after the required v3 implementation and automated gates are complete.
- [ ] Fresh Windows and Linux installs create or initialize the supported user-state location and defer worker execution until user action.
- [ ] Windows and Linux updates preserve existing planner configuration and invoke idempotent v3 adoption.
- [ ] The isolated runtime validates the pinned Textual dependency and standard-library SQLite availability.
- [ ] No third-party database, IPC, daemon, or process-manager dependency is added.
- [ ] Installer/update failure preserves the previous runnable installation and does not leave a false migration receipt.
- [ ] Uninstall preserves source checkouts, PRDs, issues, logs, branches, worktrees, catalog-owned project data needed for reinstall, and personally modified capabilities according to the established uninstall contract.
- [ ] Rollback to the prior portable runner leaves adopted project-local state readable.
- [ ] User documentation explains multi-session tabs, saved projects, distinct worktrees, default concurrency two, Options, pause/force-stop/exit, history, Relink, Plain Mode, and v0.2.1 adoption.
- [ ] Troubleshooting covers unavailable worktrees, stale/ambiguous leases, interrupted workers, catalog corruption, protocol mismatch, and lost pre-PRD v0.2.1 conversations.
- [ ] Release notes clearly distinguish Portable Dev Loop v3 from the separate CodexCLI application.
- [ ] Installer, update, uninstall, rollback, version, and documentation checks are automated where sandbox-safe and produce operator evidence for external gates.

## Blocked by

- Blocked by [Issue 0010: Adopt Existing v0.2.1 Portable Projects](./0010-adopt-existing-v021-portable-projects.md)
- Blocked by [Issue 0011: Harden the Session Protocol and Isolation Boundary](./0011-harden-the-session-protocol-and-isolation-boundary.md)

## User stories addressed

- User story 80
- User story 92
- User story 100

## Implementation Notes

Completed: [ ]
