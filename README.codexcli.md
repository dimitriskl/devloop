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

## Built with Codex and GPT-5.6

CodexCLI runs every workflow step through the installed **Codex App Server**.
Each component locks a **GPT-5.6** model and reasoning effort: Analysis,
Development, Code review, and QA default to `gpt-5.6-sol` with `xhigh` or `low`
reasoning depending on the execution profile. Codex handles sessions, approvals,
and recovery; GPT-5.6 provides the model reasoning behind each turn.

See the full model table in `docs/codexcli-user-guide.md` and the repository
root `README.md` section **Built with Codex and GPT-5.6**.

## Try without rebuilding

```text
uv tool install .
./examples/release-demo/run-demo.sh
```

Use the request in `examples/release-demo/feature-request.md`. The script
creates a disposable Git repository and opens the real workflow against it.

See `docs/codexcli-quickstart.md` and `docs/release-checklist-v0.1.0.md` in the
source repository for setup, troubleshooting, and release verification.
