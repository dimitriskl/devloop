# Portable Direct Run Defaults verification

Date: 2026-09-07. Scope: Portable Dev Loop direct delivery.
Status: implementation and focused review complete; Issue 0003 verification blocked.

## Implementation

The checkout already contained canonical Issue Index inference, the bundled
generic-minimal preset default, full issue breadth, in-place execution, enabled
wiki behavior, compatibility flags, and PRD-only documentation. Those paths were
checked against `cli.py`, `issue_pack.py`, `issue_scheduler.py`, `worktree.py`,
the two wrappers, and actual temporary package files.

This completion pass fixes the remaining single-issue eligibility gap: without
`--start-issue`, `--single-issue` uses the existing dependency scheduler to select
the first ready unfinished issue. An earlier waiting issue no longer causes
selection preflight to fail. Explicit start selections still reject omitted
unfinished prerequisites. Invalid start identifiers now produce an argparse
error instead of an uncaught exception. Planning selection and scheduler rules
were not changed.

## Tests and runtime

The workspace `.venv` fails to start its configured WindowsApps base interpreter.
Validation used the available Python 3.12.13 runtime and existing pytest 8.4.2,
Textual 8.2.8, mypy 1.20.2, and Ruff dependencies. No installation, authenticated
Codex execution, release operation, or sandbox escalation was performed.

- The new single-issue regression failed before the fix with `Selected issue
  0001 requires unfinished issue 0002, which is not selected`, then passed.
- The final focused selection finished with **125 passed, 1 failed**. It includes
  direct-run, wrapper, portable-entrypoint, dependency-scheduler, worktree, and
  interactive planning tests. The failure is the unchanged
  `BuildPlanningPromptTests.test_planning_prompt_includes_step_guidance_with_visible_precedence`:
  it expects `agents/codex/security.md`, while a Windows `Path` renders backslashes.
  Both the test and planning implementation are unchanged from the initial tree.
- Configured Ruff, explicit Ruff on both direct-run test files, and strict mypy
  pass. Mypy checks the repository's configured 83 source files.
- Full collection succeeded: **1,727 selected, 51 deselected** with
  `-p pytest_asyncio.plugin -m "not integration and not operator_install"`.
  The initial collection omitted the asyncio plugin and failed on its marker;
  explicitly loading the existing plugin resolved collection.
- The broad execution was interrupted by the managed tool with `runner error:
  runner pipe closed before exit`. Its surviving log contains 882 progress
  symbols, including 64 failures, but no final pytest summary. Neither the final
  JUnit file nor the driver's exit-result JSON exists. This is an incomplete
  gate, not a passing suite or a complete inventory of baseline failures.
- A focused diagnosis of an early, unchanged CodexCLI test reproduced
  `ComponentRegistryError: No installed Workflow Step Components were discovered.`
  The separate CodexCLI tests need installed/generated component metadata in
  this validation environment. Other broad-suite failures remain unclassified.

## Execution interruption

After the pipe-closure error, `.tmp-direct-run-gates.py` was found absent, although
it had launched the completed focused/static gates and the broad run. The
surviving full log is
`.tmp-direct-run-evidence/full-3b3425c2a7a74b5f942e446b91fb7cd8/output.log`,
SHA-256 `EEA33753EC1CC2FD81A308A0BC016B313198EC34C6C7B1041C65F3BF94016EBC`.
Its command JSON survives. A read-only process query returned no Python rows;
that does not replace the missing process-exit record. The cause of the script's
disappearance and execution interruption is not established.

The helper was not recreated and the broad gate was not relaunched. Per the
user's evidence-first instructions, clarification about security/cleanup removal
is required before reconstructing and repeating this execution. Issue 0003
remains incomplete. There is no commit, deployment, installation, or real Codex
integration result from this task.

## Public command and wrapper evidence

`tests/test_direct_run_wrappers.py` executes the Python module and unchanged
PowerShell and Bash wrapper bodies against fresh Git repositories with spaces
in their paths, supplying only `--prd` and the safety flag `--dry-run`.
The wrapper fixtures supply the available interpreter at the runtime lookup
boundary; they do not exercise runtime bootstrapping or installation. Bash ran
under Git for Windows, so these results do not establish native Linux installation
or runtime readiness.

All three launcher cases pass. Their actual `README.loop.state.json`,
`README.loop.md`, and `.loop.logs/*.prompt.md` establish canonical index selection,
both unfinished issues, source-checkout paths, default workflow prompts, enabled
wiki guidance, and unchanged issue completion markers after dry-run. The real
plain-mode session path is exercised; the direct-run tests also use an application
runtime harness that rejects unexpected worktree prompts.

Generated artifacts and exact command/result logs are retained below
`.tmp-direct-run-evidence/` and `.tmp-test-session-3q8bmyd4/`. These ignored files
contain disposable test output, not production run evidence.

## Review scope

Independent reviews used the task's initial commit
`410145f857b01631a08b7df7a43ba5e5873a8e3c` as their baseline.

| Axis | Result |
| --- | --- |
| Standards | No hard violations. One optional suggestion to reuse the dry-run test helper in the single-issue regression. |
| PRD/spec | No implementation defects or scope creep. Close the broad-suite evidence and Issue 0003 status before claiming completion. |

The final source diff is confined to the direct CLI selection/error boundary,
direct-run tests, the wrapper regression harness, matching user guidance, and this
PRD package. No unrelated workflow-editor, options-menu, installer, or CodexCLI
source files were changed. Review checks preserve explicit worktree handling,
start-issue dependency validation, compatibility flags, scheduler budgets, and
the source-checkout default. There were no pre-existing working-tree edits at
the start of this task.
