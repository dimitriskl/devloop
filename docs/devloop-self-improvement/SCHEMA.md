# Dev Loop Self-Improvement Schema

The Dev Loop self-improvement wiki stores durable lessons that improve future
Dev Loop runs. It belongs to the Dev Loop bundle, not to any one target
repository.

## Lesson Entry Format

```md
## Short Lesson Title

- Applies to: runner area, workflow, target repo family, or technology
- Lesson: one reusable rule or pattern
- Evidence: loop state, gate feedback, issue file, or command output reference
- Action: what the next agent should do differently
- Last seen: YYYY-MM-DD
```

## Promotion Rules

- Keep only lessons likely to matter in future runs.
- Prefer user instructions, implementation lessons, bug causes and fixes,
  repeated failures, blocked causes, reviewer/QA feedback, and successful
  patterns that avoided rework.
- Store target-repo facts only when they teach Dev Loop how to work better in
  future runs. Do not use this wiki as a business-domain notebook.
- Do not store raw logs, secrets, credentials, tokens, personal data, or large
  code blocks.
- Update an existing lesson instead of creating a duplicate when the meaning is
  the same.
