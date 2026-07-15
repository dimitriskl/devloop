Label: ready-for-agent

# Show Dynamic Progress Across Every Terminal Surface

## Parent

[Configurable Workflow Steps PRD](./configurable-workflow-steps.md)

## Target Product

Product: devloop-plan + devloop

Portable `devloop-plan + devloop`. Implement through `statusui.py`, `cli.py`,
`chat_loop.py`, and shared portable projections used by both shell wrappers.
The CodexCLI Textual dashboard is out of scope.

## What to build

Create one presentation-independent Workflow Progress projection keyed by Step Instance ID and consumed by every terminal surface. Show every Primary Path instance as a separate row, keep workflow-scoped work distinct from issue-scoped work, and reveal branch steps when visited or expanded. Preserve each step's status, pass, accumulated/frozen elapsed time, latest result, active model/effort/Fast settings, and safe latest activity.

Use the projection in the portable planning intake and bounded implementation console used by Bash and PowerShell. Reuse one current-issue card in place, keep the active row visible in long workflows, and show a spinner, active elapsed timer, and event-freshness timer while work is live. Non-interactive output must remain append-only and must never emit cursor-control sequences. Provide semantic color plus text labels and a no-color fallback.

Covers parent PRD user stories 94-106.

## Acceptance criteria

- [ ] A shared pure projection represents workflow, Issue, step, attempt, timer, result, and safe activity data by Step Instance ID.
- [ ] Duplicate component types render as separate rows using their configured display names.
- [ ] Workflow-scoped and issue-scoped progress are visually distinct, and branch-only steps appear when visited or expanded.
- [ ] Each row retains status, pass, and accumulated elapsed time; completed timers freeze while rework adds to the same step total.
- [ ] The active activity line shows display name, model, reasoning effort, Fast state, spinner, elapsed time, event freshness, and safe activity text.
- [ ] Planning and implementation console modes render the same projected state and preserve existing commands, line-editor access, and interruption behavior.
- [ ] The hybrid console reuses one bounded current-issue card and retains a concise Last Result when the next issue starts.
- [ ] Long workflows scroll or window while keeping the active Step Instance visible.
- [ ] Redirected/non-TTY output is append-only and contains no cursor movement or screen clearing.
- [ ] PASS is green, FAIL is red, WORKING is yellow, and text/no-color fallbacks communicate the same states.
- [ ] Unicode, ASCII/no-color, narrow, wide, Bash-wrapper, and PowerShell-wrapper behavior have automated coverage against the shared projection.
- [ ] No CodexCLI Textual widgets, launcher state, or UI tests are changed.

## Blocked by

- [Issue 0001: Run Two Review Instances Through a v2 Workflow](./0001-run-two-review-instances-through-v2.md)
- [Issue 0002: Resume and Rework Arbitrary Step Instances](./0002-resume-and-rework-arbitrary-step-instances.md)
