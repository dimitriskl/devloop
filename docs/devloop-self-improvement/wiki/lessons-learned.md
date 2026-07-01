# Dev Loop Self-Improvement Lessons

Durable, evidence-backed lessons that improve future Dev Loop runs.

## Entries

## Treat ChromeHeadless Startup Failures As Local Gate Residuals

- Applies to: coder, reviewer, QA, Angular Karma gates on Windows
- Lesson: When focused Karma compiles the selected spec bundle but ChromeHeadless cannot start because of the known GPU or persistent-cache failure, do not treat that as an application regression.
- Evidence: Issues 0003, 0004, and 0005 repeatedly compiled the selected Angular spec bundles, then ChromeHeadless failed before executing assertions with GPU/cache startup errors.
- Action: Pair the compile-only Karma attempt with TypeScript, ESLint, production build, and focused backend checks, then report browser assertions as a local-environment residual risk.
- Last seen: 2026-06-30

## Strip Generated Filters By Structure

- Applies to: coder, reviewer, QA, UI grid filter persistence
- Lesson: Cleanup logic for generated grid filters must identify the generated expression shape, not delete every column filter with the same operator and value.
- Evidence: Issue 0003 pass 1 removed any known-column `contains` filter matching the search text, so review found it could drop a legitimate header/column filter when clearing the search panel.
- Action: Add regressions that combine a real user filter with the generated search/filter group and prove cleanup preserves the real filter.
- Last seen: 2026-06-30

## Preserve Visibility Metadata On Every Viewer Surface

- Applies to: coder, reviewer, QA, public/authenticated UI parity
- Lesson: If backend payloads include hidden columns for filtering or compatibility, every viewer surface must still bind returned visibility metadata before rendering them.
- Evidence: Issue 0004 pass 1 exposed hidden public report columns because the public viewer rendered returned columns without the authenticated viewer's visibility binding.
- Action: When adding shared report/grid metadata, compare authenticated and public templates and test hidden-but-filterable columns in grouped and non-grouped views.
- Last seen: 2026-06-30

## Missing Optional Memory Tools Should Not Block Completion

- Applies to: Dev Loop agents, AutoMem integration, evidence capture
- Lesson: If an issue or instruction asks for memory storage but the memory MCP tools are not exposed in the active Codex tool context, finish the issue with repository evidence instead of blocking.
- Evidence: Issues 0003, 0004, and 0005 all reported unavailable AutoMem store/recall tools while recording implementation or verification evidence in current-feature/TDD docs.
- Action: Note the missing memory tool as a residual risk or finding, and write durable task evidence to the nearest approved project documentation.
- Last seen: 2026-06-30

## Captured Subprocess Output Must Use UTF-8 Replacement

- Applies to: Dev Loop runner on Windows, Codex and Git subprocess capture
- Lesson: Do not rely on the active Windows console code page when capturing subprocess output; force UTF-8 decoding with replacement and normalize missing stdout/stderr to empty strings.
- Evidence: A run crashed with `UnicodeDecodeError` in `encodings\cp1253.py`, then `TypeError: sequence item 0: expected str instance, NoneType found` while joining captured Codex output.
- Action: Use `run_captured_text()` for captured subprocess calls and `output_text()` before accumulating stdout/stderr.
- Last seen: 2026-06-23

## Issue Indexes Should Only Enqueue Issue Files

- Applies to: Dev Loop issue-pack parsing
- Lesson: A README can link to the parent PRD for context, but the runner should only enqueue Markdown files under the issue index folder.
- Evidence: A run selected `PRD` before `0001` because the issue README contained a parseable PRD link.
- Action: Keep issue links under `issues/`, and ignore links outside the issue folder during parsing.
- Last seen: 2026-06-23

## Exercise Orchestration Paths For Stateful Installers

- Applies to: coder, reviewer, QA, installer and service-management workflows
- Lesson: Focused helper tests are not enough when the acceptance risk is in the full orchestration sequence and shared external state.
- Evidence: Issue 0004 pass 1 focused tests passed, but review found `PerformInstallationAsync` could stop an existing base service before adding missing suffix services and leave it stopped; pass 2 added a fake service-manager full-flow regression.
- Action: For installer/service slices, include at least one orchestration-level test with fake external state for stop/start, create, preserve, and add-only paths.
- Last seen: 2026-06-30

## Compare Required Indexed Sets, Not Counts

- Applies to: coder, reviewer, QA, indexed resources and instance-scaling workflows
- Lesson: When a selected total means required indexes, sparse existing data must be compared as a set of required indexes instead of by count.
- Evidence: Issue 0005 pass 1 treated `NodeService` plus `NodeService_2` as already satisfied for target total 2, so `NodeService_1` was not created; pass 2 computed indexes `0..targetTotalInstances - 1` and added a non-contiguous regression.
- Action: Add sparse/non-contiguous fixtures whenever issue logic creates, updates, or validates numbered instances.
- Last seen: 2026-06-30

## Recover Locked Dotnet Builds Sequentially

- Applies to: reviewer, QA, Windows dotnet build and test gates
- Lesson: Locked `obj` artifacts after overlapping build/test work should be treated as an environment retry path, not as product failure.
- Evidence: Issue 0002 recorded an initial locked artifact build failure that passed after build-server shutdown and sequential `-m:1` / `UseSharedCompilation=false` settings.
- Action: On CS2012 or locked intermediate artifacts, stop dotnet build servers and rerun the scoped build sequentially with an isolated output directory.
- Last seen: 2026-06-30

## Prefer Local Package Cache When Feeds Are Unreachable

- Applies to: coder, reviewer, QA, dependency restore on restricted Windows runs
- Lesson: If default NuGet or vendor feeds are unreachable, restore once from the local package cache and then run scoped gates with `--no-restore`.
- Evidence: Issue 0001 restore required `--ignore-failed-sources --source C:\Users\Dimitris\.nuget\packages`, and later installer tests/builds used no-restore gates.
- Action: Preserve the restore command in verification evidence and avoid repeated network-dependent restores inside later passes.
- Last seen: 2026-06-30

## Record Alternate Completion Evidence When Issue Files Are Read-Only

- Applies to: coder, Dev Loop sandboxing, issue-pack completion evidence
- Lesson: If sandbox permissions prevent updating issue markdown checkboxes, completion evidence still needs to be recorded in writable project evidence files and surfaced as a residual risk.
- Evidence: Issue 0005 pass 1 could not update issue acceptance boxes under `issues\...`; the coder recorded completion in `context/current-ttd.md` and the TDD spec instead.
- Action: Do not force edits outside writable roots; update the nearest writable evidence docs and report exactly which issue-file markers remain stale.
- Last seen: 2026-06-30
