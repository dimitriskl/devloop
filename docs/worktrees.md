# Worktrees

The runner assumes implementation should happen in a dedicated worktree when you
choose one.

Interactive mode:

```powershell
.\bin\devloop.ps1 --prd E:\design\prd\feature\feature.md --issues E:\design\prd\feature\issues\README.md
```

If worktree flags are omitted, the runner asks:

1. Whether to create a dedicated implementation worktree.
2. The implementation worktree parent path.
3. The implementation worktree folder name.
4. The implementation branch name.

The interactive default is yes. If the PRD and issue pack were created but not
committed yet, the runner copies the PRD folder or legacy PRD/issue files into
the implementation worktree before starting coder/reviewer/QA passes.

Branch prompts accept friendly text and normalize it before Git runs. For
example, `Reset Queue` becomes `Reset-Queue`.

When you enter a worktree parent path, Dev Loop remembers it and offers it as
the parent default the next time.

Non-interactive creation:

```powershell
.\bin\devloop.ps1 `
  --prd E:\design\prd\feature\feature.md `
  --issues E:\design\prd\feature\issues\README.md `
  --create-worktree `
  --worktree-path E:\worktrees\my-feature-impl `
  --branch-name impl/my-feature-0001 `
  --non-interactive
```

Use the current worktree directly:

```powershell
.\bin\devloop.ps1 --prd E:\repo\prd\feature\feature.md --issues E:\repo\prd\feature\issues\README.md --no-worktree
```

Rerunning the same `--create-worktree --worktree-path ... --branch-name ...`
command after a crash reuses the existing path when it is already a registered
worktree or Git checkout. The existing checkout's current branch is kept, even
if the branch prompt now contains a slightly different normalized name. If the
branch was created by a previous partial attempt but the path was not created,
Dev Loop uses the existing branch instead of passing `git worktree add -b`
again. Empty existing folders are left for Git to populate; non-empty folders
that are not Git checkouts are rejected so unrelated files are not overwritten.

The runner does not commit, push, or delete worktrees. After a successful
development run, it asks whether to merge the implementation branch or worktree
into another branch. Automatic merge is skipped when the implementation or target
checkout has uncommitted changes.
