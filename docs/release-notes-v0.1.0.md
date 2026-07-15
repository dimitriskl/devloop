# Dev Loop CodexCLI v0.1.0

Release date: July 17, 2026

CodexCLI v0.1.0 is the first installable release of Dev Loop's local Codex App
Server workflow. It provides one explicit, resumable sequence from analysis and
PRD publication through workspace preparation, development, independent code
review, QA, and local finalization.

## Highlights

- Real Codex App Server execution with no fake or simulated backend.
- Typed, persisted workflow cursors and explicit interrupted-phase recovery.
- Installed App Server contract profiling and exact external-worktree permission preflight.
- Deterministic planning identity and Markdown rendering from structured content.
- Versioned approval policies, redacted decision artifacts, execution profiles, phase telemetry,
  checkpoint recovery, and content-addressed verification evidence.
- Dependency-aware sequential Issue scheduling with bounded review and QA
  rework.
- A shared Textual shell with distinct Step Views, a read-only Issue Board,
  fixed workflow status, transactional capability profiles, explicit approvals,
  and pause, resume, cancel, and finalization controls.
- A redacted Handoff Summary that leaves the selected workspace intact and does
  not merge, push, publish, delete branches, or remove worktrees.
- Isolated installation through `uv tool install` or `pipx install` on Python
  3.10 or newer.

## Release artifacts

- `devloop_codexcli-0.1.0-py3-none-any.whl`
- `devloop_codexcli-0.1.0.tar.gz`

The release operator must attach the artifacts produced by the clean release
gate. Do not reuse an archive from an earlier version or a dirty build output
directory.

## Verification record

Before publication, replace each `PENDING` value below with evidence from the
same release commit. A `PENDING` value means the release is not publishable.

| Evidence | Result |
| --- | --- |
| Release commit | PENDING |
| Codex CLI version | PENDING |
| Windows credential-free and authenticated gates | PENDING |
| Linux credential-free and authenticated gates | PENDING |
| Wheel SHA-256 | PENDING |
| Sdist SHA-256 | PENDING |
| Real demonstration recording | PENDING |

## Known limitations

- Third-party executable component installation is deferred.
- Capabilities cannot be installed from GitHub inside CodexCLI v0.1.0.
- Legacy PRDs and run state are not migrated.
- Scheduling is sequential in one selected workspace.
- Run retention is enabled by default; an advanced purge UI is deferred.
- Repository publication and workspace cleanup remain explicit manual actions.

See `docs/codexcli-quickstart.md` for installation and
`docs/release-checklist-v0.1.0.md` for the required release gates.
