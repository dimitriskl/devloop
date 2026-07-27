Label: ready-for-agent

# Portable Dev Loop v3 Multi-Session Application

## Target Product

Product: devloop-plan + devloop

This PRD targets Portable Dev Loop: the `devloop-plan` planning intake and the
`devloop` Markdown issue runner. It covers the Portable Application Shell,
planning and delivery orchestration, portable runtime presentation boundary,
worktree management, user-wide planner state, project-local loop state, and the
Windows and Linux wrappers and installers.

The separately installed CodexCLI application is not the target. Portable Dev
Loop v3 does not adopt CodexCLI Workflow Runs, App Server execution, CodexCLI
run directories, component locks, or CodexCLI persistence.

## Problem Statement

Portable Dev Loop currently remembers only the last selected target checkout.
At startup, it searches for unfinished PRD workflows only inside that one
checkout, and the full-screen application owns only one active planning or
delivery operation. A user who wants to work on two projects must open separate
terminals, select each folder manually, and mentally track which session is
running, waiting, blocked, or complete.

The user cannot see several independent workflows in one application, cannot
switch between their live contexts, and cannot return later to a machine-wide
list of unfinished sessions. Planning work is not durably indexed before a PRD
exists, so an interrupted planning-only conversation can be lost. Concurrent
Dev Loop processes also have no shared worktree ownership or concurrency
contract, making accidental duplicate work against one checkout possible.

Existing Portable Dev Loop projects already contain valuable PRDs, issue packs,
logs, role/pass checkpoints, and implementation worktrees. A new multi-session
version must adopt that work without moving or rewriting it, and must remain
usable on Windows and Linux without introducing another third-party runtime
dependency.

## Solution

Release Portable Dev Loop generation **v3** as software version **0.3.1**.
Turn the Portable Application Shell into a session supervisor with a permanent
Sessions tab and one in-application tab for every opened Portable Workflow
Session. Run each session in an isolated child process bound to a distinct Git
worktree, allowing the user to follow multiple projects from one screen while
keeping their workflow state, output, failures, and Codex execution independent.

Add a machine-local Portable Session Catalog backed by Python's standard
SQLite module. The catalog remembers saved checkouts, pre-PRD planning state,
session summaries, options, worktree leases, and migration receipts across
application restarts and multiple Dev Loop application instances. Existing
project-local loop state remains authoritative after a PRD exists.

Use a versioned JSON Lines protocol over child-process standard input and output
for portable supervisor/worker control and events. Keep standard error as
session diagnostics. Default to two actively executing sessions, configurable
through Options; paused and waiting-for-input sessions release capacity while
extra sessions remain visibly queued.

Provide an idempotent v3 adoption flow for existing `0.2.1` projects. Import the
last confirmed target, inspect only its targeted Git worktrees, discover
unfinished portable PRD workflows using the existing rules, and create catalog
pointers without changing project files. Additional projects are adopted when
the user selects or adds them.

## User Stories

1. As a Dev Loop user, I want one full-screen application to manage several workflows, so that I can follow concurrent work without opening separate terminals.
2. As a Dev Loop user, I want each workflow represented by its own in-application tab, so that project contexts never become mixed.
3. As a Dev Loop user, I want a permanent Sessions tab, so that I always have a home view for all saved and unfinished work.
4. As a Dev Loop user, I want the Sessions tab to be the only initial tab, so that startup is predictable and uncluttered.
5. As a Dev Loop user, I want startup to remain passive, so that opening Dev Loop never restarts expensive work automatically.
6. As a Dev Loop user, I want to explicitly resume an unfinished session, so that execution begins only when I intend it.
7. As a Dev Loop user, I want to start a new session through a `+` action, so that adding work is visible from anywhere in the shell.
8. As a Dev Loop user, I want to start a session in an available saved worktree, so that recurring projects are quick to reopen.
9. As a Dev Loop user, I want to register an existing checkout or folder, so that projects created outside Dev Loop can participate.
10. As a Dev Loop user, I want Dev Loop to create a new Git worktree from a saved repository and branch, so that parallel work in one repository remains isolated.
11. As a Dev Loop user, I want each concurrent session to target a distinct worktree, so that sessions cannot edit the same checkout simultaneously.
12. As a Dev Loop user, I want two worktrees of the same repository remembered separately, so that each branch can have an independent workflow.
13. As a Dev Loop user, I want selecting an already leased worktree to focus its existing session, so that I cannot accidentally create a duplicate runner.
14. As a Dev Loop user, I want worktree identity based on its canonical path, so that aliases and relative paths do not bypass ownership.
15. As a Dev Loop user, I want every successfully opened target checkout remembered automatically, so that it appears on later starts.
16. As a Dev Loop user, I want every implementation worktree created or reused by Dev Loop remembered automatically, so that unfinished delivery work remains discoverable.
17. As a Dev Loop user, I do not want Dev Loop crawling unrelated drives or folders, so that discovery remains bounded and private.
18. As a Dev Loop user, I want a saved project with no unfinished PRD to remain available, so that I can start a new change there.
19. As a Dev Loop user, I want every unfinished PRD and issue pack listed independently, so that multiple workflows in one worktree remain distinguishable.
20. As a Dev Loop user, I want unfinished sessions sorted by recent activity, so that the most relevant work is easiest to find.
21. As a Dev Loop user, I want each session row to show project, worktree, PRD, stage, progress, active issue, and last update, so that I can choose confidently.
22. As a Dev Loop user, I want compact tab labels to show worktree and lifecycle status, so that background progress is visible from every tab.
23. As a Dev Loop user, I want an unread marker when a background session changes materially, so that I know which tabs need attention.
24. As a Dev Loop user, I want an `[INPUT!]` marker when a session needs input or approval, so that requests are not overlooked.
25. As a Dev Loop user, I want background input requests never to steal focus, so that typing in one workflow cannot be redirected into another.
26. As a Dev Loop user, I want an optional terminal bell for attention requests, so that I can notice them without watching continuously.
27. As a Dev Loop user, I want the Sessions tab to show richer status than the compact tab bar, so that I can monitor all work from one dashboard.
28. As a Dev Loop user, I want switching tabs to preserve every session's live state, so that background work continues uninterrupted.
29. As a Dev Loop user, I want closing a session tab to hide only its view, so that an ordinary UI action cannot stop work.
30. As a Dev Loop user, I want hidden sessions to remain visible in the Sessions tab, so that I can reopen them later.
31. As a Dev Loop user, I want pause, cancellation, force stop, and tab closing to be distinct actions, so that their consequences are clear.
32. As a Dev Loop user, I want Pause to stop new scheduling and reach a durable checkpoint, so that normal interruption is safe.
33. As a Dev Loop user, I want Force Stop to be explicit, so that interrupting an active operation is never accidental.
34. As a Dev Loop user, I want partial filesystem changes and diagnostics preserved after Force Stop, so that recovery can continue from real evidence.
35. As a Dev Loop user, I want a crashed worker to affect only its own session, so that sibling workflows continue.
36. As a Dev Loop user, I want a crashed session marked `INTERRUPTED`, so that the failure is distinguishable from a deliberate pause.
37. As a Dev Loop user, I want interrupted work restarted only through explicit Resume or Retry, so that potentially non-idempotent operations are not repeated automatically.
38. As a Dev Loop user, I want application exit to prompt once for all running sessions, so that shutdown remains understandable.
39. As a Dev Loop user, I want application exit to pause sessions and stop workers, so that no hidden daemon remains after the UI closes.
40. As a Dev Loop user, I want paused sessions restored as unfinished on the next start, so that I can continue later.
41. As a Dev Loop user, I want a completed session moved to History, so that active work remains uncluttered without losing records.
42. As a Dev Loop user, I want completed history retained until I forget it, so that Dev Loop does not impose an unexpected retention deadline.
43. As a Dev Loop user, I want Forget to remove only machine-local session metadata, so that project artifacts remain safe.
44. As a Dev Loop user, I want Forget never to delete PRDs, issue packs, logs, branches, or worktrees, so that catalog cleanup is non-destructive.
45. As a Dev Loop user, I want a missing or moved worktree marked `UNAVAILABLE`, so that stale paths are visible rather than silently removed.
46. As a Dev Loop user, I want to relink an unavailable session to its moved checkout, so that I can recover after reorganizing folders.
47. As a Dev Loop user, I want to forget an unavailable entry explicitly, so that obsolete machine-local state can be cleaned safely.
48. As a Dev Loop user, I want a session to become durable immediately after selecting its worktree, so that planning work is resumable before a PRD exists.
49. As a Dev Loop user, I want the planning Codex thread identity retained, so that an interrupted planning conversation can continue.
50. As a Dev Loop user, I want planning settings snapshotted per session, so that resume does not silently change the workflow context.
51. As a Dev Loop user, I want existing PRD-local state to remain authoritative after publication, so that v3 does not create conflicting workflow histories.
52. As a Dev Loop user, I want the machine catalog to store pointers and summaries instead of copied project state, so that there is one source of truth.
53. As a Dev Loop user, I want exact role/pass and workflow-step checkpoints restored from existing loop state, so that completed work is not repeated.
54. As a Dev Loop user, I want project logs to remain in their existing locations, so that operational diagnosis continues to work.
55. As a Dev Loop user, I want session catalog updates to be atomic, so that a crash cannot leave half-created sessions.
56. As a Dev Loop user, I want multiple Dev Loop applications to share the same catalog safely, so that separate terminal instances remain supported.
57. As a Dev Loop user, I want multiple Dev Loop applications to respect the same worktree leases, so that cross-window duplication is prevented.
58. As a Dev Loop user, I want machine-wide concurrency applied across application instances, so that opening another window cannot bypass my limit.
59. As a Dev Loop user, I want two actively executing sessions by default, so that parallelism starts with a conservative machine load.
60. As a Dev Loop user, I want to change the concurrency limit in Options, so that it fits my computer and workload.
61. As a Dev Loop user, I want paused sessions not to consume execution capacity, so that other work can proceed.
62. As a Dev Loop user, I want waiting-for-input sessions not to consume execution capacity, so that unattended requests do not block unrelated work.
63. As a Dev Loop user, I want excess sessions displayed as `QUEUED`, so that capacity limits are transparent.
64. As a Dev Loop user, I want queued sessions to start when capacity is available, so that I do not have to reissue the request.
65. As a Dev Loop user, I want open tabs not to count as active execution, so that monitoring does not reduce capacity.
66. As a Dev Loop user, I want lease ownership renewed while a worker is healthy, so that active sessions remain protected.
67. As a Dev Loop user, I want a stale lease reclaimed only after its owner is confirmed dead, so that slow or disconnected workers are not duplicated.
68. As a Dev Loop user, I want ambiguous lease ownership to block execution and remain inspectable, so that safety wins over guessing.
69. As a Dev Loop user, I want the supervisor and workers to communicate consistently on Windows and Linux, so that multi-session behavior is portable.
70. As a Dev Loop user, I want malformed or incompatible worker messages rejected clearly, so that protocol drift cannot corrupt session state.
71. As a Dev Loop user, I want worker standard error retained as session diagnostics, so that process failures are explainable.
72. As a Dev Loop user, I want raw worker output prevented from corrupting the full-screen UI, so that every session remains readable.
73. As a security-conscious user, I want secrets and unredacted provider payloads excluded from the session catalog, so that machine-wide discovery data stays bounded.
74. As a security-conscious user, I want terminal content sanitized before display, so that one worker cannot inject terminal control sequences into another view.
75. As a Dev Loop user, I want existing command-line flags to remain valid, so that scripts and operator habits survive v3.
76. As a Dev Loop user, I want Plain Mode to remain deterministic and append-only, so that redirected automation is not forced into a tabbed interface.
77. As a Dev Loop user, I want Plain Mode to execute one foreground session, so that non-interactive behavior stays simple.
78. As a Dev Loop user, I want Plain Mode to honor the same worktree lease, so that automation cannot collide with an interactive tab.
79. As a Dev Loop user, I want Plain Mode to honor the same machine-wide concurrency setting, so that all entry points follow one policy.
80. As an existing `0.2.1` user, I want v3 installation to preserve all project data, so that upgrading cannot damage active work.
81. As an existing `0.2.1` user, I want my last confirmed target adopted automatically, so that v3 starts with familiar context.
82. As an existing `0.2.1` user, I want related worktrees containing unfinished portable workflows discovered through Git, so that active work is transferred without drive scanning.
83. As an existing `0.2.1` user, I want unrelated worktrees left for explicit adoption, so that the catalog does not fill with irrelevant checkouts.
84. As an existing `0.2.1` user, I want additional projects adopted when I select them, so that older working folders can be brought into v3 incrementally.
85. As an existing `0.2.1` user, I want adoption to be idempotent, so that retrying after interruption cannot duplicate sessions.
86. As an existing `0.2.1` user, I want adoption to be transactional, so that a failed upgrade leaves the prior configuration usable.
87. As an existing `0.2.1` user, I want v3 to leave PRDs, issues, logs, branches, worktrees, and loop state byte-for-byte unchanged during adoption, so that recovery evidence remains trustworthy.
88. As an existing `0.2.1` user, I want a migration receipt only after a successful catalog commit, so that upgrade status is reliable.
89. As an existing `0.2.1` user, I want an unavailable imported path shown for Relink or Forget, so that upgrade does not fail because one drive is disconnected.
90. As an existing `0.2.1` user, I want the limitation on already-lost pre-PRD planning chats stated clearly, so that v3 does not claim impossible recovery.
91. As an existing `0.2.1` user, I want adopted projects readable by the older runner, so that rolling back the application does not require rolling back project files.
92. As a maintainer, I want Portable Dev Loop v3 identified as release `0.3.1`, so that generation and software version are unambiguous.
93. As a maintainer, I want Portable Session Status represented by a closed enum, so that lifecycle values are not scattered strings.
94. As a maintainer, I want protocol message kinds and versions represented by closed contracts, so that unsupported messages fail at the boundary.
95. As a maintainer, I want catalog schema evolution versioned and migration-tested, so that later releases can upgrade user-wide state safely.
96. As a maintainer, I want session, project, worker, lease, and protocol identities stable and validated, so that events cannot cross session boundaries.
97. As a maintainer, I want the portable workflow and state-machine modules independent of Textual types, so that Plain Mode and tests use the same behavior.
98. As a maintainer, I want one supervisor seam for all session lifecycle operations, so that UI views do not start or kill workers directly.
99. As a maintainer, I want one catalog seam for projects, sessions, settings, adoption, and leases, so that cross-process consistency has a single owner.
100. As a maintainer, I want multi-session release gates on both Windows and Linux, so that v3 is not declared complete from one platform.

## Implementation Decisions

- The product-generation name is **Portable Dev Loop v3** and its target release
  version is **0.3.1**. The runtime version must remain `0.2.1` until the feature
  is implemented and its release gates pass.
- Keep Portable Dev Loop and CodexCLI as separate products. Do not import
  CodexCLI Workflow Run, Run Store, App Server, or component-lock concepts into
  this feature.
- Preserve one mounted Portable Application Shell across startup, planning,
  development, review, QA, wiki updates, errors, and completion.
- Add a permanent, non-closable Sessions tab and independent Portable Session
  Tabs. All transitions replace the contained view inside the existing bounded
  shell.
- Make the application shell a supervisor and run each Portable Workflow
  Session in a separate child process. A session owns its current directory,
  runtime bridge, Codex execution, captured output, and failure boundary.
- Introduce one supervisor interface as the sole owner of session creation,
  resume, queuing, input routing, pause, force stop, cancellation, worker
  shutdown, and status projection. Presentation views emit typed intents and do
  not manage operating-system processes directly.
- Use a versioned JSON Lines supervisor/worker protocol over redirected standard
  input and output. Reserve standard output for protocol frames and standard
  error for captured session diagnostics.
- Give every protocol frame a protocol version, session identity, monotonically
  increasing sequence, message kind, and validated payload. Reject unknown
  versions, identities, message kinds, and invalid payloads before state changes.
- Define supervisor-to-worker message kinds for start, resume, user input,
  approval decision, pause, force stop, cancel, shutdown, and snapshot request.
- Define worker-to-supervisor message kinds for hello, context, status,
  activity, safe output, input request, approval request, checkpoint, heartbeat,
  completion, and failure.
- Use enums for Portable Session Status and protocol message kinds. Portable
  Session Status is `READY`, `QUEUED`, `RUNNING`, `WAITING_FOR_INPUT`,
  `PAUSING`, `PAUSED`, `INTERRUPTED`, `COMPLETED`, `FAILED`, `CANCELLED`, or
  `UNAVAILABLE`.
- Use Python's standard `sqlite3` module for a machine-local Portable Session
  Catalog. Add no third-party database or process-management dependency.
- Place the catalog in the platform-native user state location, separate from
  permanent user preferences and project files.
- Give the catalog one owning interface for saved projects, sessions, settings,
  worktree leases, migration receipts, and discovery summaries.
- Model saved projects, sessions, concurrency settings, worktree leases, and
  migration receipts as versioned catalog records with stable validated
  identities.
- Persist a session immediately after worktree selection. Before PRD
  publication, retain the planning Codex thread identity, settings snapshot,
  lifecycle status, and activity summary in the catalog.
- After PRD publication, keep the existing worktree-local status files, issue
  files, and logs authoritative. Store only their pointers and bounded discovery
  summaries in the catalog.
- Do not copy project workflow state into SQLite and do not change existing
  loop-state schemas merely to support the catalog.
- Identify a Portable Saved Project by its canonical Git checkout path.
  Different worktrees of one repository are separate saved projects.
- Automatically register a checkout after Dev Loop successfully opens it as a
  target or creates or reuses it as an implementation worktree.
- Do not recursively scan drives or unrelated parent directories for projects.
- Add a machine-wide Portable Worktree Lease keyed by canonical worktree path.
  Only one live session may own a path, across all Dev Loop application
  instances and Plain Mode invocations.
- Store a lease owner identity, process identity, process-start fingerprint, and
  renewable heartbeat. Reclaim an expired lease only after confirming that the
  owner is dead; ambiguous ownership blocks execution and remains inspectable.
- Default the user-wide Portable Session Concurrency Limit to two actively
  executing sessions and make it editable in Options.
- Apply concurrency atomically across application instances through the
  catalog. `PAUSED` and `WAITING_FOR_INPUT` sessions release capacity, open tabs
  do not consume it, and excess starts remain visibly `QUEUED`.
- Keep startup passive. Open only the Sessions tab, discover catalog and
  PRD-backed candidates, and start no worker until explicit Resume.
- Let the `+` action choose an available saved worktree, register an existing
  checkout, or create a new Git worktree from a saved repository and branch.
- Focus an existing session when the selected worktree is already leased.
- Show project/worktree identity, lifecycle status, stage, PRD, issue progress,
  active issue, last activity, and last update in the Sessions tab.
- Show compact worktree, lifecycle, and unread state in each tab label. Use
  `[INPUT!]` for attention and support an optional terminal bell.
- Never change focus automatically when a background session asks for input or
  approval. Route user input only to the explicitly active tab.
- Make tab close a view-only operation. Pause, Force Stop, Cancel, Forget, and
  application exit remain separate explicit actions.
- Implement Pause cooperatively: stop new scheduling, allow the current
  operation to reach a durable checkpoint, record `PAUSED`, then stop its worker.
- Implement Force Stop separately: interrupt the active operation, retain
  partial filesystem work and diagnostics, and resume later from the last
  durable checkpoint through a fresh attempt when required.
- On worker crash, record `INTERRUPTED`, retain standard-error diagnostics,
  release the lease only after worker death is confirmed, and require explicit
  Resume or Retry. Do not auto-replay potentially non-idempotent work.
- On application exit, ask once, pause every running session, persist durable
  state, and stop all workers. Do not introduce a detached background daemon.
- Move completed sessions to catalog History. Forget removes machine-local
  catalog/session metadata only and never deletes project artifacts, branches,
  or worktrees.
- Mark missing or moved worktrees `UNAVAILABLE` and provide Relink and Forget.
  Never silently discard their sessions.
- Preserve existing `--repo`, `--prd`, worktree, runner, and Plain Mode command
  contracts. Plain Mode continues as one foreground session while acquiring the
  same catalog concurrency capacity and worktree lease.
- Keep non-TTY output deterministic and append-only with no tab or cursor
  control sequences.
- Sanitize all protocol-derived terminal content before rendering. Do not store
  raw credentials, tokens, complete command streams, or unredacted provider
  payloads in the catalog.
- Add a first-run, transactional, idempotent v3 adoption flow from `0.2.1`.
- Read but do not overwrite the existing user-wide planner configuration.
  Register its last confirmed target and inspect only Git worktrees related to
  that target.
- Automatically adopt related worktrees containing unfinished portable PRD
  workflows. Leave related worktrees without portable artifacts available for
  explicit adoption.
- Adopt additional existing projects when the user selects or adds their
  checkout.
- Create catalog pointers through the existing Portable Resume Candidate
  discovery rules without moving or rewriting PRDs, issue packs, status files,
  logs, branches, or worktrees.
- Commit a migration receipt only after the complete catalog adoption
  transaction succeeds. A failed or repeated adoption must be safe.
- Preserve rollback readability by leaving project-local formats unchanged.
- State the migration limitation explicitly: an already-ended `0.2.1` process
  did not persist a pre-PRD planning conversation, so v3 cannot reconstruct it.

## Testing Decisions

- Test observable session behavior rather than Textual widget internals or
  private helper call order.
- Use the supervisor with fake worker child processes as the highest execution
  seam. Drive start, queue, input, checkpoint, pause, force stop, completion,
  crash, and shutdown through the versioned protocol and assert projected
  session behavior.
- Test the Portable Session Catalog through its public interface with temporary
  SQLite databases. Verify atomic writes, transaction rollback, schema
  migration, idempotent adoption, bounded summaries, and authoritative-state
  pointers.
- Use two real test processes against one temporary catalog for leases,
  heartbeats, stale-owner recovery, worktree exclusion, and machine-wide
  concurrency. Do not substitute same-process mocks for cross-process safety.
- Test the full Portable Application Shell with a fake supervisor and fake
  session events. Verify the permanent Sessions tab, creation flow, tab
  switching, compact statuses, unread markers, focus isolation, hidden tabs,
  input requests, overlays, and responsive layouts.
- Verify that background input and approval requests never receive text typed
  into another tab.
- Verify that closing a tab neither pauses nor terminates its session.
- Verify that Pause stops new scheduling, emits a durable checkpoint, records
  `PAUSED`, and releases concurrency capacity.
- Verify Force Stop preserves partial work evidence and resumes from the last
  durable checkpoint without claiming the interrupted operation completed.
- Verify a worker crash affects one session only, retains diagnostics, records
  `INTERRUPTED`, and never triggers automatic replay.
- Verify application exit pauses all sessions through one confirmation and
  leaves no worker running.
- Verify completed History and Forget never remove project files.
- Verify missing worktrees become `UNAVAILABLE`, Relink validates the new
  canonical checkout, and Forget remains metadata-only.
- Verify protocol framing across partial reads, multiple frames, malformed JSON,
  oversized messages, duplicate/out-of-order sequence values, wrong session
  identities, unknown message kinds, and unsupported versions.
- Verify terminal sanitation and catalog redaction for hostile text, secrets,
  credentials, token-like values, and raw provider payloads.
- Verify the concurrency default is two, Options changes it transactionally,
  paused/input-waiting sessions release capacity, and queued sessions advance
  fairly when capacity becomes available.
- Verify two sessions in different worktrees of one repository may run
  concurrently while two sessions targeting one canonical worktree cannot.
- Verify separate Dev Loop application instances observe catalog updates,
  concurrency, and leases consistently.
- Verify pre-PRD planning state resumes the exact saved Codex thread and settings
  snapshot.
- Verify published workflows resume from existing project-local role/pass and
  generic workflow checkpoints without copying or resetting history.
- Verify v3 adoption from a representative `0.2.1` configuration imports the
  last target, discovers relevant Git worktrees, and creates one session per
  unfinished PRD workflow.
- Verify adoption is idempotent and records its receipt only after commit.
- Hash representative PRDs, issues, status files, and logs before and after
  adoption and require byte-for-byte equality.
- Verify unavailable imported paths do not abort the migration and remain
  actionable through Relink or Forget.
- Verify an older runner can still read adopted project-local state.
- Preserve existing portable resume, worktree mapping, runtime context,
  application shell, terminal sanitation, and Plain Mode tests as regression
  gates.
- Run the sandbox-safe test suite, Ruff, and strict mypy over affected packages.
- Validate a small local issue pack through `--dry-run --no-worktree`, then
  inspect its status files and logs.
- Validate both Windows and Linux wrappers. Real authenticated Codex integration
  and installer/update gates must be run by an operator outside an agent session
  and must write non-secret evidence into the workspace for inspection.

## Out of Scope

- Merging Portable Dev Loop with CodexCLI.
- Adopting or converting CodexCLI Workflow Runs.
- Running two sessions against the same canonical worktree.
- A detached background daemon that continues work after the application exits.
- Automatic restart or replay of a crashed worker.
- Recursive drive or parent-folder project discovery.
- Moving, renaming, rewriting, or deleting existing project PRDs, issue packs,
  state files, logs, branches, or worktrees during v3 adoption.
- Recovering a pre-PRD planning conversation already lost when an old `0.2.1`
  process ended.
- Automatically deleting completed session history.
- Making tab close equivalent to Pause, Cancel, Force Stop, or Forget.
- Replacing project-local loop state with SQLite.
- Introducing a third-party process manager, IPC framework, or database.
- Changing workflow semantics, role contracts, issue scheduling, review/QA
  behavior, or self-improvement wiki behavior except where session isolation
  requires presentation and lifecycle routing.
- Changing the current runtime version to `0.3.1` before implementation and
  release validation are complete.

## Further Notes

- The canonical vocabulary is recorded in the project glossary: Portable Dev
  Loop v3, Portable Saved Project, Portable Workflow Session, Portable Session
  Status, Portable Worktree Lease, Portable Session Catalog, Portable Session
  Concurrency Limit, Portable Session Tab, Portable Sessions Tab, and Portable
  Project Adoption.
- The accepted architecture decisions require one supervising application with
  isolated child workers, versioned JSON Lines communication, standard-library
  SQLite catalog/leases, passive startup, non-destructive tab behavior, and
  project-local workflow-state authority.
- The existing implementation reports version `0.2.1`. Version `0.3.1` is the
  release target established by this PRD, not an assertion that the feature is
  already implemented.
- The companion issue pack must be created under
  `prd/portable-devloop-v3-multi-session/issues/`.
