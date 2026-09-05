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
- [ ] R02-R06, R09-R10: open; no fixes certified.
- [x] R07-R08 scoped repairs: fresh implementation, independent review PASS,
  and independent in-memory Bash QA PASS. Native installer gates remain open.
- [ ] R11: additional fresh read-only review confirms Git repository-selection
  settings can redirect validation outside the intended worktree/index.

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
