# Real CodexCLI v0.1.0 Demonstration

This sample contains no credentials and uses no fake or simulated backend. The
scripts create a disposable Git repository under `.release-demo/`, run doctor,
and open the installed `codexcli` launcher against that repository.

Windows:

```text
.\examples\release-demo\run-demo.ps1
```

Linux:

```text
./examples/release-demo/run-demo.sh
```

Use the request from `feature-request.md`. During the recording:

1. Show a passing doctor and submit the request for real analysis.
2. Request one analysis revision, then accept the PRD Package.
3. Choose the dedicated-worktree option and show the development view.
4. Show the distinct review and QA views; use a genuine review or QA finding to
   demonstrate a fresh rework attempt.
5. Pause during QA on Issue 3 of the generated ten-Issue package, close the
   process, restart, enter `/resume`, select the run, and show the same Issue,
   QA attempt, and workspace with the interrupted operation marked unknown.
6. Complete the remaining work and show the final Handoff Summary with the
   workspace disposition `LEAVE_INTACT`.

Do not show authentication data, environment dumps, hidden reasoning, or raw
transcripts in the recording. Repository publication remains outside the app.
