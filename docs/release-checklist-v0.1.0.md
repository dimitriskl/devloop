# v0.1.0 Release Checklist

Release date: July 17, 2026. Run this checklist from a clean checkout on both
Windows and Linux. Do not tag or publish while any required gate is incomplete.
Release operators need `uv`, `pipx`, Git, and an installed Codex CLI on PATH.
The scripts check these commands before starting any long-running gate.

## Credential-free gate

Run `.\install\run-release-gates.ps1` on Windows or
`./install/run-release-gates.sh` on Linux. The gate synchronizes the locked
environment, runs Ruff, mypy, the complete credential-free pytest suite, builds
the sdist and wheel, audits package contents, installs with both `uv tool` and
`pipx`, and probes the installed `codexcli` command.

## Authenticated real-backend gate

After `codex login`, run the platform script with `-RealBackend` on Windows or
`--real-backend` on Linux. Required scenarios are:

- doctor and real App Server handshake;
- real analysis and PRD publication;
- explicit workspace preparation and one complete Issue loop;
- review or QA rework through a fresh development attempt;
- pause and explicit resume;
- ten Issues with two completed, interruption during QA on Issue 3, restart,
  explicit selection, same QA attempt/workspace/cursor, and no replay of the
  unknown operation;
- local finalization and a complete Handoff Summary.

Record the date, platform, commit, Codex CLI version, wheel SHA-256, and gate
result in the GitHub release notes. Never record credentials, transcripts,
connection strings, environment dumps, or hidden reasoning.

## Demonstration

Use `examples/release-demo/run-demo.ps1` or `run-demo.sh` and follow its README.
The short recording must show real analysis, distinct component views, one
review or QA rework loop, exact interrupted-phase resume, and finalization. No
prerecorded or simulated executable workflow is acceptable.

## Artifact audit

- Version is `0.1.0` in package metadata and `devloop.version`.
- Changelog and GitHub release title identify `v0.1.0` and July 17, 2026.
- Sdist and wheel pass `install/verify-release.py`.
- Local ignored `docs/adr/` and `docs/prd/` content is absent from artifacts.
- No fake backend, simulation option, legacy migration, GitHub capability
  installer, parallel scheduler, or implicit repository publication ships.

## Known limitations

- Third-party executable component installation is deferred.
- GitHub installation of capabilities is not part of the CodexCLI workflow.
- Legacy PRDs and legacy run state are not imported or migrated.
- Scheduling is sequential and uses one selected workspace.
- Retention keeps runs by default; advanced purge UI is deferred.
- Merge, push, pull request creation, branch deletion, and worktree removal are
  manual actions outside the application.
