# Failed verification feedback crash, 2026-09-09

The eConnectorV2 session shown at 12:51 and 13:02 was failed, not executing.
Read-only inspection of the Store-Python session catalog found session
`915a63a5-0191-4017-afca-80611a7e949f` with status FAILED, result 1, updated at
13:01:24 local time, and no execution claims. Its last stage was
"Returning failed verification to workflow". The latest agent log was from 12:02:58.

Saved request `28628807-8d55-4b9b-9f19-96b8177d4438` contained six executed tests:
four passed and two failed with missing `TableExistsAsync` mock setups. Initial
read-only source and receipt/report-hash validation succeeded. The feedback
contained 7,957 characters. Passing it to the real StepGuidance validator reproduced
`ValueError: Step Guidance cannot exceed 4000 characters before redaction.`
CodexRunner builds the prompt before creating attempt logs, explaining the absence
of a new agent log. The last working screen obscured the terminal diagnostics.

## Change

- Carry automatic verification evidence through a separate runner argument and
  prompt section for Development, Review, and QA. Preserve user Step Guidance and
  its existing limit. Retain redaction and the failed-gate repair/retest contract.
- Bound the failure screen to the existing 8,192-character protocol limit, while
  retaining the complete generated feedback in the prompt.
- Show FAILED session diagnostics without the previous working screen. Preserve
  completed-session displays.

## Validation

- [Red regression](../feedback-handoff-red.log) reproduces the prompt-validation
  exception through the actual handoff, prompt builder, and worker protocol.
- [UI and handoff checks](../feedback-handoff-ui.log): 66 passed. After narrowing
  the display change to FAILED sessions, [final cases](../feedback-handoff-final-cases.log)
  passed all seven checks. Coverage includes all three role adapters, maximum
  user guidance, large feedback, and the failed-session diagnostics view.
- [Broader regressions](../feedback-handoff-regressions.log): 151 passed, one
  skipped, one failed. The unchanged connection-retry execution-budget test
  expects two invocations inside a 0.2-second budget but observed one. This
  unrelated failure remains open.
- Repository Ruff, explicit Ruff over the changed portable modules/tests,
  configured strict mypy (83 files), and `git diff --check` passed.
- [PRD-only dry run](../feedback-handoff-dryrun.log) exited zero. Inspected four
  generated role prompts and both README.loop artifacts in the disposable
  `.tmp-feedback-dryrun-mf80xli2` pack; issue 0003 is recorded as Dry Run.
- [Full suite](../feedback-handoff-full-suite.log) stopped at the fallback
  environment's missing installed Workflow Step Components metadata: eight
  passed, one failed, 51 deselected. Full validation is not green.
- [Saved-report replay](../output/feedback-handoff-replay/result.json) supplied
  the actual failure details to one simulated backend invocation without a crash.
  Source inputs changed after the initial probe, so this replay freezes source
  identity to the saved snapshot. Receipt identity and TRX hash were validated;
  this is not current-source SQL verification.

Checks used the existing Python 3.12 runtime and cached validation dependencies;
the checkout's Store-Python venv cannot launch from the managed sandbox. No
authenticated agent or SQL test gate was launched. Restart the user-launched
Dev Loop application to load the changes and rerun the unfinished PRD. Issue
0003 and its live test repair remain incomplete. Existing unrelated work was
preserved; no commit was created.

## Follow-up: verification screen remains visible while Development runs

At 13:49 the resumed session verified request
`0e2e9656-276a-46f2-8227-7e30ab7ef3a8` (12 passed, zero skipped). A new Development
prompt was written at 13:49:15; Codex PID 37220 started at 13:49:16, and its
rollout recorded task start at 13:49:17 and tool activity through 13:54:28.
The user-visible verification screen therefore did not establish a stalled worker.

The display had a separate lifecycle defect: `suspend_updates()` stops animation
and only restarts it on a normal return. `VerificationFailed` leaves it stopped
while the handoff catches the failure and launches repair. A subsequent successful
verification finds no animation to restart. Even normal success did not immediately
replace the verification screen before the next provider message.

The console handoff now explicitly restores the role's session status, renders
its dashboard and restarts animation immediately before a resumed backend call.
It preserves the issue, pass, and elapsed role duration. Lifecycle exceptions still
propagate without invoking the resume callback or launching another attempt.

- Both actual console/handoff/dashboard transition regressions failed before the
  change: [red evidence](../dashboard-resume-red.log).
- [Focused validation](../dashboard-resume-focused.log): 38 passed, covering the
  handoff, Portable status UI and dashboard. Repository Ruff, focused Ruff,
  configured strict mypy (83 files), and diff whitespace checks passed.
- [PRD dry run](../dashboard-resume-dryrun.log) exited zero; four role prompts and
  both loop-state artifacts were inspected, with issue 0003 marked Dry Run.
- The authenticated live session was left running. It retains the previously
  loaded display code; the new display behavior requires a later application
  launch. No live SQL or authenticated provider gate was started by the agent.
