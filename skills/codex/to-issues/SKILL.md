---
name: to-issues
description: Break a plan, spec, or PRD into independently-grabbable local issue files using tracer-bullet vertical slices.
disable-model-invocation: true
---

# To Issues

Break a plan into independently-grabbable issues using vertical slices (tracer bullets).

## Local Artifact Rules

Create issue packs inside the PRD-specific folder:

- Use `<project-root>/prd/<prd-file-stem>/issues/`.
- Derive `<prd-file-stem>` from the source PRD filename without `.md`.
- Example: `prd/stock-version-conflict-retry/stock-version-conflict-retry.md` produces `prd/stock-version-conflict-retry/issues/`.
- Create the issue-pack folder if it does not exist.
- Write an index at `prd/<prd-file-stem>/issues/README.md`.
- Write issue files in the same folder using numbered kebab-case names, for example `0001-add-retry-boundary.md`.
- The README must use real Markdown links to the issue files, for example `[Issue 0001: Add Retry Boundary](./0001-add-retry-boundary.md)`.
- If the issue pack already exists, read it first and repair or extend it in place; do not silently overwrite existing issue files.

If the source is not a PRD file, choose a concise kebab-case pack name from the plan title and still use `prd/<pack-name>/issues/`.

## Process

### 1. Gather context

Work from whatever is already in the conversation context. If the user passes an issue reference (issue number, URL, or path) as an argument, fetch it from the issue tracker and read its full body and comments.

### 2. Explore the codebase (optional)

If you have not already explored the codebase, do so to understand the current state of the code. Issue titles and descriptions should use the project's domain glossary vocabulary, and respect ADRs in the area you're touching.

Look for opportunities to prefactor the code to make the implementation easier. "Make the change easy, then make the easy change."

### 3. Draft vertical slices

Break the plan into **tracer bullet** issues. Each issue is a thin vertical slice that cuts through ALL integration layers end-to-end, NOT a horizontal slice of one layer.

<vertical-slice-rules>

- Each slice delivers a narrow but COMPLETE path through every layer (schema, API, UI, tests)
- A completed slice is demoable or verifiable on its own
- Any prefactoring should be done first

</vertical-slice-rules>

### 4. Quiz the user

Present the proposed breakdown as a numbered list. For each slice, show:

- **Title**: short descriptive name
- **Blocked by**: which other slices (if any) must complete first
- **User stories covered**: which user stories this addresses (if the source material has them)

Ask the user:

- Does the granularity feel right? (too coarse / too fine)
- Are the dependency relationships correct?
- Should any slices be merged or split further?

Iterate until the user approves the breakdown.

### 5. Publish the issues to local files

For each approved slice, publish a new issue file in the local issue-pack folder from the Local Artifact Rules. Use the issue body template below. These issues are considered ready for AFK agents, so include `Label: ready-for-agent` near the top of each file unless instructed otherwise.

Publish issues in dependency order (blockers first) so you can reference earlier issue filenames in the "Blocked by" field. Keep `README.md` as the runnable index for local devloop tools.

<issue-template>
## Parent

A reference to the parent issue on the issue tracker (if the source was an existing issue, otherwise omit this section).

## What to build

A concise description of this vertical slice. Describe the end-to-end behavior, not layer-by-layer implementation.

Avoid specific file paths or code snippets — they go stale fast. Exception: if a prototype produced a snippet that encodes a decision more precisely than prose can (state machine, reducer, schema, type shape), inline it here and note briefly that it came from a prototype. Trim to the decision-rich parts — not a working demo, just the important bits.

## Acceptance criteria

- [ ] Criterion 1
- [ ] Criterion 2
- [ ] Criterion 3

## Blocked by

- A reference to the blocking ticket (if any)

Or "None - can start immediately" if no blockers.

</issue-template>

Do NOT close or modify any parent issue.
