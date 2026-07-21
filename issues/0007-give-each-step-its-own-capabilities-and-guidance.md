Label: ready-for-agent

# Give Each Step Its Own Capabilities and Guidance

## Parent

[Configurable Workflow Steps PRD](./configurable-workflow-steps.md)

## Target Product

Product: devloop-plan + devloop

Portable `devloop-plan + devloop`. Implement through the portable catalog,
options editor, prompt assembly, and loop state. Do not modify CodexCLI
component manifests or Context Manifest persistence.

## What to build

Move capability configuration from component-wide options to each Step Instance. Copy component defaults when an instance is created, keep contract-required capabilities locked with an explanation, and preserve the existing searchable/toggleable capability and installer experience for replaceable Skills and Agent References. Two Review instances must be able to use different profiles.

Add optional bounded multiline Step Guidance per instance. Redact or reject secrets before persistence, define and render the precedence that component contracts and execution policy outrank guidance, and include the resolved guidance in every portable role prompt and saved attempt context. Introduce a typed `NEEDS_REVIEW` guidance state so copied or type-changed prose must be deliberately kept, edited, or cleared before Apply.

Covers parent PRD user stories 66-78.

## Acceptance criteria

- [x] Every Step Instance owns an independent capability profile initialized from component defaults.
- [x] Required capabilities are enabled, locked, and accompanied by the component-contract reason.
- [x] Replaceable Skills and Agent References remain searchable, toggleable, installable, resettable, and transactional.
- [x] Two instances of the Review component can persist and execute with different capability profiles.
- [x] Optional multiline Step Guidance is bounded, stored per Step Instance, and included in every portable role prompt and saved attempt context.
- [x] Guidance cannot override component contracts, execution policy, permissions, or output requirements, and the precedence is visible to users.
- [x] Secret-like content is redacted or rejected before workflow configuration is written.
- [x] Copied or type-changed guidance enters the typed `NEEDS_REVIEW` state.
- [x] Apply remains blocked until `NEEDS_REVIEW` guidance is explicitly kept, edited, or cleared.
- [x] Apply, Cancel, Reset Step, snapshot, resume, and prompt/context assembly have automated coverage.
- [x] Existing portable skill/agent installation and role-prompt behavior remain available; CodexCLI component manifests are unchanged.

## Blocked by

- [Issue 0003: Edit and Persist Future Workflow Defaults](./0003-edit-and-persist-future-workflow-defaults.md)

## Implementation Notes

Completed: 2026-07-16T22:12:47+03:00

Capability profiles and bounded guidance are owned by Step Instances, copied
from component defaults, persisted in workflow snapshots and attempt context,
and assembled into role prompts beneath an explicit non-overridable precedence.
Guidance transformations use `NEEDS_REVIEW`, and secret assignment redaction
now fails closed through the full logical line so concatenated quoted fragments
cannot reach configuration, attempt state, or saved prompts.

### Verification

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest -v tests.test_portable_workflow.PortableWorkflowDefinitionTests.test_concatenated_quoted_secret_cannot_reach_configuration_or_context tests.test_codex_runner.RolePromptIdentityTests.test_concatenated_quoted_secret_is_redacted_from_persisted_role_prompt`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_portable_workflow.py' -v`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -q`
- `git diff --check`

### Review

Senior review passed after the quoted-assignment disclosure path was closed.
Authenticated Codex execution remains an operator-only gate.
