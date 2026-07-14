# CodexCLI Workflow Runner

Source: local-only PRD `docs/prd/codexcli-workflow-runner.md`

Release deadline: Friday, July 17, 2026. The release candidate must be complete Thursday evening, July 16.

## Execution Contract

- Start every Issue in a fresh Codex context.
- Use model `gpt-5.6-sol` with `model_reasoning_effort = "ultra"`.
- Execute workflows only through the real installed Codex App Server.
- Do not add a fake backend, simulated workflow, or synthetic demo mode.
- Preserve the legacy runner until the new release passes parity and release gates.
- Follow the root glossary and accepted ADRs; use enums for closed sets and validated registered values for extensible identities.

## Issues

1. [Issue 0001: Ship the installable real App Server launcher](./0001-ship-real-app-server-launcher.md)
2. [Issue 0002: Publish a resumable analysis PRD Package](./0002-publish-resumable-analysis-prd-package.md)
3. [Issue 0003: Prepare a workspace and complete development](./0003-prepare-workspace-and-complete-development.md)
4. [Issue 0004: Review and QA an Issue to completion](./0004-review-and-qa-an-issue.md)
5. [Issue 0005: Process rework and dependent Issues](./0005-process-rework-and-dependent-issues.md)
6. [Issue 0006: Recover the exact workflow cursor after shutdown](./0006-recover-exact-workflow-cursor.md)
7. [Issue 0007: Complete shared TUI operations and capability profiles](./0007-complete-shared-tui-and-capabilities.md)
8. [Issue 0008: Finalize and release v0.1.0](./0008-finalize-and-release-v0-1-0.md)

## Dependency Order

`0001 -> 0002 -> 0003 -> 0004 -> 0005 -> 0006 -> 0008`

`0002 -> 0007 -> 0008`
