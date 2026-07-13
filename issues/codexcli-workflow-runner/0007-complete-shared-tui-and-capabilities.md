Label: ready-for-agent

## Parent

Local-only PRD: `docs/prd/codexcli-workflow-runner.md`

## What to build

Complete the reusable terminal experience around the functioning workflow. Provide distinct standard Step Views inside one responsive Application Shell, a read-only Issue Board, fixed typed Workflow Status Bar, transactional user-wide capability profiles, contextual Slash Commands, typed approval and stop modals, and multilingual content handling without allowing presentation strings to control domain behavior.

Start this Issue in a fresh Codex context. Use `gpt-5.6-sol` with ultra reasoning. Exercise all workflow actions against the real App Server; presentation-model tests may remain pure.

## Acceptance criteria

- [ ] Analysis, workspace preparation, development, code review, QA, and finalization use distinct Step Views inside the same Application Shell.
- [ ] Reusable View Elements include Artifact viewing, Issue brief, diff viewing, findings, check matrix, streaming output, and attempt timeline without nested presentation/domain coupling.
- [ ] The Issue Board is persistent on wide terminals, available through `/issues` on narrow terminals, supports inspection, and cannot mutate scheduling.
- [ ] The fixed one-row Workflow Status Bar renders typed workflow, step, Issue position/status, attempt, backend activity, and elapsed time without wrapping or layout shift.
- [ ] `/options` edits user-wide Step Capability Profiles transactionally, with required capabilities locked, default capabilities replaceable, search, reset, apply, and cancel.
- [ ] Each run snapshots its resolved capabilities, and later user-default changes do not alter resume.
- [ ] `/resume`, `/options`, `/issues`, `/status`, `/language`, `/pause`, `/cancel`, and `/runs` are registered commands with typed scope and contextual availability.
- [ ] Approval requests show the requesting step/Issue, action, target, reason, and only backend-supported decisions; Dev Loop never auto-approves.
- [ ] Ctrl+C opens explicit continue, interrupt-turn, pause-run, and cancel-run actions; cancel requires confirmation and no stop action performs implicit Git cleanup.
- [ ] Composer and documents support UTF-8 multilingual content; machine tokens remain stable English identifiers; Greek, accented Latin, CJK, RTL, multiline paste, IME, and terminal-width cases are covered.
- [ ] Narrow and wide Textual pilot tests verify that content does not overlap, wrap the fixed status bar, or hide the active workflow state.
- [ ] Real-backend UI smoke tests exercise approvals, pause/resume, capability resolution, and all standard Step Views.
- [ ] Ruff, mypy, focused tests, and the real-backend UI slice pass.

## Blocked by

- [Issue 0002: Publish a resumable analysis PRD Package](./0002-publish-resumable-analysis-prd-package.md)
- [Issue 0005: Process rework and dependent Issues](./0005-process-rework-and-dependent-issues.md)
