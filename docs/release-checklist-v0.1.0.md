# v0.1.0 Release Checklist

Release date: July 17, 2026. Run this checklist from a clean checkout on both
Windows and Linux. Do not tag or publish while any required gate is incomplete.
Release operators need `uv`, `pipx`, Git, and an installed Codex CLI on PATH.
The scripts check these commands before starting any long-running gate.

## Development closure

Repository development for Issues 0001-0008 is complete. The credential-free source
checks available in the managed workspace passed on July 16, 2026: 483
standard-library tests, Python compilation, Bash syntax, and whitespace validation.
The remaining items in this checklist are release operations and evidence collection,
not deferred implementation. They must still pass before publication; development
completion does not authorize replacing `PENDING` values or claiming Windows,
authenticated App Server, installation, recording, or GitHub release evidence.

## Credential-free gate

Run `.\install\run-verification-tier.ps1 -Tier fast` on Windows or
`./install/run-verification-tier.sh fast` on Linux. The gate synchronizes the locked
environment, runs Ruff, mypy, the complete credential-free pytest suite, builds
the deterministic evidence identity, and writes the exact non-secret log and
manifest under `.release-evidence/`.

The real one-Issue phase-boundary gate is
`.\install\run-verification-tier.ps1 -Tier vertical` on Windows or
`./install/run-verification-tier.sh vertical` on Linux. It enters analysis through the
real App Server and reaches finalization only through the production publication,
workspace, scheduler, development, review, and QA services.

## Authenticated real-backend gate

After `codex login`, build and test the canonical release artifacts on Linux with:

```text
./install/run-verification-tier.sh release
```

Copy the unchanged checkout and the exact two `dist/` artifacts to the Windows
release host, then run:

```text
.\install\run-verification-tier.ps1 -Tier release -UseExistingArtifacts
```

The commands write `.release-evidence/linux-release.log`,
`.release-evidence/linux-release.json`, `.release-evidence/windows-release.log`, and
`.release-evidence/windows-release.json`. Required scenarios are:

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

Place the finished recording inside `.release-evidence/`, then record its content hash:

```text
uv run python install/record-demonstration.py --recording .release-evidence/devloop-demo.mp4
```

After the Windows and Linux manifests and recording exist, verify and combine them:

```text
uv run python install/verify-release-evidence.py
```

This last command remains blocked while release notes contain `PENDING`, while either
platform is absent, or when commits, identities, or artifact bytes differ.

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
