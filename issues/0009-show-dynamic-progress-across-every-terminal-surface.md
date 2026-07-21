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

- [x] A shared pure projection represents workflow, Issue, step, attempt, timer, result, and safe activity data by Step Instance ID.
- [x] Duplicate component types render as separate rows using their configured display names.
- [x] Workflow-scoped and issue-scoped progress are visually distinct, and branch-only steps appear when visited or expanded.
- [x] Each row retains status, pass, and accumulated elapsed time; completed timers freeze while rework adds to the same step total.
- [x] The active activity line shows display name, model, reasoning effort, Fast state, spinner, elapsed time, event freshness, and safe activity text.
- [x] Planning and implementation console modes render the same projected state and preserve existing commands, line-editor access, and interruption behavior.
- [x] The hybrid console reuses one bounded current-issue card and retains a concise Last Result when the next issue starts.
- [x] Long workflows scroll or window while keeping the active Step Instance visible.
- [x] Redirected/non-TTY output is append-only and contains no cursor movement or screen clearing.
- [x] PASS is green, FAIL is red, WORKING is yellow, and text/no-color fallbacks communicate the same states.
- [x] Unicode, ASCII/no-color, narrow, wide, Bash-wrapper, and PowerShell-wrapper behavior have automated coverage against the shared projection.
- [x] No CodexCLI Textual widgets, launcher state, or UI tests are changed.

## Blocked by

- [Issue 0001: Run Two Review Instances Through a v2 Workflow](./0001-run-two-review-instances-through-v2.md)
- [Issue 0002: Resume and Rework Arbitrary Step Instances](./0002-resume-and-rework-arbitrary-step-instances.md)

## Implementation Notes

Completed: 2026-07-16T15:26:34

### Changed Files
- `src/devloop/terminal_text.py`
- `tests/test_statusui.py`

### Verification
- `Independent sanitizer safety and OSC/DCS scaling probe for lengths 1,000–16,000`
- `Inline Python terminal-sequence safety and OSC scaling probe for lengths 1,000–16,000`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m py_compile src/devloop/terminal_text.py tests/test_statusui.py`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -p 'test*.py' -b`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_statusui tests.test_chat_loop.RunStreamingTests tests.test_chat_loop.RunPlanningChatTests tests.test_codex_runner.StreamingCodexRunnerTests -b`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest tests.test_statusui.TerminalUnicodeSafetyTests tests.test_workflow_progress -b`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 sanitizer control-sequence and 1,000–16,000 character scaling probe`
- `PYTHONPATH=src python3 -m py_compile src/devloop/terminal_text.py tests/test_statusui.py`
- `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test*.py' -b`
- `PYTHONPATH=src python3 -m unittest tests.test_statusui.TerminalUnicodeSafetyTests tests.test_workflow_progress -b`
- `Sanitizer scaling probe for OSC/DCS lengths 1000–16000`
- `git diff --check`

### Workflow Step Results

#### Development

Replaced backtracking terminal-control regexes with a linear single-pass parser and added coverage for large unterminated OSC/DCS sequences.

#### Security Review

No blocking security, correctness, or regression issues found. The terminal sanitizer now handles large unterminated control strings in linear time, and all 463 tests passed.

#### Final Review

No blocking correctness, security, performance, or regression issues found. The terminal sanitizer now processes large unterminated OSC/DCS input in linear time, and all 463 tests pass.

#### QA

QA passed. Terminal-sequence sanitization, shared terminal surfaces, and the full regression suite verified successfully; coverage supports the issue acceptance criteria.
