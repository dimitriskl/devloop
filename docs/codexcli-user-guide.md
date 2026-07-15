# CodexCLI User Guide

Installation, operation, commands, parameters, recovery, and troubleshooting

| Document | Value |
| --- | --- |
| Product | Dev Loop CodexCLI |
| Product version | v0.1.0 |
| Guide version | 1.1 |
| Last updated | 15 July 2026 |
| Implementation baseline | `origin/main` at `3f56dbf` |

> Scope: This guide covers the installable `codexcli` workflow application and the companion `codexcli-gate` verification-evidence command. The older `devloop` and `devloop-plan` wrapper scripts have different parameters and are outside this guide.

<!-- pagebreak -->

## Contents

1. Start here
2. Prerequisites
3. Installation
4. Readiness check with `doctor`
5. Start the application
6. Complete workflow walkthrough
7. Command-line parameters
8. In-application commands
9. Composer and interface controls
10. Capability and execution profiles
11. Workspace and Git behavior
12. Runs, persistence, and recovery
13. Outputs and file locations
14. Security and operational boundaries
15. Troubleshooting
16. Known limitations
17. Quick reference

## 1. Start Here

CodexCLI is a local terminal application that takes a feature request through analysis, PRD creation, workspace preparation, development, independent code review, QA, and final handoff. It uses the installed and authenticated Codex CLI App Server. There is no simulated execution backend.

### 1.1 Five-minute path

Run these commands from the Dev Loop project root. Replace the repository path with the Git repository where the feature will be built.

```text
uv tool install .
codexcli doctor --repo /path/to/repository
codexcli run --repo /path/to/repository
```

If `pipx` is your isolated Python application manager, use this installation command instead:

```text
pipx install .
```

When the launcher opens:

1. Type a clear feature request in the Composer.
2. Press `Ctrl+Enter` to submit it.
3. Answer analysis questions or request changes until the PRD and Issues are correct.
4. Select **Accept** only when the planning package is ready.
5. Choose the current checkout or the proposed dedicated worktree.
6. Optionally inspect or select component execution profiles with `/profile` before each component starts.
7. Respond to Codex approval requests when shown.
8. Allow development, review, and QA to process every Issue.
9. Run `/finalize` when finalization is ready.

> Important: Run `doctor` before the first real workflow and after changing Python, Git, Codex CLI, authentication, terminal, or storage settings.

### 1.2 What CodexCLI changes

CodexCLI may publish an accepted PRD Package under `prd/<feature-slug>/`, create a dedicated local Git branch/worktree when explicitly selected, modify source files during development, write project-local run state under `.devloop/runs/`, and save user-wide capability preferences.

CodexCLI does not automatically merge, push, open a pull request, delete a branch, remove a worktree, or clean up retained runs.

## 2. Prerequisites

Install and verify the following before installing CodexCLI.

| Requirement | Supported or required value | Verification |
| --- | --- | --- |
| Python | 3.10 or newer | `python --version` or `python3 --version` |
| Git | A working command on `PATH` | `git --version` |
| Target repository | Existing directory inside a Git work tree | `git -C /path/to/repository status` |
| Codex CLI | Release documentation targets 0.144.0 or newer; the App Server handshake is the compatibility gate | `codex --version` |
| Authentication | An authenticated Codex CLI session | `codex login` |
| Installer | `uv` or `pipx` | `uv --version` or `pipx --version` |
| Terminal | Interactive standard input and output | Run directly in a terminal, without piping |

Provider, authentication, API-key, and endpoint configuration remain Codex CLI concerns. CodexCLI uses versioned execution profiles to lock the supported model, reasoning effort, timeout, and checkpoint budget for each workflow component. Use `/profile` to inspect or select those profiles; arbitrary runtime values are not accepted by the interactive `codexcli` command.

## 3. Installation

### 3.1 Install from a source checkout

Open a terminal in the Dev Loop project root—the directory containing `pyproject.toml`—and use one isolated installer.

With `uv`:

```text
uv tool install .
```

With `pipx`:

```text
pipx install .
```

Both methods install the `devloop-codexcli` package and expose the interactive `codexcli` command and the companion `codexcli-gate` evidence recorder.

### 3.2 Install a release artifact

Use the wheel or source archive supplied with the release. Examples:

```text
uv tool install ./dist/devloop_codexcli-0.1.0-py3-none-any.whl
pipx install ./dist/devloop_codexcli-0.1.0-py3-none-any.whl
```

### 3.3 Reinstall after an update

From an updated source checkout:

```text
uv tool install --force .
```

or:

```text
pipx install --force .
```

### 3.4 Verify the installed command

```text
codexcli --help
codexcli doctor --help
codexcli run --help
codexcli-gate --help
```

The `codexcli` top-level help must list the `doctor` and `run` commands. `codexcli-gate --help` must list the verification-evidence parameters documented in Section 7.4.

### 3.5 Uninstall

Use the same tool that installed the application:

```text
uv tool uninstall devloop-codexcli
```

or:

```text
pipx uninstall devloop-codexcli
```

Uninstalling the application does not remove project PRD Packages, `.devloop/runs/`, worktrees, branches, or user capability preferences.

## 4. Readiness Check with `doctor`

Run the doctor against the exact target repository:

```text
codexcli doctor --repo /path/to/repository
```

If the terminal is already in the target repository, `--repo` may be omitted:

```text
codexcli doctor
```

The command reports `CodexCLI doctor: READY` or `CodexCLI doctor: NOT READY`, followed by each check.

| Check | What it verifies | Failure action |
| --- | --- | --- |
| Python | Runtime is Python 3.10 or newer | Install a supported Python version and reinstall the tool |
| Git | Git is on `PATH` and returns a usable version | Install or repair Git |
| Repository | `--repo` exists, is a directory, and is inside a Git work tree | Correct the path or initialize Git |
| Codex executable | `codex` is available on `PATH` | Install Codex CLI and update `PATH` |
| Codex version | `codex --version` returns a usable version | Update or reinstall Codex CLI |
| App Server compatibility | The installed stable and experimental App Server schemas satisfy the workflow contract | Update Codex CLI and retry |
| Platform preflight | The real App Server sustains a bounded workspace capability probe on this platform and filesystem | Check the model, filesystem permissions, Git, and any reported Windows ACL handoff, then retry |
| Authentication | Codex reports an authenticated account | Run `codex login` |
| Terminal | Input and output are interactive terminals | Run without a pipe or redirection |
| Storage | Project run, user configuration, and user data locations are writable | Create or grant access to the reported locations |

`doctor` exits with code `0` when no check fails and code `1` when one or more checks fail. A terminal `WARN` is shown as a warning, but interactive use still requires a real terminal.

> Privacy: Doctor output is flattened, length-limited, and redacts common email addresses, user-profile paths, bearer values, passwords, tokens, and API keys.

## 5. Start the Application

Start CodexCLI against the target repository:

```text
codexcli run --repo /path/to/repository
```

From inside that repository, the shorter command is equivalent:

```text
codexcli run
```

The launcher opens idle and does not create or resume a Workflow Run automatically. Its initial prompt is **What do you want to build?**

Type a feature request in any language and press `Ctrl+Enter`. A strong request describes the desired outcome, important constraints, and observable success conditions. For example:

```text
Add an export action to the order history page. Export the filtered rows as UTF-8 CSV,
preserve the visible column order, and add automated tests for empty and populated results.
```

## 6. Complete Workflow Walkthrough

### 6.1 Analysis

Submitting a new feature request creates a Workflow Run and starts real Codex analysis. The Analysis view displays the PRD Draft, Issues, validation findings, clarification requests, and activity.

- Type clarification answers or requested changes in the Composer and press `Ctrl+Enter`.
- Select **Request changes** or type `/request-changes`, then describe the revision.
- Select **Accept** or type `/accept` only when the draft has no validation findings and no clarification remains.

Acceptance atomically publishes:

```text
prd/<feature-slug>/<feature-slug>.md
prd/<feature-slug>/issues/index.json
prd/<feature-slug>/issues/ISSUE-<number>-<slug>.md
```

The App Server returns validated planning content; CodexCLI assigns stable machine identifiers and deterministically renders the Markdown and Issue index. The JSON Issue index is the source of truth. It contains stable Issue ordering, dependencies, Requirement IDs, Acceptance Criterion IDs, filenames, hashes, and the owning Workflow Run ID. An unrelated existing package is never overwritten.

### 6.2 Workspace preparation

After acceptance, CodexCLI displays both workspace choices before repository changes begin.

| Choice | Behavior | Use when |
| --- | --- | --- |
| Current checkout | Uses the target repository and current branch; captures its initial changed-file baseline | The checkout is already isolated and you want changes there |
| Dedicated worktree | Creates the displayed branch and worktree from the current HEAD | You want the run isolated from the current checkout |
| Cancel | Stops workspace preparation without creating the workspace | The proposal is not acceptable |

The default dedicated proposal is:

```text
Branch: devloop/<feature-slug>
Path:   <repository-parent>/worktrees/<repository-name>-<feature-slug>
```

Review the path, branch, and base commit in the UI before selecting a choice. In v0.1.0, the proposed worktree path must not already exist.

Before development begins, CodexCLI runs a permission preflight against the exact canonical workspace root. The parent process proves nested writes, directory enumeration, file hashing, bounded test execution, and clean Git inspection; the real App Server must then prove it can operate within the same workspace boundary. Temporary probe files are removed. If the preflight fails, development does not start. Correct the reported filesystem, Git, model, approval, or Windows ACL issue and choose the workspace again.

### 6.3 Development, review, and QA

Issues are processed sequentially in stable dependency order. One attempt runs at a time in the selected workspace.

Each component uses the execution profile locked into the Workflow Run. The default is `full`; before a component binds its App Server thread, `/profile <component> lightweight` can select the supported lower-reasoning, shorter-budget profile for development, code review, or QA. Analysis supports `full` only. Profile selection changes performance settings, not acceptance criteria, sandbox boundaries, approval policy, or verification requirements.

1. **Development** receives the active Issue, relevant PRD sections, repository constraints, run-locked capabilities, structured prior results, and any immediate rework request. It may edit repository files.
2. **Code review** starts a fresh read-only Codex thread. `MUST_FIX` findings return a structured rework request to development; advisory findings do not block progression.
3. **QA** starts a fresh verification thread. Each acceptance criterion must map to a required QA check. QA may run builds and tests, but it must not change source-controlled files.
4. Review or QA rework creates a fresh development attempt, followed by fresh review and QA.
5. Only successful QA marks an Issue complete.

The standard workflow allows two rework cycles per Issue and one transient backend retry. A blocked independent Issue does not prevent the scheduler from checking other dependency-ready Issues. Bounded execution telemetry records ordered milestones—context loaded, first activity, first file change where applicable, verification started, structured output, and completion—without storing hidden reasoning or full transcripts. Timeout and checkpoint budgets prevent an unresponsive turn from being treated as successful and preserve the recoverable cursor.

### 6.4 Approvals

When the Codex App Server requests approval, CodexCLI shows the requesting step, Issue, parsed action, command family, workspace boundary, policy reason, and only the decisions supported by the backend. Possible decisions include **Accept**, **Accept for session**, **Decline**, and **Cancel request**.

Review the target and reason before approving. Codex remains authoritative for sandbox and approval policy; CodexCLI does not auto-approve requests. Each explicit decision is saved as redacted, versioned evidence containing hashes and classifications rather than the raw command.

### 6.5 Finalization

When every Issue has passed QA, the status indicates that workspace finalization is ready. Type:

```text
/finalize
```

Finalization creates `.devloop/runs/<run-id>/handoff/handoff-summary.json` with completed Issues, verification evidence, changed files, residual risks, approval decisions, locked execution profiles, execution telemetry, the workspace disposition, and workspace path. The workspace is left intact.

## 7. Command-Line Parameters

### 7.1 Syntax

```text
codexcli [-h] {doctor,run} ...
codexcli doctor [-h] [--repo REPO]
codexcli run [-h] [--repo REPO]
```

### 7.2 Complete parameter reference

| Command or parameter | Required | Default | Usage |
| --- | --- | --- | --- |
| `doctor` | Yes, as a subcommand | None | Runs readiness checks and prints an aggregated report |
| `run` | Yes, as a subcommand | None | Opens the interactive launcher without starting work automatically |
| `--repo REPO` | No | Current working directory | Selects the target repository. Relative paths and `~` are resolved to an absolute path |
| `-h`, `--help` | No | Off | Prints help for the top level or selected subcommand and exits |

Examples:

```text
codexcli doctor
codexcli doctor --repo ../my-project
codexcli run --repo ~/code/my-project
codexcli run --help
```

### 7.3 Parameters not provided by v0.1.0

The interactive `codexcli` command does not expose CLI flags for workflow selection, direct run-ID resume, arbitrary model or reasoning values, provider, sandbox, approval policy, worktree path, branch name, retry counts, language, retention, purge, or finalization. Use the contextual UI commands and choices described below. Configure the provider and authentication in Codex CLI, and use `/profile` for CodexCLI's supported component execution profiles.

The legacy `devloop` and `devloop-plan` scripts expose a separate set of flags. Do not pass those flags to `codexcli`.

### 7.4 Verification evidence with `codexcli-gate`

`codexcli-gate` records one immutable JSON evidence manifest for a completed verification gate. It is a non-interactive companion command, not an in-workflow Composer command. The repository must have no tracked, staged, or untracked changes; place logs, generated evidence, and release artifacts in ignored locations such as `.release-evidence/` and `dist/`.

```text
codexcli-gate --tier TIER --repo REPO --output FILE --gate-id ID \
  --status STATUS --duration-ms MILLISECONDS --result-log FILE \
  [--artifact FILE]... [--model MODEL] [--reasoning-effort EFFORT]
```

| Parameter | Required | Default | Usage |
| --- | --- | --- | --- |
| `--tier {fast,vertical,release}` | Yes | None | Identifies the verification tier represented by the manifest |
| `--repo REPO` | No | Current working directory | Selects the existing repository whose clean implementation and Git identity are recorded |
| `--output FILE` | Yes | None | Writes the JSON manifest; the path must remain inside `REPO`, and missing parent directories are created |
| `--gate-id ID` | Yes | None | Supplies the non-empty identifier for this gate result, such as `linux-fast` |
| `--status {PASSED,FAILED,MISSING}` | Yes | None | Records the exact result state; values are uppercase |
| `--duration-ms MILLISECONDS` | Yes | None | Records the gate duration as a non-negative integer |
| `--result-log FILE` | Yes | None | References an existing result log inside `REPO`; the manifest stores its repository-relative path |
| `--artifact FILE` | No; repeatable | None | Adds an existing in-repository artifact, such as a wheel or source archive, to the evidence identity by filename and SHA-256 hash |
| `--model MODEL` | No | `gpt-5.6-sol` | Records the model used to produce the evidence identity; set it only when the gate used a different model |
| `--reasoning-effort EFFORT` | No | `xhigh` | Records the reasoning effort used to produce the evidence identity |
| `-h`, `--help` | No | Off | Prints command help and exits |

Example after a successful 42.5-second fast gate:

```text
codexcli-gate --tier fast --repo . \
  --output .release-evidence/linux-fast.json \
  --gate-id linux-fast --status PASSED --duration-ms 42500 \
  --result-log .release-evidence/linux-fast.log
```

On success, the command prints `RECORDED`, the tier, status, and evidence key. The manifest captures the current commit, Codex version, platform, model, reasoning effort, protocol and probe identities, tracked implementation/component/workflow hashes, optional artifact hashes, and the gate result. It refuses paths that escape the repository, a missing result log, an invalid duration, or a dirty repository.

For the packaged release workflow, prefer the supplied wrappers because they run the selected gate, capture the result log, calculate the duration, and invoke `codexcli-gate` consistently:

```text
./install/run-verification-tier.sh fast
./install/run-verification-tier.sh vertical
./install/run-verification-tier.sh release
```

On Windows PowerShell, use `.\install\run-verification-tier.ps1 -Tier fast`, `vertical`, or `release`.

## 8. In-application Commands

Type `/` at the beginning of a single Composer line to show commands available in the current context. Continue typing to filter the list. Selecting a suggestion inserts the command; it does not execute it. Press `Ctrl+Enter` to submit.

Commands are contextual: global commands are available at the launcher, workflow commands require an active run, and step commands apply only at an appropriate phase. Unsupported context produces an explanatory error.

| Command | Parameters | Context | Effect |
| --- | --- | --- | --- |
| `/resume` | None | Global | Lists unfinished runs for the current project and opens the selected run after validation |
| `/options` | None | Global | Opens transactional, user-wide Skill and Agent Reference profiles |
| `/status` | None | Global | Writes the current typed status-bar value to the activity log |
| `/profile` | Optional `COMPONENT PROFILE` | Global for inspection; active workflow for selection | Lists available profiles before a run, lists the run's selected profiles during a run, or locks a supported profile before that component starts |
| `/language` | Optional `TAG` | Global | Sets the content-language tag for the current launcher session; without a tag, opens a modal |
| `/runs` | None | Active workflow | Lists all current-project runs, including completed and cancelled runs |
| `/issues` | None | Active workflow | Opens or refreshes the read-only Issue Board |
| `/pause` | None | Active workflow | Persists a resumable pause. During an active development/review/QA turn, use `Ctrl+C` and select **Pause run** |
| `/cancel` | None | Active workflow | Opens the stop flow and requires confirmation before permanent cancellation |
| `/retry` | `ISSUE-ID` | Active workflow | Authorizes a new attempt for one blocked Issue |
| `/reset` | `ISSUE-ID` | Active workflow | Resets one failed Issue and authorizes a new attempt |
| `/accept` | None | Analysis | Validates and publishes the current analysis package |
| `/request-changes` | None | Analysis | Focuses the Composer so the requested revisions can be described and submitted |
| `/finalize` | None | Finalization-ready run | Creates the local Handoff Summary and completes the run |

### 8.1 Command parameter formats

`/profile` without parameters lists each profile's component, profile name, model, reasoning effort, timeout, and checkpoint budget. Before a run it lists all installed choices; during a run it lists the choices currently locked into that run. To change one, provide exactly a component ID and profile name while the run is active:

```text
/profile development lightweight
/profile code-review full
/profile qa lightweight
```

Valid component IDs are `analysis`, `development`, `code-review`, and `qa`. Valid profile names are `full` and `lightweight`, but analysis supports `full` only. A component's profile cannot change after it has emitted execution telemetry or bound its App Server thread. An unsupported component/profile combination is rejected.

`/language TAG` accepts a BCP 47-style language tag: 2–8 letters followed by optional hyphen-separated alphanumeric subtags of 1–8 characters. Examples:

```text
/language en
/language el-GR
/language zh-Hans
```

The default launcher value is `en`. The setting is session-local in v0.1.0, and machine identifiers remain stable English tokens. For predictable generated content, write the feature request and clarifications in the desired language as well.

`/retry` and `/reset` require a canonical Issue ID. Lowercase input is normalized to uppercase. Valid examples begin with `ISSUE-` and contain at least three digits:

```text
/retry ISSUE-003
/reset issue-012
```

These commands do not accept a title, filename, run ID, or multiple Issue IDs.

## 9. Composer and Interface Controls

| Control | Effect |
| --- | --- |
| `Ctrl+Enter` | Submit the current feature request, message, or command |
| `Enter` | Insert a new line in the multiline Composer |
| `Ctrl+P` | Load the previous submitted Composer entry |
| `Ctrl+N` | Load the next submitted Composer entry or return to a blank entry |
| `/` at line start | Open and filter the contextual command menu |
| `Ctrl+C` | Open the explicit stop modal; it does not immediately terminate the process |

The stop modal presents only actions valid for the current state:

- **Continue** closes the modal and returns to the workflow.
- **Interrupt turn** interrupts the active Codex turn while preserving the run.
- **Pause run** checkpoints the run and requires explicit `/resume`.
- **Cancel run** requires a second confirmation and makes the run terminal.

Cancellation leaves the workspace and Git topology intact. Cancelled runs cannot be resumed.

The fixed status bar shows Workflow Run status, active step, Issue ID and position, Issue status, attempt, backend activity, and elapsed time. On terminals at least 100 columns wide, the Issue Board remains visible; on narrower terminals, use `/issues`.

## 10. Capability and Execution Profiles

Run `/options` to configure capabilities before starting a new run. The modal supports search, selection, **Reset**, **Apply**, and **Cancel**.

- Required capabilities are visible and locked.
- Default capabilities may be replaced.
- **Apply** atomically saves user-wide preferences.
- **Cancel** discards the current edit session.
- **Reset** restores all components to their defaults.
- A Workflow Run snapshots its resolved profiles at creation; later preference changes do not alter that run or its resume behavior.

### 10.1 Built-in component profiles

| Component | Required and locked | Default selected |
| --- | --- | --- |
| Analysis | `to-prd` | `to-issues` |
| Workspace preparation | None | None |
| Development | `implement` | `tdd` |
| Code review | `senior-code-reviewer` | None |
| QA | `qa-automation-engineer` | None |
| Workspace finalization | `handoff` | None |

The installed catalog also includes the `csharp-expert-developer` and `angular-typescript-developer` Agent References. They can be selected for relevant component profiles.

User preferences are stored in `capability-profiles.json` under the platform-specific CodexCLI configuration directory listed in Section 13.

### 10.2 Built-in execution profiles

Execution profiles are separate from capability profiles. A capability profile selects Skills and Agent References; an execution profile locks the App Server model, reasoning effort, timeout, and checkpoint budget for one workflow component. Every new Workflow Run starts with the `full` profile for each App Server execution component, and the selected profiles are versioned and content-addressed in the run state.

| Component | Profile | Model | Reasoning | Timeout | Checkpoint |
| --- | --- | --- | --- | --- | --- |
| Analysis | `full` | `gpt-5.6-sol` | `xhigh` | 900 seconds | 180 seconds |
| Development | `full` | `gpt-5.6-sol` | `xhigh` | 1,800 seconds | 300 seconds |
| Development | `lightweight` | `gpt-5.6-sol` | `low` | 600 seconds | 120 seconds |
| Code review | `full` | `gpt-5.6-sol` | `xhigh` | 1,800 seconds | 240 seconds |
| Code review | `lightweight` | `gpt-5.6-sol` | `low` | 600 seconds | 120 seconds |
| QA | `full` | `gpt-5.6-sol` | `xhigh` | 1,800 seconds | 240 seconds |
| QA | `lightweight` | `gpt-5.6-sol` | `low` | 600 seconds | 120 seconds |

Use `lightweight` only when the shorter budget and lower reasoning level are appropriate for the Issue. Profile selection never relaxes workflow validation, independent review, acceptance-criterion coverage, source-change protection, approval requirements, or sandbox policy. The Handoff Summary records the exact profiles used.

## 11. Workspace and Git Behavior

CodexCLI always asks before selecting the implementation workspace. A dedicated worktree is a local Git operation that creates `devloop/<feature-slug>` from the accepted run's base commit. The current-checkout option does not create a branch.

Before choosing a workspace:

1. Review `git status` in the target repository.
2. Commit, stash, or consciously retain existing changes.
3. Check that the proposed dedicated path and branch are available.
4. Confirm the base commit shown in the Workspace view.

Development may modify tracked and untracked files in the selected workspace. Code review is read-only. QA may create ignored build output or run artifacts but must not change source-controlled files.

At completion, publish or integrate the workspace manually. Typical follow-up actions—commit, merge, push, pull request creation, branch deletion, and worktree removal—remain outside CodexCLI v0.1.0.

## 12. Runs, Persistence, and Recovery

### 12.1 Run state

Each Workflow Run has a directory under:

```text
<repository>/.devloop/runs/<run-id>/
```

The store uses flushed write-ahead events followed by atomic snapshot replacement. It records the exact workflow step, Issue, attempt, statuses, structured artifact references, component locks, workspace identity and permission profile, execution profiles and bounded telemetry, approval evidence references, and App Server thread/turn references needed for recovery.

Run IDs have this shape:

```text
run-YYYYMMDDtHHMMSS-<12 lowercase hexadecimal characters>
```

Run data is allowlist-based and redacted. It does not intentionally persist hidden reasoning, full transcripts, credentials, authentication data, connection strings, environment dumps, or unbounded tool output.

### 12.2 Pause and resume

To pause safely, use `/pause` during analysis or `Ctrl+C` → **Pause run** during active development, review, or QA. Then restart the launcher if necessary:

```text
codexcli run --repo /path/to/repository
```

Type `/resume`, select the intended unfinished run, and review its feature, workflow, active step, Issue, status, workspace, last activity, and validation result.

CodexCLI validates locked component versions, artifact hashes, workspace state, and the persisted cursor. If the original App Server thread is available, the same attempt resumes. If it is unavailable, CodexCLI offers an explicit transcript-free Recovery Attempt using locked structured context. If validation is unsafe, resume is refused with diagnostics.

An in-flight operation interrupted by shutdown becomes `UNKNOWN` and is never replayed automatically.

### 12.3 Run inspection and retention

- `/resume` lists unfinished runs only.
- `/runs` lists all runs for the current project.
- Runs are project-scoped; pointing `--repo` at another repository shows a different registry.
- Unfinished and completed runs are retained by default.
- v0.1.0 has no purge command or advanced retention UI.
- Do not manually edit a run snapshot, event log, lock, or artifact while a run is active.

## 13. Outputs and File Locations

### 13.1 Project files

| Location | Purpose |
| --- | --- |
| `prd/<feature-slug>/<feature-slug>.md` | Human-readable accepted PRD |
| `prd/<feature-slug>/issues/index.json` | Versioned IssueSet source of truth |
| `prd/<feature-slug>/issues/ISSUE-*.md` | Human-readable, agent-ready Issues |
| `.devloop/runs/<run-id>/events.jsonl` | Flushed write-ahead run events |
| `.devloop/runs/<run-id>/snapshot.json` | Current atomic run snapshot |
| `.devloop/runs/<run-id>/lease.json` | Active-run ownership lease |
| `.devloop/runs/<run-id>/analysis-draft.json` | Unpublished analysis draft |
| `.devloop/runs/<run-id>/context-manifests/` | Minimal attempt input inventories |
| `.devloop/runs/<run-id>/implementation-results/` | Structured implementation results |
| `.devloop/runs/<run-id>/review-results/` | Structured independent review results |
| `.devloop/runs/<run-id>/qa-results/` | Structured QA checks and evidence |
| `.devloop/runs/<run-id>/rework-requests/` | Normalized review and QA corrections |
| `.devloop/runs/<run-id>/approvals/` | Redacted, versioned approval requests and explicit decision evidence |
| `.devloop/runs/<run-id>/handoff/handoff-summary.json` | Final local handoff |
| `.release-evidence/` | Ignored verification logs and `codexcli-gate` manifests produced by the supplied release wrappers |

The run root creates its own ignore rule so project-local run data is not treated as source to publish.

### 13.2 User-wide files

| Platform | Configuration | Data |
| --- | --- | --- |
| Windows | `%APPDATA%\codexcli\` | `%LOCALAPPDATA%\codexcli\` |
| macOS | `~/Library/Application Support/codexcli/` | Same location |
| Linux | `${XDG_CONFIG_HOME:-~/.config}/codexcli/` | `${XDG_DATA_HOME:-~/.local/share}/codexcli/` |

The main v0.1.0 user configuration file is `capability-profiles.json`.

## 14. Security and Operational Boundaries

- CodexCLI invokes only the real installed Codex App Server.
- Codex authentication and provider configuration stay in Codex CLI.
- Installed App Server schemas and a bounded real-backend platform probe must satisfy the workflow contract before use.
- Development starts only after parent-process and real-App-Server permission probes pass for the exact selected workspace root.
- Approval requests remain explicit and Codex policy remains authoritative.
- Approval decisions are stored as bounded, redacted, versioned evidence; raw commands and secrets are not copied into decision artifacts.
- Analysis drafts stay in ignored run storage until acceptance.
- Accepted planning output is hash-locked and owned by its Workflow Run.
- Development receives minimal structured context instead of full prior transcripts.
- Review uses an independent read-only attempt.
- QA verifies acceptance criteria and may not modify source-controlled files.
- Persisted diagnostics and run data use redaction and bounded allowlists.
- Execution telemetry records ordered phase names and elapsed time, not hidden reasoning or full transcripts.
- No implicit remote or destructive Git operation is performed.

Treat the target repository and `.devloop/runs/` as potentially sensitive local material even though secrets are excluded by design. Do not commit run storage, local credentials, or private diagnostic artifacts.

## 15. Troubleshooting

### 15.1 `codexcli` is not found

Confirm the install completed with the selected tool, reopen the terminal if its application path changed, and run:

```text
uv tool list
pipx list
```

Use only the command for the installer you selected. Reinstall with `--force` if the checkout changed.

### 15.2 Doctor reports Codex or authentication failure

```text
codex --version
codex login
codexcli doctor --repo /path/to/repository
```

If App Server initialization still fails, update or reinstall Codex CLI. Do not start a real workflow until the handshake passes.

### 15.3 Doctor reports repository failure

Confirm that the path exists and is inside a Git work tree:

```text
git -C /path/to/repository rev-parse --is-inside-work-tree
```

Pass the repository directory—not a PRD file, Issue file, or nonexistent future worktree—to `--repo`.

### 15.4 Doctor reports terminal warning

Run CodexCLI directly in a normal interactive terminal. Do not pipe output to another process or redirect standard input/output for an interactive run.

### 15.5 Doctor reports storage failure

Grant the current user write access to the reported project run, configuration, or data directory. Then rerun `doctor`. Do not work around the check by running the whole workflow as an administrator unless that is your normal, reviewed environment.

### 15.6 Analysis cannot be accepted

Resolve the validation findings shown in the Analysis view. A valid package requires stable PRD markers, unique Requirement and Issue IDs, matching requirement coverage, valid dependencies, no cycles, valid Acceptance Criterion IDs, and complete Issue content. Use `/request-changes`, submit the correction, and accept again.

If `prd/<feature-slug>/` already exists and belongs to another run or has different hashes, CodexCLI refuses to overwrite it. Revise the feature slug through analysis or resolve the conflicting directory outside the application.

### 15.7 Dedicated worktree creation fails

Check the displayed path and branch:

```text
git -C /path/to/repository worktree list
git -C /path/to/repository branch --list "devloop/*"
```

The v0.1.0 proposal requires a new path and branch. Remove or rename stale resources manually only after confirming they are no longer needed, or choose the current checkout.

### 15.8 A run was interrupted

Start `codexcli run` for the same repository, type `/resume`, and select the exact unfinished run. Do not manually rerun an unknown command or edit the saved cursor. Follow the Recovery Attempt prompt if the original App Server thread is unavailable.

### 15.9 An Issue is blocked or failed

Inspect the Issue Board and activity. After addressing the external cause, use exactly one canonical ID:

```text
/retry ISSUE-003
/reset ISSUE-004
```

`/retry` applies only to blocked Issues; `/reset` applies only to failed Issues. Dependency and lifecycle validation still applies.

### 15.10 An execution profile cannot be selected

Run `/profile` to inspect the current choices. Selection requires an active, non-terminal run and exactly two parameters:

```text
/profile development lightweight
```

Use the machine component IDs `analysis`, `development`, `code-review`, or `qa`. Analysis has no `lightweight` profile. If the component has already emitted telemetry or bound an App Server thread, its profile is intentionally locked for reproducibility and cannot be changed in that run.

### 15.11 `codexcli-gate` refuses to record evidence

Check the following before retrying:

1. `--repo` points to an existing Git repository.
2. `git status --short --untracked-files=all` prints nothing; evidence, logs, build artifacts, and caches belong in ignored locations.
3. `--output`, `--result-log`, and every `--artifact` stay inside the repository.
4. The result log and every optional artifact already exist.
5. `--duration-ms` is a non-negative integer and the tier/status values use the documented spelling and case.
6. `codex --version` succeeds because its value is part of the evidence provenance.

For normal release verification, use `install/run-verification-tier.sh` or `install/run-verification-tier.ps1` so capture and recording remain consistent.

## 16. Known Limitations

- Third-party executable Step Component installation is deferred.
- GitHub installation of Skills or Agent References is not available inside CodexCLI v0.1.0.
- Legacy PRDs, issue packs, sessions, and run state are not imported or migrated.
- Scheduling is sequential in one selected workspace.
- The interactive `codexcli` CLI exposes only `doctor`, `run`, `--repo`, and help; component profiles are selected in the application, and advanced workflow flags are not implemented. `codexcli-gate` is a separate evidence-recording executable.
- Advanced retention and purge controls are deferred.
- Merge, push, pull request creation, branch deletion, and worktree removal are manual.
- The Composer exposes the verified controls in Section 9; `@` file search and attachment commands are not part of the v0.1.0 user interface.
- Content-language selection is session-local; machine identifiers and protocol tokens remain English.
- Terminal rendering depends on the terminal's Unicode, font, width, and input-method support.

## 17. Quick Reference

### Install, verify, run

```text
uv tool install .
codex --version
codex login
codexcli --help
codexcli-gate --help
codexcli doctor --repo /path/to/repository
codexcli run --repo /path/to/repository
```

### Most-used in-app commands

```text
/options
/profile
/profile development lightweight
/status
/issues
/runs
/pause
/resume
/finalize
```

### Recovery and lifecycle

| Goal | Action |
| --- | --- |
| Interrupt the active turn | `Ctrl+C` → **Interrupt turn** |
| Preserve and leave the run | `Ctrl+C` → **Pause run** |
| Resume later | Start the same repository, run `/resume`, select the run |
| Retry a blocked Issue | `/retry ISSUE-003` |
| Reset a failed Issue | `/reset ISSUE-003` |
| Permanently cancel | `/cancel`, then confirm |
| Complete local handoff | `/finalize` |

### Safe completion checklist

1. Confirm every Issue is `COMPLETED` after QA.
2. Run `/finalize` and review the Handoff Summary.
3. Inspect the workspace with `git status` and your normal test commands.
4. Commit, merge, push, or open a pull request manually according to repository policy.
5. Retain or remove the branch, worktree, and run data deliberately; CodexCLI does not clean them automatically.
