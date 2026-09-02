Label: ready-for-agent

# Issue 0001: Infer the Canonical Issue Index for a Portable Direct Run

## Target Product

Product: devloop-plan + devloop

## What to build

Allow a Portable Direct Run to begin with the accepted PRD path as its only
required artifact argument. At the core delivery entry point, infer the Issue
Index strictly from the canonical issue subdirectory in the PRD Package,
validate the resolved artifacts before agent construction, and preserve the
existing explicit Issue Index, preset, and wiki overrides. The PowerShell,
Bash, and Python entry points must inherit the same behavior from the core CLI
rather than implementing their own defaults.

This slice establishes the end-to-end PRD-only input path. It must not adopt the
planning intake's legacy Issue Index fallbacks and must not change scheduling or
worktree defaults, which are completed by the next slice.

## User stories covered

1, 5–10, 26–28, and 33–35.

## Acceptance criteria

- [ ] The accepted PRD path remains required and the Issue Index argument is optional at the public delivery entry point.
- [ ] Omitting the Issue Index resolves exactly the PRD Package's canonical `issues/README.md` and no legacy alternative.
- [ ] A missing inferred Issue Index produces a precise preflight error containing the expected location before any Codex runner or role is constructed.
- [ ] An explicit Issue Index overrides inference and retains current existence, target-product, parsing, and dependency validation.
- [ ] Omitting the preset continues to select the bundled generic-minimal preset, while an explicit preset continues to override it.
- [ ] Wiki reading and post-run updating remain enabled by omission, while the existing explicit wiki enable, disable, and custom-location controls remain accepted.
- [ ] PowerShell, Bash, and Python entry points obtain the behavior from the same core CLI contract; wrappers remain pass-through launchers.
- [ ] Public-entrypoint regression tests use representative temporary PRD Packages and replace only the expensive Codex execution boundary.
- [ ] Parser-level tests supplement rather than replace the public-entrypoint regression.
- [ ] Existing dependency parsing, issue selection breadth, scheduling, worktree choice, state, and resume behavior remain unchanged in this slice.

## Verification

- [ ] Run the focused portable entrypoint and dependency-preflight tests.
- [ ] Verify a PRD-only public invocation reaches resolved delivery inputs when the canonical Issue Index exists.
- [ ] Verify a PRD-only public invocation fails before agent construction when only a legacy fallback Issue Index exists.
- [ ] Run Ruff and strict mypy over the affected packages.

## Blocked by

None - can start immediately.
