# CodexCLI v0.1.0 Five-Minute Quick Start

## Prerequisites

Install Python 3.10 or newer, Git, the Codex CLI, and either `uv` or `pipx`.
Authenticate before using Dev Loop:

```text
codex --version
codex login
git --version
python --version
uv --version
# or: pipx --version
```

Codex App Server is the only executable backend in v0.1.0. Provider and model
configuration remains in Codex CLI.

## Install

From a clean checkout, use the isolated installer available on your machine:

```text
uv tool install .
```

or:

```text
pipx install .
```

Both expose the `codexcli` command. Reinstall a changed checkout with
`uv tool install --force .` or `pipx install --force .`.

## Doctor and run

```text
codexcli doctor --repo /path/to/git/repository
codexcli run --repo /path/to/git/repository
```

`doctor` checks Python, Git, repository state, Codex discovery/version,
authentication, the installed App Server schema contract, terminal capabilities,
and writable storage. Resolve every failure before a real run.

`run` opens the launcher and starts no work automatically. Enter a feature
request to begin analysis. Accepting analysis publishes the run-owned PRD
Package; repository changes begin only after an explicit workspace choice and
a real-backend permission preflight for that exact canonical workspace root.

## Options, runs, and resume

- `/options` chooses installed Skills and Agent References per component.
- `/profile` shows the locked model, reasoning, timeout, and checkpoint budget.
  Before a component starts, `/profile development lightweight` selects its
  supported lightweight profile without weakening acceptance or safety policy.
- `/issues` opens the read-only Issue Board.
- `/status` prints the active typed status.
- `/runs` lists current-project runs, including completed runs.
- `/pause` preserves the active run; `/cancel` is terminal.
- `/resume` lists unfinished runs and resumes only the selected run.
- `/finalize` creates the Handoff Summary after every Issue passes QA.

Finalization writes a Handoff Summary and leaves the selected workspace intact.
It never merges, pushes, opens a pull request, deletes a branch, or removes a
worktree.

## Troubleshooting

- Codex missing or unauthenticated: run `codex --version`, then `codex login`.
- App Server check fails: update Codex CLI and rerun `doctor`.
- Repository check fails: pass an existing Git work tree to `--repo`.
- Terminal warning: run directly in an interactive terminal without piping.
- Storage failure: make `.devloop/runs` and the reported user directories
  writable by the current user.
- Interrupted work: start `codexcli run`, enter `/resume`, and select the exact
  unfinished run. Unknown in-flight operations are not replayed automatically.

See `docs/release-checklist-v0.1.0.md` for clean-machine and real-backend gates.
