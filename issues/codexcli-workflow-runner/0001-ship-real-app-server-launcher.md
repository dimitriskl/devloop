Label: completed
Completed: [x]

## Parent

Local-only PRD: `docs/prd/codexcli-workflow-runner.md`

## What to build

Deliver the first real vertical slice of the new runner: an isolated install creates the `codexcli` command, `codexcli doctor` validates the local environment and completes a real Codex App Server initialize handshake, and `codexcli run` opens the Textual Run Launcher with the shared Composer without starting a Workflow Run. Establish the new architecture boundaries, typed identifiers and enums, real App Server transport, centralized path/config constants, and a thin application entry point without importing legacy orchestration.

Start this Issue in a fresh Codex context. Use `gpt-5.6-sol` with ultra reasoning. There is no executable backend other than the real Codex App Server.

## Acceptance criteria

- [x] `pyproject.toml` installs on Python 3.10+ through `uv tool` and `pipx` and exposes `codexcli`.
- [x] `codexcli doctor` reports typed checks for Python, Git, repository discovery, Codex CLI discovery/version, authentication readiness, App Server initialize handshake, terminal capability, and local storage.
- [x] Doctor failures are actionable, redact sensitive data, and return a nonzero exit code.
- [x] `codexcli run` opens a responsive Textual Run Launcher and shared Composer but creates no Workflow Run until the user submits a feature.
- [x] The Composer supports Unicode, multiline editing, selection, undo/redo, paste, history, and a registry-driven `/` menu foundation.
- [x] The execution boundary launches `codex app-server --listen stdio://`, performs JSON-RPC initialization, tracks request IDs, handles notifications, and shuts down cleanly.
- [x] App Server protocol types do not leak into domain, workflow, persistence, or Textual view models.
- [x] The new modules do not import legacy `cli`, `interactive_runner`, `chat_loop`, `codex_runner`, or legacy state models.
- [x] Pure tests cover configuration, doctor outcomes, command parsing, transport framing, and launcher behavior; an authorized integration test uses the real App Server handshake.
- [x] Ruff, mypy, and the focused test suite pass.

## Blocked by

None - can start immediately.
