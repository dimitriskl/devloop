# Dev Loop Self-Improvement Lessons

Durable, evidence-backed lessons that improve future Dev Loop runs.

## Entries

## Captured Subprocess Output Must Use UTF-8 Replacement

- Applies to: Dev Loop runner on Windows, Codex and Git subprocess capture
- Lesson: Do not rely on the active Windows console code page when capturing subprocess output; force UTF-8 decoding with replacement and normalize missing stdout/stderr to empty strings.
- Evidence: A run crashed with `UnicodeDecodeError` in `encodings\cp1253.py`, then `TypeError: sequence item 0: expected str instance, NoneType found` while joining captured Codex output.
- Action: Use `run_captured_text()` for captured subprocess calls and `output_text()` before accumulating stdout/stderr.
- Last seen: 2026-06-23

## Issue Indexes Should Only Enqueue Issue Files

- Applies to: Dev Loop issue-pack parsing
- Lesson: A README can link to the parent PRD for context, but the runner should only enqueue Markdown files under the issue index folder.
- Evidence: A run selected `PRD` before `0001` because the issue README contained a parseable PRD link.
- Action: Keep issue links under `issues/`, and ignore links outside the issue folder during parsing.
- Last seen: 2026-06-23
