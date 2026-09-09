# Automatic test verification: local validation

Validated on 2026-09-09 in the recovery checkout. Authorized typed .NET gates now
execute as owned children of the user-launched session, without a repeated
authorization or separate-terminal command. Existing pending requests remain
compatible; Pause/Cancel retains the request and stops the test tree.

- 169 focused tests passed across operator verification, process execution,
  Portable UI, status UI, and workflow execution. Evidence:
  [focused results](../automatic-verification-focused.log).
- One subsequently added handoff-to-executor-to-receipt regression passed. It uses
  the real receipt pipeline with a substituted test command, without SQL access.
- Process coverage includes a real local Python child, inherited environment,
  null stdin, redacted output, nonzero exit, cancellation, and unconfirmed cleanup.
- Repository Ruff, explicit Ruff over the changed portable modules/tests, configured
  strict mypy (83 files), and strict mypy over the three verification modules passed.
- PRD-only dry-run passed against a disposable copy of the direct-run-defaults pack.
  Inspected the four generated prompts, loop summary, and JSON state (`Dry Run`).
  Evidence: [dry-run output](../automatic-verification-dryrun.log).
- Full-suite execution is not green. The first attempt lacked test dependencies;
  cached validation dependencies resolved collection. A subsequent broad attempt
  was stopped after failures, and a focused first-failure rerun identified missing
  installed Workflow Step Components metadata in the fallback Python environment.
  Evidence: [first failure](../automatic-verification-first-failure.log).

The workspace Store-Python virtual environment cannot start from this managed
session. Checks used the existing local Python 3.12 runtime and cached validation
dependencies, with no installation or escalation. Live SQL tests were not run.
The older running Dev Loop worker must be paused and relaunched before Resume
can exercise automatic verification under the user's account.
