# Changelog

## Unreleased

### Breaking: portable workflow schema `devloop.portable-workflow/v3`

Every agent-backed Workflow Step now persists an Execution Backend alongside its
model, reasoning effort, and Fast preference. The per-step settings type is
Step Execution Settings, persisted under the step's `execution_settings` key, and
the portable workflow schema is `devloop.portable-workflow/v3`. Schemas v1 and v2
are both rejected explicitly; there is no migration, compatibility reader, or
dual-write path. Finish in-flight work before upgrading.

- **A saved Workflow Default created before this change must be recreated.**
  Open `/options`, choose `reset-workflow`, then `apply`, and reapply your
  per-step choices.
- **An unfinished Workflow Run created before this change cannot be resumed.**
  Its PRD-local `*.loop.state.json` holds a v2 resolved workflow. Finish the run
  with the previous version, or delete that loop-state file to start the PRD
  again from its first Workflow Step. Repository changes are untouched.

Both are reported as actionable messages naming the remedy, not stack traces.

Also in this change:

- Enabling Fast is now rejected with a clear message when the selected model
  advertises no Fast support, instead of being accepted and ignored.
- The `/options` Selection Preview shows each Workflow Step's Execution Backend
  ahead of its model, in both the read-only Current Run and the editable
  Workflow Default scopes.
- Component Execution Defaults supply per-role defaults for each Execution
  Backend. The existing Codex per-role model and reasoning-effort defaults are
  unchanged, and produced Codex agent invocations are unchanged.
- The `/options` `backend` action selects each agent-backed Workflow Step's
  Execution Backend from an arrow-key menu annotated with each backend's
  availability on this machine. Changing it moves that step's model and reasoning
  effort to the new backend's Component Execution Defaults, and `model`,
  `reasoning`, and `fast` then operate on that backend's Model Catalog.
- The Claude Code Backend executes a Workflow Step attempt through the installed
  Claude CLI, and its models come from `catalogs/claude-code-models.json` as
  Bundled Catalog Reference Data: browsing costs nothing, one selection costs one
  verification call, a short alias is saved as the concrete identifier that call
  reports, and a refusal is shown in the provider's own words.
- Run preflight now authorizes a Workflow against every Execution Backend it
  references, and only those. Codex-backed Workflow Steps are authorized exactly
  as before, and each distinct Claude model a run selects is verified once
  against the operator's own account before any attempt budget is spent; a
  refusal names the Workflow Step, the model, the provider's own reason, and
  `/options`. A Claude CLI that cannot be started is reported as exactly that,
  with installing or repairing the CLI or moving the Workflow Step to another
  Execution Backend as its remedies, rather than as something the account
  refused. A Workflow with no Claude-backed Workflow Steps never resolves or
  invokes the Claude CLI, and authorizing a Workflow with no Codex-backed
  Workflow Steps loads no Codex Model Catalog, invokes no Codex CLI, and needs no
  signed-in Codex session — the runner still resolves the configured `--codex`
  command once as its own default backend. The interactive repair loop keeps its
  choices and refreshes every referenced backend's catalog on retry, naming the
  backend that refused.
- A Claude provider-account condition now pauses the Workflow Run instead of
  corrupting Issue outcomes. Unauthorised and forbidden API statuses classify as
  invalid authentication, a rate-limit status or a rate-limit event reporting the
  account's usage as spent classifies as exhausted usage, server-error statuses
  classify as service unavailability, and a not-found status classifies as
  `MODEL_ACCESS_WITHDRAWN` and points at `/options`. All four reuse the existing
  pause path: the run stops scheduling immediately, the active Issue keeps its
  outcome and spends no normal or additional pass, and durable state records a
  redacted reason with the exact Issue, Workflow Step, pass, scheduling phase,
  round, and remaining budgets, so rerunning the same command resumes that work.
- **A Run-Wide Blocker now outranks a Permission Denial** when one terminal
  result carries both. A pause publishes no Step Outcome, so denied work still
  cannot be reported as done, while recording `BLOCKED` would have spent an Issue
  attempt budget on a provider that never ran the work.
- Retryability is now a per-backend predicate on the Execution Backend interface
  and both backends share one bounded retry policy: one Execution Budget across
  every process run of an attempt, one delay, one accumulated transcript. Codex
  retries exactly the connection failures it always did. Claude retries
  transport-level failures — dropped connections, network errors, server errors
  reported before any terminal result — and never retries a classified Run-Wide
  Blocker.
- `RUN PAUSED` is no longer announced with the `BLOCKED` Issue status word. A
  paused run has its own label, coloured as attention on a colour-capable stream
  and identical in words without one.
- Every Step Attempt Record now persists which Execution Backend ran the attempt,
  the model its Step Execution Settings requested, and the model the finished
  turn's own usage accounting reported, plus the cost and turn count a
  Claude-backed attempt reported. Mixed-backend attempt history is therefore
  readable from `*.loop.state.json` without opening a durable log. Only the Claude
  Code Backend reports cost and turn count today, so the two backends' spend is
  not yet directly comparable. Cost is evidence only; the Execution Budget stays time-based.
- **A requested-versus-serving model mismatch is recorded rather than
  reconciled.** A prototype observed a provider's session-initialisation event and
  its own usage accounting naming different models, and the cause is not
  understood, so both identifiers are kept and the disagreement is reported as
  `MODEL MISMATCH` in the Portable Activity Feed as soon as the attempt is
  classified and on the Workflow Status Bar for as long as the record stands. The
  flag is derived from the two identifiers, so no code path can report a mismatch
  as clean, and loading a state file whose recorded flag disagrees with the models
  it names fails with that message instead of reading the attempt back as clean.
- The Workflow Status Bar and the Workflow Progress Dashboard now name the active
  Workflow Step's Execution Backend ahead of its model, through the existing
  optional presentation fields rather than a preformatted string, so the Textual
  shell, the hybrid console dashboard, the PowerShell and Bash surfaces,
  redirected output, and Plain Mode all state the same thing. No border, region,
  or extra line is added, and step rows are unchanged for both backends. On a
  narrow terminal the bounded frame truncates the reasoning effort and the Fast
  preference before the backend, the models, or a mismatch warning.

## v0.1.0 - 2026-07-17

Initial hackathon release of the installable `codexcli` workflow runner.

- Runs the standard analysis, workspace preparation, development, code review,
  QA, and local finalization workflow through the installed Codex App Server.
- Publishes accepted PRD Packages and schedules dependency-ready Issues.
- Uses fresh role threads, typed artifacts, bounded rework, explicit approvals,
  project-local recovery, and a structured final Handoff Summary.
- Provides the Textual launcher, Composer, Slash Commands, Issue Board, status
  bar, capability profiles, pause/cancel controls, and explicit resume.
- Leaves repository publication and workspace cleanup entirely explicit.

Known limitations and release-gate instructions are documented in
`docs/release-checklist-v0.1.0.md`.
