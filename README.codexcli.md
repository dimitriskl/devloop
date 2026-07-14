# Dev Loop CodexCLI

CodexCLI v0.1.0 is a local, resumable workflow runner for the real Codex App
Server. It guides one repository through analysis, workspace preparation,
development, independent code review, QA, and explicit local finalization.

## Prerequisites

- Python 3.10 or newer
- Git
- Codex CLI 0.144.0 or newer
- An authenticated Codex CLI session
- Either `uv` or `pipx`

## Install

```text
uv tool install .
# or
pipx install .
```

## Verify and run

```text
codexcli doctor --repo /path/to/git/repository
codexcli run --repo /path/to/git/repository
```

The launcher starts idle. Submit a feature request to begin real analysis, or
use `/resume` to select an unfinished run. `/options` edits user-wide capability
profiles; each run keeps the resolved profile it captured at creation.

CodexCLI never merges, pushes, creates pull requests, deletes branches, removes
worktrees, or publishes a repository implicitly. Finalization produces a
redacted Handoff Summary and leaves the selected workspace intact.

See `docs/codexcli-quickstart.md` and `docs/release-checklist-v0.1.0.md` in the
source repository for setup, troubleshooting, and release verification.
