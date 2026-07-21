Label: ready-for-agent

# Reject Invalid Issue Dependency Graphs

## Type

AFK

## Target Product

Product: devloop-plan + devloop

Portable Markdown issue-pack parsing and preflight only. The separate CodexCLI
scheduler and its domain/application modules are out of scope.

## Parent PRD

[`issues/dependency-aware-issue-scheduling.md`](./dependency-aware-issue-scheduling.md)

## What to build

Deliver the first dependency-aware vertical slice: turn the explicit local
links under each issue's `## Blocked by` section into a validated issue graph
and use it in the public portable runner preflight. Only that section creates
dependency edges; issue-index position remains priority metadata, not an
implicit prerequisite.

Fail before any Codex call when a dependency is unknown, outside the issue
pack, duplicated, self-referential, cyclic, or omitted from an explicitly
selected subset while still unfinished. Diagnostics must identify the affected
issue, file, and invalid dependency. A valid pack must reach the existing
runner behavior unchanged, proving the slice through parsing, validation, CLI
preflight, terminal output, and standard-library tests.

## Acceptance criteria

- [ ] Only Markdown links within an issue's `## Blocked by` section become Declared Issue Dependencies.
- [ ] Reordering the issue index changes ready-issue priority without changing dependency edges.
- [ ] Unknown, out-of-pack, duplicate, and self dependencies fail preflight with actionable issue/file/link diagnostics.
- [ ] Direct and indirect dependency cycles fail preflight and report the cycle before any Codex call.
- [ ] An explicitly selected subset fails clearly when it omits an unfinished prerequisite; completed prerequisites outside the subset remain valid.
- [ ] A valid graph exposes stable issue identity, index position, and direct dependencies through one presentation-independent interface.
- [ ] Fake-runner coverage proves every invalid graph produces zero Codex calls and a valid graph reaches execution.
- [ ] Bash and PowerShell wrappers reach the same shared Python preflight behavior.
- [ ] No CodexCLI scheduler, domain, application, persistence, execution, or UI module is changed.

## Blocked by

None - can start immediately.

## User stories addressed

- User stories 1–3
- User stories 20–23
- User stories 35–36
- User story 39

## Implementation Notes

Completed: [x]

Implemented explicit `## Blocked by` parsing, validated graph nodes, actionable
unknown/out-of-pack/duplicate/self/cycle diagnostics, selected-subset preflight,
and dependency preservation across worktree mapping. Invalid packs stop before
the Codex runner is constructed.

Validation: dependency parser/graph tests, CLI preflight integration, wrapper
dry run, Python compilation, Bash syntax, and the complete test suite.
