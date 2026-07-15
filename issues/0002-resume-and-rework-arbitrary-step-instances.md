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

- [ ] Rerunning the same portable `devloop` command resumes at every Step Instance in the four-step reference workflow using persisted generic loop state.
- [ ] Recovery restores the exact Step Instance, Issue, pass, attempt history, and immutable workflow snapshot.
- [ ] Changes requested by either Review instance follow the configured route to Development and then re-enter the successful path.
- [ ] The rework attempt receives the exact changes-requested record that triggered it.
- [ ] Ordinary downstream bindings resolve the latest compatible successful artifact.
- [ ] Failed, blocked, and cancelled outputs are excluded unless a binding explicitly permits them.
- [ ] Previous attempts remain inspectable, and elapsed time accumulates across repeated attempts of the same Step Instance.
- [ ] Automated recovery tests cover interruption, resume, rework from each Review instance, and invalid artifact selection.
- [ ] The behavior is reached through `devloop.sh` and `devloop.ps1` via shared Python code, not CodexCLI `/resume`.

## Blocked by

- [Issue 0001: Run Two Review Instances Through a v2 Workflow](./0001-run-two-review-instances-through-v2.md)
