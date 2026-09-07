Label: ready-for-agent

# Portable Direct Run Defaults

## Target Product

Product: devloop-plan + devloop

This PRD targets Portable Dev Loop's Markdown issue runner and its shared core
CLI contract. It applies to direct delivery launched through the PowerShell,
Bash, or Python entry point. The separate CodexCLI product is not in scope.

## Problem Statement

A user who already has an accepted PRD Package must currently repeat values
that Dev Loop can derive or already treats as defaults. The direct runner
requires an Issue Index argument even though the canonical Issue Index belongs
under the PRD Package, and its documented command repeats the bundled preset
and positive wiki choice. More importantly, an omitted all-issues option
currently selects only one unfinished Issue, while omitted worktree options can
prompt an interactive user to create a worktree.

This makes the shortest obvious command behave differently from the intended
full-delivery workflow. Users must remember several flags, copied commands are
noisier than the decision they express, and PowerShell, Bash, Python, planning
handoff, and non-interactive use can appear to have different defaults. The
current behavior also prevents a PRD-only command from serving as a stable,
automation-friendly Portable Direct Run contract.

## Solution

Make the accepted PRD path the only required argument for a Portable Direct
Run. When no overrides are supplied, resolve the Issue Index strictly from the
canonical `issues/README.md` location inside the PRD Package, use the bundled
`generic-minimal.json` preset, process all dependency-ready unfinished Issues
in Ready Issue Order, run directly in the source checkout without a worktree
prompt, and enable self-improvement wiki reading and updates.

Keep explicit controls for every default. An explicit Issue Index or preset
overrides the derived value. A new single-issue option limits the run. An
explicit create-worktree option requests isolation. The existing no-wiki
option disables wiki access. Existing positive/default flags remain silently
accepted so scripts and operator habits continue to work.

Place this behavior at the core CLI boundary so PowerShell, Bash, and direct
Python invocation share one contract. Preserve dependency validation,
scheduling, Blocker Resolution, completion detection, resume behavior, and
worktree creation behavior once an explicit override has selected them.

## User Stories

1. As a Dev Loop user with an accepted PRD Package, I want to start delivery with only its PRD path, so that I do not repeat derivable configuration.
2. As a PowerShell user, I want the PRD-only command to launch a full Portable Direct Run, so that the common Windows command is concise.
3. As a Bash user, I want the same PRD-only behavior as PowerShell, so that platform choice does not change delivery semantics.
4. As a Python user, I want the module entry point to share the wrapper defaults, so that wrappers do not contain hidden product behavior.
5. As a Dev Loop user, I want the Issue Index inferred from the PRD Package's canonical issue subdirectory, so that accepted artifacts remain colocated.
6. As a Dev Loop user, I want Issue Index inference to use one exact location, so that Dev Loop never silently selects an unrelated legacy index.
7. As a Dev Loop user, I want a clear preflight error when the inferred Issue Index is missing, so that no Codex work starts against an incomplete package.
8. As a Dev Loop user, I want to provide an explicit Issue Index when necessary, so that exceptional package layouts remain usable by deliberate choice.
9. As a Dev Loop user, I want the bundled generic-minimal preset selected when no preset is given, so that the standard coder, reviewer, and QA workflow remains available without extra syntax.
10. As a Dev Loop user, I want an explicit preset to override the bundled default, so that specialized workflows remain supported.
11. As a Dev Loop user, I want all unfinished dependency-ready Issues selected by default, so that one command pursues the complete PRD outcome.
12. As a Dev Loop user, I want completed Issue files skipped during a default full run, so that resuming does not repeat accepted work.
13. As a Dev Loop user, I want declared dependencies validated before execution, so that a full run cannot bypass an invalid Issue graph.
14. As a Dev Loop user, I want dependencies to control eligibility and Issue Index position to retain its existing tie-breaking role, so that the new default does not change scheduling semantics.
15. As a Dev Loop user, I want independent dependency-ready work to continue when another Issue blocks, so that default full delivery preserves the existing scheduler.
16. As a Dev Loop user, I want Blocker Resolution to retain its bounded behavior, so that default full delivery does not become an unlimited retry loop.
17. As a Dev Loop user, I want a single-issue option, so that I can deliberately run one bounded work item despite the new all-issues default.
18. As a Dev Loop user, I want the single-issue option to select the first dependency-ready unfinished Issue when no start Issue is supplied, so that bounded behavior is deterministic.
19. As a Dev Loop user, I want a start Issue to begin a full run at that Issue by default, so that later unfinished work continues in the same invocation.
20. As a Dev Loop user, I want a start Issue combined with the single-issue option to stop after that selected Issue, so that targeting and run breadth are independent choices.
21. As a Dev Loop user, I want an omitted worktree option to use the source checkout directly, so that the PRD-only command starts without another decision prompt.
22. As an automation author, I want the default in-place choice to be non-interactive, so that the same command behaves predictably with and without a TTY.
23. As a Dev Loop user, I want to request a dedicated worktree explicitly, so that isolation remains available when I need it.
24. As a Dev Loop user, I want explicit worktree creation to retain its existing path, branch, validation, and reuse behavior, so that this change does not redefine isolation.
25. As a Dev Loop user, I want the existing no-worktree flag to remain accepted, so that older scripts continue to run.
26. As a Dev Loop user, I want self-improvement wiki reading and post-run updates enabled by default, so that durable lessons remain part of a normal run.
27. As a Dev Loop user, I want to disable the wiki explicitly, so that controlled runs can opt out of both reading and updating it.
28. As a Dev Loop user, I want the existing positive wiki flag to remain accepted, so that explicit intent and older commands remain valid.
29. As a Dev Loop user, I want the existing all-issues flag to remain accepted, so that copied full-run commands do not break.
30. As a Dev Loop user, I want redundant positive/default flags accepted without deprecation noise, so that compatibility does not make normal output harder to read.
31. As a Dev Loop user, I want help text to distinguish defaults from opt-outs, so that command behavior is discoverable without reading source code.
32. As a Dev Loop user, I want examples to show the PRD-only command first, so that documentation teaches the preferred contract.
33. As a maintainer, I want default resolution owned by the core CLI rather than wrapper scripts, so that all direct entry points remain consistent.
34. As a maintainer, I want invalid or missing derived inputs rejected before runner construction, so that preflight remains evidence-based and side-effect free.
35. As a maintainer, I want the public CLI entry point tested as the primary seam, so that tests prove user-visible behavior rather than parser internals alone.
36. As a maintainer, I want focused scheduler and worktree tests retained for edge cases, so that the default change cannot accidentally rewrite established domain behavior.
37. As a maintainer, I want existing explicit invocations to remain valid, so that adopting the shorthand does not require a coordinated script migration.
38. As a maintainer, I want concurrent unrelated repository changes preserved, so that implementing this PRD does not absorb or overwrite other work.

## Implementation Decisions

- A Portable Direct Run is the direct Markdown issue-runner execution of an
  accepted PRD Package without returning to planning intake.
- The core CLI owns default resolution. PowerShell and Bash wrappers remain
  pass-through launchers, and direct Python invocation observes the same
  contract.
- The accepted PRD path remains required. The Issue Index argument becomes
  optional.
- When the Issue Index is omitted, resolve only the `issues/README.md` child of
  the PRD's containing folder. Do not use the planning intake's additional
  legacy fallback locations for this direct-run shorthand.
- Validate that the resolved PRD and Issue Index exist and target Portable Dev
  Loop before parsing or executing Issues. A missing inferred index produces a
  precise error containing the expected location and starts no agent work.
- An explicit Issue Index continues to override inference and receives the same
  existence, target-product, containment, and dependency validation as today.
- Retain the existing bundled generic-minimal preset as the default. An
  explicit preset continues to override it; this PRD does not change preset
  contents or role configuration.
- Make full issue selection the default. Full selection means all unfinished
  Issues eligible for the existing dependency scheduler, not blind sequential
  execution and not bypassing declared blockers.
- Add a mutually exclusive single-issue option as the explicit breadth opt-out.
  It selects at most one unfinished Issue using the existing selection rules.
- Keep the existing all-issues flag as a silently accepted explicit expression
  of the default. It is not deprecated.
- Preserve start-Issue semantics under the new default: start at the matched
  Issue and include later unfinished Issues. Combining start-Issue with the
  single-issue option selects only the matched Issue.
- When neither worktree option is present, select the source checkout directly
  in both interactive and non-interactive modes and do not ask a worktree
  question.
- Keep explicit worktree creation as the only way to request a dedicated
  implementation worktree. Its current interactive prompts for missing values,
  non-interactive validation, branch sanitation, mapping, reuse, and transfer
  behavior remain unchanged.
- Keep the existing no-worktree flag as a silently accepted explicit
  expression of the default. It is not deprecated.
- Keep wiki reading and post-run updating enabled by default. Preserve the
  existing no-wiki option and custom wiki-location option.
- Keep the existing positive wiki flag as a silently accepted explicit
  expression of the default. It is not deprecated.
- Existing planning and session launchers may continue passing explicit values
  when reflecting choices already made in their UI. The core CLI must still
  produce the agreed defaults whenever those values are omitted.
- Preserve completion detection, Issue dependency parsing, graph validation,
  Ready Issue Order, Normal Scheduling, Blocker Resolution budgets, run-wide
  blockers, resume checkpoints, review/QA gates, and final workspace handling.
- Update CLI help, Windows and Linux quick-start examples, operator guidance,
  and repository instructions so the PRD-only invocation is presented as the
  preferred direct-run command and every opt-out is documented.
- No database, persisted-state schema, Issue schema, PRD schema, API, network,
  credential, or secret-handling change is introduced.
- The accepted ADR establishing PRD-only in-place full runs remains the
  architectural source for why these defaults intentionally differ from the
  previous cautious one-Issue and interactive-worktree behavior.

## Testing Decisions

- Use the public delivery entry point as the primary and highest test seam.
  Build representative temporary PRD Packages, invoke delivery with only the
  PRD argument, replace only the expensive Codex execution boundary, and assert
  the resolved Issue Index, preset, issue breadth, source checkout, and wiki
  choice through observable run behavior.
- Do not make parser-field assertions the sole proof of the feature. Parser
  tests may supplement, but cannot replace, a PRD-only invocation through the
  public entry point.
- Verify that a canonical Issue Index is inferred and that an explicit Issue
  Index overrides it.
- Verify that a missing inferred Issue Index fails during preflight with the
  exact expected canonical location and that no Codex runner is constructed.
- Verify that alternate and legacy Issue Index locations are not selected when
  the direct-run Issue Index argument is omitted.
- Verify that the generic-minimal preset is used by omission and that an
  explicit preset still overrides it.
- Verify that omission selects all unfinished Issues, skips completed Issues,
  respects dependency validation, and leaves dependency scheduling behavior
  unchanged.
- Verify the single-issue option with and without a start Issue, plus the
  default full-run behavior with a start Issue.
- Verify the all-issues and single-issue options are mutually exclusive and
  produce a clear command-line error when combined.
- Verify that omission of worktree options runs in the source checkout without
  invoking a worktree prompt in both TTY and non-TTY modes.
- Verify explicit worktree creation continues to prompt interactively for
  missing values, continues to reject missing values non-interactively, and
  retains existing path/branch/reuse validation.
- Verify existing all-issues, no-worktree, and positive wiki flags remain
  accepted and do not emit deprecation warnings.
- Verify wiki behavior remains enabled by omission and can still be disabled
  explicitly; do not assert private wiki helper call order when observable
  state or output can prove the result.
- Preserve and extend the existing portable-entrypoint tests as prior art for
  entry-point consistency, issue-scheduler tests for dependency behavior, and
  worktree tests for explicit isolation behavior.
- Verify help output identifies the inferred Issue Index, generic-minimal
  preset, all-Issues breadth, in-place execution, enabled wiki, and the new
  single-issue opt-out.
- Verify both wrapper families remain thin pass-through launchers and accept the
  concise command. Wrapper tests must not invoke a real authenticated Codex
  session.
- Run the focused portable CLI, entrypoint, scheduler, and worktree tests.
- Run the complete sandbox-safe non-integration test suite, Ruff, and strict
  mypy gates required by the repository.
- Run a dry-run smoke test against a temporary representative PRD Package using
  only the PRD argument. Inspect selected-Issue output and generated non-secret
  state/log evidence; do not agent-launch a real authenticated integration run.
- Treat mocks as isolation for expensive boundaries, not evidence that the
  public command works. The final verification must exercise actual argument
  resolution, file discovery, validation, selection, and worktree-default code.

## Out of Scope

- Changing `devloop-plan` planning questions, analysis behavior, or PRD/Issue
  publication semantics.
- Changing the separately installed CodexCLI product.
- Changing the generic-minimal workflow definition, roles, models,
  capabilities, retry budgets, or execution settings.
- Changing Issue dependency syntax, dependency validation, scheduler fairness,
  Ready Issue Order, Normal Scheduling, or Blocker Resolution semantics.
- Removing or deprecating the existing all-issues, no-worktree, or positive
  wiki flags.
- Removing explicit Issue Index, preset, worktree creation, start-Issue,
  no-wiki, or custom wiki-location controls.
- Searching multiple fallback locations when the canonical inferred Issue
  Index is missing.
- Automatically creating a missing Issue Index or generating Issues from the
  PRD during delivery.
- Automatically initializing a Git repository, cleaning a dirty checkout,
  creating a branch, merging, committing, pushing, installing, or releasing.
- Changing project-local loop-state formats, migration behavior, or session
  catalog schemas.
- Running real authenticated Codex integration gates from an agent-managed
  shell.
- Modifying unrelated in-progress workflow editor or options-menu work.

## Further Notes

- The agreed preferred invocation is a direct runner command containing only
  the PRD argument. The implementation must not claim this shorthand works
  until the public-entrypoint regression and dry-run smoke test pass.
- At PRD drafting time, the direct CLI required an Issue Index, all-Issues
  selection was opt-in, and an interactive omission of worktree flags offered
  worktree creation. The preset and wiki already had the required defaults.
  Current implementation and gate results are recorded in
  [verification evidence](./verification.md).
- The planning intake already knows how to locate a canonical Issue Index from
  a PRD folder, but its legacy fallbacks are not part of this direct-run
  contract.
- The project glossary defines Portable Direct Run, PRD Package,
  Dependency-Ready Issue, Ready Issue Order, Normal Scheduling, and Blocker
  Resolution terminology used by this PRD.
- The accepted architecture decision records the trade-off: concise,
  automation-friendly full delivery becomes the default, while bounded issue
  execution, worktree isolation, and wiki opt-out remain explicit controls.
- The companion issue pack must be created under the same PRD Package in its
  `issues/` subdirectory.
