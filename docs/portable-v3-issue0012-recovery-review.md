# Issue 0012 recovered-tree independent review

Date: 2026-09-05. Reviewed checkpoint: `a69baa8`, compared with `5d1d13a`.
Reviewer: fresh-context `issue0012_recovered_review` agent.
Result: **FAIL**. Read-only code review; no installer execution or runtime QA.

References below identify the reviewed checkpoint, not necessarily later lines.
None of these findings establishes the cause of the vanished original checkout.

| ID | Priority | Confirmed defect | Required repair |
| --- | --- | --- | --- |
| R01 | P1 | `transaction.py:673,776,789,815`: the journaled phase skips candidate path ownership validation before creating a receipt and moving the candidate. | Validate exact transaction-bound candidate name, parent, canonical ancestry and link status before every recovery phase, including before the ownership receipt exists. |
| R02 | P1 | `verify.py:183`, `transaction.py:951,1207`: ignored project files are invisible to cleanliness checks, but uninstall removes the complete release. | Inventory all release contents, including ignored files; preserve or refuse removal of non-owned material. |
| R03 | P1 | `transaction.py:1158,1176,1202`: resumed uninstall trusts old action evidence and permits broad descendant targets. | Bind each action to a canonical allowlist and ownership evidence; revalidate current ancestry, hashes and contents immediately before removal or restoration. |
| R04 | P1 | `transaction.py:186,219,245,264`: stale-lock reclamation can delete a replacement live lock; failed process inspection is treated as death. | Use exclusive identity-checked reclamation and distinguish confirmed-dead from inaccessible or unknown owners. |
| R05 | P1 | `transaction.py:484,520,540,549,589`: interrupted bootstrap publication recaptures published assets as legacy backups and rejects its pending record. | Resume from validated pending original-backup evidence and recognize already-published assets, including legacy migration. |
| R06 | P1 | `transaction.py:850,889,895`: fresh-install retry rejects the correct pointer if activation occurred before the switched journal checkpoint. | Accept only the exact already-published intended fresh-install pointer; reject divergent states. |
| R07 | P1 | `install/devloop.sh:68,209,230`: EXIT cleanup remains armed during preparation and can delete a journal-owned candidate. | Transfer candidate cleanup ownership before preparation; never delete transaction-owned candidates from wrapper cleanup. |
| R08 | P1 | `install/devloop.sh:170,205`: recovery is invoked in a conditional Bash context that suppresses implicit errexit; failed adoption can be followed by commit. | Explicitly check every recovery command, and prohibit activation after failed adoption. |
| R09 | P1 | `transaction.py:1052,1066,1110`, `dispatch.ps1:28`: uninstall removes prerequisites for the public retry entrypoint. Tests bypass this through an external copy. | Provide a usable external recovery entrypoint before removal and exercise retry through the shipped user interface. |
| R10 | P1 | `install/devloop.ps1:171,173`, `install/devloop.sh:198,200`, `README.md:48,54,60,66`: documented standalone or streamed installers require helper files they do not have. | Restore the documented distribution contract with verified bootstrap staging, or obtain approval for a changed distribution contract. |

Unless prefixed `install/`, Python and dispatch references are under
`install/bootstrap/`. Issue 0012 remains incomplete until all findings and
required release gates are resolved.

## Repair progress

- [x] R01 scoped repair: fresh implementation, independent review PASS, and
  independent sandbox QA PASS. Native installer/platform gates remain open.
- [x] R02 scoped repair: fresh implementation, independent review PASS and
  fresh independent QA PASS (86 distinct tests); see the 2026-09-07 checkpoint.
- [ ] R03-R06, R09-R10: open; no fixes certified.
- [x] R07-R08 scoped repairs: fresh implementation, independent review PASS,
  and independent in-memory Bash QA PASS. Native installer gates remain open.
- [ ] R11: additional fresh read-only review confirms Git repository-selection
  settings can redirect validation outside the intended worktree/index.
- [ ] R12: parent source-only reproduction of default wiki writes inside the
  immutable release; fresh validation and a storage-location decision pending.
- [x] Historical bootstrap overlay materialized from the unchanged reviewed
  patch; fresh inert-data QA PASS on 2026-09-07 (18 sources, 77,321 decoded
  bytes, 142 provenance records, 28 corruption probes). This is data integrity
  only; compatibility harness integration and native execution remain open.

### Final retry/flush scoped QA PASS; distribution incomplete, 2026-09-07

Fresh implementation corrected the caller-Path overwrite and two actual Windows
`rb`/fsync errno-9 failures. Final developer guarded suite: 36 PASS, including
the real retry continuation, interruption boundaries and preservation checks.
Fresh independent source review **PASS**:
`.tmp-transaction-retry-flush-fresh-review-20260907/review.md`, SHA-256
`B17839DB4A6AF1DF67029F8C5E90D73DE723F179544D7E922559DA6E679D18C8`.
The reviewer compared the exact preceding transaction bytes and verified all
seven other packaging files remained at their reviewed hashes.

Current transaction SHA-256:
`9F0346C2916364762E5B4933D153F87BE161EFCD02C88C13ED8CFAAF5CB8E4CD`;
guarded regression file SHA-256:
`DCED656D8E9FAA15B9B5326A043F91F4B2B5DB77A6DA725E4AB22CFC6A805377`.
Fresh independent guarded recovery QA **PASS**: 36 tests, zero failures,
errors or skips, 104.32 seconds. Root checked the retained log/JUnit and all
21 unchanged source/helper inputs. Evidence:
`.tmp-transaction-distribution-fresh-qa-20260907/transaction-junit.xml`, SHA-256
`1663CBA2A50CEB3CBD5ACCD8FE2D23A506A2861F1270FDA9E0E0610ADF54C532`.
This certifies the selected recovery slice only. Initial distribution/Bash
QA had 18 passes and 10 failures; a retry recorded two passes before its runner
pipe closed. The QA launcher and three previously recorded probe files are
now missing. Execution is paused pending clarification; the cause is unknown.
See [the interruption record](portable-v3-qa-interruption-20260907.md).
Windows read-only legacy assets still refuse publication, preserving original
and staged bytes/attributes; successful support is not claimed. Old-v2 helper
migration/refusal and R12 remain unanswered product decisions. Native/operator
gates remain open; this source PASS does not complete Issue 0012.

### Earlier packaging rework independent review FAIL, 2026-09-07

Fresh `packaging_rework_fresh_implementation` froze three P1 source repairs:
retain the release inventory from full ownership validation, parse and validate
public retry options before recovery, and durably stage legacy backups before
exclusive publication. Report and nine-file hashes:
`.tmp-packaging-rework-20260907.md`, SHA-256
`2883693F35050F0E5D0EBE3A29BE9989C6FB19AA33E42638019F5A02C78B9563`.
Native Ruff, PowerShell AST, Bash syntax, isolated option-prefix probes and
scoped whitespace checks passed. Python regressions remain unexecuted.

The fresh review found another P1: `resume_uninstall()` replaces its caller's
optional install-root Path with the journal's string, then calls `.absolute()`
on that string during option validation. Root directly verified this path and
the final review report:
`.tmp-packaging-rework-independent-review-20260907/review.md`, SHA-256
`029AEC97B635366721A9E352CC11871D7F0D5AEA578F66EBB276EAA3260651CC`.
The report found no additional source blocker in the other two narrow repairs.
A fresh implementer owns the retry correction; independent review and
executable QA of the corrected tree remain required.

The accepted older protocol-v2 helper finding is explicitly unresolved. The
user's choice between recoverable migration and refusal is pending. No combined
packaging PASS is possible from this partial rework. Fresh executable QA is
still required.

The initial safe-harness review also returned **FAIL** for fixture isolation,
inherited Linux state, incomplete operator-evidence binding and dangling-link
assertions. Its report is
`.tmp-safe-harness-review-525685970b0d4c97b46b7e00b02608fe/report.md`, SHA-256
`1E9D0B40D7C3D2A768A6AE06717FA5622B6D9113218CB40217D2BBBBC1E65FF5`.
The four repairs are now frozen in
`.tmp-safe-harness-rework-20260907/report.md`, SHA-256
`F9DF6812B4F6717BFAF0891D6CEE691D7B4FF2012CADB7FCACD31D05083CD446`.
Fresh independent source review **SCOPED PASS** is now recorded in
`.tmp-safe-harness-rework-independent-review-20260907/report.md`, SHA-256
`193FD14B73337D52D14F3BA511438D9045847D33C8E3C0E3E8CBDE1124D8DCEF`,
with 26 inert harness tests passing. Targeted
developer results span different source snapshots and are not a full final-tree
PASS. Broad pytest and operator installation gates remain unexecuted; the
harness still requires complete independent final-tree QA. Collection did not
complete, and no reviewed operator manifest was created.

### Combined packaging re-review FAIL, 2026-09-07

Fresh independent reviewer `packaging_fresh_review` rejected the nine-file
draft below with four P1 findings. Report: `.tmp-packaging-review-20260907`,
SHA-256 `ABF68F946E1409BB8A7CC693D380CE49E1431BB22C53D47A0FE6FC40FD8AE640`.
All nine frozen file hashes were independently verified unchanged.

1. Late ignored, tracked or runtime content can be captured as removal evidence
   after the original release validation. Bind that inventory to original
   release ownership before authorizing deletion.
2. Installed and external uninstall retry paths omit caller option handling.
   A PowerShell AST-only probe with fake Python confirmed that Help and
   KeepSkills still dispatch recovery. Parse help/invalid options before
   mutation and validate preservation/destination options against the plan.
3. Existing accepted protocol-v2 bootstrap helpers do not participate in the
   new advisory guard and are still selected for mutation by both drivers.
   Establish a safe compatibility/migration or exclusion boundary before
   invoking old lock mutation; testing two new guard handles is insufficient.
4. Direct copies into final legacy backups leave partial files on interruption;
   retry refuses although the original remains intact. Use validated durable
   atomic publication and recover only transaction-owned staging data.

Apart from the isolated PowerShell option-dispatch probe, these findings are
source-only. No Python, installer, old helper, deletion or native gate ran.
The partial rework above supersedes this rejected source draft; another
independent review and executable QA remain required. No repair or issue
completion marker is promoted by this review.

### R03-R06/R09-R11 implementation freeze, 2026-09-07

Fresh implementation agents `transactions_fresh_implementation` and
`distribution_fresh_implementation` completed source drafts. A new independent
`packaging_fresh_review` is reviewing the combined diff. **No new repair PASS
or completion marker is certified by this source freeze.**

Transaction scope: action ownership and current-content revalidation, guarded
stale-lock reclamation, interrupted bootstrap publication, exact fresh-pointer
recovery, retained-release reactivation, and external uninstall retry launchers.
Distribution scope: standalone/streamed source staging, effective Git path
validation, and child-only Git selector isolation including older helpers.

Native Ruff, PowerShell AST, Bash syntax and scoped whitespace checks passed.
The 22 transaction tests were not executed. Four distribution tests passed on
an intermediate tree before the runner pipe closed; later source changes and
the final 17 new plus nine existing Bash tests have no behavioral result.
Python execution subsequently returned Access denied across three contexts.
Strict mypy, final behavioral tests, independent executable QA and all native
operator gates remain pending. No installer or uninstaller was executed.
These drafts change dependencies of earlier passing slices. R01/R02/R07/R08
PASS records apply to their recorded checkpoint bytes; their regressions must
run again before the combined final tree can be certified.

| Frozen file | SHA-256 |
| --- | --- |
| `install/bootstrap/transaction.py` | `2182D695962F3D8018758662B33E9CB14455B7559DA8A9F639A7943A56764023` |
| `install/bootstrap/install/uninstall-devloop.ps1` | `C245639B3C3ACFD66315E0A65D57B318428F06169D099A3578A86DD498CFFCF6` |
| `install/bootstrap/install/uninstall-devloop.sh` | `7E32E6D266897D62CAF55F7CA58E77CDF13DEA535D75DC732D91EE2679ADE144` |
| `tests/test_portable_transaction_recovery_repairs.py` | `9484F81C6B3CBE5F964FF92ACE11CE6D815C29AE87EBB19332F6A24AF10121AD` |
| `install/devloop.ps1` | `755844DF320ADE387346DC76A7A53BD4B23EB943B217F9BD9A933A9D2A5F4EC1` |
| `install/devloop.sh` | `54F80B7928198978D4D6BCA016B8B56BF113ADAFD9BD6ACE9B6A72FDE2CDF6CF` |
| `install/bootstrap/verify.py` | `5C6FB789BE3365B65D5F2BF117D6A100349E2CFB3F4720A7BFA299C9383B3F95` |
| `tests/test_portable_bash_recovery_safety.py` | `30E8986521C2CC99E0FBCBBFF5897115C0B33FF943D3203F4E85C1D103492201` |
| `tests/test_portable_distribution_git_scope.py` | `C5CFCEFB818E3FBC1606CEDF719417028CAC6B5ABB864CC4936C5AD6EEC7C5A6` |

### Current checkpoint: R02 QA verified on 2026-09-07

Root reread the fresh QA report at
`C:\Users\Dimitris\AppData\Local\Temp\devloop-r02-qa-f7a925\report.md`
and verified SHA-256
`50D2C3346E1DE7C1C4F8072F42720958E4FC3F722B72A4EC5494ED43FB38E44A`.
Result: **SCOPED PASS**, 86 executions / 86 distinct tests, zero skips or
audit violations. All seven frozen source/test hashes match current HEAD
`561403e719750032ac992ed64b96811ee17ebc93`. Production recursive removal
was intercepted; native installer, uninstall, rollback, console and
authenticated behavior were not exercised. The recorded 2 Ruff and 26
transaction mypy diagnostics remain open until a fresh repair clears them.

The recovered E: workspace now permits working-file writes, verified with a
unique create/read/remove probe. The `.git` grant is read-only. Older pending
QA, unavailable fresh-agent and F: workspace statements below are historical;
they are superseded only for the scopes explicitly verified here. Other
findings and mandatory issue/release gates remain open.

### R02 implementation and first re-review

Fresh developer: `issue0012_ignored_data_repair`. The initial change replaces
ignore-filtered untracked enumeration with a shared NUL-delimited inventory
(`git ls-files --others --directory -z`), also rejecting empty unmanaged
directories. The isolated `.venv` exception still requires its fingerprint.
Developer evidence: eight guarded real-Git R02 tests plus 17 R01 regressions
passed (25 total, no skips); public refusal tests preserve complete snapshots.
Git fixture commands are confined to unique C: temporary directories with
repository overrides, global/system configuration, hooks and templates isolated.

The first independent re-review (`issue0012_ignored_data_review`) returned
scoped FAIL with one P1 integration regression: normal candidate bootstrap execution and
public runner entrypoints do not consistently suppress Python bytecode writes.
They can create ignored `__pycache__` content inside the release before the
new inventory check. Developer-only green results therefore do not certify R02.
The parent independently verified the unsuppressed Python call sites in both
installer drivers, dispatchers and public runner/planner wrappers. Repair this
interaction without exempting arbitrary user files, then repeat fresh review
and QA. R02 remains open.

The reviewer intercepted (without performing) the actual Python 3.12 loader
cache-write attempt and verified that the new checker rejects that inventory.
Twelve additional in-memory inventory cases passed, and all four changed files
compiled in memory with unchanged submitted hashes. No real installer or
filesystem test matrix was run by the reviewer. The 25-test developer result
remains developer evidence, not an independent QA PASS.

Fresh rework developer `issue0012_ignored_data_integration_repair` is addressing
the real startup interaction. Its pre-rework 23-file snapshot is retained at
`C:\Users\Dimitris\AppData\Local\Temp\devloop-r02-integration-baseline-19c15d0937374fa7acf75d5fdf5527fc`.
It includes the drivers, uninstall scripts, dispatchers, shared verifier and
transaction module, public launchers, reached runtime modules and the three
existing safety regression files. Review the final diff against this snapshot
to separate the startup repair from earlier R01/R02/R07/R08 changes. Independent
re-review and QA remain pending; do not mark R02 complete from an in-flight
developer report.

Rework checkpoint (not a review/QA PASS): the developer reports RED-to-GREEN
coverage for candidate top-level imports on both shell paths, six runner/planner
invocations, and two captured worker launch commands. Source-only probes use
disposable tracked C: fixtures and re-run public verification while comparing
complete snapshots. Candidate probing executes top-level script imports without
the transaction command entrypoint, so sibling `verify` loading is the observed
cache-producing path. Four update/dispatch cases check captured no-bytecode
invocation policy with a substitute import; they are not evidence that directly
executing `verify.py` caches that entry script. Remaining before freeze: consistent
bootstrap recovery/uninstall invocation handling, existing Bash regression
compatibility, focused packaging/typing/lint/syntax gates and platform-appropriate
test selection. Full installers and native release gates have not been run.

### R02 rework freeze for final review

Developer `issue0012_ignored_data_integration_repair` froze 12 existing-file
changes plus `tests/test_portable_bytecode_safety.py`. The production delta
adds per-invocation Python `-B` at the drivers, dispatchers, public launchers,
and worker launch; three Bash interpreter uses also gain quoting. No process
wide environment setting or arbitrary cache-directory exception was introduced.
The previous Bash regression stub accepts the added interpreter option.

Developer gates: 46 tests passed in 38.280 seconds, no skips (seven new bytecode
regressions, eight inherited R02 cases, 17 R01 cases, nine Bash recovery cases,
and five packaging assertions). Ruff and strict mypy with
`--follow-imports=silent` passed for the two changed test files. Five Bash
syntax-only checks, five PowerShell AST checks, and scoped whitespace checks
passed. These are developer results, not independent QA or native release
approval. The shared verifier/transaction and original R01/R02 tests were not
changed during startup rework. Final fresh review and QA remain pending.

Parent-verified frozen SHA-256 values, including the underlying initial R02
repair so both stages can be reviewed together:

| File | SHA-256 |
| --- | --- |
| `install/bootstrap/verify.py` | `8A06C59E8A565091CC65EAA92A75AECAF83C729CF31CF97508B01FF8FC543AC3` |
| `install/bootstrap/transaction.py` | `6A1222F3D7E767447BF7933E9813DC19504814279145D0924074C84A72972380` |
| `tests/test_portable_release_content_safety.py` | `02FB9C29381612FA030458A92E8E386A618EC46F67EC34B42D6ED66ED1C2E7E6` |
| `tests/test_portable_transaction_candidate_safety.py` | `B6F313BD3F41883083710A21C1F2AB370B4CEEFE05CEC729A9B6EECC843A72F9` |
| `bin/devloop-plan.ps1` | `E0E82EA9FC2A3B1946CCCD7FE5359E4EF89B1C61178DE1823D266C204255D46D` |
| `bin/devloop-plan.sh` | `74923DA0950E94CA44BB6A644118A1600D49C8E524866548C67BF253182C27E8` |
| `bin/devloop.ps1` | `B52000C38B1F4FC55F80C0EFE45438893D2F4FF701D103328C893AB14650A01D` |
| `bin/devloop.sh` | `A9BBE187335896746C72B13E8F76AE28527B637AFB7FE67726079013E13C6AF2` |
| `install/bootstrap/dispatch.ps1` | `94071CB83F5DDF0B799E5CA2FBC7056248D980F1F95B4EA7E71E89F3591A0505` |
| `install/bootstrap/dispatch.sh` | `3A0869E54BB1B47260CE4E183725E205D0D1ECFC512A602BA1E57F8FAB42B05C` |
| `install/devloop.ps1` | `AB5758376DCDD8236A8D6F5AE00C0D56E12CDEE8DFB2FE6729B7272DFE21328A` |
| `install/devloop.sh` | `97DD4FCCE27AD6437B668B47CB2531CA87ACDB36B2FC3F2E5BF4FECB25F16BCD` |
| `install/uninstall-devloop.ps1` | `908FA5BE5310F4DA2736968EDE94394B90963C9082FDF73F8BC27C6A64425ECC` |
| `install/uninstall-devloop.sh` | `87DC6046217D381055A27532726BABE6AAB8249497DEEC683926F241D0C05A2D` |
| `src/devloop/portable_sessions.py` | `99528A4311538FFA2B5938C95CF375D14852987092F9A7E0C520D79216781A74` |
| `tests/test_portable_bash_recovery_safety.py` | `77D30AFC0C2598CFC4952BD9876A3E4080E672BB0BCCEADC9243C0FE7EA7E1D0` |
| `tests/test_portable_bytecode_safety.py` | `1386480BC651AFE9861F94015A93D735024C768C04A7B8697A15CD35078BD652` |

### R02 final independent review, 2026-09-06

Fresh reviewer `issue0012_r02_review_second_audit_2` returned scoped **FAIL**
against `4a44144674cc74decb2cb052ebc72451d5bde4c7`. It verified all 17 frozen
hashes and compared both baselines directly. Two blocking P1 gaps remain:

1. **Git administration ownership**: `verify.py:92,99` fingerprints the index
   and inventories untracked worktree entries, but neither includes `.git`
   contents. Read-only real-Git probes confirmed that exclusion. Uninstall's
   preflight at `transaction.py:939` accepts that verification before the
   recursive release removal at line 1195. Unowned notes, additional refs, and
   worktree administration can therefore escape preservation checks. This is
   distinct from R11's inherited repository-selector defect. Establish Git
   administration ownership or preserve/refuse unowned material; add guarded
   real-Git regressions for `.git/operator-notes` and added refs/worktree metadata,
   asserting complete preservation before any uninstall mutation.
2. **Retained older launchers**: `dispatch.sh:42` and `dispatch.ps1:74` execute
   the selected immutable release's wrapper without conveying a no-bytecode
   policy. The verified older wrapper at `a69baa8:bin/devloop.sh:28`, and the
   pre-startup PowerShell wrapper at line 47, do not pass `-B`. Rollback retains
   those wrappers, so cold startup can create release-local `__pycache__` and
   fail later strict verification. An intercepted loader probe recorded one
   attempted cache write without suppression and zero with it; no cache was
   written. Enforce suppression across stable dispatch for accepted older
   payloads, preserve caller environment/arguments/cwd, and add an old-wrapper
   cold-cache regression followed by public verification.

Review checks: nine extracted-function Bash tests / 15 scenarios passed in
2.222 seconds, 13 in-memory inventory cases passed, and seven Python files
compiled in memory. The reviewer's initial guard rejected subprocess pipes;
after restricting its exception to verified pipe descriptors, the same tests
passed with no guard violations. Current-release invocation changes preserve
existing arguments/environment/cwd, and the earlier R01/R07/R08 scoped deltas
were retained. No files changed and no installer, uninstaller, full wrapper,
real worker, or historical source was executed. These checks are not fresh QA
or native release approval.

Fresh developer `issue0012_r02_git_and_legacy_startup_repair` was then assigned
both blockers. It must preserve existing supported manifest/migration/rollback
contracts, establish ownership before user modifications rather than trusting
a new uninstall-time snapshot, and retain immutable older payloads. Current
repair code and focused regressions must receive another fresh independent
review, then fresh QA. R02 and Issue 0012 remain incomplete.

### R02 Git-ownership and retained-wrapper developer freeze, 2026-09-06

Fresh developer: `issue0012_r02_git_and_legacy_startup_repair`. The developer
stage is complete; R02 is not certified. The subsequent fresh independent
review returned FAIL, recorded below. Fresh QA must follow a review PASS.
No issue completion marker was changed and no commit was made.

The seven-file delta against `4a44144674cc74decb2cb052ebc72451d5bde4c7` adds a
separate versioned, commit-bound Git administration ownership receipt before
activation. Recovery retains and validates the original receipt rather than
recapturing it during uninstall or retry. Changed/unowned Git files, directories,
refs and worktree administration refuse deletion before mutation. Legacy v1/v2
payloads without prior ownership evidence remain verifiable but their deletion
is refused; this preserves data, not a claim that every old uninstall succeeds.
Git inspection suppresses optional locks and automatic diff index refresh.

Stable dispatch passes `PYTHONDONTWRITEBYTECODE=1` only to the child shell.
Caller environment and immutable historical wrappers are unchanged. PowerShell
dispatch explicitly conveys argument-list entries and filesystem cwd, inherits
stdio, waits for completion, and propagates the child exit code. Native console
and interrupt behavior remain unverified by these source-only tests.

Developer evidence (not independent QA): final guarded exec session `68030`
exited zero with 82 test executions / 61 distinct tests in 123.838 seconds and
no skips/audit violations. Twenty-one content tests repeat through the bytecode
subclass. Coverage includes 17 R01 cases, nine Bash cases / 15 scenarios, five
source-only packaging checks, Git receipt/recovery cases and hash-verified
`a69baa8` wrapper cold-import regressions. RED-to-GREEN cases include unowned
Git notes, refs/worktrees, additions during recovery/uninstall retry, root
reparse metadata, strict integer receipt versions, and historical bytecode.

Three changed test files passed Ruff. The verifier and two substantial test
files passed strict mypy; five Bash syntax checks, five PowerShell AST checks,
five in-memory Python compilations and scoped whitespace checks passed.
Bootstrap-wide checks retain the exact HEAD baseline: two Ruff E501 findings
and 26 transaction strict typing diagnostics, with no additions/removals.
Those outstanding diagnostics are not waived for final release gates.

The parent verified these frozen SHA-256 values against the current files:

| File | SHA-256 |
| --- | --- |
| `install/bootstrap/verify.py` | `01B889A3C60830E81D20856EC695B307FC903C84DBD9E5C856331D1E77B36CE9` |
| `install/bootstrap/transaction.py` | `B12C03B7DF00C1429B72C636F55F78BEA6E120F7A66AA0FB64043CA50487D6D4` |
| `install/bootstrap/dispatch.sh` | `56EE2F738C5F7D628AE19BC01F18E7D3238900630DFAEC07CFEFD9B615EAD7D9` |
| `install/bootstrap/dispatch.ps1` | `3C25F125E0FC9F829653857FAEDAA91B6F2B29B3831CA0C3E729C851AD0518F2` |
| `tests/test_portable_release_content_safety.py` | `012F324C33896D797A12B271C5EEE87C4D01A6C146C97575803AB2AAC57045D4` |
| `tests/test_portable_transaction_candidate_safety.py` | `D7976EDE92CE8C8E0F91AD927F6656EF379D5EC42C31BFB3A8C87DBA9A932905` |
| `tests/test_portable_bytecode_safety.py` | `3DC3EC0F00306DB0D558ACDBE0F778E195E2ABE16DD52591765AD8147344D046` |

Developer repeat harness:
`C:\Users\Dimitris\AppData\Local\Temp\devloop-r02-admin-guard-20260906.py`.
Inspect its mutation/child allowlists before reuse. It runs only reviewed
source-only/extracted-function seams in validated disposable C: leaves;
destructive production operations are intercepted. No full installer,
uninstaller, rollback, real worker or authenticated/native release gate ran.
Independent review/QA, historical-fixture integration and R03-R06/R09-R12 remain
open. These findings do not establish the cause of the vanished F: checkout.

### R02 deletion-boundary independent review, 2026-09-06

Fresh reviewer: `issue0012_r02_git_admin_startup_fresh_review`. Scoped result:
**FAIL**, one P1 remaining ownership gap and one P2 retry regression. References
below describe the seven-file frozen checkpoint immediately above.

1. At `install/bootstrap/transaction.py:784`, recovery invokes Git ownership
   verification optionally. Missing receipts are accepted at `verify.py:150`;
   prepared recovery with both locations present reaches `rmtree(candidate)`
   at `transaction.py:825`. A real-Git fixture using a supported legacy prepared
   journal, no Git receipt and candidate `.git/operator-notes` reached the
   intercepted removal. Its complete snapshot remained unchanged. Require
   prior ownership before duplicate-candidate deletion, while preserving
   compatible non-deleting recovery. This is R02, not the broader R03 finding.
2. At `transaction.py:1189`, resumed uninstall compares the complete original
   Git fingerprint even when its release-removal action is already `before`.
   A partial removal can have deleted owned entries. The reviewer intercepted
   production `rmtree`, removed only the validated disposable C: fixture's
   owned `.git/HEAD`, then raised an injected `OSError`. Public retry failed
   with `Git ownership fingerprint mismatch` before further mutation. Retain
   original ownership proof and permit safe continuation with already-removed
   entries, while rejecting additions or changed surviving material. Do not
   recapture receipts, broadly suppress mismatches or refuse every retry.

Independent checks: 23 existing focused checks passed in 50.354 seconds; the
two boundary probes confirmed both defects in 4.071 seconds. No skips or audit
violations occurred. Five Python sources compiled in memory and scoped
whitespace checks passed. All seven supplied hashes matched before and after;
HEAD remained `4a44144674cc74decb2cb052ebc72451d5bde4c7`. Historical-wrapper blob
IDs matched actual `a69baa8` Git objects. No repository files or commits changed.

Retained probe:
`C:\Users\Dimitris\AppData\Local\Temp\devloop-r02-review-boundaries-20260906.py`,
SHA-256 `FEFB8D27C372580BD53B9183A7EAF521D5DA1E2169622908827DDE8BD5F31EC8`.
The parent read the complete probe and inspected the implicated source paths.
No production recursive removal or full installer/uninstaller/wrapper/worker
ran. Native PowerShell Ctrl-C, TTY, pipeline and actual foreground behavior
remain outside this source-only evidence. No independent QA or release PASS
is claimed, and no additional R03 scope was certified.

Fresh developer `issue0012_r02_recovery_deletion_boundaries_repair` is assigned
only these two findings, preserving prior R01/R02/R07/R08 work. It must propose
the ownership/retry approach before editing and use public recover/uninstall
regressions with filesystem effects confined to guarded disposable fixtures.
The repaired result requires a new fresh independent reviewer and then QA.

Approved internal repair approach (not yet a completed implementation): require
the existing Git receipt immediately before duplicate-candidate deletion, while
retaining optional verification for compatible non-deleting legacy recovery.
For uninstall retry, retain receipt v1 and all existing public journal/action
schemas. Before a first owned release deletion, store a private entry-proof
sidecar in the exact UUID-scoped external uninstall staging leaf. Its complete
canonical inventory must hash to the unchanged prepare-time receipt; it is not
a new ownership claim. A proof-backed `before` action may accept only missing
owned entries and unchanged surviving entries. Pending actions still require
full ownership evidence. New/changed/link/special survivors must refuse before
retry mutation. Old already-partial actions without such proof must preserve
and report insufficient evidence, never infer missing inventory or recapture
ownership. Validate sidecar schema, identity, paths and aggregate and cover
interrupted proof publication. Do not invoke Git against partial/missing `.git`.
The fresh developer is implementing public recover/uninstall RED-to-GREEN
regressions; the parent has not certified the implementation or its tests.

Developer progress: the legacy duplicate-candidate public recovery regression
went RED (one test, 1.386 seconds, intercepted removal and preserved snapshot)
to GREEN (one test, 1.394 seconds). All 17 R01 regressions then passed in 2.364
seconds, without skips/audit violations. The parent inspected the new mandatory
receipt check and test. The deleting R01 positive fixture now supplies prior
typed Git inventory proof and retains the candidate by rename at the removal
seam; its Git metadata is a scoped fixture, not a native Git recovery proof.
The partial-owned-Git-file retry regression reproduced the fingerprint mismatch
(RED, 2.668 seconds), then passed (GREEN, 2.875 seconds). The original receipt
bytes and post-interruption snapshot were retained. Existing retry-addition
refusal and unchanged-release removal-seam checks passed (two tests, 5.437
seconds). A metadata-seam proof-path reparse regression went RED to GREEN
(one test, 2.728 seconds). These checks reported zero audit violations; they
are developer evidence, not native link tests or independent QA. The complete
proof-corruption, publication-interruption and compatibility suite is still in
progress. The current type comparison retains the exact 26-diagnostic HEAD
baseline, not a clean full release type gate.
Four-file pre-edit text baseline location:
`C:\Users\Dimitris\AppData\Local\Temp\devloop-r02-boundaries-baseline-6a3d8cde23484df6bfcfc473aac5d631`.
Raw hashes differ because each saved file has exactly one extra terminal LF
byte (0x0A). The parent independently computed each file's SHA-256 excluding
only that final byte; all four matched their preceding frozen hashes exactly.
The original bytes are therefore recoverable by that precise in-memory rule;
the saved files were not silently rewritten or presented as byte-identical.
The following final checkpoint supersedes the in-progress developer status.

### R02 deletion-boundary final developer freeze, 2026-09-06

Developer `issue0012_r02_recovery_deletion_boundaries_repair` completed and
stopped. The subsequent fresh review passed, recorded below; fresh QA has
started. R02 and Issue 0012 remain incomplete pending their required gates.

The final developer suites passed 72 executions / 72 distinct tests in
110.919 seconds, without skips or audit violations:

- Session `71571`: 32 R02 content cases and 17 R01 candidate cases passed
  (49 total, 74.608 seconds).
- Session `29464`: nine bytecode/startup class-owned tests, nine Bash recovery
  tests / 15 scenarios, and five read-only packaging tests passed (23 total,
  36.311 seconds). Inherited duplicate content tests were deliberately excluded
  from this second run, not counted twice or treated as skips.

Coverage includes both original review failures, unchanged owned duplicate
recovery using real Git, changed/new surviving Git data refusal, missing `.git`
without Git invocation, pending versus interrupted action handling, intact and
partial proof-less old actions, strict/corrupt proof data, and interruption
before/after proof publication. Proof/staging link and reparse cases use
metadata seams; they are not native link/junction evidence. Production recursive
removals were intercepted. Only exact validated disposable fixture teardown
used recursive removal; partial-removal probes changed explicitly validated
fixture files or retained the Git directory by rename within the same leaf.

Both changed test files passed scoped Ruff. The verifier and content test passed
strict mypy. Bootstrap-wide diagnostics remain the exact HEAD baseline: two
Ruff E501 findings and 26 transaction typing diagnostics, no additions/removals.
Five Bash syntax checks, five PowerShell AST checks, five in-memory Python
compilations and scoped whitespace checks passed. Existing diagnostics remain
release-gate debt, not a waived or clean full gate.

Evidence and repeat commands:
`C:\Users\Dimitris\AppData\Local\Temp\devloop-r02-boundaries-evidence-20260906.json`,
SHA-256 `5A2FCD4ED4554C18D48D796935832D8CF694CB5A5A7B14F65556AE013BAF968D`.
Guard helper:
`C:\Users\Dimitris\AppData\Local\Temp\devloop-r02-boundaries-guard-20260906.py`,
SHA-256 `697D47A5015213F524831198E03E5A27CEF39AC7EF07EE29E910097BB6C6167D`.
Inspect the guard and its imported predecessor before reusing either test lane.
The parent verified both helper/evidence hashes and all seven source/test
hashes directly; it has not substituted that check for fresh review or QA.

| Changed file in this stage | Frozen SHA-256 |
| --- | --- |
| `install/bootstrap/verify.py` | `AC1B3B580432BE4ED99624A90D373E4166146A2DB092F55E658FBA9FB872FEB3` |
| `install/bootstrap/transaction.py` | `1A9443836A742C67A4B3354A49BCCBAC84C9EC91D58C0EE226A202E83C738EC7` |
| `tests/test_portable_release_content_safety.py` | `491928311BB00F871B7C43EDECB4D2BAD1B2B5037F75AE8643AB44D6B6620AEA` |
| `tests/test_portable_transaction_candidate_safety.py` | `C61CB5E25C04FDB607C128256F0EE05599E3664EA0059232C63460D30E05370D` |

The two dispatch files and bytecode test retain their preceding frozen hashes.
The preceding four-file text baseline plus its exact one-LF reconstruction
rule remains the stage comparison point; HEAD `4a44144` is the broader combined
repair baseline. No repository documentation or issue marker was changed by
the developer, no commit was made, and no native installer/rollback, real worker
or authenticated gate ran. R03 and other recorded open findings are not fixed
or certified by this developer checkpoint.

### R02 deletion-boundary independent review PASS, 2026-09-06

Fresh reviewer: `issue0012_r02_deletion_boundaries_fresh_review`. Scoped result:
**PASS**, no blocking defects found in the frozen R02 repair. This does not
complete Issue 0012 or replace fresh QA and native release evidence.

The reviewer inspected the exact four-file stage diff and combined R02
production changes, public recovery/uninstall paths, proof publication and
revalidation, and preserved dispatch changes. Twenty exact schema/preserved-code
AST definitions matched HEAD. All seven frozen hashes and HEAD stayed unchanged;
removing only the baseline files' extra final LF reconstructed all four original
frozen byte hashes. R07/R08 files stayed unchanged versus HEAD.

Independent execution: 56 tests / 56 distinct passed, zero skips/audit violations.
The 49 existing focused cases took 74.318 seconds; seven newly authored probes
took 18.220 seconds. The probes covered proof fsync failure, deletion-checkpoint
replacement interruptions before/after replace, missing owned Git subtrees,
link/reparse/FIFO surviving-entry refusal, missing `.git` without proof refusal,
and real-Git non-deleting legacy recovery preserving operator notes. Five Python
sources compiled in memory and scoped whitespace checks passed.

Retained helper:
`C:\Users\Dimitris\AppData\Local\Temp\devloop-r02-fresh-review-probes-7c6f1ec3.py`,
SHA-256 `7D12FA9EB81E60CFC1EB112C28D47330C4AFF4C14F0BAF928A842DE2A91C39DE`.
The parent read this complete helper and the complete boundaries guard and
verified the helper hash. An initial extraction-delimiter SyntaxError happened
before tests/imported fixtures; it was corrected before the successful run.

Production recursive removals were intercepted; only exact validated C fixtures
were torn down. Link/special cases used metadata simulations. No installer,
uninstaller, full wrapper, rollback, real worker, authenticated or native console
gate ran. Dispatch behavior was inspected, not executed by this reviewer.
Ruff/mypy baseline debt was not independently rerun or waived in this review.
R03-R06/R09-R11, R12's user decision, historical fixture integration/QA and
retained-release reactivation remain open or unverified as previously recorded.

Fresh QA agent `issue0012_r02_final_fresh_qa` is now assigned this frozen slice.
It must independently inspect the test/guard scope, validate the complete R02
behavior and preserved startup paths, and report actual gates and residuals.
No completion marker is promoted before QA passes; native installer/platform
and the remaining issue acceptance gates stay open regardless of scoped QA.

### R11 additional Git-scope review

Fresh read-only reviewer: `issue0012_git_scope_review`. Result: scoped FAIL,
one additional P1 safety finding. Both installer drivers inherit Git selectors
for clone, checkout, commit lookup and cleanup (`install/devloop.sh:82-93`,
`install/devloop.ps1:82-94`). The shared verifier (`verify.py:81`) also inherits
them for ownership evidence; filesystem candidate-path validation does not
establish Git's effective worktree or index.

Read-only probes with installed Git `2.50.1.windows.1` established:

- `git -C` plus `GIT_WORK_TREE` redirected worktree resolution and untracked
  enumeration to the approved C: baseline fixture, while retaining E: HEAD.
- `GIT_INDEX_FILE` pointed at a nonexistent C: baseline file changed staged
  enumeration from 791 entries to zero without creating that file.
- The tested `core.worktree` configuration overrides did not redirect scope.

Required repair: establish a consistent Git invocation scope before cloning
and throughout validation/transactions, reject or remove repository-local
selectors, and verify effective worktree/Git-directory/common-directory/index
ownership. Add inherited-selector regressions for both drivers and verification.
Sanitizing only the tests is not a production fix.

No clone, checkout, reset, clean, installer, or uninstall was executed by this
review. The worktree-selector probe does not establish fresh-install cleanup
reachability; the reviewer also found upstream clone rejection of an existing
`GIT_WORK_TREE`. Destructive outcomes and native Linux behavior remain unproved.
This finding is not an explanation of the original F: checkout disappearance.

### R12 parent diagnosis: default wiki writes inside immutable releases

Status: source-only reproduction; pending fresh independent validation and a
user storage-location decision. This is not part of the active R02 developer's
two-blocker assignment and is not an independent review/QA PASS or a diagnosis
of the missing F: checkout.

Verified current call path: `src/devloop/cli.py:1019,1022` defaults to the
bundle-relative wiki with wiki updates enabled. At lines 876-881 a real
post-run task resolves that path against `bundle.root`, initializes the wiki,
and writes compiler context. `templates.py:21` derives that root from the
executing module location. `self_improvement_wiki.py:12,26,46` keeps paths inside
that bundle, creates wiki files, and creates `.compiler-runs/*-context.md`.
The compiler-runs directory is ignored by `.gitignore:32`, but ignored content
is intentionally included by the new release inventory.

The parent ran the inspected BundleContext/wiki functions with a representative
side-by-side release path under a nonexistent C: fixture prefix and an audit
hook that refuses every filesystem mutation and child process before execution.
Two repetitions intercepted four mkdir attempts, all inside the immutable
release. No directory, cache, or context file was created. The actual extracted
inventory function rejects the corresponding sample compiler-runs inventory
with `release contains unexpected untracked content`; that inventory input is
supplied data, not a real-Git fixture or full workflow result.

Read-only probe retained for independent reproduction:
`C:\Users\Dimitris\AppData\Local\Temp\devloop-v3-wiki-readonly-d8fecfb825c048728a049d679ba33bca.py`.
It exited zero and reported no mutations or child launches. CLI source SHA-256:
`d0ad572b3d122c371d18c85c7d1ba3e19d7d77fc6b384ca9ac8f7f1c6904bac4`;
wiki source SHA-256:
`22088c625e52b1c6221783393184e87f4e018d342169f18c27e0a5c7965d353c`.

A non-blocking question asks whether installed copies should keep writable
wiki data in per-user storage while development checkouts retain the existing
bundle-relative behavior. Do not silently relocate data, disable the default
wiki feature, or exempt arbitrary wiki/context files from ownership checks.
After that decision, require a fresh developer, reviewer, and QA sequence,
including normal post-run writes followed by release verification and retained
wiki data across update/rollback. Full CLI/installer execution was not run by
this diagnostic probe.

### R01 evidence

Fresh stages: `issue0012_candidate_safety_repair`,
`issue0012_candidate_safety_review`, `issue0012_candidate_safety_qa`.

- Initial guarded RED: one test failed because old journaled recovery attempted
  Git against the temporary source fixture. The subprocess guard blocked it and
  fixture bytes remained unchanged.
- Developer GREEN: 17 focused unittest tests passed.
- Independent QA: 28 tests passed in 3.34 seconds, no failures/skips and no audit
  guard violations. This includes 17 candidate tests plus 11 release smoke
  checks. Evidence directory:
  `C:\Users\Dimitris\AppData\Local\Temp\devloop-v3-safe-check-zfvog3y0`.
- Coverage includes all eight durable phases, 88 invalid-path cases, missing or
  mismatched ownership receipts, plain-directory and reparse checks, early
  prepare/publish rejection, and valid candidate-only/moved/both-location
  recovery with byte-preservation assertions. Two additional read-only
  symbolic-link-mode probes passed.
- New regression-file Ruff, AST/in-memory compilation, and diff whitespace
  checks passed. Scoped Ruff still reports unchanged baseline E501 in
  `transaction.py:522` and I001/E501 in the older installer test; these remain
  release cleanup work, not waived gates.
- Final reviewed and QA-verified SHA-256:
  - `install/bootstrap/transaction.py`:
    `ECAE64856FE1D2EEDC985BD9CA0108761670D5208FAE543B7055D3079D530AEA`
  - `tests/test_portable_transaction_candidate_safety.py`:
    `1A975E81DC9105FE5CABFF4F175CAEAB095A9C15EB17B606B3ED23420CC205B7`
  - `tests/test_portable_side_by_side_install.py`:
    `6D4C52D02F553A466C98D46ECAF2BF1E6B4F91B7196B5DAFAC5E942577D84D33`

Git responses and link metadata in these tests are simulated. The revised
native-installer assertion was inspected and compiled, not executed. R01's
scoped PASS does not certify real installer/native Linux behavior or the rest
of Issue 0012, and does not explain the original checkout disappearance.

### R07-R08 evidence

Fresh stages: `issue0012_bash_recovery_repair`,
`issue0012_bash_recovery_review`, `issue0012_bash_recovery_qa`.

- R07 RED: failing preparation exited 91 and was followed by the stubbed removal
  event. GREEN: cleanup ownership transfers before preparation and no removal
  event follows preparation failure.
- R08 RED: failing adoption was followed by commit and capability events. GREEN:
  failures propagate explicitly and the caller cannot fall through into a new
  clone after failed recovery.
- Independent QA: nine unittest tests / 15 in-memory scenarios passed in 2.205
  seconds, without skips. Five additional in-memory failure probes passed.
- Bash syntax-only, Ruff B/E/F/I/UP for Python 3.10 and 100 columns, strict mypy
  of the new test file, and installer diff whitespace checks passed.
- Evidence directory:
  `C:\Users\Dimitris\AppData\Local\Temp\devloop-r07-r08-independent-qa-eaaf47e43aec43a5b6789c9707da56ea`.
- Final reviewed and QA-verified SHA-256:
  - `install/devloop.sh`:
    `6AD07D8786CF2EF2820893DE780DF99ABD763499BD39D67ACD6AB413CBDF8CB1`
  - `tests/test_portable_bash_recovery_safety.py`:
    `6A6B04BB05877698A3C76F2FFA931F3DDC389228C8D971358F828E7BCF6F3428`

The probes execute extracted production functions and call sites, with virtual
paths, all reached filesystem/external command seams stubbed, startup profiles
disabled, and an empty read-only PATH. They do not execute the actual installer
or establish durable filesystem recovery, real adoption, or native-platform
installer acceptance. No repository or F-drive writes occurred in QA.

## Separate QA harness observations

The first fresh QA safety audit stopped due to model capacity before issuing a
final report. Its read-only observations are leads requiring final QA review:

- Side-by-side tests allocate `ROOT/tmp` despite an external `TMP`/`TEMP`.
- The historical compatibility fixture requires absent Git object `6048556...`.
- `test_bundle_installer.py:410-430` passes a real drive root to uninstall and
  relies on refusal; do not run without a non-destructive isolation boundary.
- Installer Git calls inherit environment overrides; inspect and isolate
  `GIT_DIR`, `GIT_WORK_TREE`, and related repository-selection variables before
  running any destructive fixture operation. This is not an incident attribution.

The full installer matrix and native Windows/Linux release gates remain open.
