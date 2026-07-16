from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from devloop.codex_runner import RoleResult
from devloop.issue_pack import Issue
from devloop.portable_workflow import (
    DEVELOPMENT_STEP_ID,
    FINAL_REVIEW_STEP_ID,
    IMPLEMENTATION_RESULT_CONTRACT,
    QA_STEP_ID,
    SECURITY_REVIEW_STEP_ID,
    REVIEWER_COMPONENT_ID,
    IssueStatus,
    PORTABLE_WORKFLOW_SCHEMA,
    QA_RESULT_CONTRACT,
    PortableRoleAdapter,
    PortableStepComponent,
    PortableStepComponentCatalog,
    PortableWorkflowExecutor,
    PortableWorkflowCheckpoint,
    StepComponentId,
    StepOutcome,
    StepScope,
    StepRuntimeState,
    StepRuntimeStatus,
    canonical_workflow_hash,
    default_portable_component_catalog,
    default_portable_workflow,
    load_portable_workflow,
    resolve_portable_inputs,
    step_attempt_record_to_dict,
    validate_portable_workflow_for_apply,
)
from devloop.state import LoopStateWriter
from devloop.step_configuration import MAX_STEP_GUIDANCE_CHARACTERS, StepGuidance
from devloop.statusui import project_workflow_step_progress, render_step_progress_rows
from devloop.workflow_defaults import WorkflowDefaultStore


def _persist_guidance_surfaces(guidance_text: str) -> tuple[str, str]:
    class PassingRoleRunner:
        def run_role(self, **_arguments: object) -> RoleResult:
            return RoleResult(status="PASS")

    document = default_portable_workflow().to_dict()
    security_review = next(
        step
        for step in document["steps"]
        if step["instance_id"] == SECURITY_REVIEW_STEP_ID
    )
    security_review["guidance"] = {
        "text": guidance_text,
        "review_state": "READY",
    }
    catalog = default_portable_component_catalog()
    workflow = load_portable_workflow(document, catalog)

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        configuration_path = root / "devloop-plan.json"
        issue_path = root / "0001.md"
        issue_path.write_text("# Secret persistence regression\n", encoding="utf-8")
        index = root / "README.md"
        index.write_text("[Secret persistence regression](./0001.md)\n", encoding="utf-8")
        issue = Issue("0001", "Secret persistence regression", issue_path, False)
        WorkflowDefaultStore(configuration_path, catalog).replace(workflow)
        execution = PortableWorkflowExecutor(workflow, catalog, PassingRoleRunner()).run(
            issue,
            pass_number=1,
        )
        writer = LoopStateWriter(index)
        writer.record_resolved_workflow(workflow, catalog)
        writer.record_portable_execution_result(issue, execution)
        state = json.loads(writer.state_path.read_text(encoding="utf-8"))
        attempt_context = state["step_attempt_records"][
            str(SECURITY_REVIEW_STEP_ID)
        ][issue.number][0]["attempt_context"]
        return (
            configuration_path.read_text(encoding="utf-8"),
            json.dumps(attempt_context),
        )


class PortableWorkflowDefinitionTests(unittest.TestCase):
    def test_loader_rejects_an_unreachable_step_with_actionable_transition_error(
        self,
    ) -> None:
        document = default_portable_workflow().to_dict()
        document["steps"].append(
            {
                "instance_id": "54de2be8-1d79-4c8a-b347-b1a2894def43",
                "display_name": "Detached Review",
                "component_id": "devloop.reviewer",
                "transitions": {
                    "SUCCEEDED": None,
                    "CHANGES_REQUESTED": None,
                    "BLOCKED": None,
                    "FAILED": None,
                    "CANCELLED": None,
                },
                "input_bindings": {
                    "implementation": {
                        "producer_step_id": str(DEVELOPMENT_STEP_ID),
                        "output_port": "implementation",
                    }
                },
            }
        )

        with self.assertRaisesRegex(
            ValueError,
            "Detached Review.*54de2be8.*unreachable.*Outcome Transition",
        ):
            load_portable_workflow(document, default_portable_component_catalog())

    def test_loader_requires_an_explicit_successful_terminal_path(self) -> None:
        builtin_catalog = default_portable_component_catalog()
        blocker_component = PortableStepComponent(
            component_id=StepComponentId("example.blocker"),
            default_display_name="Blocker",
            scope=StepScope.ISSUE,
            supported_outcomes=frozenset({StepOutcome.BLOCKED}),
            adapter=PortableRoleAdapter("coder"),
        )
        catalog = PortableStepComponentCatalog(
            (*builtin_catalog.components, blocker_component)
        )
        blocker_id = "3d0eab2d-a815-4f89-93dc-27cf8f815342"
        document = {
            "schema": PORTABLE_WORKFLOW_SCHEMA,
            "start_step_id": blocker_id,
            "steps": [
                {
                    "instance_id": blocker_id,
                    "display_name": "Blocker",
                    "component_id": "example.blocker",
                    "transitions": {"BLOCKED": None},
                    "input_bindings": {},
                }
            ],
        }

        with self.assertRaisesRegex(
            ValueError,
            "Blocker.*SUCCEEDED transition.*successful terminal",
        ):
            load_portable_workflow(document, catalog)

    def test_apply_requires_every_declared_outcome_to_have_an_explicit_route(
        self,
    ) -> None:
        document = default_portable_workflow().to_dict()
        development = next(
            step
            for step in document["steps"]
            if step["instance_id"] == DEVELOPMENT_STEP_ID
        )
        development["transitions"].pop("BLOCKED")

        with self.assertRaisesRegex(
            ValueError,
            "Development.*Outcome Transition.*BLOCKED.*explicit terminal",
        ):
            catalog = default_portable_component_catalog()
            validate_portable_workflow_for_apply(
                load_portable_workflow(document, catalog),
                catalog,
            )

    def test_loader_rejects_issue_output_bound_into_workflow_scope(self) -> None:
        builtin_catalog = default_portable_component_catalog()
        summary_component = PortableStepComponent(
            component_id=StepComponentId("example.workflow-summary"),
            default_display_name="Workflow Summary",
            scope=StepScope.WORKFLOW,
            supported_outcomes=frozenset({StepOutcome.SUCCEEDED}),
            adapter=PortableRoleAdapter("reviewer"),
            input_ports={"implementation": IMPLEMENTATION_RESULT_CONTRACT},
        )
        catalog = PortableStepComponentCatalog(
            (*builtin_catalog.components, summary_component)
        )
        document = default_portable_workflow().to_dict()
        analysis = document["steps"][0]
        development = next(
            step
            for step in document["steps"]
            if step["instance_id"] == DEVELOPMENT_STEP_ID
        )
        summary_id = "7630e52d-f84e-466a-b889-a30b58b4954d"
        development["transitions"]["SUCCEEDED"] = summary_id
        document["steps"] = [
            analysis,
            development,
            {
                "instance_id": summary_id,
                "display_name": "Workflow Summary",
                "component_id": "example.workflow-summary",
                "transitions": {"SUCCEEDED": None},
                "input_bindings": {
                    "implementation": {
                        "producer_step_id": str(DEVELOPMENT_STEP_ID),
                        "output_port": "implementation",
                    }
                },
            },
        ]

        with self.assertRaisesRegex(
            ValueError,
            "Workflow Summary.*7630e52d.*implementation.*scope",
        ):
            load_portable_workflow(document, catalog)

    def test_loader_rejects_branch_binding_to_a_later_producer(self) -> None:
        document = default_portable_workflow().to_dict()
        steps = {step["instance_id"]: step for step in document["steps"]}
        branch_review_id = "5cd93486-2a72-4020-bb61-efda6b8500c8"
        later_development_id = "4866bbc4-77ce-48df-93b3-4363d266e17d"
        steps[str(SECURITY_REVIEW_STEP_ID)]["transitions"]["BLOCKED"] = (
            branch_review_id
        )
        document["steps"].extend(
            [
                {
                    "instance_id": branch_review_id,
                    "display_name": "Branch Review",
                    "component_id": "devloop.reviewer",
                    "transitions": {
                        "SUCCEEDED": later_development_id,
                        "CHANGES_REQUESTED": None,
                        "BLOCKED": None,
                        "FAILED": None,
                        "CANCELLED": None,
                    },
                    "input_bindings": {
                        "implementation": {
                            "producer_step_id": later_development_id,
                            "output_port": "implementation",
                        }
                    },
                },
                {
                    "instance_id": later_development_id,
                    "display_name": "Later Development",
                    "component_id": "devloop.development",
                    "transitions": {
                        "SUCCEEDED": None,
                        "BLOCKED": None,
                        "FAILED": None,
                        "CANCELLED": None,
                    },
                    "input_bindings": {},
                },
            ]
        )

        with self.assertRaisesRegex(
            ValueError,
            "Branch Review.*5cd93486.*implementation.*every executable path",
        ):
            load_portable_workflow(document, default_portable_component_catalog())

    def test_default_workflow_has_two_independent_reviewer_instances(self) -> None:
        workflow = default_portable_workflow()

        review_steps = [
            step
            for step in workflow.steps
            if step.component_id == REVIEWER_COMPONENT_ID
        ]

        self.assertEqual(workflow.schema, PORTABLE_WORKFLOW_SCHEMA)
        self.assertEqual(
            [step.display_name for step in workflow.primary_path()],
            ["Analysis", "Development", "Security Review", "Final Review", "QA"],
        )
        self.assertEqual(len(review_steps), 2)
        self.assertNotEqual(review_steps[0].instance_id, review_steps[1].instance_id)

    def test_duplicate_component_instances_round_trip_distinct_capability_profiles(
        self,
    ) -> None:
        catalog = default_portable_component_catalog()
        document = default_portable_workflow().to_dict()
        steps = {step["instance_id"]: step for step in document["steps"]}
        security_profile = steps[str(SECURITY_REVIEW_STEP_ID)][
            "capability_profile"
        ]
        final_profile = steps[str(FINAL_REVIEW_STEP_ID)]["capability_profile"]

        security_profile["agent_references"] = []
        workflow = load_portable_workflow(document, catalog)

        self.assertEqual(
            workflow.step(SECURITY_REVIEW_STEP_ID).capability_profile.agent_references,
            (),
        )
        self.assertEqual(
            workflow.step(FINAL_REVIEW_STEP_ID).capability_profile.agent_references,
            tuple(final_profile["agent_references"]),
        )

    def test_step_guidance_redacts_common_secret_forms_before_persistence(self) -> None:
        document = default_portable_workflow().to_dict()
        security_review = next(
            step
            for step in document["steps"]
            if step["instance_id"] == SECURITY_REVIEW_STEP_ID
        )
        secret_values = (
            "aws-secret-value",
            "github-secret-value",
            "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
            "yaml-secret-value",
            "quoted secret value",
            "multiline-quoted-one",
            "multiline-quoted-two",
            "multiline-secret-one",
            "multiline-secret-two",
        )
        security_review["guidance"] = {
            "text": (
                "Check authentication.\n"
                "AWS_SECRET_ACCESS_KEY=aws-secret-value\n"
                "github_token=github-secret-value\n"
                "A GitHub credential is ghp_abcdefghijklmnopqrstuvwxyz1234567890\n"
                "password: yaml-secret-value\n"
                '"client_secret": "quoted secret value"\n'
                'refresh_token = """multiline-quoted-one\n'
                'multiline-quoted-two"""\n'
                "private_key: |\n"
                "  multiline-secret-one\n"
                "  multiline-secret-two\n"
                "Keep this safe instruction."
            ),
            "review_state": "READY",
        }

        workflow = load_portable_workflow(
            document,
            default_portable_component_catalog(),
        )

        guidance = workflow.step(SECURITY_REVIEW_STEP_ID).guidance
        assert guidance is not None
        self.assertIn("Check authentication.", guidance.text)
        self.assertIn("Keep this safe instruction.", guidance.text)
        self.assertGreaterEqual(guidance.text.count("[redacted]"), 5)
        serialized_workflow = json.dumps(workflow.to_dict())
        for secret in secret_values:
            self.assertNotIn(secret, serialized_workflow)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).replace(workflow)
            persisted = path.read_text(encoding="utf-8")
            for secret in secret_values:
                self.assertNotIn(secret, persisted)

    def test_escaped_quoted_secret_cannot_reach_persisted_configuration_or_context(
        self,
    ) -> None:
        secret_fragments = ("alpha", "omega")
        persisted_surfaces = _persist_guidance_surfaces(
            'Check JSON input: "client_secret": "alpha\\"omega".',
        )

        for persisted in persisted_surfaces:
            self.assertIn("Check JSON input:", persisted)
            self.assertIn("[redacted]", persisted)
            for fragment in secret_fragments:
                self.assertNotIn(fragment, persisted)

    def test_block_secret_after_blank_line_cannot_reach_configuration_or_context(
        self,
    ) -> None:
        secret_values = ("first-secret", "second-secret")
        persisted_surfaces = _persist_guidance_surfaces(
            (
                "private_key: |\n"
                "  first-secret\n"
                "\n"
                "  second-secret\n"
                "Keep this safe instruction."
            )
        )

        for persisted in persisted_surfaces:
            self.assertIn("Keep this safe instruction.", persisted)
            self.assertIn("[redacted]", persisted)
            for secret in secret_values:
                self.assertNotIn(secret, persisted)

    def test_apply_blocks_needs_review_guidance_until_it_is_resolved(self) -> None:
        document = default_portable_workflow().to_dict()
        security_review = next(
            step
            for step in document["steps"]
            if step["instance_id"] == SECURITY_REVIEW_STEP_ID
        )
        security_review["guidance"] = {
            "text": "Copied review instructions",
            "review_state": "NEEDS_REVIEW",
        }
        catalog = default_portable_component_catalog()
        workflow = load_portable_workflow(document, catalog)

        with self.assertRaisesRegex(
            ValueError,
            "Security Review.*NEEDS_REVIEW.*keep, edit, or clear",
        ):
            validate_portable_workflow_for_apply(workflow, catalog)

    def test_step_guidance_rejects_text_over_the_persistence_bound(self) -> None:
        document = default_portable_workflow().to_dict()
        security_review = next(
            step
            for step in document["steps"]
            if step["instance_id"] == SECURITY_REVIEW_STEP_ID
        )
        security_review["guidance"] = {
            "text": "x" * (MAX_STEP_GUIDANCE_CHARACTERS + 1),
            "review_state": "READY",
        }

        with self.assertRaisesRegex(ValueError, "cannot exceed 4000 characters"):
            load_portable_workflow(
                document,
                default_portable_component_catalog(),
            )

    def test_v2_loader_rejects_identity_type_and_scope_contract_violations(self) -> None:
        catalog = default_portable_component_catalog()
        valid = default_portable_workflow().to_dict()

        invalid_cases = {
            "devloop.portable-workflow/v2": (
                "schema",
                "devloop.portable-workflow/v1",
            ),
            "canonical UUIDv4": ("start_step_id", "not-a-uuid"),
            "unique display names": (
                "steps.1.display_name",
                valid["steps"][0]["display_name"],
            ),
            "not installed": ("steps.1.component_id", "example.missing"),
            "component-owned": ("steps.1.scope", "WORKFLOW"),
        }

        for expected, (path, value) in invalid_cases.items():
            with self.subTest(path=path):
                document = deepcopy(valid)
                target = document
                segments = path.split(".")
                for segment in segments[:-1]:
                    target = target[int(segment)] if segment.isdigit() else target[segment]
                target[segments[-1]] = value

                with self.assertRaisesRegex(ValueError, expected):
                    load_portable_workflow(document, catalog)

    def test_v2_loader_rejects_display_names_with_rendering_controls(self) -> None:
        catalog = default_portable_component_catalog()

        for display_name in (
            "Injected\nHeading",
            "\x1b[2JInjected",
            "Injected\u2028Heading",
        ):
            with self.subTest(display_name=repr(display_name)):
                document = default_portable_workflow().to_dict()
                document["steps"][1]["display_name"] = display_name

                with self.assertRaisesRegex(
                    ValueError,
                    "control characters or line breaks",
                ):
                    load_portable_workflow(document, catalog)

    def test_v2_loader_preserves_rtl_joining_and_joined_emoji_display_names(
        self,
    ) -> None:
        document = default_portable_workflow().to_dict()
        display_name = "بررسی می\u200cروم 👩\u200d💻"
        document["steps"][1]["display_name"] = display_name

        workflow = load_portable_workflow(
            document,
            default_portable_component_catalog(),
        )

        self.assertEqual(workflow.steps[1].display_name, display_name)

    def test_portable_models_enforce_safe_display_names_at_construction(self) -> None:
        workflow = default_portable_workflow()
        component = default_portable_component_catalog().resolve(
            REVIEWER_COMPONENT_ID
        )

        with self.assertRaisesRegex(ValueError, "control characters or line breaks"):
            replace(component, default_display_name="\x1b[2JInjected")
        with self.assertRaisesRegex(ValueError, "control characters or line breaks"):
            replace(
                workflow.step(SECURITY_REVIEW_STEP_ID),
                display_name="Injected\nHeading",
            )

    def test_qa_binding_selects_the_typed_final_review_result(self) -> None:
        workflow = default_portable_workflow()

        review_binding = workflow.step(QA_STEP_ID).input_bindings["review_result"]

        self.assertEqual(review_binding.producer_step_id, FINAL_REVIEW_STEP_ID)
        document = workflow.to_dict()
        qa_document = next(
            step for step in document["steps"] if step["instance_id"] == QA_STEP_ID
        )
        qa_document["input_bindings"]["review_result"]["producer_step_id"] = str(
            DEVELOPMENT_STEP_ID
        )
        qa_document["input_bindings"]["review_result"]["output_port"] = "implementation"
        with self.assertRaisesRegex(ValueError, "incompatible"):
            load_portable_workflow(document, default_portable_component_catalog())

    def test_loader_rejects_a_binding_to_a_downstream_primary_path_producer(
        self,
    ) -> None:
        document = default_portable_workflow().to_dict()
        steps = {step["instance_id"]: step for step in document["steps"]}
        steps[str(DEVELOPMENT_STEP_ID)]["transitions"]["SUCCEEDED"] = str(QA_STEP_ID)
        steps[str(QA_STEP_ID)]["transitions"]["SUCCEEDED"] = str(
            SECURITY_REVIEW_STEP_ID
        )
        steps[str(SECURITY_REVIEW_STEP_ID)]["transitions"]["SUCCEEDED"] = str(
            FINAL_REVIEW_STEP_ID
        )
        steps[str(FINAL_REVIEW_STEP_ID)]["transitions"]["SUCCEEDED"] = None

        with self.assertRaisesRegex(ValueError, "every executable path"):
            load_portable_workflow(document, default_portable_component_catalog())

    def test_workflow_does_not_require_any_builtin_phase_list(self) -> None:
        builtin_catalog = default_portable_component_catalog()
        custom_component = PortableStepComponent(
            component_id=StepComponentId("example.local-check"),
            default_display_name="Local Check",
            scope=StepScope.ISSUE,
            supported_outcomes=frozenset({StepOutcome.SUCCEEDED}),
            adapter=PortableRoleAdapter("coder"),
        )
        catalog = PortableStepComponentCatalog(
            (*builtin_catalog.components, custom_component)
        )
        document = default_portable_workflow().to_dict()
        replacement = document["steps"][0]
        replacement["display_name"] = "Local Check"
        replacement["component_id"] = "example.local-check"
        replacement["transitions"] = {StepOutcome.SUCCEEDED.value: None}
        replacement["input_bindings"] = {}
        document["steps"] = [replacement]

        workflow = load_portable_workflow(document, catalog)

        self.assertEqual(
            [step.display_name for step in workflow.primary_path()],
            ["Local Check"],
        )


class PortableWorkflowExecutionTests(unittest.TestCase):
    def test_each_step_instance_can_be_interrupted_and_resumed(self) -> None:
        expected_steps = [
            "Development",
            "Security Review",
            "Final Review",
            "QA",
        ]

        for interrupted_step in expected_steps:
            with self.subTest(step=interrupted_step), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                issue_path = root / "0001.md"
                issue_path.write_text("# Portable workflow\n", encoding="utf-8")
                index = root / "README.md"
                index.write_text("[Portable workflow](./0001.md)\n", encoding="utf-8")
                issue = Issue("0001", "Portable workflow", issue_path, False)
                workflow = default_portable_workflow()
                catalog = default_portable_component_catalog()
                writer = LoopStateWriter(index)
                writer.record_resolved_workflow(workflow, catalog)

                class InterruptingRoleRunner:
                    def run_role(self, **arguments: object) -> RoleResult:
                        if arguments["step_display_name"] == interrupted_step:
                            raise RuntimeError("simulated interruption")
                        return RoleResult(status="PASS")

                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    PortableWorkflowExecutor(
                        workflow,
                        catalog,
                        InterruptingRoleRunner(),
                    ).run(
                        issue,
                        pass_number=1,
                        checkpoint=lambda checkpoint: writer.record_portable_checkpoint(
                            issue,
                            checkpoint,
                        ),
                    )

                restored = LoopStateWriter(index)
                recovery = restored.resume_portable_workflow(issue, workflow)
                self.assertIsNotNone(recovery)
                assert recovery is not None
                resumed_calls: list[str] = []

                class PassingRoleRunner:
                    def run_role(self, **arguments: object) -> RoleResult:
                        resumed_calls.append(str(arguments["step_display_name"]))
                        return RoleResult(status="PASS")

                result = PortableWorkflowExecutor(
                    restored.resolved_workflow(catalog),
                    catalog,
                    PassingRoleRunner(),
                ).run(
                    issue,
                    pass_number=1,
                    recovery=recovery,
                )

                interrupted_position = expected_steps.index(interrupted_step)
                self.assertEqual(resumed_calls, expected_steps[interrupted_position:])
                self.assertEqual(result.issue_status, IssueStatus.COMPLETED)
                self.assertEqual(result.attempts[:interrupted_position], recovery.attempts)
                self.assertEqual(recovery.pass_number, 1)
                self.assertEqual(recovery.issue_id, issue.number)

    def test_each_blocked_step_instance_can_be_retried_in_place(self) -> None:
        expected_steps = [
            "Development",
            "Security Review",
            "Final Review",
            "QA",
        ]

        for blocked_step in expected_steps:
            with self.subTest(step=blocked_step), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                issue_path = root / "0001.md"
                issue_path.write_text("# Portable workflow\n", encoding="utf-8")
                index = root / "README.md"
                index.write_text("[Portable workflow](./0001.md)\n", encoding="utf-8")
                issue = Issue("0001", "Portable workflow", issue_path, False)
                workflow = default_portable_workflow()
                catalog = default_portable_component_catalog()
                writer = LoopStateWriter(index)
                writer.record_resolved_workflow(workflow, catalog)

                class BlockedRoleRunner:
                    def run_role(self, **arguments: object) -> RoleResult:
                        if arguments["step_display_name"] == blocked_step:
                            return RoleResult(
                                status="BLOCKED",
                                summary="temporary external blocker",
                            )
                        return RoleResult(status="PASS")

                blocked = PortableWorkflowExecutor(
                    workflow,
                    catalog,
                    BlockedRoleRunner(),
                ).run(
                    issue,
                    pass_number=1,
                    checkpoint=lambda checkpoint: writer.record_portable_checkpoint(
                        issue,
                        checkpoint,
                    ),
                )
                self.assertEqual(blocked.issue_status, IssueStatus.BLOCKED)

                restored = LoopStateWriter(index)
                retry = restored.retry_portable_workflow(issue, workflow)
                retried_calls: list[str] = []

                class PassingRoleRunner:
                    def run_role(self, **arguments: object) -> RoleResult:
                        retried_calls.append(str(arguments["step_display_name"]))
                        return RoleResult(status="PASS")

                result = PortableWorkflowExecutor(
                    restored.resolved_workflow(catalog),
                    catalog,
                    PassingRoleRunner(),
                ).run(
                    issue,
                    pass_number=1,
                    recovery=retry,
                )

                blocked_position = expected_steps.index(blocked_step)
                self.assertEqual(retried_calls, expected_steps[blocked_position:])
                self.assertEqual(result.issue_status, IssueStatus.COMPLETED)
                self.assertIn(
                    StepOutcome.BLOCKED,
                    [attempt.outcome for attempt in result.attempts],
                )

    def test_interrupted_final_review_resumes_from_the_persisted_step_cursor(self) -> None:
        class InterruptingRoleRunner:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def run_role(self, **arguments: object) -> RoleResult:
                display_name = str(arguments["step_display_name"])
                self.calls.append(display_name)
                if display_name == "Final Review":
                    raise RuntimeError("simulated interruption")
                return RoleResult(status="PASS", summary=display_name)

        class PassingRoleRunner:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def run_role(self, **arguments: object) -> RoleResult:
                display_name = str(arguments["step_display_name"])
                self.calls.append(display_name)
                return RoleResult(status="PASS", summary=display_name)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Portable workflow\n", encoding="utf-8")
            index = root / "README.md"
            index.write_text("[Portable workflow](./0001.md)\n", encoding="utf-8")
            issue = Issue("0001", "Portable workflow", issue_path, False)
            workflow = default_portable_workflow()
            catalog = default_portable_component_catalog()
            writer = LoopStateWriter(index)
            writer.record_resolved_workflow(workflow, catalog)
            interrupted_runner = InterruptingRoleRunner()

            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                PortableWorkflowExecutor(workflow, catalog, interrupted_runner).run(
                    issue,
                    pass_number=1,
                    checkpoint=lambda checkpoint: writer.record_portable_checkpoint(
                        issue,
                        checkpoint,
                    ),
                )

            restored = LoopStateWriter(index)
            recovery = restored.resume_portable_workflow(issue, workflow)
            resumed_runner = PassingRoleRunner()
            execution = PortableWorkflowExecutor(
                restored.resolved_workflow(catalog),
                catalog,
                resumed_runner,
            ).run(
                issue,
                pass_number=1,
                recovery=recovery,
                checkpoint=lambda checkpoint: restored.record_portable_checkpoint(
                    issue,
                    checkpoint,
                ),
            )

        self.assertEqual(
            interrupted_runner.calls,
            ["Development", "Security Review", "Final Review"],
        )
        self.assertEqual(resumed_runner.calls, ["Final Review", "QA"])
        self.assertEqual(execution.issue_status, IssueStatus.COMPLETED)
        self.assertEqual(
            [attempt.step_instance_id for attempt in execution.attempts],
            [
                DEVELOPMENT_STEP_ID,
                SECURITY_REVIEW_STEP_ID,
                FINAL_REVIEW_STEP_ID,
                QA_STEP_ID,
            ],
        )

    def test_interrupted_attempt_identity_remains_inspectable_after_resume(self) -> None:
        class InterruptingFinalReviewRunner:
            def run_role(self, **arguments: object) -> RoleResult:
                if arguments["step_display_name"] == "Final Review":
                    raise RuntimeError("simulated interruption")
                return RoleResult(status="PASS")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Portable workflow\n", encoding="utf-8")
            index = root / "README.md"
            index.write_text("[Portable workflow](./0001.md)\n", encoding="utf-8")
            issue = Issue("0001", "Portable workflow", issue_path, False)
            workflow = default_portable_workflow()
            final_review = workflow.step(FINAL_REVIEW_STEP_ID)
            workflow = replace(
                workflow,
                steps=tuple(
                    replace(
                        step,
                        guidance=StepGuidance("Persist interrupted review focus."),
                    )
                    if step.instance_id == FINAL_REVIEW_STEP_ID
                    else step
                    for step in workflow.steps
                ),
            )
            catalog = default_portable_component_catalog()
            writer = LoopStateWriter(index)
            writer.record_resolved_workflow(workflow, catalog)

            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                PortableWorkflowExecutor(
                    workflow,
                    catalog,
                    InterruptingFinalReviewRunner(),
                ).run(
                    issue,
                    pass_number=1,
                    checkpoint=lambda checkpoint: writer.record_portable_checkpoint(
                        issue,
                        checkpoint,
                    ),
                )

            restored = LoopStateWriter(index)
            recovery = restored.resume_portable_workflow(issue, workflow)
            self.assertIsNotNone(recovery)
            assert recovery is not None
            interrupted_runtime = next(
                runtime
                for runtime in recovery.runtime_states
                if runtime.step_instance_id == FINAL_REVIEW_STEP_ID
            )
            resumed_identities: list[tuple[object, object]] = []

            class PassingRoleRunner:
                def run_role(self, **arguments: object) -> RoleResult:
                    if arguments["step_display_name"] == "Final Review":
                        resumed_identities.append(
                            (
                                arguments["step_attempt_id"],
                                arguments["prompt_session_id"],
                            )
                        )
                    return RoleResult(status="PASS")

            execution = PortableWorkflowExecutor(
                restored.resolved_workflow(catalog),
                catalog,
                PassingRoleRunner(),
            ).run(
                issue,
                pass_number=1,
                recovery=recovery,
                checkpoint=lambda checkpoint: restored.record_portable_checkpoint(
                    issue,
                    checkpoint,
                ),
            )
            restored.record_portable_execution_result(issue, execution)
            interrupted_attempts = LoopStateWriter(
                index
            ).interrupted_step_attempt_records(issue.number)

        self.assertIsNotNone(interrupted_runtime.attempt_id)
        self.assertIsNotNone(interrupted_runtime.prompt_session_id)
        self.assertIsNotNone(interrupted_runtime.attempt_context)
        assert interrupted_runtime.attempt_context is not None
        self.assertEqual(
            interrupted_runtime.attempt_context.capability_profile,
            final_review.capability_profile,
        )
        self.assertEqual(
            interrupted_runtime.attempt_context.guidance,
            "Persist interrupted review focus.",
        )
        self.assertEqual(len(interrupted_attempts), 1)
        self.assertEqual(interrupted_attempts[0].attempt_id, interrupted_runtime.attempt_id)
        self.assertEqual(
            interrupted_attempts[0].prompt_session_id,
            interrupted_runtime.prompt_session_id,
        )
        self.assertEqual(
            interrupted_attempts[0].attempt_context,
            interrupted_runtime.attempt_context,
        )
        self.assertNotEqual(
            resumed_identities,
            [(interrupted_runtime.attempt_id, interrupted_runtime.prompt_session_id)],
        )
        completed_final_review = next(
            attempt
            for attempt in execution.attempts
            if attempt.step_instance_id == FINAL_REVIEW_STEP_ID
        )
        self.assertEqual(resumed_identities[0][0], completed_final_review.attempt_id)
        self.assertEqual(
            resumed_identities[0][1],
            completed_final_review.prompt_session_id,
        )

    def test_issue_runs_two_independent_reviews_and_binds_final_review_to_qa(self) -> None:
        class PassingRoleRunner:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def run_role(self, **arguments: object) -> RoleResult:
                self.calls.append(arguments)
                return RoleResult(status="PASS", summary=str(arguments["step_display_name"]))

        role_runner = PassingRoleRunner()
        executor = PortableWorkflowExecutor(
            default_portable_workflow(),
            default_portable_component_catalog(),
            role_runner,
        )
        issue = Issue("0001", "Portable workflow", Path("0001.md"), False)

        result = executor.run(issue, pass_number=1)

        self.assertEqual(
            [call["step_display_name"] for call in role_runner.calls],
            ["Development", "Security Review", "Final Review", "QA"],
        )
        reviews = [call for call in role_runner.calls if call["role"] == "reviewer"]
        self.assertNotEqual(reviews[0]["step_instance_id"], reviews[1]["step_instance_id"])
        self.assertNotEqual(reviews[0]["prompt_session_id"], reviews[1]["prompt_session_id"])
        self.assertEqual(
            role_runner.calls[-1]["review_result"].summary,
            "Final Review",
        )
        self.assertEqual(result.issue_status, IssueStatus.COMPLETED)
        self.assertIsNone(result.current_step_instance_id)
        self.assertEqual(len(result.attempts), 4)
        self.assertEqual(len({attempt.attempt_id for attempt in result.attempts}), 4)
        self.assertTrue(
            all(runtime.status is StepRuntimeStatus.COMPLETED for runtime in result.runtime_states)
        )

    def test_attempts_capture_each_step_profile_and_resolved_guidance(self) -> None:
        workflow = default_portable_workflow()
        security_review = workflow.step(SECURITY_REVIEW_STEP_ID)
        replaceable = security_review.capability_profile.capabilities[-1]
        specialized_review = replace(
            security_review,
            capability_profile=security_review.capability_profile.toggled(replaceable),
            guidance=StepGuidance("Focus on authentication boundaries."),
        )
        workflow = replace(
            workflow,
            steps=tuple(
                specialized_review
                if step.instance_id == SECURITY_REVIEW_STEP_ID
                else step
                for step in workflow.steps
            ),
        )

        class PassingRoleRunner:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def run_role(self, **arguments: object) -> RoleResult:
                self.calls.append(arguments)
                return RoleResult(status="PASS")

        role_runner = PassingRoleRunner()
        result = PortableWorkflowExecutor(
            workflow,
            default_portable_component_catalog(),
            role_runner,
        ).run(Issue("0001", "Portable workflow", Path("0001.md"), False), pass_number=1)

        security_attempt = next(
            attempt
            for attempt in result.attempts
            if attempt.step_instance_id == SECURITY_REVIEW_STEP_ID
        )
        final_attempt = next(
            attempt
            for attempt in result.attempts
            if attempt.step_instance_id == FINAL_REVIEW_STEP_ID
        )
        assert security_attempt.attempt_context is not None
        assert final_attempt.attempt_context is not None
        self.assertEqual(
            security_attempt.attempt_context.guidance,
            "Focus on authentication boundaries.",
        )
        self.assertNotEqual(
            security_attempt.attempt_context.capability_profile,
            final_attempt.attempt_context.capability_profile,
        )
        security_call = next(
            call
            for call in role_runner.calls
            if call["step_display_name"] == "Security Review"
        )
        final_call = next(
            call
            for call in role_runner.calls
            if call["step_display_name"] == "Final Review"
        )
        self.assertNotEqual(
            security_call["agent_paths"],
            final_call["agent_paths"],
        )
        self.assertEqual(
            security_call["step_guidance"],
            "Focus on authentication boundaries.",
        )

    def test_issue_cannot_complete_until_security_review_succeeds(self) -> None:
        class FailingSecurityRunner:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def run_role(self, **arguments: object) -> RoleResult:
                self.calls.append(arguments)
                if arguments["step_display_name"] == "Security Review":
                    return RoleResult(
                        status="FAIL",
                        summary="Security changes are required.",
                        findings=["Unsafe input reaches the command builder."],
                        fix_list=["Validate the command input."],
                    )
                return RoleResult(status="PASS")

        role_runner = FailingSecurityRunner()
        executor = PortableWorkflowExecutor(
            default_portable_workflow(),
            default_portable_component_catalog(),
            role_runner,
        )
        issue = Issue("0001", "Portable workflow", Path("0001.md"), False)

        result = executor.run(issue, pass_number=1, max_passes=2)

        self.assertEqual(
            [call["step_display_name"] for call in role_runner.calls],
            ["Development", "Security Review", "Development", "Security Review"],
        )
        self.assertEqual(
            role_runner.calls[2]["fix_list"],
            ["Validate the command input."],
        )
        self.assertEqual(result.issue_status, IssueStatus.CHANGES_REQUESTED)
        self.assertNotEqual(result.issue_status, IssueStatus.COMPLETED)
        self.assertEqual(
            [attempt.pass_number for attempt in result.attempts],
            [1, 1, 2, 2],
        )

    def test_blocked_self_loop_stops_at_the_configured_pass_limit(self) -> None:
        document = default_portable_workflow().to_dict()
        security_review = next(
            step
            for step in document["steps"]
            if step["instance_id"] == SECURITY_REVIEW_STEP_ID
        )
        security_review["transitions"]["BLOCKED"] = str(
            SECURITY_REVIEW_STEP_ID
        )
        workflow = load_portable_workflow(
            document,
            default_portable_component_catalog(),
        )

        class BlockedSelfLoopRunner:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def run_role(self, **arguments: object) -> RoleResult:
                self.calls.append(str(arguments["step_display_name"]))
                if len(self.calls) > 2:
                    raise AssertionError("The BLOCKED self-loop exceeded its budget.")
                if arguments["step_display_name"] == "Security Review":
                    return RoleResult(status="BLOCKED", summary="Retry security review.")
                return RoleResult(status="PASS")

        runner = BlockedSelfLoopRunner()
        result = PortableWorkflowExecutor(
            workflow,
            default_portable_component_catalog(),
            runner,
        ).run(
            Issue("0001", "Portable workflow", Path("0001.md"), False),
            pass_number=1,
            max_passes=1,
        )

        self.assertEqual(runner.calls, ["Development", "Security Review"])
        self.assertEqual(result.issue_status, IssueStatus.BLOCKED)
        self.assertEqual(
            [attempt.outcome for attempt in result.attempts],
            [StepOutcome.SUCCEEDED, StepOutcome.BLOCKED],
        )

    def test_succeeded_cycle_edge_cannot_complete_an_exhausted_run(self) -> None:
        recovery_development_id = "26dcaf60-5af4-4db0-9a1e-206fed74f387"
        document = default_portable_workflow().to_dict()
        security_review = next(
            step
            for step in document["steps"]
            if step["instance_id"] == SECURITY_REVIEW_STEP_ID
        )
        security_review["transitions"]["BLOCKED"] = recovery_development_id
        document["steps"].append(
            {
                "instance_id": recovery_development_id,
                "display_name": "Recovery Development",
                "component_id": "devloop.development",
                "transitions": {
                    "SUCCEEDED": str(SECURITY_REVIEW_STEP_ID),
                    "BLOCKED": None,
                    "FAILED": None,
                    "CANCELLED": None,
                },
                "input_bindings": {},
            }
        )
        catalog = default_portable_component_catalog()
        workflow = load_portable_workflow(document, catalog)

        class CloseCycleWithSuccessRunner:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def run_role(self, **arguments: object) -> RoleResult:
                self.calls.append(str(arguments["step_display_name"]))
                if arguments["step_display_name"] == "Security Review":
                    return RoleResult(status="BLOCKED")
                return RoleResult(status="PASS")

        runner = CloseCycleWithSuccessRunner()
        result = PortableWorkflowExecutor(workflow, catalog, runner).run(
            Issue("0001", "Portable workflow", Path("0001.md"), False),
            pass_number=1,
            max_passes=1,
        )

        self.assertEqual(
            runner.calls,
            ["Development", "Security Review", "Recovery Development"],
        )
        self.assertEqual(result.attempts[-1].outcome, StepOutcome.SUCCEEDED)
        self.assertEqual(result.issue_status, IssueStatus.BLOCKED)
        self.assertEqual(result.role_result.status, "BLOCKED")
        self.assertIn("cycle budget", result.role_result.summary.lower())
        self.assertIn("Recovery Development", result.role_result.summary)
        self.assertIn("Security Review", result.role_result.summary)
        self.assertIn("Increase", " ".join(result.role_result.fix_list))

    def test_succeeded_cycle_edge_cannot_complete_a_recovered_exhausted_run(
        self,
    ) -> None:
        retry_development_id = "26dcaf60-5af4-4db0-9a1e-206fed74f387"
        document = default_portable_workflow().to_dict()
        security_review = next(
            step
            for step in document["steps"]
            if step["instance_id"] == SECURITY_REVIEW_STEP_ID
        )
        security_review["transitions"]["BLOCKED"] = retry_development_id
        document["steps"].append(
            {
                "instance_id": retry_development_id,
                "display_name": "Recovery Development",
                "component_id": "devloop.development",
                "transitions": {
                    "SUCCEEDED": str(SECURITY_REVIEW_STEP_ID),
                    "BLOCKED": None,
                    "FAILED": None,
                    "CANCELLED": None,
                },
                "input_bindings": {},
            }
        )
        catalog = default_portable_component_catalog()
        workflow = load_portable_workflow(document, catalog)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Portable workflow\n", encoding="utf-8")
            index = root / "README.md"
            index.write_text("[Portable workflow](./0001.md)\n", encoding="utf-8")
            issue = Issue("0001", "Portable workflow", issue_path, False)
            writer = LoopStateWriter(index)
            writer.record_resolved_workflow(workflow, catalog)

            class ReachCycleRunner:
                def run_role(self, **arguments: object) -> RoleResult:
                    if arguments["step_display_name"] == "Security Review":
                        return RoleResult(status="BLOCKED")
                    if arguments["step_display_name"] == "Recovery Development":
                        raise AssertionError("Recovery interrupted too late.")
                    return RoleResult(status="PASS")

            def interrupt_before_cycle_closes(
                checkpoint: PortableWorkflowCheckpoint,
            ) -> None:
                writer.record_portable_checkpoint(issue, checkpoint)
                if str(checkpoint.current_step_instance_id) == retry_development_id:
                    raise RuntimeError("interrupted inside cycle")

            with self.assertRaisesRegex(RuntimeError, "interrupted inside cycle"):
                PortableWorkflowExecutor(
                    workflow,
                    catalog,
                    ReachCycleRunner(),
                ).run(
                    issue,
                    pass_number=1,
                    max_passes=1,
                    checkpoint=interrupt_before_cycle_closes,
                )

            recovery = LoopStateWriter(index).resume_portable_workflow(
                issue,
                workflow,
            )
            self.assertIsNotNone(recovery)
            assert recovery is not None

            class CloseCycleRunner:
                def __init__(self) -> None:
                    self.calls: list[str] = []

                def run_role(self, **arguments: object) -> RoleResult:
                    self.calls.append(str(arguments["step_display_name"]))
                    if len(self.calls) > 1:
                        raise AssertionError("Recovery reset the persisted cycle budget.")
                    return RoleResult(status="PASS", summary="Recovery succeeded.")

            resumed_runner = CloseCycleRunner()
            result = PortableWorkflowExecutor(
                workflow,
                catalog,
                resumed_runner,
            ).run(
                issue,
                pass_number=1,
                max_passes=1,
                recovery=recovery,
            )

        self.assertEqual(
            [str(step_id) for step_id in recovery.cycle_path_step_instance_ids],
            [
                str(DEVELOPMENT_STEP_ID),
                str(SECURITY_REVIEW_STEP_ID),
                retry_development_id,
            ],
        )
        self.assertEqual(resumed_runner.calls, ["Recovery Development"])
        self.assertEqual(result.attempts[-1].outcome, StepOutcome.SUCCEEDED)
        self.assertEqual(result.issue_status, IssueStatus.BLOCKED)
        self.assertEqual(result.role_result.status, "BLOCKED")
        self.assertIn("cycle budget", result.role_result.summary.lower())
        self.assertIn("Recovery Development", result.role_result.summary)
        self.assertIn("Security Review", result.role_result.summary)

    def test_issue_cannot_reach_qa_until_final_review_succeeds(self) -> None:
        class FailingFinalReviewRunner:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def run_role(self, **arguments: object) -> RoleResult:
                self.calls.append(arguments)
                if arguments["step_display_name"] == "Final Review":
                    return RoleResult(
                        status="FAIL",
                        findings=["The final acceptance evidence is incomplete."],
                        fix_list=["Add the missing acceptance evidence."],
                    )
                return RoleResult(status="PASS")

        role_runner = FailingFinalReviewRunner()
        result = PortableWorkflowExecutor(
            default_portable_workflow(),
            default_portable_component_catalog(),
            role_runner,
        ).run(
            Issue("0001", "Portable workflow", Path("0001.md"), False),
            pass_number=1,
            max_passes=1,
        )

        self.assertEqual(
            [call["step_display_name"] for call in role_runner.calls],
            ["Development", "Security Review", "Final Review"],
        )
        self.assertEqual(result.issue_status, IssueStatus.CHANGES_REQUESTED)
        self.assertNotEqual(result.issue_status, IssueStatus.COMPLETED)

    def test_rework_from_each_review_receives_the_exact_triggering_attempt(self) -> None:
        review_steps = {
            "Security Review": SECURITY_REVIEW_STEP_ID,
            "Final Review": FINAL_REVIEW_STEP_ID,
        }

        for review_name, review_step_id in review_steps.items():
            with self.subTest(review=review_name):
                class ReworkRoleRunner:
                    def __init__(self) -> None:
                        self.failed_review = False
                        self.rework_records: list[object] = []
                        self.calls: list[str] = []

                    def run_role(self, **arguments: object) -> RoleResult:
                        self.calls.append(str(arguments["step_display_name"]))
                        if (
                            arguments["step_display_name"] == review_name
                            and not self.failed_review
                        ):
                            self.failed_review = True
                            return RoleResult(
                                status="FAIL",
                                findings=[f"{review_name} finding"],
                                fix_list=[f"Correct {review_name}"],
                            )
                        if (
                            arguments["step_display_name"] == "Development"
                            and arguments["pass_number"] == 2
                        ):
                            self.rework_records.append(
                                arguments["rework_attempt_record"]
                            )
                        return RoleResult(status="PASS")

                runner = ReworkRoleRunner()
                result = PortableWorkflowExecutor(
                    default_portable_workflow(),
                    default_portable_component_catalog(),
                    runner,
                ).run(
                    Issue("0001", "Portable workflow", Path("0001.md"), False),
                    pass_number=1,
                    max_passes=2,
                )

                trigger = next(
                    attempt
                    for attempt in result.attempts
                    if attempt.step_instance_id == review_step_id
                    and attempt.outcome is StepOutcome.CHANGES_REQUESTED
                )
                rework = next(
                    attempt
                    for attempt in result.attempts
                    if attempt.step_instance_id == DEVELOPMENT_STEP_ID
                    and attempt.pass_number == 2
                )
                self.assertEqual(
                    runner.rework_records,
                    [step_attempt_record_to_dict(trigger)],
                )
                self.assertEqual(rework.rework_attempt_id, trigger.attempt_id)
                self.assertEqual(result.issue_status, IssueStatus.COMPLETED)
                expected_calls = (
                    [
                        "Development",
                        "Security Review",
                        "Development",
                        "Security Review",
                        "Final Review",
                        "QA",
                    ]
                    if review_name == "Security Review"
                    else [
                        "Development",
                        "Security Review",
                        "Final Review",
                        "Development",
                        "Security Review",
                        "Final Review",
                        "QA",
                    ]
                )
                self.assertEqual(runner.calls, expected_calls)

    def test_rework_trigger_survives_interruption_before_development_restarts(self) -> None:
        class ChangesRequestedOnceRunner:
            def __init__(self) -> None:
                self.requested = False

            def run_role(self, **arguments: object) -> RoleResult:
                if (
                    arguments["step_display_name"] == "Final Review"
                    and not self.requested
                ):
                    self.requested = True
                    return RoleResult(
                        status="FAIL",
                        fix_list=["Correct the final review finding."],
                    )
                return RoleResult(status="PASS")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Portable workflow\n", encoding="utf-8")
            index = root / "README.md"
            index.write_text("[Portable workflow](./0001.md)\n", encoding="utf-8")
            issue = Issue("0001", "Portable workflow", issue_path, False)
            workflow = default_portable_workflow()
            catalog = default_portable_component_catalog()
            writer = LoopStateWriter(index)
            writer.record_resolved_workflow(workflow, catalog)

            def interrupt_before_rework(
                checkpoint: PortableWorkflowCheckpoint,
            ) -> None:
                writer.record_portable_checkpoint(issue, checkpoint)
                if (
                    checkpoint.current_step_instance_id == DEVELOPMENT_STEP_ID
                    and checkpoint.pass_number == 2
                ):
                    raise RuntimeError("interrupted before rework")

            with self.assertRaisesRegex(RuntimeError, "interrupted before rework"):
                PortableWorkflowExecutor(
                    workflow,
                    catalog,
                    ChangesRequestedOnceRunner(),
                ).run(
                    issue,
                    pass_number=1,
                    max_passes=2,
                    checkpoint=interrupt_before_rework,
                )

            restored = LoopStateWriter(index)
            recovery = restored.resume_portable_workflow(issue, workflow)
            self.assertIsNotNone(recovery)
            assert recovery is not None
            received_rework_records: list[object] = []

            class PassingRoleRunner:
                def run_role(self, **arguments: object) -> RoleResult:
                    if arguments["step_display_name"] == "Development":
                        received_rework_records.append(
                            arguments["rework_attempt_record"]
                        )
                    return RoleResult(status="PASS")

            result = PortableWorkflowExecutor(
                restored.resolved_workflow(catalog),
                catalog,
                PassingRoleRunner(),
            ).run(
                issue,
                pass_number=1,
                max_passes=2,
                recovery=recovery,
            )

        trigger = next(
            attempt
            for attempt in recovery.attempts
            if attempt.outcome is StepOutcome.CHANGES_REQUESTED
        )
        self.assertEqual(
            received_rework_records,
            [step_attempt_record_to_dict(trigger)],
        )
        self.assertEqual(result.issue_status, IssueStatus.COMPLETED)

    def test_blocked_rework_round_trip_reuses_the_trigger_until_development_succeeds(
        self,
    ) -> None:
        class BlockedReworkRunner:
            def __init__(self) -> None:
                self.requested = False

            def run_role(self, **arguments: object) -> RoleResult:
                if (
                    arguments["step_display_name"] == "Security Review"
                    and not self.requested
                ):
                    self.requested = True
                    return RoleResult(
                        status="FAIL",
                        findings=["SEC-021 survives a blocked correction."],
                        fix_list=["Correct SEC-021."],
                    )
                if (
                    arguments["step_display_name"] == "Development"
                    and arguments["pass_number"] == 2
                ):
                    return RoleResult(
                        status="BLOCKED",
                        summary="The correction needs an external fixture.",
                    )
                return RoleResult(status="PASS")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Portable workflow\n", encoding="utf-8")
            index = root / "README.md"
            index.write_text("[Portable workflow](./0001.md)\n", encoding="utf-8")
            issue = Issue("0001", "Portable workflow", issue_path, False)
            workflow = default_portable_workflow()
            catalog = default_portable_component_catalog()
            writer = LoopStateWriter(index)
            writer.record_resolved_workflow(workflow, catalog)
            blocked = PortableWorkflowExecutor(
                workflow,
                catalog,
                BlockedReworkRunner(),
            ).run(
                issue,
                pass_number=1,
                max_passes=2,
                checkpoint=lambda checkpoint: writer.record_portable_checkpoint(
                    issue,
                    checkpoint,
                ),
            )
            writer.record_portable_execution_result(issue, blocked)
            trigger = next(
                attempt
                for attempt in blocked.attempts
                if attempt.outcome is StepOutcome.CHANGES_REQUESTED
            )
            blocked_development = blocked.attempts[-1]

            restored = LoopStateWriter(index)
            retry = restored.retry_portable_workflow(issue, workflow)
            self.assertIsNotNone(retry)
            assert retry is not None
            received_rework_records: list[object] = []

            class PassingRoleRunner:
                def run_role(self, **arguments: object) -> RoleResult:
                    if arguments["step_display_name"] == "Development":
                        received_rework_records.append(
                            arguments["rework_attempt_record"]
                        )
                    return RoleResult(status="PASS")

            result = PortableWorkflowExecutor(
                restored.resolved_workflow(catalog),
                catalog,
                PassingRoleRunner(),
            ).run(
                issue,
                pass_number=1,
                max_passes=2,
                recovery=retry,
                checkpoint=lambda checkpoint: restored.record_portable_checkpoint(
                    issue,
                    checkpoint,
                ),
            )

            final_issue_state = LoopStateWriter(index).issue_state(issue)

        self.assertEqual(blocked.issue_status, IssueStatus.BLOCKED)
        self.assertEqual(blocked_development.rework_attempt_id, trigger.attempt_id)
        self.assertEqual(retry.pending_rework_attempt_id, trigger.attempt_id)
        self.assertEqual(
            received_rework_records,
            [step_attempt_record_to_dict(trigger)],
        )
        self.assertEqual(result.issue_status, IssueStatus.COMPLETED)
        self.assertNotIn("pending_rework_attempt_id", final_issue_state)

    def test_failed_and_cancelled_rework_retries_restore_the_linked_trigger(self) -> None:
        class FailedReworkRunner:
            def __init__(self) -> None:
                self.requested = False

            def run_role(self, **arguments: object) -> RoleResult:
                if (
                    arguments["step_display_name"] == "Security Review"
                    and not self.requested
                ):
                    self.requested = True
                    return RoleResult(status="FAIL", fix_list=["Correct SEC-022."])
                if (
                    arguments["step_display_name"] == "Development"
                    and arguments["pass_number"] == 2
                ):
                    return RoleResult(status="FAIL", summary="Correction attempt failed.")
                return RoleResult(status="PASS")

        issue = Issue("0001", "Portable workflow", Path("0001.md"), False)
        workflow = default_portable_workflow()
        catalog = default_portable_component_catalog()
        failed = PortableWorkflowExecutor(
            workflow,
            catalog,
            FailedReworkRunner(),
        ).run(issue, pass_number=1, max_passes=2)
        trigger = next(
            attempt
            for attempt in failed.attempts
            if attempt.outcome is StepOutcome.CHANGES_REQUESTED
        )

        for terminal_outcome in (StepOutcome.FAILED, StepOutcome.CANCELLED):
            with self.subTest(outcome=terminal_outcome.value), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                issue_path = root / "0001.md"
                issue_path.write_text("# Portable workflow\n", encoding="utf-8")
                index = root / "README.md"
                index.write_text("[Portable workflow](./0001.md)\n", encoding="utf-8")
                persisted_issue = replace(issue, path=issue_path)
                terminal_attempt = replace(
                    failed.attempts[-1],
                    outcome=terminal_outcome,
                )
                terminal_runtime = replace(
                    failed.runtime_states[0],
                    outcome=terminal_outcome,
                )
                attempts = (*failed.attempts[:-1], terminal_attempt)
                runtimes = (
                    terminal_runtime,
                    *failed.runtime_states[1:],
                )
                writer = LoopStateWriter(index)
                writer.record_resolved_workflow(workflow, catalog)
                writer.record_portable_checkpoint(
                    persisted_issue,
                    PortableWorkflowCheckpoint(
                        issue_id=issue.number,
                        issue_status=IssueStatus(terminal_outcome.value),
                        current_step_instance_id=None,
                        pass_number=2,
                        runtime_states=runtimes,
                        attempts=attempts,
                        pending_rework_attempt_id=trigger.attempt_id,
                    ),
                )

                retry = LoopStateWriter(index).retry_portable_workflow(
                    persisted_issue,
                    workflow,
                )

                self.assertIsNotNone(retry)
                assert retry is not None
                self.assertEqual(
                    retry.pending_rework_attempt_id,
                    trigger.attempt_id,
                )
                self.assertEqual(
                    retry.current_step_instance_id,
                    DEVELOPMENT_STEP_ID,
                )

    def test_changes_requested_at_the_pass_limit_retries_with_the_exact_record(self) -> None:
        class ChangesRequestedRunner:
            def run_role(self, **arguments: object) -> RoleResult:
                if arguments["step_display_name"] == "Security Review":
                    return RoleResult(
                        status="FAIL",
                        fix_list=["Correct the pass-limited finding."],
                    )
                return RoleResult(status="PASS")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Portable workflow\n", encoding="utf-8")
            index = root / "README.md"
            index.write_text("[Portable workflow](./0001.md)\n", encoding="utf-8")
            issue = Issue("0001", "Portable workflow", issue_path, False)
            workflow = default_portable_workflow()
            catalog = default_portable_component_catalog()
            writer = LoopStateWriter(index)
            writer.record_resolved_workflow(workflow, catalog)
            changes_requested = PortableWorkflowExecutor(
                workflow,
                catalog,
                ChangesRequestedRunner(),
            ).run(
                issue,
                pass_number=1,
                max_passes=1,
                checkpoint=lambda checkpoint: writer.record_portable_checkpoint(
                    issue,
                    checkpoint,
                ),
            )
            self.assertEqual(
                changes_requested.issue_status,
                IssueStatus.CHANGES_REQUESTED,
            )

            restored = LoopStateWriter(index)
            retry = restored.retry_portable_workflow(
                issue,
                workflow,
                pass_number=1,
            )
            self.assertIsNotNone(retry)
            assert retry is not None
            received_rework_records: list[object] = []

            class PassingRoleRunner:
                def run_role(self, **arguments: object) -> RoleResult:
                    if arguments["step_display_name"] == "Development":
                        received_rework_records.append(
                            arguments["rework_attempt_record"]
                        )
                    return RoleResult(status="PASS")

            result = PortableWorkflowExecutor(
                restored.resolved_workflow(catalog),
                catalog,
                PassingRoleRunner(),
            ).run(
                issue,
                pass_number=1,
                max_passes=1,
                recovery=retry,
            )

        trigger = next(
            attempt
            for attempt in retry.attempts
            if attempt.outcome is StepOutcome.CHANGES_REQUESTED
        )
        self.assertEqual(retry.current_step_instance_id, DEVELOPMENT_STEP_ID)
        self.assertEqual(
            received_rework_records,
            [step_attempt_record_to_dict(trigger)],
        )
        self.assertEqual(result.issue_status, IssueStatus.COMPLETED)

    def test_state_round_trip_keeps_workflow_runtime_and_attempt_identity(self) -> None:
        class PassingRoleRunner:
            def run_role(self, **arguments: object) -> RoleResult:
                return RoleResult(status="PASS", summary=str(arguments["step_display_name"]))

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Portable workflow\n", encoding="utf-8")
            index = root / "README.md"
            index.write_text("[Portable workflow](./0001.md)\n", encoding="utf-8")
            issue = Issue("0001", "Portable workflow", issue_path, False)
            workflow = default_portable_workflow()
            workflow = replace(
                workflow,
                steps=tuple(
                    replace(
                        step,
                        guidance=StepGuidance("Persist this review focus."),
                    )
                    if step.instance_id == SECURITY_REVIEW_STEP_ID
                    else step
                    for step in workflow.steps
                ),
            )
            catalog = default_portable_component_catalog()
            writer = LoopStateWriter(index)
            writer.record_resolved_workflow(workflow, catalog)
            writer.record_step_runtime_state(
                issue,
                StepRuntimeState(
                    SECURITY_REVIEW_STEP_ID,
                    issue.number,
                    StepRuntimeStatus.RUNNING,
                    1,
                    "security-session",
                ),
            )

            active_state = json.loads(writer.state_path.read_text(encoding="utf-8"))
            self.assertEqual(active_state["issues"][issue.number]["status"], "IN_PROGRESS")
            self.assertEqual(
                active_state["issues"][issue.number]["current_step_instance_id"],
                SECURITY_REVIEW_STEP_ID,
            )

            execution = PortableWorkflowExecutor(workflow, catalog, PassingRoleRunner()).run(
                issue,
                pass_number=1,
            )
            writer.record_portable_execution_result(issue, execution)
            restored = LoopStateWriter(index)

            self.assertEqual(restored.resolved_workflow(catalog).to_dict(), workflow.to_dict())
            self.assertEqual(
                restored.state["resolved_workflow_hash"],
                canonical_workflow_hash(workflow),
            )
            self.assertEqual(
                set(restored.state["step_runtime_states"]),
                {
                    str(step.instance_id)
                    for step in workflow.steps
                    if catalog.resolve(step.component_id).scope is StepScope.ISSUE
                },
            )
            self.assertEqual(
                sum(
                    len(records)
                    for issues in restored.state["step_attempt_records"].values()
                    for records in issues.values()
                ),
                4,
            )
            review_attempts = [
                attempt
                for step_id, issues in restored.state["step_attempt_records"].items()
                if step_id in {str(SECURITY_REVIEW_STEP_ID), str(FINAL_REVIEW_STEP_ID)}
                for attempt in issues[issue.number]
            ]
            self.assertEqual(len(review_attempts), 2)
            self.assertEqual(
                {
                    attempt["outputs"]["review"]["result"]["summary"]
                    for attempt in review_attempts
                },
                {"Security Review", "Final Review"},
            )
            self.assertNotEqual(
                review_attempts[0]["prompt_session_id"],
                review_attempts[1]["prompt_session_id"],
            )
            restored_security_attempt = next(
                attempt
                for attempt in restored.step_attempt_records(issue.number)
                if attempt.step_instance_id == SECURITY_REVIEW_STEP_ID
            )
            assert restored_security_attempt.attempt_context is not None
            self.assertEqual(
                restored_security_attempt.attempt_context.guidance,
                "Persist this review focus.",
            )
            self.assertIn(
                "safety boundaries",
                restored_security_attempt.attempt_context.guidance_precedence,
            )
            tampered_state = deepcopy(restored.state)
            tampered_state["resolved_workflow"]["steps"][0]["display_name"] = "Tampered"
            restored.state_path.write_text(
                json.dumps(tampered_state),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "hash does not match"):
                LoopStateWriter(index).resolved_workflow(catalog)

    def test_state_round_trip_accepts_an_intact_legacy_sparse_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index = root / "README.md"
            index.write_text("# Issues\n", encoding="utf-8")
            workflow = default_portable_workflow()
            document = workflow.to_dict()
            for step in document["steps"]:
                step.pop("capability_profile")
                step.pop("codex_settings")
                step.pop("execution_budget")
            canonical_document = json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            writer = LoopStateWriter(index)
            writer.state["resolved_workflow"] = document
            writer.state["resolved_workflow_hash"] = hashlib.sha256(
                canonical_document.encode("utf-8")
            ).hexdigest()
            writer.flush()

            restored = LoopStateWriter(index).resolved_workflow(
                default_portable_component_catalog()
            )

            self.assertEqual(restored.to_dict(), workflow.to_dict())
            persisted = json.loads(writer.state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["resolved_workflow"], document)

    def test_recording_workflow_refuses_to_overwrite_a_tampered_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index = root / "README.md"
            index.write_text("# Issues\n", encoding="utf-8")
            workflow = default_portable_workflow()
            catalog = default_portable_component_catalog()
            writer = LoopStateWriter(index)
            writer.record_resolved_workflow(workflow, catalog)
            tampered_state = json.loads(writer.state_path.read_text(encoding="utf-8"))
            tampered_state["resolved_workflow"]["steps"][0]["display_name"] = "Tampered"
            writer.state_path.write_text(json.dumps(tampered_state), encoding="utf-8")

            reopened = LoopStateWriter(index)
            with self.assertRaisesRegex(ValueError, "hash does not match"):
                reopened.record_resolved_workflow(workflow, catalog)

            persisted = json.loads(writer.state_path.read_text(encoding="utf-8"))

        self.assertEqual(
            persisted["resolved_workflow"]["steps"][0]["display_name"],
            "Tampered",
        )

    def test_state_round_trip_rejects_unknown_workflow_root_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index = root / "README.md"
            index.write_text("# Issues\n", encoding="utf-8")
            catalog = default_portable_component_catalog()
            writer = LoopStateWriter(index)
            writer.record_resolved_workflow(default_portable_workflow(), catalog)
            tampered_state = json.loads(writer.state_path.read_text(encoding="utf-8"))
            tampered_state["resolved_workflow"]["unexpected"] = "not hashed"
            writer.state_path.write_text(json.dumps(tampered_state), encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                r"workflow fields.*unexpected",
            ):
                LoopStateWriter(index).resolved_workflow(catalog)

            persisted = json.loads(writer.state_path.read_text(encoding="utf-8"))

        self.assertEqual(
            persisted["resolved_workflow"]["unexpected"],
            "not hashed",
        )

    def test_changes_requested_findings_and_fix_list_survive_state_round_trip(self) -> None:
        class ChangesRequestedRunner:
            def run_role(self, **arguments: object) -> RoleResult:
                if arguments["step_display_name"] == "Security Review":
                    return RoleResult(
                        status="FAIL",
                        summary="Security review requested changes.",
                        findings=["Finding SEC-001 survives persistence."],
                        fix_list=["Fix SEC-001 before another review."],
                    )
                return RoleResult(status="PASS", summary="Development passed.")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Portable workflow\n", encoding="utf-8")
            index = root / "README.md"
            index.write_text("[Portable workflow](./0001.md)\n", encoding="utf-8")
            issue = Issue("0001", "Portable workflow", issue_path, False)
            workflow = default_portable_workflow()
            catalog = default_portable_component_catalog()
            execution = PortableWorkflowExecutor(
                workflow,
                catalog,
                ChangesRequestedRunner(),
            ).run(issue, pass_number=1, max_passes=1)
            writer = LoopStateWriter(index)
            writer.record_resolved_workflow(workflow, catalog)
            writer.record_portable_execution_result(issue, execution)

            restored_attempts = LoopStateWriter(index).step_attempt_records()

        review_attempt = next(
            attempt
            for attempt in restored_attempts
            if attempt.step_instance_id == SECURITY_REVIEW_STEP_ID
        )
        self.assertEqual(review_attempt.outcome, StepOutcome.CHANGES_REQUESTED)
        self.assertEqual(
            review_attempt.result.findings,
            ["Finding SEC-001 survives persistence."],
        )
        self.assertEqual(
            review_attempt.result.fix_list,
            ["Fix SEC-001 before another review."],
        )

    def test_blocker_details_survive_state_round_trip(self) -> None:
        class BlockedRunner:
            def run_role(self, **_: object) -> RoleResult:
                return RoleResult(
                    status="BLOCKED",
                    summary="An external signing key is unavailable.",
                    findings=["The signing key cannot be read in this environment."],
                    fix_list=["Provide the signing key through the approved operator flow."],
                )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Portable workflow\n", encoding="utf-8")
            index = root / "README.md"
            index.write_text("[Portable workflow](./0001.md)\n", encoding="utf-8")
            issue = Issue("0001", "Portable workflow", issue_path, False)
            workflow = default_portable_workflow()
            catalog = default_portable_component_catalog()
            execution = PortableWorkflowExecutor(
                workflow,
                catalog,
                BlockedRunner(),
            ).run(issue, pass_number=1, max_passes=1)
            writer = LoopStateWriter(index)
            writer.record_resolved_workflow(workflow, catalog)
            writer.record_portable_execution_result(issue, execution)

            restored_attempt = LoopStateWriter(index).step_attempt_records()[0]

        self.assertEqual(restored_attempt.outcome, StepOutcome.BLOCKED)
        self.assertEqual(
            restored_attempt.blocked_reason,
            "An external signing key is unavailable.",
        )
        self.assertEqual(
            restored_attempt.blocker_details,
            ("Provide the signing key through the approved operator flow.",),
        )
        self.assertEqual(
            restored_attempt.result.findings,
            ["The signing key cannot be read in this environment."],
        )

    def test_console_projection_keeps_both_review_instances_visible(self) -> None:
        class PassingRoleRunner:
            def run_role(self, **arguments: object) -> RoleResult:
                return RoleResult(status="PASS", summary=str(arguments["step_display_name"]))

        workflow = default_portable_workflow()
        catalog = default_portable_component_catalog()
        issue = Issue("0001", "Portable workflow", Path("0001.md"), False)
        execution = PortableWorkflowExecutor(workflow, catalog, PassingRoleRunner()).run(
            issue,
            pass_number=1,
        )

        progress = project_workflow_step_progress(
            workflow,
            catalog,
            execution.runtime_states,
            execution.attempts,
            issue_id=issue.number,
        )
        rendered = render_step_progress_rows(
            progress,
            width=79,
            color=False,
            unicode=False,
        )

        review_rows = [row for row in progress if row.component_id == REVIEWER_COMPONENT_ID]
        self.assertEqual(
            [(row.display_name, row.status.value) for row in review_rows],
            [("Security Review", "PASS"), ("Final Review", "PASS")],
        )
        self.assertNotEqual(review_rows[0].step_instance_id, review_rows[1].step_instance_id)
        self.assertIn("Security Review", rendered)
        self.assertIn("Final Review", rendered)

    def test_repeated_step_attempts_remain_ordered_and_accumulate_elapsed_time(self) -> None:
        class ReworkOnceRunner:
            def __init__(self) -> None:
                self.requested = False

            def run_role(self, **arguments: object) -> RoleResult:
                if (
                    arguments["step_display_name"] == "Security Review"
                    and not self.requested
                ):
                    self.requested = True
                    return RoleResult(status="FAIL", fix_list=["Correct SEC-003."])
                return RoleResult(status="PASS")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_path = root / "0001.md"
            issue_path.write_text("# Portable workflow\n", encoding="utf-8")
            index = root / "README.md"
            index.write_text("[Portable workflow](./0001.md)\n", encoding="utf-8")
            issue = Issue("0001", "Portable workflow", issue_path, False)
            workflow = default_portable_workflow()
            catalog = default_portable_component_catalog()
            writer = LoopStateWriter(index)
            writer.record_resolved_workflow(workflow, catalog)
            execution = PortableWorkflowExecutor(
                workflow,
                catalog,
                ReworkOnceRunner(),
            ).run(
                issue,
                pass_number=1,
                max_passes=2,
                checkpoint=lambda checkpoint: writer.record_portable_checkpoint(
                    issue,
                    checkpoint,
                ),
            )
            writer.record_portable_execution_result(issue, execution)

            restored = LoopStateWriter(index)
            attempts = restored.step_attempt_records(issue.number)
            runtimes = restored.step_runtime_states(issue.number)
            progress = project_workflow_step_progress(
                workflow,
                catalog,
                runtimes,
                attempts,
                issue_id=issue.number,
            )

        development_attempts = [
            attempt
            for attempt in attempts
            if attempt.step_instance_id == DEVELOPMENT_STEP_ID
        ]
        development_progress = next(
            step
            for step in progress
            if step.step_instance_id == DEVELOPMENT_STEP_ID
        )
        self.assertEqual(
            [attempt.step_instance_id for attempt in attempts],
            [attempt.step_instance_id for attempt in execution.attempts],
        )
        self.assertEqual(
            [attempt.pass_number for attempt in development_attempts],
            [1, 2],
        )
        self.assertAlmostEqual(
            development_progress.elapsed_seconds,
            sum(attempt.elapsed_seconds for attempt in development_attempts),
        )
        self.assertEqual(development_progress.pass_number, 2)

    def test_binding_resolution_uses_latest_success_and_excludes_failed_output(self) -> None:
        class SequencedRoleRunner:
            def __init__(self, run_label: str) -> None:
                self.run_label = run_label

            def run_role(self, **arguments: object) -> RoleResult:
                return RoleResult(
                    status="PASS",
                    summary=f"{self.run_label} {arguments['step_display_name']}",
                )

        workflow = default_portable_workflow()
        catalog = default_portable_component_catalog()
        issue = Issue("0001", "Portable workflow", Path("0001.md"), False)
        first = PortableWorkflowExecutor(workflow, catalog, SequencedRoleRunner("first")).run(
            issue,
            pass_number=1,
        )
        second = PortableWorkflowExecutor(workflow, catalog, SequencedRoleRunner("second")).run(
            issue,
            pass_number=2,
        )
        final_review = next(
            attempt
            for attempt in second.attempts
            if attempt.step_instance_id == FINAL_REVIEW_STEP_ID
        )
        invalid_later_attempt = replace(
            final_review,
            attempt_id="failed-later-attempt",
            outcome=StepOutcome.FAILED,
        )

        inputs = resolve_portable_inputs(
            workflow.step(QA_STEP_ID),
            (*first.attempts, *second.attempts, invalid_later_attempt),
            issue_id=issue.number,
            catalog=catalog,
        )

        self.assertEqual(inputs["review_result"].summary, "second Final Review")

    def test_binding_can_explicitly_permit_a_failed_output(self) -> None:
        class PassingRoleRunner:
            def run_role(self, **arguments: object) -> RoleResult:
                return RoleResult(
                    status="PASS",
                    summary=str(arguments["step_display_name"]),
                )

        catalog = default_portable_component_catalog()
        workflow = default_portable_workflow()
        issue = Issue("0001", "Portable workflow", Path("0001.md"), False)
        execution = PortableWorkflowExecutor(
            workflow,
            catalog,
            PassingRoleRunner(),
        ).run(issue, pass_number=1)
        successful_review = next(
            attempt
            for attempt in execution.attempts
            if attempt.step_instance_id == FINAL_REVIEW_STEP_ID
        )
        failed_result = RoleResult(status="FAIL", summary="permitted failed review")
        failed_review = replace(
            successful_review,
            attempt_id="failed-review-artifact",
            outcome=StepOutcome.FAILED,
            result=failed_result,
            outputs={
                port_name: replace(output, value=failed_result)
                for port_name, output in successful_review.outputs.items()
            },
        )
        document = workflow.to_dict()
        qa_step = next(
            step for step in document["steps"] if step["instance_id"] == QA_STEP_ID
        )
        qa_step["input_bindings"]["review_result"]["allowed_outcomes"] = [
            "SUCCEEDED",
            "FAILED",
        ]
        permissive_workflow = load_portable_workflow(document, catalog)

        inputs = resolve_portable_inputs(
            permissive_workflow.step(QA_STEP_ID),
            (*execution.attempts, failed_review),
            issue_id=issue.number,
            catalog=catalog,
        )

        self.assertEqual(inputs["review_result"].summary, "permitted failed review")

    def test_default_binding_excludes_every_non_success_outcome(self) -> None:
        class PassingRoleRunner:
            def run_role(self, **arguments: object) -> RoleResult:
                return RoleResult(
                    status="PASS",
                    summary=str(arguments["step_display_name"]),
                )

        workflow = default_portable_workflow()
        catalog = default_portable_component_catalog()
        issue = Issue("0001", "Portable workflow", Path("0001.md"), False)
        execution = PortableWorkflowExecutor(
            workflow,
            catalog,
            PassingRoleRunner(),
        ).run(issue, pass_number=1)
        successful_review = next(
            attempt
            for attempt in execution.attempts
            if attempt.step_instance_id == FINAL_REVIEW_STEP_ID
        )

        for outcome in (
            StepOutcome.CHANGES_REQUESTED,
            StepOutcome.BLOCKED,
            StepOutcome.FAILED,
            StepOutcome.CANCELLED,
        ):
            with self.subTest(outcome=outcome.value):
                invalid_result = RoleResult(
                    status="FAIL",
                    summary=f"newer {outcome.value} output",
                )
                invalid_attempt = replace(
                    successful_review,
                    attempt_id=f"invalid-{outcome.value.lower()}",
                    outcome=outcome,
                    result=invalid_result,
                    outputs={
                        port_name: replace(output, value=invalid_result)
                        for port_name, output in successful_review.outputs.items()
                    },
                )

                inputs = resolve_portable_inputs(
                    workflow.step(QA_STEP_ID),
                    (*execution.attempts, invalid_attempt),
                    issue_id=issue.number,
                    catalog=catalog,
                )

                self.assertEqual(inputs["review_result"].summary, "Final Review")

    def test_binding_skips_a_newer_output_with_an_incompatible_contract(self) -> None:
        class PassingRoleRunner:
            def run_role(self, **arguments: object) -> RoleResult:
                return RoleResult(
                    status="PASS",
                    summary=str(arguments["step_display_name"]),
                )

        workflow = default_portable_workflow()
        catalog = default_portable_component_catalog()
        issue = Issue("0001", "Portable workflow", Path("0001.md"), False)
        execution = PortableWorkflowExecutor(
            workflow,
            catalog,
            PassingRoleRunner(),
        ).run(issue, pass_number=1)
        compatible_review = next(
            attempt
            for attempt in execution.attempts
            if attempt.step_instance_id == FINAL_REVIEW_STEP_ID
        )
        incompatible_result = RoleResult(
            status="PASS",
            summary="newer incompatible output",
        )
        incompatible_review = replace(
            compatible_review,
            attempt_id="incompatible-review-artifact",
            result=incompatible_result,
            outputs={
                port_name: replace(
                    output,
                    contract_id=QA_RESULT_CONTRACT,
                    value=incompatible_result,
                )
                for port_name, output in compatible_review.outputs.items()
            },
        )

        inputs = resolve_portable_inputs(
            workflow.step(QA_STEP_ID),
            (*execution.attempts, incompatible_review),
            issue_id=issue.number,
            catalog=catalog,
        )

        self.assertEqual(inputs["review_result"].summary, "Final Review")
        with self.assertRaisesRegex(RuntimeError, "compatible successful output"):
            resolve_portable_inputs(
                workflow.step(QA_STEP_ID),
                (incompatible_review,),
                issue_id=issue.number,
                catalog=catalog,
            )


if __name__ == "__main__":
    unittest.main()
