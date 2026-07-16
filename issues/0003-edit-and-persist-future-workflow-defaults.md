Label: ready-for-agent

# Edit and Persist Future Workflow Defaults

## Parent

[Configurable Workflow Steps PRD](./configurable-workflow-steps.md)

## Target Product

Product: devloop-plan + devloop

Portable `devloop-plan + devloop`. Implement through `interactive_runner.py`,
`chat_loop.py`, `lineeditor.py`, `catalog.py`, and portable configuration
modules. Do not implement this in the CodexCLI Textual UI.

## What to build

Turn portable `devloop-plan` `/options` into the entry point for a transactional terminal Workflow Editor. Present the Primary Path and a focused selected-step inspector through the existing line editor. Load the User Workflow Default from portable planner configuration when it exists and otherwise load the built-in workflow. Allow unique display-name edits while keeping machine GUIDs out of the normal view and available in an advanced view.

Maintain an isolated draft with Undo, Reset Step, Reset Workflow, atomic Apply, and whole-draft Cancel. During a run, show Current Run as inspectable but read-only and Future Runs as editable, with explicit copy that changes apply only to newly created runs. A new run must capture the resolved default and canonical hash; the active run must remain unchanged.

Covers parent PRD user stories 1-3, 11-20, and 79-86.

## Acceptance criteria

- [x] `/options` opens a Workflow Editor showing the Primary Path and selected-step settings.
- [x] The editor loads the persisted User Workflow Default or the built-in default when none exists.
- [x] Step display names can be changed and must remain unique within the draft.
- [x] UUIDs are hidden in the normal editor and inspectable through an advanced detail.
- [x] Undo, Reset Step, and Reset Workflow operate only on the current draft.
- [x] Cancel performs no persistence write; Apply validates and replaces the default atomically.
- [x] During an active run, Current Run is read-only and Future Runs remains editable with an explicit scope message.
- [x] Editing Future Runs does not mutate the active run snapshot, cursor, or hash.
- [x] A subsequent run captures the newly resolved workflow and canonical hash.
- [x] Fake-editor and public planner-flow coverage exercises Apply, Cancel, reset, undo, current/future scope, and narrow and wide terminal layouts.
- [x] Bash and PowerShell wrappers reach the same editor behavior without invoking CodexCLI.

## Blocked by

- [Issue 0001: Run Two Review Instances Through a v2 Workflow](./0001-run-two-review-instances-through-v2.md)

## Implementation Notes

Completed: 2026-07-16T02:07:55

### Changed Files
- `docs/skills-and-agents.md`

### Verification
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -q`
- `PYTHONPATH=src python3 -m unittest tests.test_workflow_editor tests.test_interactive_runner.PlanStateTests tests.test_interactive_runner.BuildDevloopArgsTests tests.test_chat_loop tests.test_cli_banners.ResolveRunWorkflowTests`
- `bash bin/devloop-plan.sh --help`
- `bash bin/devloop.sh --help`
- `command -v pwsh || command -v powershell || true`
- `git diff --check`
- `rg -n -F 'Inside the planning chat, follow `/options` → `capabilities` → **Add skill or' docs/skills-and-agents.md`
- `rg -n -F 'print("  3. Add skill or agent from GitHub")' src/devloop/interactive_runner.py`

### Review
The corrected documentation now matches the implemented `/options` → `capabilities` → “Add skill or agent from GitHub” flow. No blocking issues remain.

### QA
All Issue 0003 acceptance criteria have sufficient automated or source-inspection coverage. Focused tests and the full Python regression suite passed; Bash entry points started successfully.
