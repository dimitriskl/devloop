# Portable Dev Loop v3 Multi-Session Design

## Decision status

Accepted design for Portable Dev Loop generation **v3**, targeted for software
version **0.3.1**. The currently implemented version remains `0.2.1` until this
design is implemented and passes its release gates.

This design applies only to the portable `devloop-plan` and `devloop` wrappers.
It does not merge Portable Dev Loop with the separate CodexCLI application or
adopt CodexCLI Workflow Runs.

## Outcome

One full-screen Portable Application Shell can display and control several
independent Portable Workflow Sessions. Each session targets a different Git
worktree, runs through its own worker process, retains its own planning and
issue-runner state, and appears in an in-application tab. Users can follow all
sessions from one screen without combining their workflow contexts.

## Application model

- A permanent, non-closable **Sessions** tab is the initial view.
- The Sessions tab lists saved projects and ready, queued, running,
  waiting-for-input, paused, interrupted, failed, and unfinished sessions.
- A `+` action starts from an available saved worktree, registers an existing
  checkout, or creates a new Git worktree from a saved repository and branch.
- Selecting a leased worktree focuses its existing session instead of opening a
  competing session.
- A workflow tab shows the selected session's current stage, issue, activity,
  context, controls, and durable logs.
- Compact tab labels show worktree name, lifecycle status, and unseen activity.
  Background input requests use `[INPUT!]`; they never steal focus. A terminal
  bell is optional.
- Closing a workflow tab only hides it. The session remains available in the
  Sessions tab.

## Execution and isolation

The application shell is a supervisor. Every Portable Workflow Session runs in
a separate child process with its own current directory, runtime bridge, Codex
execution, output capture, and failure boundary. A failed worker cannot stop a
sibling session.

Supervisor and worker exchange a versioned JSON Lines protocol over redirected
standard input and output. Standard error is captured as per-session
diagnostics. Python's standard library and the already pinned Textual runtime
are sufficient on Windows and Linux; no new third-party runtime dependency is
required.

A machine-wide Portable Worktree Lease permits one live session for each
canonical worktree path. Multiple sessions may use worktrees belonging to the
same repository. Separate Dev Loop application instances share the same lease
and catalog rules.

## Concurrency

The user-wide **Portable Session Concurrency Limit** defaults to two actively
executing sessions and is editable in Options. `PAUSED` and
`WAITING_FOR_INPUT` sessions release capacity. Extra sessions remain visibly
`QUEUED` and start when capacity becomes available. Open tabs do not consume
execution capacity.

## Persistence

The machine-local Portable Session Catalog uses Python's standard `sqlite3`
module for transactional, cross-process access. It stores:

- stable session identity and lifecycle status;
- canonical saved-project and worktree paths;
- pre-PRD planning state, including the Codex planning thread identity;
- settings snapshot, timestamps, discovery summaries, and authoritative-state
  pointers;
- worktree leases, owner identities, and renewable heartbeats;
- the configurable concurrency limit.

A session becomes durable immediately after worktree selection. Before a PRD
exists, the catalog owns its resumable planning state. After publication, the
existing worktree-local `devloop.status.json`, `*.loop.state.json`, issue files,
and logs remain authoritative; the catalog stores only pointers and summaries.
It never becomes a second copy of workflow progress.

No raw credentials, tokens, complete command streams, or unredacted provider
payloads enter the catalog. Protocol values shown in the TUI pass through the
existing terminal sanitization boundary.

## Lifecycle and recovery

- Startup is passive: only the Sessions tab opens and no worker starts until an
  explicit Resume.
- Pause stops new scheduling and lets the active operation reach a durable
  checkpoint. Force Stop is a separate explicit action that may interrupt the
  current operation while retaining partial filesystem work, diagnostics, and
  the last checkpoint.
- Application exit prompts once, pauses all running sessions, persists their
  latest durable state, and stops their workers. v3 has no detached daemon.
- A worker crash marks only its session `INTERRUPTED`; restart is explicit.
- Completed sessions move to History. Forget removes machine-local metadata
  only and never deletes project files, logs, branches, or worktrees.
- Missing or moved worktrees become `UNAVAILABLE` and offer Relink or Forget.
- An expired lease is reclaimed only after its owner is confirmed dead. An
  ambiguous lease blocks execution and remains visible for inspection.

## Existing CLI and Plain Mode

Existing `--repo`, `--prd`, and runner flags remain valid. Plain Mode continues
to execute one foreground session without tab rendering while using the same
catalog registration, concurrency, and worktree-lease contracts. Non-TTY output
remains deterministic and append-only.

## v3 adoption from 0.2.1

The installer/updater installs v3 `0.3.1` without deleting project data. On
first v3 startup, an idempotent adoption flow:

1. Reads the existing user-wide planner configuration without overwriting it.
2. Registers its last confirmed target checkout.
3. Uses targeted `git worktree list` results and existing Portable Resume
   Candidate discovery to register related worktrees that contain unfinished
   portable workflows; it does not scan unrelated folders or drives.
4. Leaves related worktrees without portable workflow artifacts available for
   explicit adoption instead of registering them silently.
5. Creates catalog sessions pointing to existing PRD and issue-pack state.
6. Records a migration receipt only after the catalog transaction commits.

Users can adopt additional working projects later by selecting or adding their
checkout. Project-local workflow files are not migrated or rewritten, making
adoption safe to retry and preserving rollback readability for the older
runner.

The limitation is explicit: version `0.2.1` kept a planning Codex thread only in
the running process before a PRD existed. v3 can adopt all PRD-backed unfinished
work but cannot reconstruct a planning-only conversation that the old process
already lost.

## Validation gates

- Catalog schema migration, transaction rollback, and idempotent adoption tests.
- Cross-process lease, stale-owner, heartbeat, and machine-wide concurrency
  tests.
- Protocol framing, version rejection, malformed-message, redaction, and worker
  crash tests.
- Independent-session tests proving project paths, events, inputs, logs, and
  failures cannot cross tabs.
- TUI tests for the permanent Sessions tab, tab switching, unread status,
  non-focus-stealing input requests, hide/focus behavior, and responsive layout.
- Exact resume tests for pre-PRD planning and existing PRD-local role/pass
  checkpoints.
- Windows and Linux wrapper tests plus deterministic Plain Mode regression
  tests.
- Upgrade tests from a representative `0.2.1` planner configuration and
  unfinished project, proving that project files are byte-for-byte unchanged.
