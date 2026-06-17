# Usage

Run one pending issue:

Windows:

```powershell
.\bin\devloop.ps1 --prd E:\repo\docs\feature\prd.md --issues E:\repo\docs\feature\issues\README.md
```

Ubuntu/Linux:

````bash
./bin/devloop.sh \
  --prd /home/dimitris/code/diagnostics-collector/docs/diagnostic-collector/production-diagnostic-collector-prd.md \
  --issues /home/dimitris/code/diagnostics-collector/docs/diagnostic-collector/issues/README.md```

./bin/devloop.sh \
  --prd /home/dimitris/code/diagnostics-collector/docs/diagnostic-collector/production-diagnostic-collector-prd.md \
  --issues /home/dimitris/code/diagnostics-collector/docs/diagnostic-collector/issues/README.md \
  --no-worktree

  ./bin/devloop.sh \
  --prd /home/dimitris/code/diagnostics-collector/docs/diagnostic-collector/production-diagnostic-collector-prd.md \
  --issues /home/dimitris/code/diagnostics-collector/docs/diagnostic-collector/issues/README.md \
  --no-worktree


Run every pending issue:

Windows:

```powershell
.\bin\devloop.ps1 --prd E:\repo\docs\feature\prd.md --issues E:\repo\docs\feature\issues\README.md --all
````

Ubuntu/Linux:

```bash
./bin/devloop.sh --prd /home/you/repo/docs/feature/prd.md --issues /home/you/repo/docs/feature/issues/README.md --all
```

./bin/devloop.sh \
 --prd /home/dimitris/code/diagnostics-collector/docs/diagnostic-collector/production-diagnostic-collector-prd.md \
 --issues /home/dimitris/code/diagnostics-collector/docs/diagnostic-collector/issues/README.md \
 --all \
 --no-worktree

Start at a specific issue:

Windows:

```powershell
.\bin\devloop.ps1 --prd E:\repo\docs\feature\prd.md --issues E:\repo\docs\feature\issues\README.md --start-issue 0004
```

Ubuntu/Linux:

```bash
./bin/devloop.sh --prd /home/you/repo/docs/feature/prd.md --issues /home/you/repo/docs/feature/issues/README.md --start-issue 0004
```

Preview prompts without invoking Codex:

Windows:

```powershell
.\bin\devloop.ps1 --prd E:\repo\docs\feature\prd.md --issues E:\repo\docs\feature\issues\README.md --dry-run --no-worktree
```

Ubuntu/Linux:

```bash
./bin/devloop.sh --prd /home/you/repo/docs/feature/prd.md --issues /home/you/repo/docs/feature/issues/README.md --dry-run --no-worktree
```

The runner creates loop state next to the issue README in the active worktree:

- `README.loop.md`
- `README.loop.state.json`
- `.loop.logs/`

Completed issues are updated in place:

- `Completed: [x]`
- checked acceptance criteria
- appended `## Implementation Notes`
