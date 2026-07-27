# Dev Loop CodexCLI v0.2.1

Release date: July 27, 2026

Dev Loop v0.2.1 adds configurable execution backends and model catalogs to the
portable workflow, including Claude Code support, while retaining Codex CLI as
the existing default backend.

## Highlights

- Select an Execution Backend, model, reasoning effort, and Fast preference per
  agent-backed Workflow Step from the full-screen workflow editor.
- Browse Codex CLI and bundled Claude Code model catalogs without leaving the
  current application frame.
- Validate only the backends referenced by the resolved Workflow before work
  begins.
- Pause safely on provider account, usage, service, or model-access blockers
  without consuming an Issue attempt.
- Record requested and serving models, backend identity, and available usage
  evidence in durable Step Attempt Records.
- Apply matching-step workflow preferences, including Execution Backend
  switches, to subsequent attempts on unfinished Current Runs without changing
  their issue cursors or completed attempt history.
- Migrate resolved v2 Workflow Runs to v3 in place after verifying their stored
  workflow hash, preserving the active Issue, Step, pass, history, and budgets.
- Resume runs whose historical Step Attempt Records use the former
  Codex-specific guidance-precedence wording, while preserving that audit
  evidence and rejecting unknown precedence values.
- Make Ctrl+C stop the active portable operation and exit the full-screen
  application cleanly.
- Keep recovery-only editor states restricted to recovery actions so choosing a
  model cannot silently return to an invalid normal menu.

## Release artifacts

- `devloop_codexcli-0.2.1-py3-none-any.whl`
- `devloop_codexcli-0.2.1.tar.gz`

The release operator must attach artifacts produced by the clean release gate.
Do not reuse an archive from another version or a dirty build output directory.

## Verification record

Before publication, replace every `PENDING` value below with evidence from the
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

- Saved v1 and v2 Workflow Defaults must be recreated.
- Schema v1 resolved Workflow Runs are not migrated.
- Third-party executable component installation is deferred.
- Scheduling remains sequential in one selected workspace.
- Repository publication and workspace cleanup remain explicit manual actions.

See `docs/codexcli-quickstart.md` for installation and
`docs/release-checklist-v0.2.1.md` for the required release gates.
