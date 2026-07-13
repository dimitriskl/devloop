# Dev Loop Self-Improvement Lessons

Durable, evidence-backed lessons that improve future Dev Loop runs.

## Entries

## Preflight External Acceptance Prerequisites

- Applies to: Dev Loop startup, authenticated backends, cross-platform and release workflows
- Lesson: Detect mandatory gates that require credentials, network access, another operating system, writable user storage, recording, or publication authority before starting a long issue pack.
- Evidence: Issues 0002 through 0007 repeatedly passed deterministic gates but could not run authenticated App Server scenarios because Codex was not logged in; Issues 0001 and 0008 also required unavailable Linux or clean release environments, and all eight issues finished blocked.
- Action: Preflight every non-repository prerequisite, show which acceptance gates are unavailable, and ask the operator to satisfy them or explicitly accept a partial run before issue execution.
- Last seen: 2026-07-13

## Retry External Blockers Only After State Changes

- Applies to: blocked retry rounds, coder scheduling and long-running issue packs
- Lesson: A clean Codex attempt cannot fix a proven external prerequisite, so unchanged authentication, connectivity, platform, permission, or publication blockers should not consume another retry round.
- Evidence: All eight issues entered three clean-retry rounds; Issues 0002 through 0007 repeatedly reported passing local gates with no code changes while the same missing Codex authentication or Responses API connectivity blocker remained.
- Action: Fingerprint external blockers and the relevant environment state, skip equivalent retries until that state changes, and leave one concise operator action on the loop board.
- Last seen: 2026-07-13

## Keep Completion Markers Behind Acceptance Gates

- Applies to: coder role, issue markdown and completed-issue selection
- Lesson: Do not add a parser-recognized completion marker from implementation evidence alone when reviewer, QA, or required integration gates have not passed.
- Evidence: Issue 0005 was marked `Completed: [x]` after focused verification, but its first review found five high-severity scheduler, isolation, retry, and workflow-transition defects and required removing the marker.
- Action: Let the runner write completion only after every required gate passes; if a coder encounters an existing implementation, verify it without changing issue selection state prematurely.
- Last seen: 2026-07-13

## Review Foundational Mechanisms Before Building On Them

- Applies to: reviewer scheduling, foundational launcher, persistence, process and concurrency slices
- Lesson: When an early issue establishes shared lifecycle or safety mechanisms, review its invariants deeply before later issues build on it; focused fixes can expose another layer of the same mechanism.
- Evidence: Issue 0001 failed three reviews that successively found six, nine, and five blocking defects across single-flight execution, shutdown, process trees, storage containment, leases, recovery state, and responsive layout.
- Action: Add mechanism-level stress and adversarial checks for cancellation, teardown, concurrency, containment, and cross-platform behavior at the first foundational slice, then rerun the full suite before advancing dependent issues.
- Last seen: 2026-07-13

## Isolate Tests From User Git Configuration

- Applies to: coder, reviewer, QA, temporary Git repositories and sandboxed runs
- Lesson: Tests that create or inspect repositories must not depend on readable user-level Git excludes or configuration.
- Evidence: Issue 0005 initially produced 24 broad-suite failures solely because the sandbox denied the user Git ignore file; isolated Git/XDG settings passed, and Issue 0008 later made temporary repositories hermetic by disabling inherited global excludes.
- Action: Give subprocesses process-local Git and XDG configuration, disable inherited global excludes for temporary repositories, and keep caches and tool homes inside approved writable paths.
- Last seen: 2026-07-13

## Preflight Prompt-Required Context Artifacts Once

- Applies to: Dev Loop runner, prompt assembly and coder/reviewer/QA startup
- Lesson: Resolve prompt-required context paths before role execution so every role does not repeat the same search for an absent artifact.
- Evidence: Issue 0003 reviewer and QA each reported that required `CONTEXT-MAP.md` was absent from both the target repository and bundle even though the available acceptance gates passed.
- Action: Preflight declared context paths once, tell roles when an optional fallback is intentional, and fail before execution only when a missing artifact is indispensable.
- Last seen: 2026-07-11

## Default Multi-Outcome Results To Unknown, Not Success

- Applies to: coder, reviewer, QA, command-result contracts and store implementations
- Lesson: A result contract with success, conflict, and missing outcomes must not default to success; an omitted assignment in a future implementation should fail closed.
- Evidence: Issue 0003 passed because the production reopen store explicitly assigned every outcome, but reviewer and QA independently flagged its legacy `Reopened` defaults as a future fail-open risk.
- Action: Prefer an explicit `Unknown` default or require the outcome at construction, map every outcome at each boundary, and test an implementation that omits the assignment.
- Last seen: 2026-07-11

## Keep Atomic SQL Transitions Pool-Safe

- Applies to: coder, reviewer, QA, SQL-backed state transitions and pooled database connections
- Lesson: Atomic state classification must not leave a session-level isolation change behind for the next borrower of a pooled connection.
- Evidence: Issue 0003 passed after its store classified persisted state atomically without leaking `SERIALIZABLE` into pooled connections and added generated-command locking and isolation-safety tests; all roles retained the missing live multi-session SQL test as a residual risk.
- Action: Prefer transaction-scoped locking, restore any session isolation change on every exit, and treat generated-command tests as contract evidence until live contention, cancellation, and pooled reuse are exercised.
- Last seen: 2026-07-11

## Start Gates From A Clean Tracked Diff

- Applies to: coder, reviewer, QA, dirty worktrees and verification gates
- Lesson: A passing no-build or partial focused gate is not enough when tracked non-loop deletions are present; restore or isolate unrelated deletions before accepting issue evidence.
- Evidence: Issue 0002 pass 1 had a missing shared test project and 726 deleted tracked non-loop files, so clean focused test compilation failed even though the stale no-build ProductTranslations assembly passed.
- Action: Before reviewer or QA gates, run a deleted-file check and require source-built focused tests once tracked deletions have been restored.
- Last seen: 2026-07-04

## Keep Object-Specific Paths Out Of Legacy Metadata

- Applies to: coder, reviewer, QA, template orchestration paths and compatibility metadata
- Lesson: New object-specific template paths must bypass legacy metadata branches that were designed for another object type.
- Evidence: Issue 0003 pass 1 failed because the full API orchestrator ran the legacy Products child-translation block for ProductTranslations templates carrying stale MasterTemplateId and LanguageCode metadata.
- Action: Gate legacy branches by the exact object type they serve, and add regressions with stale or rejected metadata on the new object type.
- Last seen: 2026-07-04

## Treat SQL Id As The ProductTranslations Row Identity

- Applies to: coder, reviewer, QA, ledger write-back and sync-row lookup
- Lesson: When the PRD says a returned SQL Id identifies the exact sync row to update, do not add non-identity filters that can reject that row.
- Evidence: Issue 0003 pass 2 failed because both orchestrators looked up the ProductTranslations ledger row by Id, CompanyId, and SiteType; a valid SQL-selected row with a different stored SiteType was treated as missing.
- Action: Keep only required tenant/data-safety constraints around authoritative row IDs, and test mismatched non-identity fields plus no duplicate row insertion.
- Last seen: 2026-07-04

## Resolve Durable Rows Before Fallible Rendering

- Applies to: coder, reviewer, QA, row-level error persistence and integration payloads
- Lesson: If a row has a durable identity, load that row before fallible payload rendering so validation or placeholder failures can be persisted to the operator-visible source row.
- Evidence: Issue 0005 pass 1 failed because ProductTranslations JSON rendering happened before SQL Id sync-row lookup, leaving missing-alias failures only as in-memory row errors instead of persisted ErrorMessage/ErrorDetails.
- Action: Order integration execution as identify row, render or validate payload, persist row error on failure, then call the external system only after the row-safe payload succeeds.
- Last seen: 2026-07-04

## Use Structural DOCX QA When Renderers Are Missing

- Applies to: coder, reviewer, QA, generated documentation and DOCX artifacts
- Lesson: When LibreOffice or another visual renderer is unavailable, validate generated DOCX files structurally and by required content, and report visual render QA as a residual risk.
- Evidence: Issue 0006 passed with reproducible OpenXML package and content checks while soffice was unavailable; Issue 0003 later passed package, XML, required-text, and stale-text checks while LibreOffice and Poppler were unavailable.
- Action: For DOCX slices, keep the generator reproducible, inspect the package entries and document text, compare required acceptance content, and explicitly call out skipped visual rendering.
- Last seen: 2026-07-11

## Gate Sensitive Diagnostics At Every Read Surface

- Applies to: coder, reviewer, QA, log/audit/diagnostic features, permissions
- Lesson: Sensitive diagnostic records that include raw SQL or payload text need authorization at the frontend route, backend read endpoints, missing-context path, and tenant scope; one gate alone is insufficient.
- Evidence: Issue 0005 clean retries failed first because `/logs` and human `LogsController` reads were not Logs-rights gated, then failed again because missing company context could pass and `GetAll`, `Get(Guid)`, and `GetLogSources` were not tenant-scoped.
- Action: For log/audit features, add regressions for no rights, missing company context, cross-company isolation, and allowed access before marking the slice complete.
- Last seen: 2026-07-03

## Verify Routed Deep Links At The Owning Shell

- Applies to: coder, reviewer, QA, Angular routes and query-param filters
- Lesson: Query-param behavior is not proven by testing an inner component when the route renders a different shell or hides that component behind a popup.
- Evidence: Issue 0005 failed review when `/logs?source=sql-navigator` reached the routed `TaskExecutionExplorerComponent` shell while the filtered `LogsComponent` stayed hidden in a closed popup; pass 2 fixed the owning shell to render the raw Logs grid for source-filter links.
- Action: Add route/shell-level tests for deep links and make the owning routed component open or route to the visible surface for query modes.
- Last seen: 2026-07-04

## Check Frontend Dependency Availability Before Angular Gates

- Applies to: coder, reviewer, QA, Angular worktrees
- Lesson: Missing `node_modules`, `package-lock.json`, or `@angular/build` builder packages are setup residuals, not application failures.
- Evidence: Issues 0001 and 0005 repeatedly could not start Karma or Angular build because the worktree lacked local frontend dependencies/builders, including the July 4 SQL Navigator logs run where dependency probes returned missing before Angular compilation.
- Action: Start Angular verification with `Test-Path` checks for web `node_modules`, `package-lock.json`, and required builder packages; report unavailable browser/build gates explicitly and use TypeScript, i18n, and backend gates where available.
- Last seen: 2026-07-04

## Treat Disk-Full Runtime Failures As Infrastructure Blockers

- Applies to: Dev Loop runner, Codex sandbox/runtime, issue execution
- Lesson: When shell setup and fallback runtimes both fail before file inspection with disk-full errors, the issue is blocked by host or temp storage, not code.
- Evidence: Issue 0005 first pass could not read files because PowerShell helper setup and Node REPL kernel asset writes both failed with disk error 112.
- Action: Stop the issue pass, ask the operator to free space on the sandbox or temp drive, then rerun; do not infer repository state from failed pre-command setup.
- Last seen: 2026-07-03

## Prove Missing Parents Before WooCommerce Side Effects

- Applies to: coder, reviewer, QA, translation and integration workflows
- Lesson: Missing-parent and missing-identity checks must run before any WooCommerce-facing preprocessing, lookup, create, update, or ledger write-back.
- Evidence: Issue 0002 needed two review fixes: the NodeService path called a product preprocessor before parent-ledger resolution, and the full orchestrator path only rejected null parent IDs while allowing blank or whitespace IDs into `translation_of`.
- Action: In negative-path tests, register strict preprocessors and strict API mocks, cover both simplified and full orchestrator paths, and treat null, empty, and whitespace external IDs as unsynced before side effects.
- Last seen: 2026-07-03

## Require Positive Evidence Before Ledger Adoption

- Applies to: coder, reviewer, QA, duplicate recovery and ledger write-back
- Lesson: A single non-parent search result is not enough to adopt an existing external record when the result will drive an authoritative ledger update.
- Evidence: Issue 0003 pass 1 could adopt one non-parent WooCommerce SKU match without `lang == en` or `translation_of == parentWooCommerceId`, then write `Translations["en"]` or `EnglishWooId` to the parent ledger; pass 2 required positive translation evidence.
- Action: Remove sole-candidate fallbacks from duplicate recovery paths unless they also prove the required identity, and add regressions that assert no PUT and no ledger write-back for unproven matches.
- Last seen: 2026-07-03

## Prove SQL JSON Storage Shape

- Applies to: coder, reviewer, QA, support SQL artifacts and JSON-backed contracts
- Lesson: Script-text assertions are not enough for SQL JSON mutations; prove the persisted scalar/object shape that runtime deserialization expects.
- Evidence: Issue 0004 pass 1 tests passed, but review simulated the `JSON_MODIFY` update and found `jsonTemplate` would be stored as a nested object instead of scalar JSON text; pass 2 used `CONCAT` and SQL guards to preserve the shape.
- Action: For JSON-updating SQL scripts, add a read-only SQL simulation or focused guard that checks `JSON_VALUE` and `JSON_QUERY` behavior against the persisted contract.
- Last seen: 2026-07-03

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
- Evidence: Issue 0001 restored from the local NuGet package cache after feeds failed; Issue 0003 later confirmed that already-restored focused tests and builds could complete with `--no-restore` while the unreachable vulnerability feed emitted NU1900 warnings.
- Action: Preserve the restore command in verification evidence and avoid repeated network-dependent restores inside later passes.
- Last seen: 2026-07-11

## Preserve Issue Markdown Before Recording Alternate Evidence

- Applies to: coder, Dev Loop sandboxing, issue-pack completion evidence
- Lesson: Issue markdown files are control-plane artifacts; do not delete or depend on recreating them when sandbox permissions may allow edits to product files but deny issue-pack file creation.
- Evidence: Issue 0005 could not update issue acceptance boxes, and Issue 0004 later stayed blocked through three clean retries after the required issue markdown was missing and both `apply_patch` and direct creation were denied at the issue-pack path.
- Action: Before patching issue files, verify the file exists and avoid delete/recreate flows; when issue markers cannot be updated, record completion evidence in writable project docs, report the exact stale or missing marker, and stop retrying once the same permission blocker is proven.
- Last seen: 2026-07-03
