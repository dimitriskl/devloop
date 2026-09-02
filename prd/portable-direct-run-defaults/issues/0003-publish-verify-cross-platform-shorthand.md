Label: ready-for-agent

# Issue 0003: Publish and Verify the Cross-Platform Shorthand Contract

## Target Product

Product: devloop-plan + devloop

## What to build

Publish the completed Portable Direct Run contract across user-facing help,
Windows and Linux quick starts, and repository operator guidance. Present the
PRD-only invocation as the preferred direct-delivery command, document every
opt-out and compatibility flag, and verify that PowerShell, Bash, and Python
entry points expose the same core behavior without wrapper-specific defaults.

Finish the feature through focused regression gates, the complete sandbox-safe
suite, static checks, and a representative PRD-only dry run. Preserve unrelated
in-progress changes and do not launch a real authenticated Codex integration
run from an agent-managed shell.

## User stories covered

2–4, 31–32, 36, and 38.

## Acceptance criteria

- [ ] CLI help states that the Issue Index is inferred from the canonical PRD Package location when omitted.
- [ ] CLI help identifies generic-minimal, all-Issues breadth, source-checkout execution, and wiki access as defaults.
- [ ] CLI help documents explicit Issue Index and preset overrides, the single-issue opt-out, explicit worktree creation, the no-wiki opt-out, and retained compatibility flags.
- [ ] Windows and Linux quick starts show the PRD-only invocation as the preferred direct-run command.
- [ ] Documentation no longer claims that one pending Issue or interactive worktree creation is the default for a Portable Direct Run.
- [ ] Documentation distinguishes direct runner defaults from explicit choices already collected by planning or session launchers.
- [ ] PowerShell and Bash wrappers remain thin pass-through launchers and do not duplicate core default logic.
- [ ] Automated regressions prove equivalent argument resolution through the PowerShell, Bash, and Python entrypoint contracts without invoking a real authenticated Codex session.
- [ ] A representative dry run supplied only a valid PRD path resolves the canonical Issue Index, generic-minimal preset, all unfinished Issues, source checkout, and enabled wiki behavior.
- [ ] Missing canonical artifacts fail with actionable preflight output and no agent execution.
- [ ] Existing explicit commands using all-issues, no-worktree, positive wiki, Issue Index, preset, start-Issue, and worktree-creation options remain valid.
- [ ] The complete sandbox-safe non-integration test suite, Ruff, and strict mypy gates pass, or any unrelated pre-existing failure is reported with exact evidence.
- [ ] Final diff inspection confirms only this PRD's implementation, tests, and documentation are included; unrelated workflow-editor and options-menu changes remain untouched.

## Verification

- [ ] Run the focused portable CLI, entrypoint, scheduler, worktree, and documentation tests.
- [ ] Run the complete sandbox-safe non-integration test suite.
- [ ] Run repository Ruff and strict mypy gates.
- [ ] Run the PRD-only dry-run smoke test against a temporary representative PRD Package and inspect its non-secret status and log evidence.
- [ ] Inspect the final working-tree diff independently and record any excluded concurrent changes.

## Blocked by

- [Issue 0001: Infer the Canonical Issue Index for a Portable Direct Run](./0001-infer-canonical-issue-index.md)
- [Issue 0002: Default Delivery to a Complete In-Place Run](./0002-default-to-complete-in-place-run.md)
