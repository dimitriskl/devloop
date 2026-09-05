# Portable v3 recovery and release status

Updated: 2026-09-05. This is a resumable work record, not release approval.

## Verified recovery checkpoint

- Recovered source checkout: `E:\devloop-recovery-20260905`.
- Recovery checkpoint: `a69baa8`; working tree was clean before this record.
- Original `F:\devloop` contains only `.ruff_cache`; do not write there.
- The cause of the original checkout disappearance remains unproven.
- Issues 0001-0011 retain their earlier completion records. The recovered tree
  has not passed fresh full release validation.
- Issues 0012 and 0013 remain incomplete.

## Current gates

- [x] Issue 0012: fresh independent review completed; FAIL with ten P1 findings
  in [the recovery review](./portable-v3-issue0012-recovery-review.md).
- [ ] Repair review findings with a fresh implementation agent.
- [ ] Fresh independent review of the final repair diff.
- [ ] Fresh QA of the repaired code and safe automated tests.
- [ ] Issue 0013: integrated multi-session and cross-platform release evidence.
- [ ] Inspect required operator evidence before marking release complete.

Latest repair checkpoint: R01 candidate-path safety passed fresh independent
review and sandbox QA (28 tests plus two read-only symbolic-mode probes). R07
and R08 Bash recovery repairs also passed separate fresh independent review and
QA (nine tests / 15 in-memory scenarios plus five additional probes). See the
linked review record for exact scopes and hashes. This closes three scoped
repair findings, not Issue 0012 or any native installer release gate.

## Git checkpoint blocker and next action

All repairs and this work record are saved in the recovered E: checkout, but
remain uncommitted. A normal, non-escalated attempt to selectively stage the
reviewed R01 files failed with:

```text
fatal: Unable to create 'E:/devloop-recovery-20260905/.git/index.lock': Permission denied
```

The index remains empty (`git diff --cached --name-only` returned no paths).
HEAD remains recovery checkpoint `a69baa8`. No escalation or index-file
workaround was attempted. The active session still names `F:\devloop` as its
writable workspace. Resume from the recovered E: checkout with Git-write
access, verify the recorded final hashes and current diff, and selectively
commit the reviewed repairs and work records. Do not stage unrelated concurrent
changes. The blocked Git step does not suspend other already-authorized source
repairs: continue those through the approved file-edit path and keep their
implementation/review/QA states explicit until the commit can be made.

Next development: R02-R06 and R09-R11 (eight blocking review findings), then
materialize the verified historical-bootstrap overlay and repair the test
harness. Complete fresh review/QA for each repair, clean up baseline lint
findings, and continue the full Issue 0012/0013 gate sequence. Keep both issue
completion markers unchecked until their complete acceptance evidence exists.

Current R02 checkpoint (2026-09-05): after the first independent review rejected
the initial 25-test repair for a bytecode/startup regression, a fresh developer
completed and froze the startup rework. Its 46 focused developer tests, two-file
Ruff/strict-mypy checks, five Bash syntax checks, five PowerShell AST checks and
scoped whitespace checks passed. The parent verified all 17 combined R02 file
hashes, saved in the repair checklist. This is not an independent review or QA
PASS. A separate fresh read-only review confirmed R11: inherited Git worktree
and index selectors can redirect ownership validation away from the intended
release. Neither finding establishes the cause of the original checkout loss.

## Fresh-agent blocker and exact resume point

The agent tool repeatedly rejected fresh reviewer/QA creation with
`agent thread limit reached` across three consecutive goal-continuation turns.
At the final handoff the developer reported its code frozen and finished;
new final-review agent requests still failed. Do not substitute an old agent
context or parent-only QA for the user's required fresh-agent stage sequence.
No final R02 review or independent QA has started, and no additional issue or
repair completion marker was promoted.

Resume with fresh-agent capacity and the recovered E: checkout selected as the
workspace (the old session still targets F:). First assign a new independent
reviewer the combined R02 ignored-content and bytecode-startup repair. Use the
two C: baseline paths and 17 frozen hashes in the repair checklist; inspect the
current diff before relying on any saved result. Then assign fresh QA if review
passes, or a fresh repair stage for any new blocking findings. Do not run full
installer tests merely to bypass the agent limit or missing fixture material.

Subsequent work remains R03-R06/R09-R11, source-only QA and materialization of
the reviewed historical fixture, safe harness repair, commit/checkpoint access,
and every Issue 0012/0013 mandatory integration/native/operator release gate.
Both issue completion markers remain unchecked. Re-establish actual Git-write
and fixture-directory access rather than assuming a new workspace fixes them.

## Confirmed test constraints

- `tests/test_portable_side_by_side_install.py` references historical commit
  `6048556f0f279cb54f4d1afa00f764227049eb8f`, absent from recovery Git. Recover
  verifiable historical fixture content; do not substitute a current release or
  silently skip the compatibility requirement.
- Its temporary-directory helper uses checkout-local `ROOT / "tmp"`; changing
  `TMP`, `TEMP`, or pytest `--basetemp` does not redirect it.
- The installer test that passes a real filesystem root to uninstall must not
  run without a verified non-destructive isolation boundary.
- Authenticated and installation-profile gates remain operator-only under the
  issue acceptance criteria. Do not launch them from an agent session.
- `pytest -m "not integration"` is not sufficient to select a safe lane here:
  `pyproject.toml` defines `integration` for the real Codex App Server, while
  `test_bundle_installer.py` and `test_portable_side_by_side_install.py` declare
  no such marker and directly invoke installer/uninstaller/transaction scripts.
  Some side-by-side cases also launch process holders. Keep the already-reviewed
  source-only classes separate from these unclassified execution tests. Repair
  and independently review the lane selection and fixture isolation before a
  full suite run; do not silently skip their required operator evidence.
- Test payload provenance differs: the Unix local-bundle cases in
  `test_bundle_installer.py` use `file://<ROOT>` with ref `HEAD`, so their cloned
  payload excludes uncommitted repairs. The side-by-side `_release_repository`
  helper instead copies current `src`, `bin`, and `install` into a fixture Git
  repository. Release evidence must identify the exact tested source snapshot;
  do not describe a HEAD-based clone as verification of a dirty working tree.

Review findings and regression evidence will be recorded here as they are
independently verified. Existing completion markers must not be promoted based
only on recovered file integrity or historical test results.

## Verification environment and current results

- A clean Python 3.12 tooling directory is available at
  `C:\Users\Dimitris\AppData\Local\Temp\devloop-v3-validation-20260905`.
  The portable runtime dependencies match `requirements-portable.lock`; pytest
  8.4.2, pytest-asyncio 1.4.0, Hypothesis 6.156.5, mypy 1.20.2, and Ruff 0.15.21
  match the direct development-tool versions in `uv.lock`.
- Guarded source-only/runtime-preparation smoke: **11 passed** in 0.56 seconds.
  Scope: `PortableRuntimePackagingTests` in `test_bundle_installer.py` and all
  of `test_portable_v3_release.py`. No installer or Codex worker was launched.
- Evidence: `C:\Users\Dimitris\AppData\Local\Temp\devloop-v3-safe-check-k21cou_x`
  contains `result.json`, `junit.xml`, and `pytest-output.txt`; result records
  exit zero and no audit-guard violations.
- The temporary Python audit runner forbids child launches and writes outside
  each new C: fixture directory (apart from the null sink); it is not a sandbox
  for untrusted native code. Its preflight caches Python's read-only Windows
  version query before enabling the no-child test lane.
- A targeted Ruff 0.14.1 baseline check found existing E501 at
  `install/bootstrap/verify.py:224` and I001 in
  `tests/test_portable_v3_release.py`. These are not fixed or waived.

## Historical compatibility fixture recovery lead

A fresh read-only history audit reconstructed the historical bootstrap in
memory from `5d1d13a` and successful recorded patches: 142 records, zero replay
failures. This establishes an 18-file historical-bootstrap overlay, not the
complete missing `6048556` tree. The overlay must include the 16 installer files,
historical `portable-release.json`, and historical
`src/devloop/portable_release.py`; non-bootstrap test support must be explicitly
identified as current code.

Source author log:
`rollout-2026-09-04T19-54-43-01a06d58-1286-7b42-8317-216d34059805.jsonl`.
Final staging occurred at 2026-09-04T18:22:49.111Z; amendment completed at
18:23:08.091Z. Lines 1060-1073 establish the committed paths and protected dirty
files. Replay must include the Ruff I001 fix at 17:36:32.923Z (lines 520-521),
but exclude later 19:15/20:05 formatting. Four reconstructed Git blob hashes
match the seven-character historical diff target prefixes recorded at line 974.
The full hashes below are computed from reconstructed content; the original log
does not independently record their full 40 characters:

| Historical path | Reconstructed Git blob matching logged prefix |
| --- | --- |
| `install/bootstrap/install/devloop.ps1` | `ff543870efc07a30a1533f4be75325f353ec5634` |
| `install/bootstrap/transaction.py` | `796ebecf10db980f37b822832506d2bd82ba06f7` |
| `install/devloop.ps1` | `dfe87df637d93808d5082879cb8da074fcf06204` |
| `install/devloop.sh` | `021dd03262d8e36bdffcb3b29b51621dca5c5ffd` |

Pending work: materialize and independently hash-check the frozen overlay,
record per-file SHA-256 and UTF-8/LF provenance, and replace the absent-SHA test
dependency without weakening the v1 install -> v2 migration -> rollback ->
update assertions. Do not claim the original Git commit has been recovered.

The fresh fixture builder completed the 18-file replay (77,321 decoded UTF-8/LF
bytes; 142 records, zero failures) and added the source-only integrity test
`tests/test_portable_historical_bootstrap_fixture.py`. Creating the new
`tests/fixtures/portable_bootstrap_604` directory failed in the file-edit
tool (`Failed to create parent directories`). Consequently the test is still
RED because `manifest.json` is absent;
the fixture is not installed and no compatibility gate has passed.

The exact inert three-file patch is retained at
`C:\Users\Dimitris\AppData\Local\Temp\devloop-historical604-fixture-2b3a9c11-pending.patch.txt`
with SHA-256
`42C024582F33282F97820EA15F0FC87A7CCEA0C8D6ABB8CD1D8C4AEA9A84C24B`.
The parent independently parsed that patch as data and verified all 18 decoded
source byte lengths and SHA-256 values against its proposed manifest.

Fresh independent reviewer `issue0012_historical_fixture_review` returned
scoped PASS for the proposed data and integrity-test logic. It verified all
142 file records against 101 successful raw-log patch events across four source
logs, and a separately implemented replay reproduced the same sources with
exact hunk offsets/context/counts and zero relocations. The unchanged test
passed with in-memory fixture data and rejected corruption of each of the 18
sources plus an unexpected executable filename. Focused Ruff passed. Test
SHA-256: `772282591BEF7D8427FA59B36840AC372D80AE02F8F659B249E6BC43BAAFD39F`.
Review audit script:
`C:\Users\Dimitris\AppData\Local\Temp\devloop-historical604-review-68f7932d.py`.

An optional stricter replay probe initially omitted CRLF normalization and
delete-event handling; correcting that audit, not the fixture, produced the
exact replay above. No fixture defect was found. This PASS does not change the
actual missing-manifest RED. Fresh source-only QA, materialization, and
integration into the existing compatibility harness remain pending. Do not
execute the historical source as part of integrity verification.
