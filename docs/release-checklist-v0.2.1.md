# v0.2.1 Release Checklist

Release date: July 27, 2026. Run this checklist from a clean checkout on both
Windows and Linux. Do not tag or publish while any required gate is incomplete.
Release operators need `uv`, `pipx`, Git, and Codex CLI on PATH.

## Credential-free gate

Run `.\install\run-verification-tier.ps1 -Tier fast` on Windows or
`./install/run-verification-tier.sh fast` on Linux. The gate synchronizes the
locked environment, runs Ruff, mypy, the complete credential-free pytest suite,
builds the deterministic evidence identity, and writes its non-secret log and
manifest under `.release-evidence/`.

## Authenticated real-backend gate

After `codex login`, build and test the canonical release artifacts on Linux
with:

```text
./install/run-verification-tier.sh release
```

Copy the unchanged checkout and exact two `dist/` artifacts to the Windows
release host, then run:

```text
.\install\run-verification-tier.ps1 -Tier release -UseExistingArtifacts
```

Record the date, platform, commit, CLI versions, artifact SHA-256 values, and gate
result in the release notes. Never record credentials, raw transcripts,
connection strings, environment dumps, or hidden reasoning.

## Demonstration

Use `examples/release-demo/run-demo.ps1` or `run-demo.sh` and follow its README.
The recording must exercise the real workflow and at least one configured
Execution Backend. Place it inside `.release-evidence/`, then record its hash:

```text
uv run python install/record-demonstration.py --recording .release-evidence/devloop-demo.mp4
```

After both platform manifests and the recording exist, verify and combine them:

```text
uv run python install/verify-release-evidence.py
```

This remains blocked while the v0.2.1 release notes contain `PENDING`, either
platform is absent, or commits, identities, or artifact bytes differ.

## Artifact audit

- Version is `0.2.1` in package metadata and `devloop.version`.
- Changelog and release title identify `v0.2.1` and July 27, 2026.
- Sdist and wheel pass `install/verify-release.py`.
- Only `devloop_codexcli-0.2.1-py3-none-any.whl` and
  `devloop_codexcli-0.2.1.tar.gz` are present in `dist/`.
- Local ignored `docs/adr/` and `docs/prd/` content is absent from artifacts.
- v2 resolved-run migration preserves cursor, issue, attempt, and budget state;
  invalid hashes and incomplete settings fail closed.
- No gate is marked complete without current operator evidence.

## Publication

- Replace every `PENDING` value in `docs/release-notes-v0.2.1.md`.
- Re-run the evidence combiner against the final release commit.
- Tag and publish only the two verified artifacts.
- Keep repository publication and workspace cleanup explicit.
