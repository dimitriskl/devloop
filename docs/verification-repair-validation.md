# Verification failure repair: local validation

Validated on 2026-09-09 in the recovery checkout. The regression first failed
because a saved failed receipt was executed again before the coder received it.
The corrected path returns bound, redacted failure evidence to the workflow and
requires fresh passing verification before accepting a claimed repair.

- 182 tests passed across operator verification, process execution, Portable UI,
  status UI, and workflow execution:
  [regression results](../verification-repair-validation.log).
- Three final targeted checks passed, including one subsequently added regression:
  a claimed repair that still fails returns to the worker rather than completing.
  [Targeted results](../verification-repair-final-cases.log).
- Coverage includes recovery of saved failures, pause during repair, unchanged
  failing requests, attempts to replace a failing filter, receipt identity/hash/
  source validation, build failures without TRX, and both Review/QA -> Development
  repair transitions through the real workflow executor.
- Repository Ruff, explicit portable-module/test Ruff, configured strict mypy
  (83 files), strict mypy over the three changed verification modules, and
  `git diff --check` passed.
- PRD-only dry-run passed on a disposable copy of the direct-run-defaults pack.
  Inspected the four role prompts, README.loop.md, and README.loop.state.json;
  the prompts contain the repair guidance and the issue status is `Dry Run`.
  [Dry-run output](../verification-repair-dryrun.log).
- A read-only probe checked the real saved request
  `28628807-8d55-4b9b-9f19-96b8177d4438` against current source fingerprints and
  its TRX hash. The new feedback contained the `TableExistsAsync` failure and
  Review/QA rework guidance. No SQL tests or authenticated agent were launched.
- The full suite is not green: it stops at the existing missing installed Workflow
  Step Components metadata in the fallback Python environment:
  [full-suite first failure](../verification-repair-full-suite.log).

Checks used the existing Python 3.12 runtime and cached validation dependencies.
The real-request probe used a process-local Git safe.directory setting for the
named repository; no global Git configuration was changed. The old running Dev
Loop process must be restarted to load this fix before rerunning unfinished issues.
The underlying eConnectorV2 test repair and fresh live SQL pass remain work for
that resumed workflow; this validation does not mark issue 0003 complete.
