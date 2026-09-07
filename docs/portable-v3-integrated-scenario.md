# Portable v3 integrated scenario

Status on 2026-09-07: **focused cleanup/attention checks PASS; connected scenario
FAILED before input routing**. The restored Python runtime executed the tests.
Seventeen tests covering cleanup and the attention predicate passed without real
workers. Two connected attempts failed while waiting for beta's input state; the
diagnostic attempt captured beta's actual `PermissionError` while reading its
test-local `commands/beta/1/1.json`, before the real worker input request. Alpha
read its own command successfully. The underlying access-denial cause remains
unverified; no permission change or denial-suppression retry was added.

Both failed runs retained evidence and completed owned-worker cleanup without
reported errors. Native Ruff `B,E,F,I,UP` checks passed for the scenario sources.
Fresh independent review/QA and the remaining runtime gates are still required.
No Issue 0013 acceptance checkbox or release-completion marker is established by
this file. Developer logs and source freezes are retained in
`.tmp-retry-scenario-fresh-repair-20260907/`.

The connected test in
[`tests/test_portable_v3_integrated_scenario.py`](../tests/test_portable_v3_integrated_scenario.py)
keeps one disposable Git repository, three linked Git worktrees, and one SQLite
catalog throughout these connected phases:

1. Seed an unfinished workflow in beta. Adopt a representative v0.2.1-shaped
   configuration pointing to alpha, discovering beta through the actual Git
   worktree list. Repeat adoption and compare project SHA-256 inventories,
   configuration bytes, branch refs, worktree listings, session identities and
   the single migration receipt.
2. Open the headless shell passively, explicitly start alpha and beta, and
   visibly queue gamma under the catalog's unchanged default limit of two.
3. Submit a duplicate alpha launch through the same supervisor, then probe its
   lease from separate application and Plain Mode processes. Each conflicting
   path must avoid backend invocation; Plain Mode must return 73 without terminal
   escape sequences.
4. Display distinct tab contexts. Produce alpha activity and an input request
   while beta remains focused; check unread/attention markers and the optional
   bell callback. Its synchronization requires alpha's exact tab and input state
   together with the expected bell callback. Gamma advances when beta waits.
   Alpha's background request
   arrives while beta already contains typed text. Submit beta text through the
   real UI and require alpha to remain unanswered. Queue alpha's answer while
   beta and gamma run; pausing beta must release capacity for that exact answer.
5. Hide gamma with Esc, observe its continuing activity on Sessions, and reopen
   its retained projection. Pause all workers and close the first shell.
6. Create a fresh catalog, supervisor and headless shell against the same files.
   Verify passive startup, then explicitly resume. The fake backend must receive
   alpha's exact durable planning thread/settings and beta's reviewer/pass-2
   checkpoint validated by the real worker recovery path.
7. Persist a partial beta edit and diagnostic, Force Stop beta, and require its
   unfinished marker and reviewer/pass-2 cursor to survive. Gamma advances into
   the released slot, then exits abruptly with code 17. Alpha must keep working,
   both interrupted sessions must retain diagnostics, and neither may replay
   automatically. Explicit recovery completes beta and gamma into History.
8. Move gamma with Git, detect its unavailable old path, reject an invalid Relink,
   and accept its real moved worktree. Forget beta's metadata and compare project
   bytes, branch refs and worktree listings again. Reap every owned worker.

The backend helper is
[`tests/portable_v3_scenario_worker.py`](../tests/portable_v3_scenario_worker.py).
It replaces only the worker's operation body with a deterministic fixture
backend. The test uses the actual supervisor, catalog, process ownership,
`portable_worker.main`, versioned protocol, heartbeat, input routing, checkpoint
capture, recovery comparison and `LoopStateWriter` APIs. Backend commands are
test-local JSON files; no installed Codex/Claude command, provider account,
installer, release updater or profile-backed gate is invoked.

The planning assertion observes the recovery payload delivered to this fake
backend. It does not execute the interactive planner's real chat backend.
Application restart here means new shell/catalog/supervisor instances after all
first-shell workers have stopped; the supervising pytest process remains alive.
The other-application and Plain Mode exclusion probes are separate processes.

All fixture files live under the safety harness's fresh workspace-owned pytest
temporary directory. Child environments isolate HOME, profile, configuration,
cache, temporary files, Git configuration, templates and hooks. Workers use the
repository's process-tree launcher, retain redirected streams, and are waited
for during normal and failure cleanup. Git operations touch only the private
fixture repository and its validated worktrees. Evidence and fixture files are
retained for inspection.

Cleanup attempts every supervisor, owned process and stream even after another
cleanup action fails. It aggregates failures, requires confirmed process-tree
termination and a completed wait, preserves an original scenario exception, and
writes final PASS evidence only after cleanup succeeds. Focused fault-injection
tests also cover unconfirmed termination, multiple simultaneous failures and
failure after an otherwise successful scenario body.

After the command-file access failure is resolved, the focused connected selector
is `tests/test_portable_v3_integrated_scenario.py::test_three_worktrees_share_one_catalog_across_the_complete_scenario`
with pytest arguments `-q -s -W error::pytest.PytestUnhandledThreadExceptionWarning -m "not integration and not operator_install"`. Use the interpreter confirmed by
the runtime gate; do not switch interpreters to bypass a denial.

The test prints its retained `evidence.json` path. That file records each phase
only after its assertions pass, plus synthetic backend requests, project hashes,
launch order and final worker cleanup. Timeout diagnostics capture actual
supervisor snapshots before UI unmount; worker milestones retain exact command
paths. A scoped PASS there does not establish
Windows/Linux wrapper parity, native console input/interrupt behavior,
installation/profile correctness, historical v0.2.1 installation compatibility,
old-runner readability, authenticated integration, or the remaining full-suite
release gates. The v0.2.1-shaped fixture is generated through verified current
adopter/state contracts; it has no historical project provenance.

Native Windows and Linux wrappers, terminal behavior and installation/profile
validation remain mandatory separate operator gates under
[`AGENTS.md`](../AGENTS.md). The existing layout, malformed-protocol, redaction,
stale-lease and migration-rollback suites remain separate regression gates.
