Label: ready-for-agent

# Create and Lease Distinct Worktrees

## Target Product

Product: devloop-plan + devloop

Portable Sessions-tab creation flow, Git worktree handling, saved-project
catalog records, session supervision, and machine-local ownership. CodexCLI
workspace preparation is out of scope.

## What to build

Complete the new-session path around distinct Git worktrees. From the `+`
action, let the user choose an available saved worktree, register an existing
checkout, or create/reuse a Git worktree from a saved repository and branch.
Canonicalize the resulting checkout and acquire a machine-wide Portable
Worktree Lease before launching a worker.

Prevent concurrent sessions from targeting the same canonical worktree across
tabs or Dev Loop application instances. If the worktree belongs to an existing
session, focus that session locally or report its external owner. Automatically
remember successfully opened, created, and reused worktrees without scanning
unrelated folders.

Covers parent PRD user stories 8–18, 57, and 66–68.

## Acceptance criteria

- [ ] The `+` flow offers an available saved worktree, an existing checkout, and creation of a new worktree from a saved repository and branch.
- [ ] Every selected path is resolved to a canonical Git checkout root before session creation.
- [ ] Two worktrees of one repository are stored as distinct Portable Saved Projects.
- [ ] A successfully opened target checkout is registered automatically.
- [ ] An implementation worktree created or reused by Dev Loop is registered automatically.
- [ ] Project discovery never recursively scans unrelated drives or parent directories.
- [ ] A machine-wide lease is acquired atomically before the worker starts and is keyed by canonical worktree path.
- [ ] Selecting a worktree leased by a session in the current shell focuses that existing tab.
- [ ] Selecting a worktree leased by another application reports the owner and starts no competing worker.
- [ ] Different worktrees of the same repository may be leased by different sessions concurrently.
- [ ] Worktree creation/reuse errors leave no session, catalog, or lease record claiming successful ownership.
- [ ] End-to-end tests cover saved selection, existing checkout registration, worktree creation/reuse, local focus, external conflict, and same-repository distinct paths.

## Blocked by

- Blocked by [Issue 0002: Resume Pre-PRD Sessions from the Machine Catalog](./0002-resume-pre-prd-sessions-from-the-machine-catalog.md)

## User stories addressed

- User stories 8–18
- User story 57
- User stories 66–68

## Implementation Notes

Completed: [x]

- Independent code review passed the complete Issue 0003 implementation from
  `f9139aa` through `4ebceb3`, including all worktree selection, lease,
  implementation-handoff, and delivery-transfer fixes.
- Selected checkouts are canonicalized before an atomic machine-wide lease is
  acquired; same-shell ownership focuses the existing session, external ownership
  blocks a competing worker, and distinct worktrees from one repository remain
  independently leasable and registered.
- Planning-to-implementation handoff transfers the session and lease to the
  confirmed implementation worktree, preserves the source-project relationship,
  updates delivery pointers consistently, and rolls back partial transfer failures.
- Independent QA passed 239 tests with 1 platform skip; compile, diff, and
  product-boundary checks passed. One unchanged Windows path assertion failure was
  confirmed unrelated to Issue 0003.
- The repository virtual environment was unavailable, so pytest, Ruff, and mypy
  could not run. Validation used the sandbox Python interpreter and the
  standard-library unittest suite.
- Stale-owner detection, lease heartbeats, and crashed-owner reclamation remain
  intentionally excluded and are owned by
  [Issue 0007](./0007-recover-crashed-workers-and-stale-leases.md).
