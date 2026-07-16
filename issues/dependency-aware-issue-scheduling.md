Label: ready-for-issues

# Dependency-Aware Issue Scheduling

## Target Product

Product: devloop-plan + devloop

This PRD targets the portable `devloop-plan` planning intake and `devloop`
Markdown issue runner. The separate CodexCLI Textual application and its
scheduler are out of scope.

## Problem Statement

The portable runner says that `--all` executes issues in dependency order, but
it does not read the `## Blocked by` declarations in issue files. It selects
every unfinished issue and runs them in index order. When an upstream issue
fails or exhausts its normal passes, Dev Loop can therefore spend Codex calls on
downstream issues that cannot be completed safely. It also conflates an issue
that failed its own execution with an issue that was never eligible because a
dependency remains incomplete.

The existing blocked-retry sweep retries every blocked issue in batches without
recomputing dependency readiness after each result. A global Codex condition,
such as exhausted usage, invalid authentication, or service unavailability,
can be recorded repeatedly as issue-level failures and consume retry budgets
even though changing issues cannot resolve the condition. The user cannot tell
which work is genuinely ready, which issue is the current upstream stopper, or
which waiting issues will be unlocked by resolving it.

## Solution

Make the portable issue runner dependency-aware and token-conscious. Parse the
explicit local issue links under each `## Blocked by` section into a validated
directed acyclic graph before any Codex call. Missing references,
self-dependencies, and cycles fail preflight with actionable diagnostics. Index
order remains a deterministic priority only among issues that are currently
dependency-ready; it never creates implicit dependencies.

An unfinished issue becomes dependency-ready only after every declared
dependency reaches full `COMPLETED` status through its configured Development,
Review, and QA workflow. An issue that cannot yet run receives
`WAITING_ON_DEPENDENCY`, consumes no Codex call, and consumes no attempt or retry
budget. `BLOCKED` and `FAILED` remain outcomes of an issue that was actually
attempted.

Use two explicit scheduling phases. Normal Scheduling repeatedly chooses the
first dependency-ready issue in issue-index order that still has its normal
execution budget. If it cannot complete, the scheduler continues with other
ready work and leaves its descendants waiting. Blocker Resolution begins only
when no dependency-ready issue has unused normal budget. It allocates at most
five additional workflow passes per unresolved ready issue, round-robin and one
pass at a time in issue-index order. Readiness is recomputed after every pass;
newly unlocked normal work immediately takes priority. After the additional
budget is exhausted, the run stops nonzero with the unresolved dependency cut
instead of retrying indefinitely.

Classify run-wide backend conditions separately from issue results. Usage
limits, invalid authentication, and service unavailability pause the whole run
immediately without changing the issue outcome or consuming any issue budget.
Rerunning the same command resumes the same issue, Workflow Step, pass, phase,
and remaining budgets from durable loop state.

## User Stories

1. As a Dev Loop user, I want explicit issue dependencies enforced, so that downstream work never starts from an unfinished prerequisite.
2. As a Dev Loop user, I want only links under `## Blocked by` to define dependencies, so that reordering the issue index cannot silently change correctness.
3. As a Dev Loop user, I want issue-index order preserved among ready issues, so that execution remains deterministic and follows the authored priority.
4. As a Dev Loop user, I want a dependency satisfied only by full issue completion, so that unreviewed or untested code cannot unlock downstream work.
5. As a Dev Loop user, I want dependency-waiting issues to receive no Codex calls, so that blocked chains do not burn tokens.
6. As a Dev Loop user, I want dependency-waiting issues to consume no attempt budget, so that they retain their full budget when finally unlocked.
7. As a Dev Loop user, I want dependency waiting distinguished from execution failure, so that the task board explains why an issue did not run.
8. As a Dev Loop user, I want the dashboard to name incomplete dependencies, so that I can see what will unlock each waiting issue.
9. As a Dev Loop user, I want independent ready work to continue after another issue blocks, so that one failed branch does not stop useful progress.
10. As a Dev Loop user, I want dependent issues reconsidered immediately after a prerequisite completes, so that the scheduler does not wait for another full sweep.
11. As a Dev Loop user, I want normal scheduling to finish available work before escalation, so that retries do not displace fresh work.
12. As a Dev Loop user, I want blocker resolution to begin only when no ready issue has normal budget, so that extra attempts are spent deliberately.
13. As a Dev Loop user, I want five additional passes per unresolved ready issue, so that difficult blockers receive a bounded second chance.
14. As a Dev Loop user, I want additional passes allocated one at a time, so that one difficult blocker cannot consume the entire escalation phase first.
15. As a Dev Loop user, I want blocker-resolution attempts ordered by the issue index, so that escalation remains deterministic.
16. As a Dev Loop user, I want readiness recomputed after every additional pass, so that a successful blocker immediately unlocks normal work.
17. As a Dev Loop user, I want newly unlocked normal work to interrupt blocker resolution, so that dependency chains advance as soon as possible.
18. As a Dev Loop user, I want a hard bound on additional passes, so that an impossible issue cannot create an infinite token loop.
19. As a Dev Loop user, I want an unresolved dependency report when budgets expire, so that I know the root blockers and every affected descendant.
20. As a Dev Loop user, I want malformed dependency references rejected before execution, so that typos do not create misleading waits.
21. As a Dev Loop user, I want self-dependencies rejected before execution, so that an issue cannot wait on itself forever.
22. As a Dev Loop user, I want dependency cycles rejected before execution, so that a cyclic pack cannot consume Codex calls.
23. As a Dev Loop user, I want validation diagnostics to name issue numbers, files, and invalid links, so that graph repairs are actionable.
24. As a Dev Loop user, I want completed dependencies recognized from durable issue state, so that rerunning the same command does not repeat completed work.
25. As a Dev Loop user, I want the active scheduling phase and remaining budgets persisted, so that interruption does not reset escalation accounting.
26. As a Dev Loop user, I want resume to continue the exact issue, Workflow Step, and pass, so that recovery does not duplicate successful work.
27. As a Dev Loop user, I want a usage-limit response to pause the whole run, so that every remaining issue is not attempted pointlessly.
28. As a Dev Loop user, I want authentication failure to pause the whole run, so that credentials can be repaired once before resuming.
29. As a Dev Loop user, I want service unavailability to pause the whole run, so that transient infrastructure failure is not recorded against issues.
30. As a Dev Loop user, I want run-wide blockers to consume no issue attempt budget, so that external failures do not reduce implementation opportunities.
31. As a Dev Loop user, I want the paused run to preserve its current cursor and budgets, so that the same command can resume safely.
32. As a Dev Loop user, I want non-interactive output to report ready, waiting, blocked, and paused states without cursor control, so that CI logs remain understandable.
33. As a Dev Loop user, I want interactive progress to show the ready issue count, waiting issue count, root blockers, and newly unlocked work, so that scheduling decisions are visible.
34. As a Dev Loop user, I want the final summary to separate completed, execution-blocked, dependency-waiting, and run-paused issues, so that no category is hidden.
35. As a Dev Loop maintainer, I want scheduling decisions produced by a pure deterministic module, so that dependency and retry behavior can be tested without Codex.
36. As a Dev Loop maintainer, I want closed readiness and scheduling states represented by enums, so that state is not scattered as magic strings.
37. As a Dev Loop maintainer, I want old sequential state interpreted conservatively, so that an existing unfinished run can enter dependency-aware scheduling without losing completed evidence.
38. As a Dev Loop maintainer, I want Bash and PowerShell wrappers to use the same scheduler, so that shell choice cannot change dependency behavior.
39. As a Dev Loop maintainer, I want issue selection to fail clearly when its selected subset omits an unfinished prerequisite, so that an explicitly requested range cannot bypass correctness.
40. As a Dev Loop maintainer, I want every scheduling transition persisted atomically, so that a process interruption cannot spend an attempt without recording it.

## Implementation Decisions

- Treat the portable Markdown issue pack as the scheduling source of truth. Parse each issue's `Blocked by` section separately from the issue index.
- Model direct dependencies explicitly and validate the whole selected graph before creating a Codex attempt.
- Reject unknown references, references outside the issue pack, self-dependencies, duplicate dependency declarations, and cycles with actionable preflight errors.
- Do not infer dependencies from index order, filenames, issue numbers, PRD order, or prose outside `Blocked by`.
- Use a deep deterministic scheduler interface that accepts the issue graph, durable issue states, phase, and budgets and returns the next scheduling decision without performing execution or presentation side effects.
- Represent dependency readiness, scheduling phase, and scheduling decision kinds as closed enums. Keep issue identifiers as validated open values.
- Add `WAITING_ON_DEPENDENCY` as a durable issue status distinct from `BLOCKED`, `FAILED`, and `WAITING_FOR_INPUT`.
- Consider a dependency satisfied only when its issue is durably `COMPLETED` after its entire configured workflow.
- During Normal Scheduling, choose the first dependency-ready issue in issue-index order that has unused normal budget.
- When a ready issue exhausts normal budget, retain its own execution outcome, continue other ready work, and leave descendants in `WAITING_ON_DEPENDENCY`.
- Enter Blocker Resolution only when no dependency-ready issue has unused normal budget.
- Give each unresolved dependency-ready issue a total Blocker Resolution Budget of five additional workflow passes.
- Allocate Blocker Resolution one pass per issue per round in issue-index order. Recompute the graph after every pass and immediately return to Normal Scheduling when work becomes newly ready.
- Stop nonzero after all eligible additional budgets are exhausted. Report the unresolved dependency cut: root execution blockers and their directly or transitively waiting descendants.
- Do not automatically retry explicit user cancellation or a request that requires human input. Preserve those as deliberate pause/input states.
- Classify account usage exhaustion, invalid authentication, and backend service unavailability as Run-Wide Blockers rather than issue outcomes.
- A Run-Wide Blocker atomically pauses the run before an issue attempt is charged. Resume retains the exact issue, Workflow Step, pass, phase, and remaining budgets.
- Persist normal attempts, additional attempts, completed dependencies, waiting dependencies, scheduling phase, round cursor, and run-wide pause reason in the existing portable loop state.
- For an explicitly selected subset or start issue, fail preflight when an unfinished declared prerequisite is outside the selection rather than silently bypassing or auto-expanding the user's scope.
- Derive interactive and redirected scheduling output from the same presentation-independent scheduling projection.
- Keep execution sequential. Dependency awareness selects the next safe issue but does not introduce parallel Codex attempts.
- Apply all behavior through the shared Python runner used by both Bash and PowerShell wrappers.

## Testing Decisions

- Test externally observable scheduling behavior through issue-pack parsing, a pure scheduler interface, durable loop-state recovery, the public runner, and terminal projections. Avoid assertions against private helper call order.
- Use small temporary Markdown issue packs to cover linear chains, diamonds, independent branches, multiple roots, multiple leaves, and mixed completed/blocked/waiting states.
- Verify that unknown references, self-dependencies, duplicate declarations, and cycles fail before the fake Codex runner receives a call.
- Verify that only explicit `Blocked by` links create edges and that changing index order changes tie priority without changing graph meaning.
- Verify that downstream issues receive zero fake-runner calls and unchanged budgets until all dependencies are fully complete.
- Verify that an execution-blocked branch does not prevent an independent ready branch from completing.
- Verify the exact Normal Scheduling to Blocker Resolution transition, five-pass cap, round-robin allocation, and immediate return to normal work after an unlock.
- Verify interruption and rerun at every scheduling transition, including after an attempt is reserved but before its result is recorded.
- Replay representative Codex error events to verify usage, authentication, and service failures pause globally without producing issue role results or spending issue budgets.
- Verify terminal output distinguishes ready, dependency-waiting, execution-blocked, and run-wide-paused states in TTY, non-TTY, color, and no-color modes.
- Verify Bash and PowerShell argument forwarding reaches the same shared scheduling behavior without duplicating scheduler assertions.
- Run the full standard-library test suite, Python compilation, shell syntax checks, a local dry run, dependency-pack validation, and whitespace checks.
- Do not launch a real authenticated Codex integration from an agent session. If needed, provide one operator-run command that writes a non-secret result log in the workspace.

## Out of Scope

- Changing or reusing the separate CodexCLI Textual scheduler, domain model, RunStore, App Server workflow, or UI.
- Inferring dependencies from issue-index position or automatically generating dependencies from prose.
- Parallel execution of issues or Workflow Steps.
- Infinite retries or automatic expansion beyond the five additional passes per unresolved ready issue.
- Treating user cancellation, approval denial, or required human input as an automatically retryable blocker.
- Silently adding omitted dependencies to an explicitly selected issue subset.
- Changing the internal Workflow Step graph or its per-step model, effort, Fast, capability, or guidance settings.
- Modifying completed issue evidence merely to make the dependency graph appear satisfied.

## Further Notes

- In the current configurable-workflow pack, Issue 0007 is independent of failed Issue 0006 and remains safe to run. Issues 0008 and 0010 depend on 0006 and must wait, while Issue 0009 may run when its own dependencies are complete.
- `WAITING_ON_DEPENDENCY` means no attempt occurred. `BLOCKED` or `FAILED` means the issue itself executed and did not complete.
- The normal attempt budget remains independently configurable; the existing default is three passes. The Blocker Resolution Budget adds five passes after normal ready work is exhausted.
- The scheduler must remain useful without Codex: graph validation, readiness, decision order, budget accounting, and state recovery are deterministic local behavior.
