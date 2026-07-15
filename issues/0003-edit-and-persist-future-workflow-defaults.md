Label: ready-for-agent

# Edit and Persist Future Workflow Defaults

## Parent

[Configurable Workflow Steps PRD](./configurable-workflow-steps.md)

## What to build

Turn `/options` into the entry point for a transactional Workflow Editor. Present the Primary Path and a focused selected-step inspector. Load the User Workflow Default when it exists and otherwise load the built-in workflow. Allow unique display-name edits while keeping machine GUIDs out of the normal view and available in an advanced view.

Maintain an isolated draft with Undo, Reset Step, Reset Workflow, atomic Apply, and whole-draft Cancel. During a run, show Current Run as inspectable but read-only and Future Runs as editable, with explicit copy that changes apply only to newly created runs. A new run must capture the resolved default and canonical hash; the active run must remain unchanged.

Covers parent PRD user stories 1-3, 11-20, and 79-86.

## Acceptance criteria

- [ ] `/options` opens a Workflow Editor showing the Primary Path and selected-step settings.
- [ ] The editor loads the persisted User Workflow Default or the built-in default when none exists.
- [ ] Step display names can be changed and must remain unique within the draft.
- [ ] UUIDs are hidden in the normal editor and inspectable through an advanced detail.
- [ ] Undo, Reset Step, and Reset Workflow operate only on the current draft.
- [ ] Cancel performs no persistence write; Apply validates and replaces the default atomically.
- [ ] During an active run, Current Run is read-only and Future Runs remains editable with an explicit scope message.
- [ ] Editing Future Runs does not mutate the active run snapshot, cursor, or hash.
- [ ] A subsequent run captures the newly resolved workflow and canonical hash.
- [ ] Textual Pilot coverage exercises Apply, Cancel, reset, undo, current/future scope, and narrow and wide layouts.

## Blocked by

- [Issue 0001: Run Two Review Instances Through a v2 Workflow](./0001-run-two-review-instances-through-v2.md)
