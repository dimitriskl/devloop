# Interactive Plan-To-Dev Loop Runner

`devloop-plan` is the interactive front door for a new change. It asks where the
planning work should happen, starts an interactive Codex session for design and
local artifact creation, then asks for the development parameters before running
the Dev Loop implementation runner with the generated PRD and issue-pack paths.

## Windows PowerShell

```powershell
& 'F:\devloop\bin\devloop-plan.ps1' --repo 'C:\LocalCode\eConnectorV2'
```

## Ubuntu Or Mac

```bash
/path/to/devloop/bin/devloop-plan.sh --repo /path/to/project
```

## Flow

1. Select the target checkout. The first run has no default. After a valid
   selection, that checkout is saved and shown as the default on later runs.
2. Choose current branch, a new branch, or a new worktree.
3. Codex opens interactively. If `--goal` was not passed, describe the feature
   or fix inside Codex. Use the normal Codex input behavior, including
   arrow-key editing and Alt+V image paste when your installed CLI supports it.
4. Codex follows `$grill-with-docs`, `$to-prd`, then `$to-issues`.
5. Exit Codex after it reports the generated PRD and issue README paths.
6. Confirm the detected `prd/<prd-stem>/<prd-stem>.md` and
   `prd/<prd-stem>/issues/README.md` pair.
7. Choose whether to continue to development, then answer the development
   parameter prompts.

The generated PRD is expected under `prd/<prd-file-stem>/`. The issue pack is
expected under `prd/<prd-file-stem>/issues/README.md` with real Markdown links
to numbered issue files. Loop state, logs, and other PRD execution artifacts stay
under the same PRD folder.

## Final Dev Loop Options

Before starting implementation, the runner asks for:

- start issue, defaulting to `0001`
- whether to run all pending issues
- whether Dev Loop should create a dedicated implementation worktree, defaulting
  to yes
- whether to use the Dev Loop self-improvement wiki during and after
  development, defaulting to yes

For the common worktree/wiki run, it builds the equivalent of:

```powershell
& 'F:\devloop\bin\devloop.ps1' --prd 'C:\LocalCode\eConnectorV2\prd\example\example.md' --issues 'C:\LocalCode\eConnectorV2\prd\example\issues\README.md' --start-issue 0001 --all --create-worktree --worktree-path 'C:\LocalCode\eConnectorV2-example-dev' --branch-name 'devloop/example'
```

After a successful development run, `devloop` asks whether to merge the
implementation branch or worktree into another branch. It skips automatic merge
when the implementation or target checkout has uncommitted changes.
