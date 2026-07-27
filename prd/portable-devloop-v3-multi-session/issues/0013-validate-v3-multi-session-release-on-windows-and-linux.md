Label: ready-for-agent

# Validate the v3 Multi-Session Release on Windows and Linux

## Target Product

Product: devloop-plan + devloop

Portable v3 integrated behavior, wrappers, installers, session catalog,
supervisor/workers, worktree leases, lifecycle, adoption, documentation, and
release evidence. CodexCLI release gates are out of scope.

## What to build

Close v3 with an integrated, production-shaped demonstration and durable release
evidence. Exercise at least three sessions across distinct worktrees: two active
under the default limit, one queued, a background input request, a cooperative
pause, a force stop with partial work, a worker crash, exact resume, completion
history, unavailable-worktree relink, and interaction with a Plain Mode process.

Run the same behavior through Windows and Linux wrappers, and prove adoption
from a representative `0.2.1` installation without changing project artifacts.
Complete sandbox-safe build, test, lint, type, dry-run, terminal, migration, and
documentation gates. Hand any authenticated or installer-profile gate to the
operator as one paste-ready command that writes non-secret evidence into the
workspace; do not launch it from an agent session.

Covers all parent PRD user stories, with final ownership of story 100.

## Acceptance criteria

- [ ] One integrated scenario runs two sessions concurrently in distinct worktrees and visibly queues a third under the default limit.
- [ ] The scenario proves per-tab context, activity, unread state, input isolation, optional notification, hide/reopen, and non-focus-stealing behavior.
- [ ] The scenario proves waiting-for-input and paused sessions release capacity and queued work advances deterministically.
- [ ] Cooperative Pause preserves the exact durable cursor; Force Stop preserves partial work and restarts from the last checkpoint without false completion.
- [ ] An abrupt worker crash interrupts only its session, retains diagnostics, and requires explicit recovery.
- [ ] Same-worktree conflicts are prevented across tabs, another application instance, and Plain Mode.
- [ ] Completed History, metadata-only Forget, missing-worktree detection, and valid/invalid Relink behavior are demonstrated.
- [ ] A pre-PRD planning session survives application restart and resumes the exact saved Codex thread/settings through a fake or sandbox-safe backend.
- [ ] A PRD-backed workflow resumes the exact existing role/pass or generic Workflow Step checkpoint.
- [ ] A representative `0.2.1` configuration and unfinished project adopt idempotently into v3 with byte-for-byte unchanged project artifacts.
- [ ] Windows and Linux wrappers enter the same supervisor, catalog, lease, concurrency, lifecycle, and Plain Mode behavior.
- [ ] The complete sandbox-safe pytest suite, Ruff, strict mypy, compilation, dry-run, terminal safety, link validation, and `git diff --check` pass.
- [ ] TUI coverage includes narrow/wide layouts, resize, Unicode/ASCII behavior, overlays, several tabs, long labels, and bounded activity.
- [ ] Protocol fuzz/malformed-frame, redaction, cross-session isolation, cross-process catalog, stale lease, and migration rollback tests pass.
- [ ] Documentation and release metadata consistently name Portable Dev Loop v3 `0.3.1` and preserve the CodexCLI product boundary.
- [ ] Any unavailable authenticated, installation-profile, or external-platform gate is reported as operator-only with one physical paste-ready command and a workspace evidence-log path.
- [ ] No completion claim is made until required operator evidence has been inspected and all mandatory release gates are green.

## Blocked by

- Blocked by [Issue 0012: Package Portable Dev Loop v3 as v0.3.1](./0012-package-portable-devloop-v3-as-v031.md)

## User stories addressed

- User stories 1–100
- Final release ownership: user story 100

## Implementation Notes

Completed: [ ]
