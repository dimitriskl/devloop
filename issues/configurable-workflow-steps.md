Label: ready-for-agent

# Configurable Workflow Steps

## Target Product

Product: devloop-plan + devloop

This PRD targets the portable **`devloop-plan + devloop`** application started
through `bin/devloop-plan.sh` / `bin/devloop-plan.ps1` and
`bin/devloop.sh` / `bin/devloop.ps1`.

The primary implementation modules are `interactive_runner.py`, `chat_loop.py`,
`cli.py`, `codex_runner.py`, `issue_pack.py`, `state.py`, `statusui.py`,
`lineeditor.py`, `catalog.py`, and `worktree.py`, plus new portable-runner
modules beside them when a deep module is needed.

The separately installed `codexcli` Textual application is not the target.
Changes under `src/devloop/application/`, `components/`, `domain/`,
`execution/`, `persistence/`, `ui/`, or `workflow/` do not satisfy this PRD.
See `docs/product-boundaries.md`.

## Problem Statement

Dev Loop presents Analysis, Development, Code Review, and QA as fixed phases even though its architecture is intended to support installed Workflow Step Components and declarative Workflow Definitions. The `/options` experience currently configures capabilities by component type, execution profiles can silently change reasoning effort, workflow persistence and scheduling assume one instance of each built-in phase, and the dashboards cannot distinguish two steps backed by the same component.

As a result, a user cannot construct the workflow that fits the work. The user cannot insert a Security Review before a Final Review, choose different Codex models and reasoning efforts for each instance, enable Fast for only selected steps, add step-specific guidance, or see every distinct step and its elapsed time while Dev Loop runs. The fixed presentation also makes it difficult to know whether a long-running step is active or stuck.

## Solution

Provide a transactional Workflow Editor through `/options`. Treat each Workflow Step as an independently configured object with a permanent UUIDv4 Step Instance ID, a unique editable display name, a selected installed Workflow Step Type, a position on the Primary Path or an outcome branch, typed Port Bindings, explicit Outcome Transitions, per-step capabilities, optional Step Guidance, an Execution Budget, and—when the component is Codex-backed—authoritative model, reasoning-effort, and Fast settings.

The User Workflow Default remains freely editable and applies to new Workflow Runs. Each new run captures an immutable resolved Workflow Definition and per-step configuration so pause, resume, recovery, evidence, and auditing remain deterministic. Runtime state, attempt history, Issue progress, and dashboard projections become generic and Step Instance ID keyed rather than hardcoded around Development, Review, and QA.

The same dynamic Workflow Progress Dashboard serves the portable planning intake, the bounded implementation console, Bash and PowerShell entry points, and redirected output. Every distinct step—including multiple instances of the same component type—has its own status, pass, timer, result, and activity presentation.

## User Stories

1. As a Dev Loop user, I want `/options` to open a Workflow Editor, so that I can configure the whole delivery workflow in one place.
2. As a Dev Loop user, I want the editor to show the Primary Path in execution order, so that I can understand the normal successful flow immediately.
3. As a Dev Loop user, I want to select a step and inspect all of its settings, so that configuration remains focused and understandable.
4. As a Dev Loop user, I want to add a step, so that I can introduce another responsibility into the workflow.
5. As a Dev Loop user, I want to insert a step at a selected position, so that I can extend a workflow without rebuilding it.
6. As a Dev Loop user, I want to duplicate a step, so that I can create a similar specialized step efficiently.
7. As a Dev Loop user, I want to delete a step transactionally, so that I can simplify the workflow without corrupting its graph.
8. As a Dev Loop user, I want to move a step up or down, so that I can adjust the successful execution order quickly.
9. As a Dev Loop user, I want to enter a one-based Position number, so that I can move a step directly in a long workflow.
10. As a Dev Loop user, I want positions renumbered without gaps, so that the Primary Path remains easy to read.
11. As a Dev Loop user, I want Undo while editing, so that I can safely explore workflow changes.
12. As a Dev Loop user, I want Cancel to discard the entire draft, so that an experiment cannot alter my future runs accidentally.
13. As a Dev Loop user, I want Apply to be atomic, so that the saved workflow is always complete and valid.
14. As a Dev Loop user, I want Reset Step to restore component defaults, so that I can recover from complicated per-step edits.
15. As a Dev Loop user, I want Reset Workflow to restore the built-in workflow, so that I can return to a known working baseline.
16. As a Dev Loop user, I want each step to receive a UUIDv4 identity, so that references remain stable when names and positions change.
17. As a Dev Loop user, I want machine GUIDs hidden during normal editing, so that technical identity does not clutter the interface.
18. As an advanced user, I want to inspect a step GUID, so that I can diagnose persisted bindings and run state.
19. As a Dev Loop user, I want to rename a step independently of its type, so that the dashboard describes its actual responsibility.
20. As a Dev Loop user, I want display names to be unique within the workflow, so that transition and dashboard labels are unambiguous.
21. As a Dev Loop user, I want a Type picker populated from installed Workflow Step Components, so that built-in and custom component types are available.
22. As a component author, I want my installed component to appear without a core enum change, so that the workflow type catalog remains extensible.
23. As a Dev Loop user, I want the selected component's Step Scope displayed, so that I know whether it runs once or per Issue.
24. As a Dev Loop user, I want Step Scope to remain component-owned, so that a configured step cannot contradict its Step Contract.
25. As a Dev Loop user, I want any built-in step to be replaceable or removable, so that the core does not impose a fixed phase list.
26. As a Dev Loop user, I want Apply disabled when no valid start or successful terminal path exists, so that an unusable workflow cannot be saved.
27. As a Dev Loop user, I want `SUCCEEDED` ordering represented as the Primary Path, so that common editing remains simple.
28. As a Dev Loop user, I want to edit every supported Step Outcome destination, so that I can configure changes, blocked, failed, and cancelled routes.
29. As a Dev Loop user, I want to route an outcome to an existing step, so that I can create loops and shared branches.
30. As a Dev Loop user, I want to create a new step directly on a transition, so that branch construction remains efficient.
31. As a Dev Loop user, I want to insert a step between a transition source and destination, so that graph changes are explicit.
32. As a Dev Loop user, I want to terminate an outcome explicitly, so that terminal behavior is intentional and visible.
33. As a Dev Loop user, I want branch-local ordering, so that branch steps do not pretend to have one global list position.
34. As a Dev Loop user, I want a live graph preview, so that I can understand the effect of structured transition edits.
35. As a keyboard user, I want graph editing through structured controls, so that drag-and-drop is never required.
36. As a Dev Loop user, I want compatible required inputs connected automatically when there is exactly one source, so that routine insertion is quick.
37. As a Dev Loop user, I want ambiguous compatible sources presented for selection, so that Dev Loop never guesses which artifact I intended.
38. As a Dev Loop user, I want missing and incompatible Port Bindings explained, so that I can repair the workflow before Apply.
39. As a Dev Loop user, I want bindings visible under an Advanced section, so that the normal editor remains approachable.
40. As a Dev Loop user, I want deletion to list affected transitions and bindings, so that destructive effects are understood beforehand.
41. As a Dev Loop user, I want deletion to avoid cascading into downstream steps, so that one action cannot erase unrelated workflow work.
42. As a Dev Loop user, I want only an unambiguous Primary Path success link repaired automatically after deletion, so that safe convenience does not become guessing.
43. As a Dev Loop user, I want a duplicated step to receive a new GUID and unique name, so that it is a genuinely independent instance.
44. As a Dev Loop user, I want duplicated Step Guidance marked for review, so that copied instructions are not applied blindly to a new responsibility.
45. As a Dev Loop user, I want unused duplicate outputs reported as warnings, so that I can bind downstream consumers deliberately.
46. As a Dev Loop user, I want every Codex-backed step to select its own model, so that expensive reasoning is reserved for the steps that need it.
47. As a Dev Loop user, I want every Codex-backed step to select its own reasoning effort, so that quality and latency match the responsibility.
48. As a Dev Loop user, I want every Codex-backed step to select Fast independently, so that speed is an explicit per-step choice.
49. As a Dev Loop user, I want Fast to default Off, so that increased service-tier usage is never enabled implicitly.
50. As a Dev Loop user, I want model choices loaded from the live Codex Model Catalog, so that `/options` reflects my installation and account.
51. As a Dev Loop user, I want reasoning choices filtered by the selected model, so that unsupported combinations cannot be selected.
52. As a Dev Loop user, I want Fast enabled only when advertised by the selected model, so that the saved setting is executable.
53. As a Dev Loop user, I want model-catalog pagination handled transparently, so that all available models appear.
54. As a Dev Loop user, I want cached model data used only for display, so that stale availability cannot authorize a run.
55. As a Dev Loop user, I want a Retry Catalog action after discovery failure, so that temporary backend problems are recoverable.
56. As a Dev Loop user, I want preflight to block an unavailable model, effort, or Fast tier, so that execution never silently falls back.
57. As a Dev Loop user, I want the invalid step and setting named in preflight, so that I can repair it directly in `/options`.
58. As a Dev Loop user, I want explicit per-step settings to override global Codex defaults, so that changing `/fast` elsewhere cannot alter a snapshotted workflow.
59. As a Dev Loop user, I want automatic FULL/LIGHTWEIGHT model-and-effort switching removed, so that Dev Loop honors my exact selections.
60. As a Dev Loop user, I want timeout and checkpoint settings kept as a separate Execution Budget, so that operational limits do not change intelligence.
61. As a Dev Loop user, I want local deterministic component types to show that no Codex model is required, so that meaningless fields are not displayed.
62. As a Dev Loop user, I want Analysis to default to Sol with xhigh reasoning, so that ambiguous planning receives deep reasoning.
63. As a Dev Loop user, I want Development to default to Luna with high reasoning, so that scoped implementation favors focused execution.
64. As a Dev Loop user, I want Code Review to default to Sol with xhigh reasoning, so that difficult correctness checks receive deep scrutiny.
65. As a Dev Loop user, I want QA to default to Terra with high reasoning, so that pragmatic verification receives strong tool use.
66. As a Dev Loop user, I want each step to own an independent Step Capability Profile, so that duplicate component types can use different Skills and Agent References.
67. As a Dev Loop user, I want Required Capabilities locked with an explanation, so that customization cannot invalidate a component contract.
68. As a Dev Loop user, I want replaceable capabilities searchable and toggleable, so that existing `/options` capability behavior is preserved.
69. As a Dev Loop user, I want new steps to copy component capability defaults, so that they begin with a useful configuration.
70. As a Dev Loop user, I want optional multiline Step Guidance, so that I can give a specific model responsibility additional direction.
71. As a Dev Loop user, I want Step Guidance stored per instance, so that Security Review and Final Review can receive different instructions.
72. As a Dev Loop user, I want component contracts and execution policy to outrank Step Guidance, so that prose cannot weaken permissions or output requirements.
73. As a Dev Loop user, I want Step Guidance checked and redacted before persistence, so that secrets are not written into workflow configuration.
74. As a Dev Loop user, I want guidance included in every portable-runner attempt prompt and saved attempt context, so that execution is reproducible and inspectable.
75. As a Dev Loop user, I want guidance preserved but marked `NEEDS_REVIEW` after a Type change, so that my text is neither lost nor reused silently.
76. As a Dev Loop user, I want Apply blocked until stale guidance is kept, edited, or cleared, so that type changes remain deliberate.
77. As a Dev Loop user, I want Type changes to preserve GUID, display name, and position, so that the logical step remains identifiable.
78. As a Dev Loop user, I want Type changes to reset type-dependent settings, so that configuration from the old component is not carried into the new one.
79. As a Dev Loop user, I want `/options` changes saved as my User Workflow Default, so that they apply across future Dev Loop sessions.
80. As a Dev Loop user, I want Apply to replace that default atomically, so that partial configuration cannot be observed.
81. As a Dev Loop user, I want an active run's workflow to remain immutable, so that pause, resume, and recovery are deterministic.
82. As a Dev Loop user, I want `/options` during a run to separate Current Run from Future Runs, so that the scope of my changes is obvious.
83. As a Dev Loop user, I want Current Run settings inspectable but read-only, so that I can see exactly what the run is using.
84. As a Dev Loop user, I want a clear message that edits affect new runs only, so that I do not expect a running thread to change.
85. As a Dev Loop user, I want each `*.loop.state.json` run state to store its resolved workflow and canonical hash, so that later resume and diagnosis use exact configuration.
86. As a Dev Loop user, I want no workflow revision archive, so that local configuration remains simple while run snapshots preserve evidence.
87. As a Dev Loop user, I want Issue Status to use the generic `IN_PROGRESS` enum member, so that lifecycle does not encode a fixed component type.
88. As a Dev Loop user, I want the current Step Instance ID stored separately from Issue Status, so that repeated and custom steps are represented exactly.
89. As a Dev Loop user, I want every execution retained as a Step Attempt Record, so that duplicate reviews and retries never overwrite one another.
90. As a Dev Loop user, I want ordinary inputs to resolve the latest compatible successful output, so that downstream steps receive current evidence.
91. As a Dev Loop user, I want rework to consume the exact record that requested changes, so that corrections address the triggering findings.
92. As a Dev Loop user, I want failed, blocked, or cancelled outputs excluded unless explicitly permitted, so that invalid artifacts do not flow forward.
93. As a Dev Loop user, I want generic Step Runtime States keyed by step and optional Issue, so that any installed component can pause and resume.
94. As a Dev Loop user, I want every distinct step shown separately on the dashboard, so that two review instances never collapse into one row.
95. As a Dev Loop user, I want every step row to retain its status, pass, and elapsed time, so that completed work remains visible.
96. As a Dev Loop user, I want rework time accumulated for the same step, so that total effort is visible without losing pass information.
97. As a Dev Loop user, I want the active activity line to show display name, model, effort, Fast, spinner, elapsed time, and latest safe activity, so that I know the run is working.
98. As a Dev Loop user, I want workflow-scoped and issue-scoped progress separated visually, so that global setup is not confused with the active Issue path.
99. As a Dev Loop user, I want branch-only steps shown when visited or expanded, so that the main dashboard remains compact without hiding execution.
100. As a Dev Loop user, I want long workflows to scroll while keeping the active step visible, so that progress remains understandable on small terminals.
101. As a Dev Loop user, I want PASS green, FAIL and BLOCKED red, WORKING yellow, and WAITING neutral, so that status is quickly recognizable.
102. As a Dev Loop user, I want text labels to carry all status meaning, so that color is optional and accessible.
103. As a Dev Loop user, I want the same progress projection in the planning intake, implementation console, Bash, PowerShell, and redirected output, so that behavior does not depend on the wrapper.
104. As a Dev Loop user, I want redirected output to remain append-only and readable, so that logs preserve durable progress without cursor control sequences.
105. As a Dev Loop user, I want the line editor, slash commands, search, paste, and capability installer preserved, so that workflow configuration does not remove existing abilities.
106. As a Dev Loop user, I want narrow and wide layouts that do not overlap or wrap critical state, so that the console remains usable across terminal sizes.
107. As a maintainer, I want validation errors tied to Step Instance IDs and display names, so that failures remain actionable after reordering.
108. As a maintainer, I want closed lifecycle and outcome sets represented by enums, so that domain state is never scattered as magic strings.
109. As a maintainer, I want extensible component and catalog identities represented by validated value objects, so that installed additions do not require enum edits.
110. As a maintainer, I want all saved drafts and snapshots content-addressed and validated at boundaries, so that corruption fails clearly before execution.

## Implementation Decisions

- Introduce portable Workflow Definition schema `devloop.portable-workflow/v2`. This is an intentional hard cutover for the portable runner; no v1 workflow loader, migration, or compatibility mode is required.
- Model an installed Workflow Step Component as the reusable type and a Workflow Step as a distinct configured instance of that type.
- Give every Workflow Step a canonical lowercase hyphenated UUIDv4 Step Instance ID generated once at creation. Preserve it through rename, movement, Type change, export, default replacement, and Run Snapshotting.
- Add a unique non-empty Step Display Name distinct from the Step Component ID and Step Instance ID.
- Populate the Workflow Step Type picker from a portable Workflow Step Component Catalog built from the installed role/skill catalog and portable step adapters. Do not import or reuse the CodexCLI Component Registry, and do not introduce a closed Analysis/Development/Review/QA type enum.
- Keep Step Scope component-owned and read-only in `/options`.
- Do not hardcode any component type as mandatory. Validate graph reachability, required ports, supported outcomes, scope compatibility, one start, and at least one successful terminal path.
- Represent the normal ordered flow as the Primary Path obtained from `SUCCEEDED` transitions. Expose one-based editable Primary Path Position, Move Up, and Move Down as equivalent transactional operations.
- Represent secondary outcomes as explicit graph transitions. Provide structured destination selection, new-step creation, insert-on-route, explicit terminal outcomes, loops, branch reconnection, and branch-local ordering.
- Render a live graph preview from the draft but keep typed transitions and Port Bindings authoritative.
- Auto-bind an Input Port only when exactly one compatible upstream output exists. Require explicit selection for zero or multiple candidates.
- Keep Port Binding editing in an Advanced section and block Apply for missing, incompatible, or ambiguous required inputs.
- Make step deletion non-cascading. Repair only the unambiguous Primary Path success link, remove invalid producer bindings, retain downstream nodes and branches, and require explicit repair before Apply.
- Make Duplicate insert immediately after the source with a new UUIDv4 and unique name. Copy component Type, Codex settings, Execution Budget, capabilities, input bindings, and non-success transitions; copy Step Guidance as `NEEDS_REVIEW`; do not silently redirect consumers to duplicate outputs.
- Replace the current portable `/options` capability menu with a transactional terminal Workflow Editor. Preserve search, required-capability locking, GitHub capability installation, Reset, Apply, Cancel, the line editor, and existing slash-command behavior.
- Use a two-pane layout on wide terminals and a stacked layout on narrow terminals. The left side owns workflow navigation/actions; the selected-step pane owns General, Codex, Execution Budget, Capabilities, Step Guidance, Inputs, Outcomes, and Advanced sections.
- Store each Step Capability Profile on the Workflow Step instance. Component Required and Default Capabilities seed a new instance; required entries remain locked and replaceable entries remain user-editable.
- Add optional bounded multiline Step Guidance to agent-backed steps. Persist it after shared secret handling, include it in every generated role prompt and saved attempt context, and give component instructions, Step Contract, Step Execution Policy, output schema, required capabilities, and safety boundaries precedence.
- Preserve user-authored Step Guidance across a Type change but mark it `NEEDS_REVIEW`; block Apply until the user chooses Keep, Edit, or Clear.
- On Type change, preserve UUIDv4, display name, and position while resetting Type-dependent capabilities, Codex settings, Execution Budget, ports, bindings, and supported outcomes to new component defaults.
- Replace component-keyed user capability defaults with one mutable User Workflow Default containing the complete per-step configuration. Apply writes it atomically; Cancel does not write; Reset restores the built-in default.
- Do not add an immutable workflow-revision archive. Every portable development run instead stores its immutable resolved Workflow Definition and canonical hash in the issue pack's `*.loop.state.json`.
- During an active run, `/options` shows a read-only Current Run view and an editable Future Runs view with an explicit scope message. It cannot mutate unstarted steps in the current Run Snapshot.
- Extend component execution defaults to provide optional Codex Execution Settings and an independent Execution Budget. Remove FULL/LIGHTWEIGHT intelligence profiles and issue-size-based model or effort switching.
- For agent-backed steps, Codex Execution Settings contain model, reasoning effort, and explicit Fast preference. Every attempt uses the values frozen in the Run Snapshot.
- For local deterministic components, omit Codex settings and display a local-execution explanation rather than disabled meaningless fields.
- Load the live account-aware model picker through a small portable Codex Model Catalog adapter beside `codex_runner.py`. The adapter may query the installed Codex backend, but it must not depend on CodexCLI RunStore, Workflow Runs, Textual UI, or CodexCLI application/domain modules.
- Cache the most recently loaded model catalog for display only. A fresh preflight must authorize every selected model/effort/Fast combination before a run starts.
- Do not expose unrestricted free-text model input in the normal editor.
- Map Fast On and Off to explicit arguments/configuration supported by the installed `codex exec` command so a user-global Fast default cannot change the snapshotted step setting. Verify this mapping at the portable command-construction seam.
- Pass selected model, reasoning effort, and Fast setting in every fresh or resumed portable `codex exec` role command. Do not rely on a previously active Codex CLI session setting.
- Use the following built-in defaults when available in the live catalog: Analysis `gpt-5.6-sol`/`xhigh`; Development `gpt-5.6-luna`/`high`; Code Review `gpt-5.6-sol`/`xhigh`; QA `gpt-5.6-terra`/`high`; Fast Off for all.
- Block preflight rather than silently falling back when any configured model, effort, or service tier is unavailable. Name the affected Step Display Name and field and provide a route back to `/options`.
- Retain Execution Budget timeouts and checkpoint deadlines independently from model intelligence.
- Keep `IssueStatus` as a closed enum and replace phase-specific members with generic `IN_PROGRESS`; store the exact active Step Instance ID separately.
- Replace fixed coder/reviewer/QA cursor fields in `state.py` with generic Step Runtime States keyed by Step Instance ID and, for issue-scoped steps, Issue number. Analysis handoff state remains owned by `interactive_runner.py` but uses the same resolved workflow identity when development starts.
- Store current status, pass, backend thread and turn identity, checkpoint, and component-owned resumable state in each Step Runtime State.
- Replace fixed implementation/review/QA attempt fields with immutable Step Attempt Records keyed by Step Instance ID and optional Issue ID. Record pass, thread/turn, outcome, typed output Artifacts by Output Port, timing, and blocked/failure/rework references.
- Resolve ordinary bindings from the latest compatible `SUCCEEDED` Step Attempt Record. Resolve rework input from the exact `CHANGES_REQUESTED` record that triggered the transition. Exclude failed, blocked, or cancelled outputs unless the target contract explicitly permits them.
- Dispatch portable role execution, recovery, dashboard rows, and transitions through the portable Workflow Step Component Catalog and Step Instance ID instead of hardcoded phase constants or component-to-single-step lookup.
- Keep closed domain sets such as Issue Status, Step Run Status, Workflow Run Status, Step Outcome, guidance review state, and dashboard status as enums. Keep installed Component IDs, model IDs, service-tier IDs, and Step Instance GUIDs as validated boundary types appropriate to their open or externally advertised sets.
- Build one GUID-keyed Step Progress projection shared by every presentation. Include display name, component information, status, pass, elapsed/accumulated duration, active settings, Issue context where applicable, and safe backend activity.
- Render every issue-scoped Primary Path instance as a separate Current Issue row. Separate workflow-scoped progress, show branch-only steps when visited or expanded, retain completed durations, and accumulate rework time without losing pass information.
- Preserve the compact active Workflow Status Bar as the current-step summary within the larger dynamic dashboard.
- Keep interactive dashboards bounded and cursor-updated; keep redirected/non-TTY output append-only. Both derive from the same typed projection.
- Preserve semantic colors while retaining complete text labels: PASS green, FAIL/BLOCKED red, WORKING yellow, WAITING neutral, with `NO_COLOR` support.
- Apply the shared Python behavior to both Bash and PowerShell wrappers; do not fork presentation or workflow logic by shell.
- Supersede earlier component-keyed capability-default and one-row-only progress decisions with this per-instance workflow model.

## Testing Decisions

- Good tests assert externally observable portable-runner behavior: what `/options` displays and saves, which workflow a new run snapshots, which `codex exec` command is built, which transition is selected, which role result reaches a downstream input, what rerun resume restores, and what the user sees on the dashboard. Tests should not assert helper call order or incidental serialization implementation.
- Use the public `interactive_runner`, fake editor, command builder, issue loop, and `statusui` projection seams as the highest user-facing automated tests. Exercise opening `/options`, adding/duplicating/moving/deleting/retargeting steps, selecting Type/model/effort/Fast, editing capabilities and guidance, resolving validation, Apply/Cancel/Reset, narrow/wide layouts, and read-only Current Run versus editable Future Runs.
- Exercise the Workflow Editor through portable configuration functions and temporary directories. Verify atomic Apply, no write on Cancel, built-in Reset, GUID stability, unique names, deterministic positions, deletion impact, Duplicate semantics, stale-guidance acknowledgement, and full round-trip equality.
- Extend Workflow Definition contract tests for schema v2, UUIDv4 identities, open Component IDs, Primary Path derivation, branch-local ordering, loops, supported outcomes, start/terminal reachability, scope compatibility, and typed Port Binding validation.
- Add explicit rejection coverage for v1 workflows. Do not add migration fixtures or compatibility behavior.
- Test Codex Model Catalog discovery at its portable backend-adapter seam, including pagination when advertised, hidden/unavailable models, supported reasoning efforts, Fast availability, empty results, malformed responses, connection failure, cached display, Retry Catalog, and fresh preflight.
- Test portable `codex exec` command construction. Assert exact model, reasoning-effort configuration, Fast-on/Fast-off override, selected capability paths, component instructions, and bounded Step Guidance composition.
- Test that a user-global Codex model or Fast change cannot alter an explicitly configured Run Snapshot.
- Replace execution-profile tests that expect automatic LIGHTWEIGHT selection with tests proving per-step model/effort/Fast authority and independent Execution Budget selection.
- Extend `LoopStateWriter` round-trip and recovery tests for immutable resolved workflow snapshots, canonical hashes, generic Step Runtime States, Step Attempt Records, independent duplicate review sessions, branch cursors, and latest-compatible result resolution.
- Extend portable issue-loop tests so Issue Status remains generic `IN_PROGRESS` while current Step Instance ID changes through arbitrary issue-scoped paths and rework loops.
- Verify that two instances of the same component execute independently, receive distinct portable role prompts and Codex sessions, retain distinct outputs, and both appear in history and dashboard projections.
- Verify ordinary binding selection chooses the latest successful output, rework chooses the triggering changes-requested record, and disallowed outcomes never flow downstream.
- Test Step Guidance precedence and safety through observable role prompts and saved attempt context: the guidance is present, bounded, redacted, and unable to change permission, schema, model, Fast, or transition settings.
- Test the shared Step Progress projection once as a pure model, then render it through the planning and bounded implementation console adapters. Cover duplicate component instances, independent timers, accumulated retries, branch visibility, workflow versus Issue scope, Last Result, spinner/activity freshness, and completed-time freezing.
- Preserve console snapshot coverage for alignment, display width, Unicode, narrow terminals, `NO_COLOR`, redirected output, and absence of terminal cursor sequences outside a TTY.
- Verify Bash and PowerShell launch the same shared Python workflow behavior through wrapper argument-forwarding tests rather than duplicating workflow assertions per shell.
- Provide an optional operator-run authenticated smoke gate for the portable wrappers, but do not agent-launch that long-running real Codex integration gate. Standard-library unit/integration tests remain the required automated evidence in this implementation environment.
- Run the repository's full Python test suite, focused planner/runner tests, syntax compilation, `git diff --check`, and a local `--dry-run --no-worktree` issue-pack validation before declaring the feature complete.

## Out of Scope

- Changing or extending the separate CodexCLI Textual application, its `RunStore`, Workflow Runs, App Server execution threads, launcher, or modules under `src/devloop/application/`, `components/`, `domain/`, `execution/`, `persistence/`, `ui/`, and `workflow/`.

- Migrating, reading, resuming, or preserving v1 Workflow Definitions or v1 workflow snapshots.
- Mutating the workflow graph, execution settings, guidance, capabilities, or bindings of an already-started Workflow Run.
- Keeping immutable history or user-visible revisions of the mutable User Workflow Default.
- Adaptive or automatic model, reasoning-effort, or Fast selection based on Issue size, retry count, cost, or elapsed time.
- Silent model fallback, effort reduction, Fast disabling, or substitution when catalog validation fails.
- Unrestricted free-text model IDs in the normal `/options` editor.
- A free-form graphical drag-and-drop canvas; the live graph is a preview of structured transition controls.
- Parallel execution of independent Workflow Steps or multiple Codex agent attempts; the established one-at-a-time execution policy remains unchanged.
- Installing new Workflow Step Components from inside the Type picker. The picker consumes the portable installed component catalog.
- Allowing Step Guidance to override component contracts, permissions, approval policy, output schemas, required capabilities, safety rules, or workflow structure.
- Changing the existing portable line editor, slash-command syntax, capability GitHub installer, explicit approval flow, stop controls, or remote-side-effect restrictions except where integration with the new editor is required.
- Agent-launched authenticated Codex integration, installation, release, or publishing gates.
- Publishing this PRD to an external GitHub issue; the canonical artifact is local to the repository.

## Further Notes

- The accepted analysis deliberately treats Workflow Step Components as reusable classes and Workflow Steps as independently configured objects. Two Code Review objects therefore have different GUIDs, names, settings, capabilities, guidance, runtime state, attempt history, and dashboard rows.
- The user is the only v1 Dev Loop user, so the hard schema cutover is intentional and preferred over compatibility complexity.
- Official Codex terminology is used: model, reasoning effort, and service tier/Fast are separate controls.
- Model, reasoning-effort, and Fast availability are account- and installation-dependent; the installed Codex backend queried by the portable catalog adapter is authoritative.
- The same shared implementation serves Linux and PowerShell because both wrappers enter the Python application.
- The repository's local ADR directory is intentionally ignored by Git, while the root glossary and this PRD are the durable tracked analysis artifacts.
