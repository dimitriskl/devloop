# Portable v3 recovery and release status

Updated: 2026-09-06. This is a resumable work record, not release approval.

## Verified recovery checkpoint

- Recovered source checkout: `E:\devloop-recovery-20260905`.
- Original recovery checkpoint: `a69baa8`.
- Current committed repair checkpoint: `4a44144`. The earlier 17-file R02
  checkpoint is committed; the latest seven-file Git-ownership/legacy-startup
  repair and these recovery records are uncommitted. A commit is not
  independent review or release approval.
- Original `F:\devloop` contains only `.ruff_cache`; do not write there.
- The cause of the original checkout disappearance remains unproven.
- Issues 0001-0011 retain their earlier completion records. The recovered tree
  has not passed fresh full release validation.
- Issues 0012 and 0013 remain incomplete.

## Current gates

Current stage (2026-09-06): fresh independent reviewer
`issue0012_r02_deletion_boundaries_fresh_review` returned scoped PASS on the
frozen R02 repair. It independently passed 56 distinct tests (49 existing and
seven new probes), without skips/audit violations, and verified all seven
frozen hashes and the exact prior baseline. Developer evidence remains 72
distinct passing tests. Neither result certifies Issue 0012 or native release
behavior. Fresh QA agent `issue0012_r02_final_fresh_qa` has now started; R02
remains unchecked pending that result. Exact evidence, baselines, commands and
hashes are in the repair checklist. These stages supersede historical
no-review/no-developer and agent
blocker statements below. Historical-fixture QA remains unstarted:
`issue0012_historical_fixture_fresh_source_qa` and the latest
`issue0012_historical_fixture_source_qa_retry` creation attempts failed with
`agent thread limit reached`. This does not block the live R02 review stage.

Separate R12 follow-up: a parent read-only source-path/write-interception probe
confirmed the default post-run wiki writer targets the immutable release tree.
No files were written by the probe and no full workflow ran. Fresh independent
validation and a user decision on installed wiki storage remain pending; see
the repair checklist. This does not expand the active R02 developer assignment
or waive the existing default wiki behavior.

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

## Git checkpoint status and next action

The earlier repair is committed as `4a44144`, observed on 2026-09-06. Its
inventory includes earlier repair code, regression tests and recovery records.
The newer seven-file Git-ownership/legacy-startup delta is uncommitted and has
a separate frozen hash table in the repair checklist. The earlier 17 hashes
describe the previous checkpoint, not every current file. No commit was
created by the agent during this resumed turn.

Earlier, a normal non-escalated attempt to selectively stage the reviewed R01
files failed with:

```text
fatal: Unable to create 'E:/devloop-recovery-20260905/.git/index.lock': Permission denied
```

That is a historical agent-access failure, not proof of the current Git-write
permission. No escalation or index-file workaround was attempted, and agent
write access has not been re-tested on this resumed turn. The active session
still names `F:\devloop` as its writable workspace. Use the recovered E:
checkout for continuation and verify actual access before future commits.
Do not stage unrelated changes or infer review completion from the new commit.

Next stages: finish fresh QA of the reviewed R02 repair; then R03-R06 and
R09-R11. R12 requires its pending storage-location decision and a separate
fresh implementation/review/QA sequence. Materialize and independently validate
the reviewed historical-bootstrap overlay and repair the test harness. Resolve
baseline lint/type findings and complete the full Issue 0012/0013 gate sequence.
Keep both issue completion markers unchecked until their complete acceptance
evidence exists.

Earlier R02 checkpoint (2026-09-05): after the first independent review rejected
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

Resumed audit, 2026-09-06, turn 1: the new committed repair checkpoint and both
baseline directories were verified, but a fresh final-review request still
failed with the same `agent thread limit reached` response. The resumed goal
starts a new blocked audit; this single resumed failure is not a new three-turn
blocked determination. Final R02 review and QA remain unstarted.

Resumed audit follow-up, 2026-09-06: the intervening user-requested status
turn was no development progress, not a verified wait; the agent list showed
only completed child agents. In the following goal continuation, a new request
for `issue0012_r02_review_resume_sep06` again failed with
`agent thread limit reached`. A fresh agent-list query again confirmed every
child terminal. HEAD remains `4a44144`, all 17 frozen repair hashes match, both
C: review baseline directories exist, and Issues 0012/0013 remain unchecked.
The same fresh-stage blocker has persisted through the resumed checkpoint,
status-only turn, and this continuation. There is no live review or QA handle
to wait on. Continuation requires an external capacity/workspace change;
do not reuse a completed agent or substitute parent-only review/QA. No code,
test, or release acceptance progress is claimed from these status checks.

Second resumed run, 2026-09-06, audit turn 1: the goal was externally resumed
after the preceding blocked determination. Root creation of
`issue0012_r02_review_resumed_fresh` again failed with `agent thread limit reached`.
A coordination-only follow-up to the completed R01 reviewer attempted exactly
one child creation with `fork_turns="none"`; it returned the same failure and
finished without reviewing, testing, or editing anything. Neither route created
a fresh reviewer. HEAD and all 17 repair hashes remain unchanged, and both C:
review baselines exist. This is the first turn of this new resumed audit, not
another completed three-turn blocked audit.

The parent also retried materializing the already independently reviewed
historical fixture, without reimplementing it or executing historical code.
It verified the saved patch's recorded SHA-256, its exact three Add File targets,
and plain existing destination ancestors. Applying that unchanged patch again
failed with `Failed to create parent directories` for
`tests/fixtures/portable_bootstrap_604/sources.json`. No shell-based directory
creation or permission workaround was attempted. The fixture is still absent;
fresh fixture QA and compatibility integration remain pending. The prior
review PASS remains a review of proposed data, not installed fixture evidence.

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

Additional source-inspection lead for the later compatibility stage
(2026-09-06; not an executed installer failure): the existing historical test
updates to `candidate`, rolls back, then updates from the retained old runner
to that same `candidate` commit again. Preserve that final positive assertion.
Both current release drivers skip activation only when the requested commit is
the current commit. `prepare()` otherwise refuses an already-existing canonical
release before creating a new journal. Verify the retained-release reactivation
path explicitly when repairing the historical harness; a simple same-current
commit reinstall does not exercise it. This is not a new certified finding,
does not expand the active R02 repair, and was not tested through an installer.
