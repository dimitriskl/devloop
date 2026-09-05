Label: ready-for-agent

# Manage Session History and Unavailable Worktrees

## Target Product

Product: devloop-plan + devloop

Portable Sessions tab, catalog history, saved-project availability, relinking,
and non-destructive metadata cleanup. Project deletion and CodexCLI retention
are out of scope.

## What to build

Complete catalog lifecycle after execution and after filesystem changes.
Move completed sessions out of the active list into History while keeping them
inspectable until the user explicitly forgets them. Forget must remove only
machine-local catalog and pre-PRD metadata; it must never delete or edit project
artifacts, logs, branches, or worktrees.

Validate saved worktrees during discovery. Mark missing or moved paths
`UNAVAILABLE`, keep their session evidence visible, and provide Relink and
Forget actions. Relink must verify the replacement Git checkout and its
authoritative PRD state before updating catalog pointers.

Covers parent PRD user stories 41–47.

## Acceptance criteria

- [ ] A completed session leaves the active list and appears in a distinct History view.
- [ ] History retains project/worktree, PRD, result, progress summary, and last update until explicit Forget.
- [ ] Failed, cancelled, interrupted, and paused sessions are not misclassified as completed history.
- [ ] Forget removes only machine-local catalog/session metadata.
- [ ] Forget leaves PRDs, issue packs, status files, logs, branches, and worktrees byte-for-byte unchanged.
- [ ] A missing or moved saved worktree marks its sessions `UNAVAILABLE` without deleting them.
- [ ] `UNAVAILABLE` sessions start no worker and acquire no execution capacity or worktree lease.
- [ ] Relink requires an existing canonical Git checkout and validates the referenced PRD/issue state before committing.
- [ ] Relink updates all relevant catalog pointers transactionally while preserving stable session identity.
- [ ] An invalid replacement path leaves the original unavailable record unchanged with an actionable explanation.
- [ ] Forget remains available for obsolete unavailable records and is clearly described as metadata-only.
- [ ] Filesystem-move, disconnected-drive, valid relink, invalid relink, history, and non-destructive Forget behavior have end-to-end tests.

## Blocked by

- Blocked by [Issue 0002: Resume Pre-PRD Sessions from the Machine Catalog](./0002-resume-pre-prd-sessions-from-the-machine-catalog.md)
- Blocked by [Issue 0003: Create and Lease Distinct Worktrees](./0003-create-and-lease-distinct-worktrees.md)
- Blocked by [Issue 0006: Pause, Stop, and Exit Sessions Safely](./0006-pause-stop-and-exit-sessions-safely.md)
- Blocked by [Issue 0007: Recover Crashed Workers and Stale Leases](./0007-recover-crashed-workers-and-stale-leases.md)

## User stories addressed

- User stories 41–47

## Implementation Notes

Completed: [x]

- Implementation commit span: `bef3a59` through `41d6260`; issue commits are
  `bef3a59`, `f5b925c`, `3d58b85`, and `41d6260`.
- Fresh independent review: PASS. No functional defect was identified.
- Fresh QA: PASS. Shared regression coverage and a clean-snapshot rerun reported
  59/59; stress coverage reported 96/96; the broad sandbox-safe run reported
  254 passed and 1 POSIX-only skip; and focused behavior probes reported 7/7.
- Platform and environment limitation: these results are Windows-side. No Linux
  gate or real/authenticated Codex gate ran. The prescribed all-tests environment
  is not claimed green because cached Python 3.12 cannot load the repository
  venv's Python 3.13 Hypothesis native module.
- Tooling limitation: strict mypy did not run because the repository venv launcher
  points to an unavailable Windows Store Python, and cached Python 3.12 reports
  `No module named mypy`. Repo-wide Ruff remains red with nine unrelated/pre-existing
  findings; four findings in `portable_session_catalog.py` and `test_portable_ui.py`
  reproduce at `bef3a59^`, while the other eight issue-changed files pass focused
  Ruff. This record does not claim whole-repository or release green.
