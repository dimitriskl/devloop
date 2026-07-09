# Usage

## Interactive plan-to-development flow

Use `devloop-plan` when you want the runner to start from an idea, drive an
interactive Codex planning session, create `prd/<change>/` artifacts, and then
offer to start the existing implementation loop.

Windows:

```powershell
.\bin\devloop-plan.ps1 --repo C:\LocalCode\eConnectorV2
```

Ubuntu/macOS:

```bash
./bin/devloop-plan.sh --repo /path/to/project
```

See `docs/interactive-runner.md` for the full flow.

### Stage pipeline

The session always moves through four stages: `analysis -> development ->
review -> qa`. A banner showing the pipeline and the active stage prints at
every stage transition and again before every input prompt, so the current
stage stays visible no matter how much output has scrolled by. Set
`NO_COLOR=1` to disable the banner's color highlighting; consoles that cannot
encode the default Unicode markers automatically fall back to ASCII markers.

When `--repo` is omitted, the first run has no target default. If the selected
folder does not exist, the runner asks whether to create it, then asks whether to
initialize Git in the new folder so it can be used as a target checkout. After
you select a valid target checkout, later runs show that checkout as the default.
When `--goal` is omitted, type the change request at the chat prompt; devloop's
own line editor provides arrow-key editing, command history, and Alt+V
screenshot paste regardless of what the installed Codex CLI supports natively.

## Existing PRD and issue pack

Run one pending issue:

Windows:

```powershell
.\bin\devloop.ps1 --prd E:\repo\prd\feature\feature.md --issues E:\repo\prd\feature\issues\README.md
```

Ubuntu/Linux:

```bash
./bin/devloop.sh --prd /home/you/repo/prd/feature/feature.md --issues /home/you/repo/prd/feature/issues/README.md
```

Run every pending issue:

Windows:

```powershell
.\bin\devloop.ps1 --prd E:\repo\prd\feature\feature.md --issues E:\repo\prd\feature\issues\README.md --all
```

Ubuntu/Linux:

```bash
./bin/devloop.sh --prd /home/you/repo/prd/feature/feature.md --issues /home/you/repo/prd/feature/issues/README.md --all
```

Completed issue files are skipped. With `--all`, the runner selects only blocked
or unfinished issues. With `--start-issue`, if the requested issue is already
completed, the runner starts at the next unfinished issue in the index.

Start at a specific issue:

Windows:

```powershell
.\bin\devloop.ps1 --prd E:\repo\prd\feature\feature.md --issues E:\repo\prd\feature\issues\README.md --start-issue 0004
```

Ubuntu/Linux:

```bash
./bin/devloop.sh --prd /home/you/repo/prd/feature/feature.md --issues /home/you/repo/prd/feature/issues/README.md --start-issue 0004
```

Blocked issues are retried after the normal run. Each retry round starts a clean
Codex attempt for one blocked issue at a time and passes only a compact blocker
summary into the coder prompt. Defaults are three retry rounds and one pass per
clean retry.

```powershell
.\bin\devloop.ps1 --prd E:\repo\prd\feature\feature.md --issues E:\repo\prd\feature\issues\README.md --all --blocked-retry-rounds 5
```

Disable blocked retries:

```powershell
.\bin\devloop.ps1 --prd E:\repo\prd\feature\feature.md --issues E:\repo\prd\feature\issues\README.md --all --no-blocked-retry
```

Preview prompts without invoking Codex:

Windows:

```powershell
.\bin\devloop.ps1 --prd E:\repo\prd\feature\feature.md --issues E:\repo\prd\feature\issues\README.md --dry-run --no-worktree
```

Ubuntu/Linux:

```bash
./bin/devloop.sh --prd /home/you/repo/prd/feature/feature.md --issues /home/you/repo/prd/feature/issues/README.md --dry-run --no-worktree
```

The runner creates loop state next to the issue README in the active worktree:

- `README.loop.md`
- `README.loop.state.json`
- `.loop.logs/`

Completed issues are updated in place:

- `Completed: [x]`
- checked acceptance criteria
- appended `## Implementation Notes`

After a real run, the runner reads and updates a Markdown self-improvement wiki
in the Dev Loop bundle by default:

- `docs/devloop-self-improvement/SCHEMA.md`
- `docs/devloop-self-improvement/wiki/index.md`
- `docs/devloop-self-improvement/wiki/lessons-learned.md`

The compiler promotes durable lessons, such as user instructions, implementation
lessons, bugs and fixes, blocked causes, repeated reviewer/QA findings,
environment mistakes, and reusable repo patterns. It skips raw logs and secrets.
Dry runs do not update the wiki.

Disable wiki reading and the post-run memory update:

```powershell
.\bin\devloop.ps1 --prd E:\repo\prd\feature\feature.md --issues E:\repo\prd\feature\issues\README.md --no-self-improvement-wiki
```

Use a different bundle-relative wiki path:

```powershell
.\bin\devloop.ps1 --prd E:\repo\prd\feature\feature.md --issues E:\repo\prd\feature\issues\README.md --self-improvement-wiki-path docs\custom-self-improvement\wiki
```

In interactive mode, development defaults to a dedicated implementation
worktree. After all selected issues pass coder, senior review, and QA gates, the
runner asks whether to merge the implementation branch or worktree into another
branch. It skips automatic merge if either checkout has uncommitted changes.
