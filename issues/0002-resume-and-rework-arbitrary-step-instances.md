Label: ready-for-agent

# Resume and Rework Arbitrary Step Instances

## Parent

[Configurable Workflow Steps PRD](./configurable-workflow-steps.md)

## Target Product

Product: devloop-plan + devloop

Portable `devloop-plan + devloop`. Implement through `cli.py`, `state.py`,
`codex_runner.py`, and portable workflow modules. Do not use CodexCLI Workflow
Runs, RunStore, or application/domain recovery modules.

## What to build

Extend the portable runner so any Step Instance can be resumed, retried, or revisited without fixed coder/reviewer/QA cursor fields. Checkpoint the exact workflow cursor in `*.loop.state.json` by Step Instance ID and optional Issue. Support changes-requested outcomes from either Security Review or Final Review: return to Development, create a new Development attempt, then execute both reviews and QA again according to the configured graph.

Normal inputs must resolve the latest compatible successful artifact. Rework must additionally retain and consume the exact Step Attempt Record that requested the correction. Failed, blocked, and cancelled outputs must not become ordinary inputs. Preserve prior attempts and accumulate timing rather than overwriting evidence.

Covers parent PRD user stories 81, 85, and 87-93.

## Acceptance criteria

- [x] Rerunning the same portable `devloop` command resumes at every Step Instance in the four-step reference workflow using persisted generic loop state.
- [x] Recovery restores the exact Step Instance, Issue, pass, attempt history, and stable workflow structure; a rerun may refresh matching execution preferences before the next attempt.
- [x] Changes requested by either Review instance follow the configured route to Development and then re-enter the successful path.
- [x] The rework attempt receives the exact changes-requested record that triggered it.
- [x] Ordinary downstream bindings resolve the latest compatible successful artifact.
- [x] Failed, blocked, and cancelled outputs are excluded unless a binding explicitly permits them.
- [x] Previous attempts remain inspectable, and elapsed time accumulates across repeated attempts of the same Step Instance.
- [x] Automated recovery tests cover interruption, resume, rework from each Review instance, and invalid artifact selection.
- [x] The behavior is reached through `devloop.sh` and `devloop.ps1` via shared Python code, not CodexCLI `/resume`.

## Blocked by

- [Issue 0001: Run Two Review Instances Through a v2 Workflow](./0001-run-two-review-instances-through-v2.md)

## Implementation Notes

Completed: 2026-07-16T01:23:28

### Changed Files
- `prompts/coder.md`
- `prompts/qa.md`
- `prompts/reviewer.md`
- `src/devloop/cli.py`
- `src/devloop/codex_runner.py`
- `src/devloop/portable_workflow.py`
- `src/devloop/state.py`
- `tests/test_cli_banners.py`
- `tests/test_codex_runner.py`
- `tests/test_portable_workflow.py`

### Verification
- `./bin/devloop.sh --prd /tmp/devloop-issue-0002-smoke/prd.md --issues /tmp/devloop-issue-0002-smoke/issues/README.md --dry-run --no-worktree --no-self-improvement-wiki --non-interactive`
- `PYTHONPATH=src python3 -m unittest discover -s tests`
- `PYTHONPATH=src python3 -m unittest tests.test_portable_workflow tests.test_codex_runner tests.test_cli_banners tests.test_resume`
- `git diff --check`
- `python3 -m compileall -q src tests`

### Review
No blocking findings. Generic step recovery, exact rework-record propagation,
compatible artifact selection, stable workflow structure, resumable execution
preferences, and retained attempt history match the issue requirements.

### QA
All acceptance criteria have sufficient automated coverage. Focused and full suites passed; compilation, diff validation, shared Bash/Python wrapper smoke, persisted workflow snapshot, and per-step prompt identities were verified.
