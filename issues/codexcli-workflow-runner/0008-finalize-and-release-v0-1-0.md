Label: ready-for-agent

## Parent

Local-only PRD: `docs/prd/codexcli-workflow-runner.md`

## What to build

Complete the standard Workflow with explicit local finalization and turn the verified application into the installable hackathon `v0.1.0` release. Produce a structured Handoff Summary, enforce the persistence/redaction boundary, verify clean installation and real operation on Windows and Linux, dogfood the packaged command, document prerequisites and known limitations, and create release artifacts without introducing deferred features.

Start this Issue in a fresh Codex context. Use `gpt-5.6-sol` with ultra reasoning. All release demonstrations and acceptance tests must use the real Codex App Server.

## Acceptance criteria

- [x] `finalize-workspace` consumes typed aggregated results and produces a Handoff Summary covering completed Issues, verification evidence, changed files, residual risks, and workspace disposition.
- [x] Finalization can leave the workspace intact and performs no implicit merge, push, pull-request creation, branch deletion, or worktree removal.
- [x] Persistence stores only the approved redacted allowlist and never stores hidden reasoning, full transcripts, environment dumps, credentials, authentication data, unbounded output, or binary output.
- [x] Run data is Git-ignored, current-user protected where supported, retained by default, and discoverable through `/runs`.
- [ ] `uv tool install` and `pipx install` succeed from a clean checkout and expose working `codexcli doctor` and `codexcli run` commands.
- [ ] Windows and Linux release gates pass doctor, real analysis, PRD publication, workspace preparation, one complete Issue loop, rework, explicit pause/resume, exact Issue 3-of-10 QA recovery, and finalization.
- [x] The packaged application contains no fake backend, simulation flag, synthetic executable workflow, legacy-state migration, GitHub capability installation, parallel scheduler, or implicit repository publication.
- [x] Documentation provides a five-minute prerequisite, installation, doctor, run, options, resume, and troubleshooting path.
- [x] A sample repository and reproducible real-backend demonstration script exercise the standard workflow without embedded credentials.
- [ ] A short demonstration recording shows real analysis, distinct component views, one review or QA rework loop, and exact interrupted-phase resume.
- [ ] Version metadata, changelog/release notes, wheel, and GitHub release artifacts identify `v0.1.0` and the July 17, 2026 hackathon release.
- [x] Local `docs/adr/` and `docs/prd/` content remains ignored and absent from the release.
- [ ] Full Ruff, mypy, pure test, Textual test, real App Server, Windows, and Linux gates pass with no critical known defect.

## Blocked by

- [Issue 0006: Recover the exact workflow cursor after shutdown](./0006-recover-exact-workflow-cursor.md)
- [Issue 0007: Complete shared TUI operations and capability profiles](./0007-complete-shared-tui-and-capabilities.md)
