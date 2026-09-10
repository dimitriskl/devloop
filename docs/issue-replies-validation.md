# Issue reply editor validation — 2026-09-09

The original Completion Review exposed rerun and exit, with no reply input.
The delivery worker and supervisor now carry a typed REPLY request, including
request identity and generation. The full-screen editor accepts multiline text,
clipboard images, Windows file-drop objects, and complete file paths. Submission
copies attachments into issue-scoped storage and forwards the saved answer through
normal role prompts; it does not mark an issue or a gate complete.

`--reply` opens a previously saved blocked issue before provider preflight. The
existing `resume-feedback.ps1` launcher accepts `-Reply` for this path. Its
PowerShell syntax was parsed successfully without launching the live workflow.

Evidence:

- [Final focused checks](../reply-final-focused.log): 37 passed. These exercise
  the real editor at 80×24 and 120×40, multiline terminal paste, screenshot and
  file-drop clipboard simulation, filenames with spaces and Unicode, attachment
  persistence after original files disappear, cancellation, validation failures,
  stale request rejection by the real supervisor with a controlled worker, and
  saved replies reaching Development, Review, and QA backend requests.
- [Broader regression checks](../reply-regressions-final.log): 213 passed,
  one skipped, one failed. The failure is the existing 0.2-second connection
  retry timing assertion in `test_codex_runner.py`. The retry function's AST
  matches HEAD; the new image argument handling does not change that function.
  A session transfer timing failure in the first broad run passed on the rerun.
- [PRD-only dry run](../reply-dryrun.log): exit zero against a fresh local copy
  of `portable-direct-run-defaults`. Four role prompts, `README.loop.md`, and
  `README.loop.state.json` were generated and inspected; issue 0003 has the
  existing `Dry Run` state. The copied pack and local session catalog were
  confined to `.tmp-reply-dryrun-cu_vgnus` in this workspace.
- Configured Ruff and strict mypy passed (83 source files). The new modules and
  changed runner/protocol/backend contracts passed
  [explicit portable-module lint](../reply-lint.log). Diff whitespace checks passed.
- [Full-suite attempt](../reply-full-suite.log): stopped after eight passes at
  missing installed Workflow Step Component metadata in the fallback validation
  environment. Full-suite validation is not green.

Final code review checked issue isolation, bounded attachment copies, atomic reply
record publication, explicit submission, prompt/Step Guidance separation, native
Codex image arguments, backward-compatible dataclass defaults, and request
correlation. Review found and resolved duplicate terminal paste handling, prompt
builder coupling to filesystem state, delayed snapshot reopening, and clipping at
the minimum terminal size.

The external eConnector loop state was read, not modified: 0001–0003 are COMPLETED,
0004 is BLOCKED, and 0005 is WAITING_ON_DEPENDENCY. No answer to the Store persistence
question was selected on the operator's behalf. No authenticated provider, live
SQL gate, or real desktop clipboard mutation was launched. Clipboard evidence
uses controlled inputs; live use requires restarting the user-owned application.

Checks used the existing Python 3.12 interpreter and cached validation packages.
The Store-Python venv remains unavailable from the managed sandbox. Existing work
was preserved and no commit was created.

## Follow-up: PowerShell reply argument forwarding

The operator's first `-Reply` launch failed before the editor opened. The launcher
assigned the output of an `if` expression to an untyped variable: PowerShell
unwrapped the single-element array to a string, which splatting expanded into
`- - r e p l y`. With the switch absent, it also forwarded an empty argument.
The initial syntax-only check could not detect either runtime failure.

`replyArguments` is now an explicitly initialized `string[]`, with `--reply`
appended only when requested. The new regression executes copies of both actual
PowerShell wrappers against an argument-capturing Python module and validates the
captured arguments with Dev Loop's real parser. Both modes
[failed before the change](../reply-launcher-red.log) and
[passed after it](../reply-launcher-validation.log). No authenticated agent or
installation was launched. Focused lint and diff whitespace checks also passed.
