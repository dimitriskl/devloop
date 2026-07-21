# Harness Engineering Implementation Checklist

This checklist maps the masterclass's harness primitives to Dev Loop. Preserve
this order when auditing the implementation. Dev Loop has two products, so
evidence must be identified as Portable Dev Loop, CodexCLI, or both.

## Status legend

- **Implemented**: clear repository support exists.
- **Partial**: useful support exists, but not the complete primitive.
- **Delegated**: the Codex execution backend owns the behavior.
- **Not found**: this audit found no clear implementation evidence.
- **Needs runtime check**: static evidence exists, but a real run must prove it.

## 1. Instructions

**Idea:** Persist roles, constraints, coding conventions, review rules, and
prohibited actions so users do not repeat them on every run.

- [x] **Implemented — both.** `AGENTS.md`, `prompts/`, `agents/codex/`, and
  `skills/codex/` provide repository, role, agent, and procedural instructions.
- [x] **Implemented — CodexCLI.** Workflow configuration supports per-step
  guidance and capability selection.
- [ ] **Runtime check:** Confirm the Context Manifest records the exact
  instruction and capability versions delivered to an attempt.

## 2. Context delivery

**Idea:** Supply relevant files, failures, requirements, logs, and documentation
instead of making the model guess.

- [x] **Implemented — Portable.** Runs assemble the PRD, Issue, role prompt,
  prior role results, and self-improvement guidance.
- [x] **Implemented — CodexCLI.** Step inputs, Artifacts, repository scope,
  instructions, and capabilities are modeled as execution context.
- [ ] **Runtime check:** Compare generated prompts/manifests with declared inputs
  for development, review, and QA attempts.

## 3. Context management

**Idea:** Protect attention by selecting, ranking, summarizing, and compacting
context; irrelevant context can be worse than missing context.

- [~] **Partial — Portable.** Role-specific prompts, compact blocker context,
  clean blocked retries, and bounded sessions reduce irrelevant carry-over.
- [~] **Partial — CodexCLI.** Typed inputs, Context Manifests, Recovery Attempts,
  and scoped Execution Threads constrain context.
- [ ] **Not found — both.** No Dev Loop-owned RAG, retrieval ranking,
  token-budget-aware selection, prompt caching, or compaction policy was found.
  Backend compaction is delegated, not a Dev Loop implementation.
- [ ] **Follow-up:** Define context budgets and selection rules and record
  included and omitted context in the Context Manifest.

## 4. Tool interface

**Idea:** Expose structured actions with names, descriptions, input schemas, and
preferably output schemas. Shell, search, function calls, and MCP are examples.

- [x] **Implemented/delegated — both.** Codex CLI supplies the agent tool
  interface.
- [x] **Implemented — optional.** `mcp/sql_diagnostics/` provides a read-only SQL
  diagnostics MCP server.
- [x] **Implemented.** `schemas/` constrains role results; CodexCLI models typed
  Step contracts and output Artifacts.
- [ ] **Runtime check:** Exercise malformed arguments, partial MCP output, and
  schema-invalid role output and confirm explicit recovery behavior.

## 5. Execution environment

**Idea:** Bound filesystem, network, credentials, repository scope, and human
approval. Worktrees and sandboxes belong to this layer.

- [x] **Implemented — both.** Repository scope and Git worktrees are supported
  by `src/devloop/worktree.py`, `src/devloop/infrastructure/git.py`, and
  `docs/worktrees.md`.
- [~] **Partial/delegated — both.** Dev Loop configures permissions and approvals;
  the invoked Codex runtime enforces filesystem/network sandboxing.
- [ ] **Not found.** Dev Loop-owned containers, credential brokering, untrusted
  output filtering, and browser-profile isolation.
- [ ] **Runtime check:** In a disposable checkout, attempt out-of-scope writes
  and disallowed network access and verify denial or approval prompts.

## 6. Durable state

**Idea:** Keep plans, checkpoints, state, logs, diffs, and evidence outside the
prompt so work survives crashes and resumes without starting over.

- [x] **Implemented — Portable.** PRD-local `*.loop.state.json`, loop logs, Issue
  status, role/pass state, Git changes, and resume behavior preserve continuity.
- [x] **Implemented — CodexCLI.** `.devloop/runs/<run-id>/` stores snapshots,
  events, manifests, Step attempts, and Artifacts through
  `src/devloop/persistence/`.
- [ ] **Runtime check:** Interrupt active attempts in each product and confirm
  resume restores the exact unfinished unit without replaying completed work.

## 7. Orchestration

**Idea:** Control ordering, transitions, retries, approval gates, handoffs,
lifecycle events, failure recovery, and routing.

- [x] **Implemented — Portable.** The runner sequences development, review, and
  QA, skips completed Issues, retries blocked Issues, and persists role/pass.
- [x] **Implemented — CodexCLI.** Workflow Definitions, typed outcomes,
  transitions, scheduling, retry/recovery, approvals, and finalization form an
  explicit orchestration layer.
- [ ] **Runtime check:** Cover success, changes-requested, blocked, interrupted,
  rejected-approval, and exhausted-retry transitions.

## 8. Sub-agents and delegation

**Idea:** Split independent work into bounded specialist loops with narrower
context/tools while a manager integrates the results.

- [~] **Partial — Portable.** Coder, reviewer, and QA use separate specialized
  sessions, but run sequentially rather than as manager-created parallel agents.
- [~] **Partial — CodexCLI.** Steps and capability profiles create specialist
  scopes and distinct threads, but do not prove in-attempt sub-agent spawning.
- [ ] **Not found.** A manager dynamically delegating parallel bounded tasks and
  integrating them inside one Step attempt.
- [ ] **Decision needed:** Confirm this is a product goal; deterministic Workflow
  Steps may be safer than dynamic delegation for Dev Loop.

## 9. Skills and reusable procedures

**Idea:** Encode recurring expertise as named procedures with triggers, inputs,
ordered steps, and preferred tools.

- [x] **Implemented — both.** Bundled skills live under `skills/codex/`, have an
  installer, and are documented in `docs/skills-and-agents.md`.
- [x] **Implemented — CodexCLI.** Capability profiles distinguish required and
  optional skills/agents.
- [ ] **Runtime check:** On a clean machine, confirm a required skill is
  installed, selected, manifested, and available to the Execution Thread.

## 10. Verification

**Idea:** Require receipts—tests, builds, lint, checks, screenshots, inspection,
evals, or source evidence—instead of trusting a confident final message.

- [x] **Implemented — Portable.** Review and QA are explicit roles; structured
  results and Issue verification requirements control progress and rework.
- [x] **Implemented — CodexCLI.** QA Results and Checks model requirements,
  status, acceptance-criterion mappings, and evidence; see
  `src/devloop/verification/`.
- [ ] **Runtime check:** Submit a confident result with a failing required check
  and prove the harness refuses completion.
- [ ] **Follow-up:** Map every acceptance criterion to a required QA Check or an
  explicit evidence-backed reason it cannot be automated.

## 11. Observability

**Idea:** Record model context, tool calls and arguments, results, changes,
approvals, versions, cost, latency, and the chain from intent to output.

- [x] **Implemented — baseline.** Portable logs and CodexCLI snapshots, events,
  Step Attempt Records, Context Manifests, Artifacts, timing, and telemetry form
  an inspectable history.
- [~] **Partial — both.** Complete queryable tool-call timelines, tool versions,
  prompt hashes, token/cost accounting, and per-action latency were not
  established by this audit.
- [ ] **Runtime check:** From a failed QA Check, trace to its attempt, context,
  approvals, changes, tool/command result, and prompt/capability versions.
- [ ] **Follow-up:** Define mandatory observability fields, retention, redaction,
  and correlation identifiers.

## 12. Self-improving harness

**Idea:** Convert repeated failures into retrieval rules, schemas, permission
gates, tests, memory, and skills so the next run starts from a better place.

- [x] **Implemented — both.** Real runs read and update the safe durable wiki in
  `docs/devloop-self-improvement/wiki/`; implementation is in
  `src/devloop/self_improvement_wiki.py`.
- [~] **Partial.** The wiki compounds lessons, but automatic promotion into
  tests, schemas, permission rules, context rules, or skills was not found.
- [ ] **Runtime check:** Repeat a safe failure across two runs and prove the next
  run receives a concise relevant lesson without logs, secrets, or unrelated
  advice.

## Summary

| Primitive | Portable | CodexCLI | Main gap/check |
| --- | --- | --- | --- |
| Instructions | Implemented | Implemented | Manifest fidelity |
| Context delivery | Implemented | Implemented | Input completeness |
| Context management | Partial | Partial | Selection and compaction |
| Tool interface | Implemented/delegated | Implemented/delegated | Failure contracts |
| Execution environment | Partial/delegated | Partial/delegated | Enforced isolation |
| Durable state | Implemented | Implemented | Crash/resume gate |
| Orchestration | Implemented | Implemented | Transition coverage |
| Sub-agents | Partial | Partial | Dynamic delegation decision |
| Skills | Implemented | Implemented | Clean-machine availability |
| Verification | Implemented | Implemented | False-success rejection |
| Observability | Partial | Partial | End-to-end traceability |
| Self-improvement | Implemented | Implemented | Cross-run relevance/safety |

Run the unchecked gates before adding features. Static structure cannot prove
that context, isolation, resume, verification, or trace correlation works in a
real Codex run.
