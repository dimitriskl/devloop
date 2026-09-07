# Portable v3 recovery and release status

Updated: 2026-09-07. This is a resumable work record, not release approval.

## Current continuation: 2026-09-07

- Latest checkpoint: test execution and implementation are paused after QA
  evidence files disappeared. Root independently confirmed the missing files,
  the surviving process record, all 21 unchanged transaction/distribution
  source/helper hashes, and the unchanged session backup. The cause is unknown;
  a user clarification about removal or quarantine is pending. No affected probe
  was recreated or rerun. See
  [the retained interruption record](portable-v3-qa-interruption-20260907.md).
- Workspace and working-file write access verified at
  `E:\devloop-recovery-20260905` by creating, reading and removing one unique
  probe file. The managed grant exposes `.git` read-only; no escalation or
  Git permission workaround is authorized.
- HEAD verified as `561403e719750032ac992ed64b96811ee17ebc93`. The seven R02
  source/test SHA-256 values in the 2026-09-06 handoff match this checkpoint.
- Fresh independent R02 QA `issue0012_r02_final_fresh_qa`: **SCOPED PASS**,
  86 distinct tests, no skips or audit violations. Root reread its report and
  verified SHA-256
  `50D2C3346E1DE7C1C4F8072F42720958E4FC3F722B72A4EC5494ED43FB38E44A`.
  This completes R02's scoped developer/review/QA sequence only.
- The supplied session-reset report records four removed sessions, zero
  remaining sessions, three preserved saved projects and preserved settings.
  Its backup and report are unrelated user artifacts and must remain intact.
- [x] Startup crash scoped repair: fresh implementation, independent source
  review and fresh executable QA **PASS**. Implementation is frozen in `src/devloop/portable_sessions.py`
  with regressions in `tests/test_portable_session_startup.py`: 13 focused
  developer tests passed, and immutable read-only replay displays all four
  backed-up records. Startup and peer refresh now defer launch reconstruction
  until explicit Resume, which first checks UNAVAILABLE status. Fresh
  independent source review **PASS** is recorded in
  `.tmp-startup-review-20260907/report.md`. Fresh QA independently passed 13
  startup regressions, replayed all four actual backup rows without launches,
  verified unavailable Resume gives the Relink instruction, and confirmed the
  backup hash and absence of sidecars. The actual PRD-only dry-run also passed,
  with eight prompts, board/state and mirrors verified in retained artifacts.
  This certifies the scoped repair, not live native-terminal or release behavior.
- Issues 0012 and 0013 remain incomplete. R12's installed wiki storage decision
  has been requested; dependent changes wait for the user's answer.
- The old protocol-v2 helper finding exposed a second unresolved product
  boundary: permit an explicit recoverable security migration of verified
  unchanged bootstrap helpers, or refuse old helpers pending operator migration.
  The user was asked; no answer is recorded. The other three source repairs are
  frozen for a fresh independent review; this behavior remains undecided.
- The unchanged, previously reviewed historical overlay patch was materialized
  successfully after verifying its SHA-256 and three exact Add File targets.
  The source-only integrity test now passes (one test, 2026-09-07); no historical
  code was executed. Fresh independent inert-data QA now **PASS**: all 18
  decoded sources (77,321 bytes), 142 provenance records from 101 original
  patch events, and 28 corruption probes were checked with PowerShell/.NET.
  Evidence: `.tmp-startup-fixture-fresh-qa-20260907/inert-fixture-result.json`,
  SHA-256 `6A633976AB0BC0F63CC3E72667B2D5E1BE007842A6010A7ABEC987E3588CB079`.
  Python integrity-test reexecution, historical patch replay and historical
  installer execution were not part of that QA. Compatibility harness
  integration is drafted but has not passed review or executable QA. Test source hash remains
  `772282591BEF7D8427FA59B36840AC372D80AE02F8F659B249E6BC43BAAFD39F`.
- The recovered `.venv` cannot launch its unavailable WindowsApps Python base.
  Python 3.12.13 from the available Codex runtime imports the existing validation
  packages successfully: pytest 8.4.2, Textual 8.2.8 and mypy 1.20.2. No runtime
  or dependency installation was performed. Initial configured Ruff and mypy
  diagnostics are recorded in `.tmp-sep07-configured-ruff.txt` and
  `.tmp-sep07-configured-mypy.txt`. A fresh seven-file baseline source repair
  now passes configured native Ruff (`src tests`, exit zero). Fresh independent
  source review **PASS** and a second native Ruff run are recorded in
  `.tmp-baseline-fresh-review-independent-20260907/review.md` (SHA-256
  `D7C01CD01EEF379A0ED2B176569BCB59AE7C9D91B70A6C2D4C823D947170601D`).
  Fresh executable QA now also **PASS** for this seven-file baseline slice:
  configured strict mypy (83 files), configured Ruff, nine-file in-memory
  compilation and 27 selected baseline cases. Five initial metadata-prerequisite
  failures were rerun successfully after actual setuptools metadata generation
  in a fresh workspace snapshot; no installation or manual entry-point stub
  was used. The startup source retains its independently confirmed pre-existing
  one scoped Ruff and twelve scoped mypy diagnostics; configured gates are clean.
- After those successful executions, the exact Python runtime began returning
  Access denied in three independent agent contexts. Following the user's
  continuation request, root rechecked that same executable in the sandbox:
  Python 3.12.13 and the existing pytest/Textual/mypy imports now pass. Evidence:
  `.tmp-python-access-restored-20260907.log`. Focused safe executable checks may
  resume. The cause of the temporary denial is not established; no escalation,
  interpreter substitution, installation or executable copy was attempted.
  The broad suite still waits for the harness safety repairs and fresh review.
- Static local-file link validation checked 55 links across 20 current release
  and PRD documents: all targets exist. This checks file targets, not anchors,
  remote links, terminal behavior or release readiness.
- Fresh transaction/distribution drafts received independent source review
  **FAIL**. Four P1 findings require further repair: partial legacy
  backup copies, uninstall retry argument handling, recaptured release-content
  ownership, and old protocol-v2 helpers bypassing new lock protection. These
  findings are from source inspection and one isolated fake-Python PowerShell
  option-dispatch probe; no installer execution is claimed. A fresh
  implementation agent repaired three findings and froze nine files in
  `.tmp-packaging-rework-20260907.md`, SHA-256
  `2883693F35050F0E5D0EBE3A29BE9989C6FB19AA33E42638019F5A02C78B9563`.
  Native source checks and isolated option parsing passed; no Python behavior
  was executed. Fresh review **FAIL** found a further retry defect: the journal
  string overwrites the caller's install-root Path before option validation.
  Root directly verified the code and the completed report at
  `.tmp-packaging-rework-independent-review-20260907/review.md` (SHA-256
  `029AEC97B635366721A9E352CC11871D7F0D5AEA578F66EBB276EAA3260651CC`).
  The caller-Path defect and two reproduced Windows read-only-handle fsync
  failures are now repaired. Final developer guarded suite: 36 PASS. Fresh
  independent source review **PASS**:
  `.tmp-transaction-retry-flush-fresh-review-20260907/review.md`, SHA-256
  `B17839DB4A6AF1DF67029F8C5E90D73DE723F179544D7E922559DA6E679D18C8`.
  Transaction source SHA-256 is
  `9F0346C2916364762E5B4933D153F87BE161EFCD02C88C13ED8CFAAF5CB8E4CD`.
  Fresh independent executable guarded recovery QA **PASS**: 36 tests,
  zero failures/errors/skips, 104.32 seconds. Root verified the JUnit, log and
  all 21 input hashes. Distribution QA remains incomplete: its initial run
  had 18 passes and 10 failures; the retry recorded two passes before the
  runner pipe closed, and several evidence files are now missing. Read-only
  Windows legacy assets still refuse publication
  while preserving originals/staging; successful support is not claimed.
- The first safe-harness independent source review returned **FAIL** for four
  findings: profile-temp fixtures bypass workspace isolation; Linux can inherit
  the operator's XDG_STATE_HOME; operator PASS does not bind the complete test
  inventory and unchanged source; and dangling symlink assertions cannot pass.
  Report: `.tmp-safe-harness-review-525685970b0d4c97b46b7e00b02608fe/report.md`,
  SHA-256 `1E9D0B40D7C3D2A768A6AE06717FA5622B6D9113218CB40217D2BBBBC1E65FF5`.
  The four repairs are now frozen for a new independent review in
  `.tmp-safe-harness-rework-20260907/report.md`, SHA-256
  `F9DF6812B4F6717BFAF0891D6CEE691D7B4FF2012CADB7FCACD31D05083CD446`.
  Targeted runs resolved fixture/diagnostic mismatches while retaining mutation
  guards and preservation assertions. Earlier runs imported older transaction
  bytes; their counts must not be combined into a final-tree full PASS. Fresh
  independent source review now **SCOPED PASS**, with 26 inert harness tests
  passing. Report: `.tmp-safe-harness-rework-independent-review-20260907/report.md`,
  SHA-256 `193FD14B73337D52D14F3BA511438D9045847D33C8E3C0E3E8CBDE1124D8DCEF`.
  Complete final-tree QA remains required before broad execution. Collection
  was incomplete because of missing explicit pytest-asyncio loading; the
  corrective collection failed at the managed launcher before Python started.
  No approved operator manifest or installer gate exists yet.
- The connected three-worktree Issue 0013 scenario is implemented and frozen
  and received fresh independent source review **FAIL** for two P2 findings:
  cleanup errors can skip remaining owned workers, and the background-alpha
  notification wait can be satisfied by beta's earlier attention marker.
  Report: `.tmp-integrated-scenario-independent-review-20260907/review.md`,
  SHA-256 `4C1BAB157645B7C1E31D64D2C309D05AD25E23B583C54B3E424FCE08B09E3D19`.
  A fresh implementation agent owns both corrections. Source and limits are in
  `docs/portable-v3-integrated-scenario.md`. Native Ruff and whitespace checks
  passed. Seventeen focused cleanup/notification regressions now pass. The first
  connected run **FAILED** waiting for beta input. One instrumented diagnostic
  run also **FAILED**, proving beta's fake backend encountered PermissionError
  reading its exact command file before requesting input. Both runs preserved
  the original error and confirmed cleanup without reported failures. Later
  parent reads succeed and alpha/beta file ACLs match, but the earlier worker
  denial's cause remains unknown. No retry mask or permission change was added.
  Final source freeze: `.tmp-retry-scenario-fresh-repair-20260907/scenario-freeze.md`,
  SHA-256 `A8EE8EB439543CED6169850B4263A3D6FC3A3B7A8F02DEC88DE37E734EBC3E55`.
  Fresh independent final source review now **SCOPED PASS**:
  `.tmp-scenario-rework-fresh-review-20260907/review.md`, SHA-256
  `513031EEA2237B864F227B7E255E5D8E337E6D87C6A8352D89C647E3B918B38E`.
  Fresh bounded QA reached all 17 test bodies but did not complete successfully:
  JUnit reporting triggered a Windows platform subprocess blocked by its process
  guard. Exit 1 and incomplete JUnit are not a QA PASS. The reporting correction,
  fresh bounded QA and operator replay preparation remain pending. No third connected
  agent run is authorized while this evidence gap remains.
  Its representative v0.2.1-shaped
  project is synthetic, and app restarts use fresh instances in one pytest
  process. Separate child processes test application/Plain Mode leases. Native
  wrappers, real planning chat, actual historical installation compatibility
  and full release gates remain separate requirements. The full safe suite
  remains pending; installer-bearing suites must not be agent-launched.
- A usage-limit interruption stopped the three active agents and rejected one
  recovery-document update. After the user requested continuation, read-only
  workspace access succeeded and those agent tasks resumed. The refused update
  was not treated as persisted evidence.
- [x] Fresh scoped startup/baseline QA: **41 distinct selected cases passed**,
  46 executions including five preserved initial metadata failures, no skips.
  Final report `.tmp-startup-baseline-fresh-executable-qa-20260907/report.md`,
  SHA-256 `0ADB8A07003A04E2B46103D74E8A296E9B57E2A140A70362D807193545FF8BE2`;
  final evidence freeze SHA-256
  `A84A9E3E0F82310AAF8A56B08B4D4A208D3B0CFE42AC28C42B52E4E868D67B10`.
  Root reread the final results and verified these hashes and the unchanged
  session backup. Neither Issue 0012 nor Issue 0013 is complete.
- [x] Guarded transaction recovery slice: fresh implementation, independent
  source review and fresh independent executable QA **SCOPED PASS**. All 36
  selected recovery cases passed on the current transaction/test hashes;
  `.tmp-transaction-distribution-fresh-qa-20260907/transaction-junit.xml`
  SHA-256 `1663CBA2A50CEB3CBD5ACCD8FE2D23A506A2861F1270FDA9E0E0610ADF54C532`.
  This marker excludes distribution, native installers, older-helper policy,
  wiki storage, broad final-tree regression and release certification.

All older access, agent-capacity and uncommitted-checkpoint paragraphs below
are historical records. They do not describe this session's working-file
permission, current HEAD or R02 QA status. Earlier native/operator gates and
other unresolved repair findings remain open until replaced by new evidence.

## Historical recovery checkpoint (2026-09-06)

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
