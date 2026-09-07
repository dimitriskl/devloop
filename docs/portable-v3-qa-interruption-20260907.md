# Portable v3 QA interruption: 2026-09-07

Status: execution paused; cause unknown. Issues 0012 and 0013 remain incomplete.
This record preserves observations and scoped results, not release approval.

## Missing evidence

Fresh transaction/distribution QA initially completed 28 selected tests with
18 passes and 10 failures. Nine failures were the QA driver's case-sensitive
PowerShell executable comparison; one was a read-only Git ownership refusal.
The retry corrected the executable comparison and selected nine cases. Its log
records two passes, then the managed tool reported `runner pipe closed before
exit`. There is no complete retry JUnit, final process record or success exit.

The following paths under
`.tmp-transaction-distribution-fresh-qa-20260907/` are now absent:

| File | Previously recorded SHA-256 | Recorded exit |
| --- | --- | --- |
| `probe-025.sh` | `C901FA83DB7D8D4AE8434BDA10475E71988A70E8870DFE350FD2437795FF6389` | 0 |
| `probe-026.sh` | `5E548BC9DCF9F0B0014FC5A9C11C6C8C055C8C8EB8A7BE2614BDC8FACDEB1AD0` | 37 |
| `probe-027.sh` | `A131CBB3CCBD53D07015E065889A7F0136E672610CEC6694C484D6B651120D6E` | 31 |

Root independently checked the directory and the surviving `process-evidence.json`
(SHA-256 `A3589E156AE94EFFC2BFB8FBCFE5ECA8A8FDDB19D0A672E00CB7BA692E12E768`),
which contains these three hashes and completed process records. The active
`run_distribution.py` is also missing. `run_distribution_initial.py` and both
PowerShell launchers survive. Retry probe creation is inferred from the launcher
control flow and two test passes; no retained prior snapshot binds their bytes.
Do not treat that inference as the same evidence as the three recorded probes.

The proposed process-local read-only Git `safe.directory` adapter was never
applied or executed. The attempted final QA wrapper was never created.

Root read the surviving launcher, test seams, clone functions and fixture
configuration. Those inspections did not identify a cause for the disappearance.
The absence of recent matching Windows Defender Operational events 1116/1117
does not rule out security software, cleanup, manual action or another cause.
No missing script has been recreated and no affected test has been rerun.
The user was asked whether any tool or manual action removed or quarantined
workspace files; no answer is recorded at this checkpoint.

## Results that remain verified

- Startup repair: fresh implementation, independent review and fresh executable
  QA passed. The combined startup/baseline report records 41 distinct passing
  cases, configured Ruff and strict mypy, actual backed-up session replay and
  a PRD-only dry-run. See `portable-v3-recovery-status.md` for its frozen evidence.
- Guarded transaction recovery: independent QA passed all 36 selected cases,
  zero failures/errors/skips, 104.32 seconds. Root inspected the complete log
  and JUnit and rechecked all 21 source/helper hashes against QA's baseline.
  `transaction.log` SHA-256:
  `9A6DB02AB5BAF47E344AEE37C5B3FF37A0D91CAB9A975E9EE85FA521A32B91AF`.
  `transaction-junit.xml` SHA-256:
  `1663CBA2A50CEB3CBD5ACCD8FE2D23A506A2861F1270FDA9E0E0610ADF54C532`.
- Session reset backup remains unchanged, SHA-256:
  `1116A03C7769AB4E6DBDC1FAE31D0DE1E1DE3BDD09820DA5740A1268DC9B1531`.
  The live catalog was not modified by these checks.
- Safe-harness rework received fresh independent source review PASS and 26 inert
  test passes. Complete collection/final-tree QA and operator gates remain open.
- Scenario rework received fresh independent source review PASS. Fresh bounded
  QA reached all 17 test bodies but exited 1 during guarded JUnit platform
  reporting; its incomplete JUnit does not establish QA PASS. Both connected
  scenario failures remain unresolved. No third connected agent run occurred.

## Resume boundaries

Account for the evidence loss before reconstructing or rerunning the affected
probes. Preserve surviving artifacts and original failure reports. No permission
change, escalation, detached process, runtime installation or security bypass
is authorized as a workaround.

After the environment question is resolved, fresh QA still needs to complete
distribution and the final harness selection. Correct only the bounded scenario
QA reporting defect before its fresh rerun; prepare and independently review a
concrete operator replay for the connected case. The wiki storage decision and
older protocol-v2 bootstrap migration/refusal decision remain unanswered.
Native Windows/Linux, installer, real-runtime and complete release evidence
remain required. Retain both issue completion markers as unchecked.
