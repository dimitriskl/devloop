# Automatic .NET verification

When Development, Review or QA cannot run an authorized .NET test gate inside
its restricted worker, it can return a typed `operator_verification` request.
Dev Loop keeps the current issue, step and pass open and executes that gate
automatically under the user-launched session's existing account. Authorization
covers these existing test gates throughout the session: no repeated approval or
separate-terminal command is required. Pause and Cancel remain available through
F9 and terminate the owned test process tree. Pending requests from the earlier
manual handoff implementation are recovered on resume. Older running workers
must be paused and relaunched with the updated runner to use automatic execution.

Preparing the request is shown as a separate stage before source inspection.
The Git child reads from a null input stream, keeping it separate from the
worker's live control pipe. Its source-listing command has a 30-second timeout.
In particular, Git for Windows must never inherit that pipe: it can wait for
the control reader indefinitely even after the agent has returned its result.

The runner uses `operator_verification.py` and a supervised child process to execute
`dotnet test` against the specified existing project and filter. It builds with
`--no-restore`, writes a fresh TRX and an atomic non-secret `result.json` receipt
under `.loop.logs/operator-verifications/<request-id>/`, and leaves connection
and encryption settings unchanged. Test output is streamed to the session and
saved through the existing redaction service as `verification.log` beside the TRX.
The child inherits the session environment, uses null stdin, and stays in an owned
process tree. Dev Loop does not elevate the process or obtain new credentials.
The standalone script remains available for diagnostic reruns. Automatic execution
covers only test gates already authorized for their target database and writes.

Dev Loop independently requires a successful process exit, the expected test
count, every test executed and passed, and zero skipped/failed tests. It checks
the request identity, report hash and source fingerprint. Git-visible .NET source,
build and configuration files and local appsettings in their directories are
hashed; ignored build output and test results are excluded. This fingerprint
does not certify external database state, environment variables or SDK identity.
Changed source inputs create a fresh request and automatic execution. Executed
failures are returned to the active agent with test counters, failure details, and
TRX/log paths. Development repairs the failure and requests verification again;
Review/QA reports FAIL so the existing workflow routes the findings to Development.
The failed receipt is reused after pause, restart, or a scheduler retry, giving the
agent a repair turn before any unchanged test rerun. A claimed PASS after a repair
is independently verified against the failed gate before it can complete the step.
An unchanged failed gate or an attempt to substitute a different gate returns FAIL
without another test execution. The pending failure must be resolved first.

Failure feedback checks request identity, source fingerprint, and TRX hash before
sharing redacted diagnostics. Build failures without a TRX use the bounded tail of
their verification log. Missing/corrupt evidence is never treated as test-failure
feedback; unresolved evidence and process-launch failures remain blockers.
A paused incomplete gate runs again on resume, while accepted evidence
on unchanged inputs is reused. Successful verification and lifecycle interruptions
do not consume issue retry rounds.

After accepted evidence arrives, the runner invokes the same workflow step with
the evidence in Automatic Verification Evidence. It retains the current issue and pass and uses a
new log label. The worker continues implementation; independent review and QA
still run. This feature does not mark an issue complete merely because a preflight
passed. A worker that repeatedly requests the identical already-verified gate
without source changes is reported as blocked rather than looping paid model calls.

Verification evidence uses its own prompt section, preserving user Step Guidance
and its 4,000-character limit. Long test failures retain their details in the
prompt; the session screen shows a preview within the protocol's 8,192-character
limit. Failed sessions show diagnostics without presenting their last working
screen as live progress.

The result contract is nullable for ordinary outcomes. For an external test gate:

```json
{
  "kind": "DOTNET_TEST",
  "project_path": "tests/Example.Tests/Example.Tests.csproj",
  "test_filter": "FullyQualifiedName~ExampleSqlTests",
  "expected_tests": 2,
  "reason": "These authorized SQL tests cannot execute in this restricted worker."
}
```

The worker must inspect the actual project/tests to supply those values; the
example is not a default. Unsupported blockers still use the existing blocked
workflow. Automatic operator handoff currently supports `.csproj` test gates,
not arbitrary commands, deployment, installations or permission requests.

Local repair-path validation is recorded in
[verification-repair-validation.md](verification-repair-validation.md).
