Label: ready-for-agent

# Adopt Existing v0.2.1 Portable Projects

## Target Product

Product: devloop-plan + devloop

Portable installer/update adoption, existing user-wide planner configuration,
Git worktree discovery, Portable Resume Candidate discovery, catalog migration,
and Sessions-tab reporting. CodexCLI legacy import is out of scope.

## What to build

Add a first-run Portable Project Adoption flow from `0.2.1` into the v3 catalog.
Read the existing planner configuration without overwriting it, register its
last confirmed target, inspect only Git worktrees related to that target, and
adopt worktrees containing unfinished portable PRD workflows through the
existing discovery rules.

Create catalog pointers and migration receipts transactionally and
idempotently. Leave PRDs, issue packs, status files, logs, branches, and
worktrees unchanged. Make unavailable paths actionable and state the one hard
limit clearly: a planning-only conversation already lost when an old process
ended cannot be reconstructed.

Covers parent PRD user stories 80–91.

## Acceptance criteria

- [ ] First v3 startup detects an eligible `0.2.1` planner configuration without modifying it.
- [ ] Adoption registers the last confirmed target only when it remains a valid Git checkout.
- [ ] Targeted Git worktree discovery inspects related worktrees without scanning unrelated folders or drives.
- [ ] Related worktrees containing unfinished portable PRD workflows are adopted automatically through existing resume-candidate rules.
- [ ] Related worktrees without portable artifacts remain available for explicit adoption and are not registered silently.
- [ ] Each unfinished PRD and issue pack creates one stable catalog session pointing to its existing authoritative state.
- [ ] Project PRDs, issues, status files, logs, branches, and worktrees remain byte-for-byte unchanged.
- [ ] Adoption is one catalog transaction and writes its migration receipt only after successful commit.
- [ ] Repeating adoption produces no duplicate projects, sessions, leases, or receipts.
- [ ] An unavailable imported path is represented for Relink or Forget and does not abort other valid adoption.
- [ ] Additional existing projects adopt when the user selects or adds their checkout.
- [ ] User-facing migration results distinguish adopted, already adopted, unavailable, ignored, and unsupported entries.
- [ ] Documentation states that an already-ended pre-PRD `0.2.1` conversation cannot be reconstructed.
- [ ] Representative older-runner checks prove adopted project-local workflow state remains readable after rollback.
- [ ] Upgrade tests hash project artifacts before and after success, failure, retry, and rollback scenarios.

## Blocked by

- Blocked by [Issue 0002: Resume Pre-PRD Sessions from the Machine Catalog](./0002-resume-pre-prd-sessions-from-the-machine-catalog.md)
- Blocked by [Issue 0003: Create and Lease Distinct Worktrees](./0003-create-and-lease-distinct-worktrees.md)
- Blocked by [Issue 0008: Manage Session History and Unavailable Worktrees](./0008-manage-session-history-and-unavailable-worktrees.md)
- Blocked by [Issue 0009: Preserve CLI and Plain Mode Contracts](./0009-preserve-cli-and-plain-mode-contracts.md)

## User stories addressed

- User stories 80–91

## Implementation Notes

Completed: [ ]
