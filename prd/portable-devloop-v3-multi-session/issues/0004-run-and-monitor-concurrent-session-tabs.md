Label: ready-for-agent

# Run and Monitor Concurrent Session Tabs

## Target Product

Product: devloop-plan + devloop

Portable Application Shell, session supervisor, worker protocol, Sessions tab,
session tabs, context projection, and input routing. CodexCLI views and App
Server threads are out of scope.

## What to build

Extend the single-worker tracer bullet to run and observe multiple independent
Portable Workflow Sessions at the same time. Route every worker event and user
intent by stable session identity, retain each tab's context while background
work advances, and aggregate all session progress in the permanent Sessions tab.

Make attention safe and visible. Compact tab labels show worktree, lifecycle,
and unread state; background input or approval requests show `[INPUT!]` and may
ring an optional terminal bell, but never steal focus. Closing a tab hides only
its view, and reopening it restores the retained session projection.

Covers parent PRD user stories 1–3, 21–30, 35, and 56.

## Acceptance criteria

- [ ] At least two workers in different worktrees run concurrently under one Portable Application Shell.
- [ ] Every protocol frame and user intent is routed by stable session identity and cannot cross into a sibling session.
- [ ] Switching tabs replaces only contained content and does not pause, restart, or lose background state.
- [ ] The Sessions tab shows project/worktree, status, stage, PRD, issue progress, active issue, latest activity, and last update for each session.
- [ ] Compact tab labels show worktree identity, lifecycle state, and unseen activity.
- [ ] A background input or approval request marks only its owning tab `[INPUT!]`.
- [ ] Text typed in the active tab can never satisfy a background session's request.
- [ ] Attention requests do not change focus; the terminal bell is optional and configurable.
- [ ] Closing a workflow tab hides its view without changing session lifecycle or worker state.
- [ ] Reopening or focusing a hidden session reconstructs its current projection from retained supervisor state.
- [ ] One session failure remains visible in its tab and does not stop or corrupt sibling workers.
- [ ] Full-shell tests drive multiple fake workers through interleaved events, input requests, focus changes, hide/reopen, completion, and failure.

## Blocked by

- Blocked by [Issue 0001: Launch One Isolated Session from the Sessions Tab](./0001-launch-one-isolated-session-from-the-sessions-tab.md)
- Blocked by [Issue 0002: Resume Pre-PRD Sessions from the Machine Catalog](./0002-resume-pre-prd-sessions-from-the-machine-catalog.md)
- Blocked by [Issue 0003: Create and Lease Distinct Worktrees](./0003-create-and-lease-distinct-worktrees.md)

## User stories addressed

- User stories 1–3
- User stories 21–30
- User story 35
- User story 56

## Implementation Notes

Completed: [ ]
