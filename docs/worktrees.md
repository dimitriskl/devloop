# Worktrees

The runner assumes implementation should happen in a dedicated worktree when you
choose one.

Interactive mode:

```powershell
.\bin\devloop.ps1 --prd E:\design\prd\feature\feature.md --issues E:\design\prd\feature\issues\README.md
```

If worktree flags are omitted, the runner asks:

1. Whether to create a dedicated implementation worktree.
2. The implementation worktree path.
3. The implementation branch name.

The interactive default is yes. If the PRD and issue pack were created but not
committed yet, the runner copies the PRD folder or legacy PRD/issue files into
the implementation worktree before starting coder/reviewer/QA passes.

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

The runner does not commit, push, or delete worktrees. After a successful
development run, it asks whether to merge the implementation branch or worktree
into another branch. Automatic merge is skipped when the implementation or target
checkout has uncommitted changes.
