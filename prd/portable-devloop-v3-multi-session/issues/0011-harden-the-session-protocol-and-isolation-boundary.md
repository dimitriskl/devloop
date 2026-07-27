Label: ready-for-agent

# Harden the Session Protocol and Isolation Boundary

## Target Product

Product: devloop-plan + devloop

Portable supervisor/worker protocol, session identity, terminal sanitization,
catalog redaction, failure projection, and multi-session security boundary.
CodexCLI protocol and App Server transport are out of scope.

## What to build

Harden the complete v3 supervisor/worker boundary against malformed, hostile,
stale, duplicated, oversized, and cross-session traffic. Drive hostile fake
workers through the real supervisor and prove that invalid traffic fails only
the owning session, cannot satisfy another session's input, cannot alter another
session's catalog state, and cannot inject raw terminal control.

Complete versioning, framing, sequencing, payload limits, typed message kinds,
terminal sanitation, and catalog redaction for the protocol established by the
earlier tracer bullets. Preserve bounded diagnostics without storing secrets,
complete command streams, or unredacted provider payloads.

Covers parent PRD user stories 69–74 and 93–99.

## Acceptance criteria

- [ ] Every protocol frame requires a supported version, exact session identity, validated message kind, sequence value, and schema-valid payload.
- [ ] Partial reads, multiple frames per read, and platform line endings are framed deterministically.
- [ ] Malformed JSON, invalid encoding, missing fields, extra forbidden fields, unknown kinds, and unsupported versions fail closed.
- [ ] Duplicate, stale, skipped, and out-of-order sequence values cannot mutate session state silently.
- [ ] Configured frame and diagnostic size limits prevent unbounded memory or catalog growth.
- [ ] A frame naming another session is rejected and cannot update that session's view, input request, lease, capacity, or workflow pointer.
- [ ] User input and approval decisions require a currently pending request belonging to the active session.
- [ ] Protocol-derived display text passes through terminal sanitation before reaching any view.
- [ ] Catalog persistence excludes raw credentials, tokens, authorization data, complete command streams, and unredacted provider payloads.
- [ ] Bounded standard-error diagnostics are redacted before durable retention.
- [ ] A hostile or broken worker becomes failed/interrupted without crashing the shell or affecting sibling sessions.
- [ ] Concurrent stress tests interleave valid and hostile traffic from several fake workers and prove strict session isolation.
- [ ] Protocol and catalog schema versions have compatibility/rejection tests and actionable diagnostics.
- [ ] Existing terminal-safety, output-routing, redaction, and portable workflow regression suites remain green.

## Blocked by

- Blocked by [Issue 0004: Run and Monitor Concurrent Session Tabs](./0004-run-and-monitor-concurrent-session-tabs.md)
- Blocked by [Issue 0006: Pause, Stop, and Exit Sessions Safely](./0006-pause-stop-and-exit-sessions-safely.md)
- Blocked by [Issue 0007: Recover Crashed Workers and Stale Leases](./0007-recover-crashed-workers-and-stale-leases.md)
- Blocked by [Issue 0009: Preserve CLI and Plain Mode Contracts](./0009-preserve-cli-and-plain-mode-contracts.md)

## User stories addressed

- User stories 69–74
- User stories 93–99

## Implementation Notes

Completed: [ ]
