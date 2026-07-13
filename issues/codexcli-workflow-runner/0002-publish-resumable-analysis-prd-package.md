Label: completed
Completed: [x]

## Parent

Local-only PRD: `docs/prd/codexcli-workflow-runner.md`

## What to build

Turn a launcher feature request into a real, resumable analysis Workflow Run. Register the built-in analysis component through the same Component Registry contract used by future components, load a versioned Workflow Definition, persist write-ahead Run Events and atomic snapshots, stream the real analysis thread into `AnalysisView`, and require explicit acceptance before atomically publishing the project-owned PRD Package and IssueSet.

Start this Issue in a fresh Codex context. Supply only this Issue, the relevant PRD/ADR decisions, repository constraints, and outputs from Issue 0001. Use `gpt-5.6-sol` with ultra reasoning.

## Acceptance criteria

- [x] A submitted feature creates a Workflow Run with stable IDs, locked component metadata, resolved workflow metadata, lifecycle enums, and a Run Lease.
- [x] The built-in analysis component is discovered through a versioned Component Manifest and invoked through a framework-neutral runner.
- [x] Analysis uses one real resumable App Server thread and supports user clarification through the shared Composer.
- [x] Analysis drafts autosave only in the Git-ignored Run Directory and survive pause, process exit, and explicit `/resume`.
- [x] `AnalysisView` presents PRD and Issues drafts, validation findings, streamed activity, and `REQUEST_CHANGES`/`ACCEPT` intents.
- [x] Acceptance validates PRD sections, stable requirement IDs, Issue sections, acceptance-criterion IDs, unique Issue IDs, filenames, dependency references, dependency cycles, requirement coverage, schemas, ports, and hashes.
- [x] Acceptance atomically publishes `prd/<feature-slug>/<feature-slug>.md`, `issues/index.json`, and stable Issue Markdown files without overwriting unrelated content.
- [x] The IssueSet index records its schema, owning Run ID, PRD hash, ordered Issue metadata, dependencies, requirement references, filenames, and hashes, but no runtime statuses.
- [x] `/resume` lists unfinished current-project runs and never starts one automatically; selecting the analysis run validates locks and continues its persisted thread.
- [x] Event replay reconstructs the same snapshot after interruption, and a partial final event cannot destroy earlier valid state.
- [x] Real App Server integration demonstrates feature request, clarification, pause, explicit resume, acceptance, and PRD Package publication.
- [x] Ruff, mypy, pure tests, Textual tests, and the focused real-backend test pass.

## Blocked by

- [Issue 0001: Ship the installable real App Server launcher](./0001-ship-real-app-server-launcher.md)
