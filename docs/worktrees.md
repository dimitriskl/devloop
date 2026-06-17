# Worktrees

The runner assumes implementation should happen in a dedicated worktree when you
choose one.

Interactive mode:

```powershell
.\bin\devloop.ps1 --prd E:\design\docs\prd.md --issues E:\design\docs\issues\README.md
```

If worktree flags are omitted, the runner asks:

1. Whether to create a dedicated implementation worktree.
2. The implementation worktree path.
3. The implementation branch name.

Non-interactive creation:

```powershell
.\bin\devloop.ps1 `
  --prd E:\design\docs\prd.md `
  --issues E:\design\docs\issues\README.md `
  --create-worktree `
  --worktree-path E:\worktrees\my-feature-impl `
  --branch-name impl/my-feature-0001 `
  --non-interactive
```

Use the current worktree directly:

```powershell
.\bin\devloop.ps1 --prd E:\repo\docs\prd.md --issues E:\repo\docs\issues\README.md --no-worktree
```

The runner does not commit, push, merge, or delete worktrees.


