# Interactive Plan-To-Dev Loop Runner

`devloop-plan` is the interactive front door for a new change. It asks where the
planning work should happen, starts an interactive Codex session for design and
local artifact creation, then offers to run the existing Dev Loop implementation
runner with the generated PRD and issue-pack paths.

## Windows PowerShell

```powershell
& 'F:\devloop\bin\devloop-plan.ps1' --repo 'C:\LocalCode\eConnectorV2'
```

## Ubuntu Or Mac

```bash
/path/to/devloop/bin/devloop-plan.sh --repo /path/to/project
```

## Flow

1. Select the target checkout.
2. Choose current branch, a new branch, or a new worktree.
3. Describe the feature or fix. For multi-line input, finish with `END`.
4. Codex opens interactively and follows `$grill-with-docs`, `$to-prd`, then `$to-issues`.
5. Exit Codex after it reports the generated PRD and issue README paths.
6. Confirm the detected `prd/*.md` and `issues/<prd-stem>/README.md` pair.
7. Choose whether to start Dev Loop immediately.

The generated PRD is expected under `prd/`. The issue pack is expected under
`issues/<prd-file-stem>/README.md` with real Markdown links to numbered issue
files.

## Final Dev Loop Options

Before starting implementation, the runner asks for:

- start issue, defaulting to `0001`
- whether to run all pending issues
- whether Dev Loop should use the same checkout or create an implementation worktree
- whether to update the Dev Loop self-improvement wiki

For the common same-checkout/no-wiki run, it builds the equivalent of:

```powershell
& 'F:\devloop\bin\devloop.ps1' --prd 'C:\LocalCode\eConnectorV2\prd\example.md' --issues 'C:\LocalCode\eConnectorV2\issues\example\README.md' --start-issue 0001 --all --no-worktree --no-self-improvement-wiki
```
