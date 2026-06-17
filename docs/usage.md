# Usage

Run one pending issue:

```powershell
.\bin\devloop.ps1 --prd E:\repo\docs\feature\prd.md --issues E:\repo\docs\feature\issues\README.md
```

Run every pending issue:

```powershell
.\bin\devloop.ps1 --prd E:\repo\docs\feature\prd.md --issues E:\repo\docs\feature\issues\README.md --all
```

Start at a specific issue:

```powershell
.\bin\devloop.ps1 --prd E:\repo\docs\feature\prd.md --issues E:\repo\docs\feature\issues\README.md --start-issue 0004
```

Preview prompts without invoking Codex:

```powershell
.\bin\devloop.ps1 --prd E:\repo\docs\feature\prd.md --issues E:\repo\docs\feature\issues\README.md --dry-run --no-worktree
```

The runner creates loop state next to the issue README in the active worktree:

- `README.loop.md`
- `README.loop.state.json`
- `.loop.logs/`

Completed issues are updated in place:

- `Completed: [x]`
- checked acceptance criteria
- appended `## Implementation Notes`


