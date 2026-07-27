Label: ready-for-agent

# Launch One Isolated Session from the Sessions Tab

## Target Product

Product: devloop-plan + devloop

Portable Application Shell, portable runtime presentation boundary, planning
and delivery orchestration, and a new portable session-supervisor boundary.
CodexCLI is out of scope.

## What to build

Deliver the first v3 tracer bullet without changing workflow behavior: start
Portable Dev Loop into a permanent Sessions tab, create one Portable Workflow
Session, launch its existing planning or delivery operation in an isolated child
worker, and project its context, activity, status, completion, or failure into
one session tab.

Introduce the smallest versioned supervisor/worker protocol needed for this
path. The shell owns worker lifecycle through a presentation-independent
supervisor; UI views emit typed intents and never manage processes directly.
Keep redirected and Plain Mode behavior working while the new interactive path
proves the full shell-to-worker-to-workflow round trip.

Covers parent PRD user stories 1–7, 22, 69, and 93–98.

## Acceptance criteria

- [ ] Interactive startup mounts one persistent Portable Application Shell with a permanent Sessions tab.
- [ ] Startup launches no workflow worker until the user selects the new-session action.
- [ ] One new session starts the existing portable planning or delivery operation in a separate child process.
- [ ] The session tab renders the selected checkout context, current activity, lifecycle status, and terminal result.
- [ ] Supervisor and worker exchange versioned, session-identified JSON Lines frames over redirected standard input and output.
- [ ] Worker standard error is captured as session diagnostics and cannot write directly into the terminal UI.
- [ ] Unknown protocol versions, wrong session identities, and malformed frames fail the session clearly without crashing the shell.
- [ ] The portable supervisor owns start and shutdown; presentation views emit typed intents and contain no operating-system process control.
- [ ] Workflow, state-machine, worktree, and execution modules remain free of Textual types.
- [ ] Existing one-session Plain Mode and redirected-output regression tests remain green.
- [ ] A fake worker proves start, activity, context, successful completion, and isolated failure through the highest supervisor seam.
- [ ] No CodexCLI application, domain, execution, persistence, UI, or workflow module is changed.

## Blocked by

None - can start immediately.

## User stories addressed

- User stories 1–7
- User story 22
- User story 69
- User stories 93–98

## Implementation Notes

Completed: [ ]
