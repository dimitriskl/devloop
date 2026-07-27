from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

from devloop.codex_runner import RoleResult
from devloop.issue_pack import Issue
from devloop.model_catalog import (
    CatalogDiscoveryError,
    CatalogModel,
    ModelCatalog,
    ModelCatalogCache,
    model_catalog_cache_path,
)
from devloop.portable_execution_backend import (
    BackendAvailability,
    ExecutionBackendId,
    load_bundled_model_catalog,
)
from devloop.portable_runtime import (
    PortableRuntimeBridge,
    PortableRuntimeEventKind,
    portable_runtime_session,
)
from devloop.portable_component_catalog import build_portable_component_catalog
from devloop.portable_workflow import (
    ANALYSIS_COMPONENT_ID,
    ANALYSIS_STEP_ID,
    DEVELOPMENT_COMPONENT_ID,
    DEVELOPMENT_STEP_ID,
    FINAL_REVIEW_STEP_ID,
    IMPLEMENTATION_RESULT_CONTRACT,
    QA_COMPONENT_ID,
    REVIEW_DEFAULT_GUIDANCE,
    QA_STEP_ID,
    REVIEW_RESULT_CONTRACT,
    REVIEWER_COMPONENT_ID,
    SECURITY_REVIEW_STEP_ID,
    SECURITY_REVIEW_DEFAULT_GUIDANCE,
    FastPreference,
    IssueStatus,
    PortableRoleAdapter,
    PortableStepComponent,
    PortableStepComponentCatalog,
    PortableWorkflowExecutor,
    PortBinding,
    StepComponentId,
    StepOutcome,
    StepScope,
    default_portable_component_catalog,
    default_portable_workflow,
    load_portable_workflow,
    validate_portable_workflow_for_apply,
)
from devloop.workflow_defaults import WorkflowDefaultStore
from devloop.workflow_editor import (
    BACKEND_SWITCH_KEY,
    EDITOR_COMMAND_GROUPS,
    FREE_TEXT_MODEL_KEY,
    WORKFLOW_ACTIONS,
    EditorResult,
    EditorScope,
    SelectionMenu,
    WorkflowDraft,
    render_model_picker,
    render_workflow_editor,
    run_workflow_editor,
)
from devloop.state import LoopStateWriter
from devloop.step_configuration import GuidanceReviewState, StepGuidance


BUNDLED_CLAUDE_CATALOG = (
    Path(__file__).resolve().parents[1] / "catalogs" / "claude-code-models.json"
)


class FakeEditor:
    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.prompts: list[str] = []

    def read_line(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return next(self._responses)


class _MenuRecorder:
    """Stands in for the arrow-key menu, recording what each view offered."""

    def __init__(self, answers: dict[str, str]) -> None:
        self._answers = answers
        self.menus: list[SelectionMenu] = []

    def select(self, menu: SelectionMenu) -> str:
        self.menus.append(menu)
        for prefix, answer in self._answers.items():
            if menu.title.startswith(prefix):
                return answer
        return menu.cancel_key

    def menu(self, title_prefix: str) -> SelectionMenu:
        for menu in self.menus:
            if menu.title.startswith(title_prefix):
                return menu
        raise AssertionError(
            f"No menu titled {title_prefix!r}; saw {[m.title for m in self.menus]}"
        )


class _RecordingVerifier:
    """The one verification call per selection, driven without a provider."""

    def __init__(
        self,
        resolutions: dict[str, str] | None = None,
        refusals: dict[str, str] | None = None,
    ) -> None:
        self._resolutions = resolutions or {}
        self._refusals = refusals or {}
        self.calls: list[tuple[ExecutionBackendId, str]] = []

    def verify(self, backend: ExecutionBackendId, model_id: str) -> str:
        self.calls.append((backend, model_id))
        refusal = self._refusals.get(model_id)
        if refusal is not None:
            raise RuntimeError(refusal)
        return self._resolutions.get(model_id, model_id)


def _bundled_claude_catalog() -> ModelCatalog:
    return load_bundled_model_catalog(
        BUNDLED_CLAUDE_CATALOG,
        fetched_at="2026-07-25T12:00:00",
    )


def _codex_catalog() -> ModelCatalog:
    return ModelCatalog(
        models=(
            CatalogModel("gpt-5.6-sol", "Sol", "", ("xhigh",), advertises_fast=True),
            CatalogModel("gpt-5.6-luna", "Luna", "", ("high",)),
        ),
        fetched_at="2026-07-25T12:00:00",
    )


def _per_backend_catalog_loader():
    def load(backend: ExecutionBackendId) -> ModelCatalog:
        if backend is ExecutionBackendId.CLAUDE_CODE:
            return _bundled_claude_catalog()
        return _codex_catalog()

    return load


def _fake_availability(*, claude_installed: bool = True):
    def availability() -> tuple[BackendAvailability, ...]:
        return (
            BackendAvailability(ExecutionBackendId.CODEX_CLI, True, "installed"),
            BackendAvailability(
                ExecutionBackendId.CLAUDE_CODE,
                claude_installed,
                "installed" if claude_installed else "not installed",
            ),
        )

    return availability


def _claude_backed_default(
    raw: str,
    component_catalog: PortableStepComponentCatalog,
) -> Path:
    """A stored Workflow Default whose first Workflow Step runs on Claude Code."""
    configuration_path = Path(raw) / "devloop-plan.json"
    document = default_portable_workflow().to_dict()
    document["steps"][0]["execution_settings"] = {
        "backend": "CLAUDE_CODE",
        "model": "claude-sonnet-5",
        "reasoning_effort": "high",
        "fast": "OFF",
    }
    WorkflowDefaultStore(configuration_path, component_catalog).replace(
        load_portable_workflow(document, component_catalog)
    )
    return configuration_path


class PortableComponentCatalogTests(unittest.TestCase):
    def test_component_defaults_reject_rendering_controls(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for display_name in ("Injected\nHeading", "\x1b[2JInjected"):
                with self.subTest(display_name=repr(display_name)):
                    roles = {
                        "security-review": {
                            "step_adapter": "reviewer",
                            "component_id": "example.security-review",
                            "display_name": display_name,
                            "skills": [],
                            "agents": [],
                        }
                    }

                    with self.assertRaisesRegex(
                        ValueError,
                        "control characters or line breaks",
                    ):
                        build_portable_component_catalog(root, roles)


class WorkflowDefaultStoreTests(unittest.TestCase):
    def test_missing_default_loads_builtin_and_replace_preserves_planner_settings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            store = WorkflowDefaultStore(path, default_portable_component_catalog())

            self.assertEqual(store.load(), default_portable_workflow())

            path.write_text(
                json.dumps({"target_repo": "/repo", "selection": {"planning_skills": []}}),
                encoding="utf-8",
            )
            document = default_portable_workflow().to_dict()
            security_review = next(
                step
                for step in document["steps"]
                if step["instance_id"] == SECURITY_REVIEW_STEP_ID
            )
            security_review["display_name"] = "Threat Review"
            edited = load_portable_workflow(
                document,
                default_portable_component_catalog(),
            )

            store.replace(edited)

            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["target_repo"], "/repo")
            self.assertEqual(persisted["selection"], {"planning_skills": []})
            self.assertEqual(
                store.load().step(SECURITY_REVIEW_STEP_ID).display_name,
                "Threat Review",
            )

    def test_load_accepts_an_intact_legacy_sparse_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            workflow = default_portable_workflow()
            document = workflow.to_dict()
            for step in document["steps"]:
                step.pop("capability_profile")
                step.pop("execution_settings")
                step.pop("execution_budget")
            canonical_document = json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            path.write_text(
                json.dumps(
                    {
                        "user_workflow_default": document,
                        "user_workflow_default_hash": hashlib.sha256(
                            canonical_document.encode("utf-8")
                        ).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )

            restored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        self.assertEqual(restored.to_dict(), workflow.to_dict())

    def test_failed_atomic_replace_preserves_the_previous_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            original = '{"target_repo": "/repo"}\n'
            path.write_text(original, encoding="utf-8")
            store = WorkflowDefaultStore(path, default_portable_component_catalog())

            with mock.patch.object(Path, "replace", side_effect=OSError("interrupted")):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    store.replace(default_portable_workflow())

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])


class WorkflowDraftCapabilityTests(unittest.TestCase):
    def test_new_step_copies_component_capability_defaults(self) -> None:
        catalog = default_portable_component_catalog()
        draft = WorkflowDraft(default_portable_workflow(), catalog)

        added_step_id = draft.add(REVIEWER_COMPONENT_ID)

        self.assertEqual(
            draft.workflow.step(added_step_id).capability_profile,
            catalog.resolve(REVIEWER_COMPONENT_ID).default_capability_profile(),
        )
        self.assertEqual(
            draft.workflow.step(added_step_id).guidance,
            REVIEW_DEFAULT_GUIDANCE,
        )

    def test_required_capability_is_enabled_locked_and_explains_the_contract(
        self,
    ) -> None:
        catalog = default_portable_component_catalog()
        draft = WorkflowDraft(default_portable_workflow(), catalog)
        component = catalog.resolve(REVIEWER_COMPONENT_ID)
        required = component.required_capabilities[0]

        self.assertTrue(
            draft.workflow.step(SECURITY_REVIEW_STEP_ID).capability_profile.contains(
                required.reference
            )
        )
        with self.assertRaisesRegex(
            ValueError,
            "locked.*component contract.*senior code-review gate",
        ):
            draft.toggle_capability(
                SECURITY_REVIEW_STEP_ID,
                required.reference,
            )

    def test_reset_step_uses_component_defaults_for_added_repeated_instances(
        self,
    ) -> None:
        catalog = default_portable_component_catalog()
        component = catalog.resolve(REVIEWER_COMPONENT_ID)
        draft = WorkflowDraft(default_portable_workflow(), catalog)
        first_added_id = draft.add(REVIEWER_COMPONENT_ID)
        second_added_id = draft.add(REVIEWER_COMPONENT_ID)

        for step_id, display_name in (
            (first_added_id, "Custom Review One"),
            (second_added_id, "Custom Review Two"),
        ):
            draft.rename(step_id, display_name)
            draft.set_guidance(step_id, "Temporary review focus.")
            draft.toggle_capability(step_id, component.default_capabilities[0])
            draft.reset_step(step_id)

        first_reset = draft.workflow.step(first_added_id)
        second_reset = draft.workflow.step(second_added_id)
        self.assertEqual(first_reset.display_name, "Code Review")
        self.assertEqual(second_reset.display_name, "Code Review 2")
        for reset_step in (first_reset, second_reset):
            self.assertEqual(
                reset_step.capability_profile,
                component.default_capability_profile(),
            )
            self.assertEqual(
                reset_step.execution_settings,
                component.default_execution_settings,
            )
            self.assertEqual(
                reset_step.execution_budget,
                component.execution_budget_defaults,
            )
            self.assertEqual(reset_step.guidance, component.default_guidance)

    def test_reset_typed_builtin_instance_uses_its_current_component_guidance(self) -> None:
        catalog = default_portable_component_catalog()
        draft = WorkflowDraft(default_portable_workflow(), catalog)
        draft.change_type(QA_STEP_ID, REVIEWER_COMPONENT_ID)
        draft.set_guidance(QA_STEP_ID, "Temporary QA review focus.")

        draft.reset_step(QA_STEP_ID)

        reset_step = draft.workflow.step(QA_STEP_ID)
        self.assertEqual(reset_step.component_id, REVIEWER_COMPONENT_ID)
        self.assertEqual(reset_step.guidance, REVIEW_DEFAULT_GUIDANCE)

    def test_copied_guidance_is_typed_needs_review_and_each_resolution_unblocks_apply(
        self,
    ) -> None:
        catalog = default_portable_component_catalog()
        copied = StepGuidance("Review authentication.").marked_for_review()
        self.assertIs(copied.review_state, GuidanceReviewState.NEEDS_REVIEW)

        workflow = default_portable_workflow()
        workflow = replace(
            workflow,
            steps=tuple(
                replace(step, guidance=copied)
                if step.instance_id == SECURITY_REVIEW_STEP_ID
                else step
                for step in workflow.steps
            ),
        )
        for action in ("keep", "edit", "clear"):
            with self.subTest(action=action):
                draft = WorkflowDraft(workflow, catalog)
                if action == "keep":
                    draft.keep_guidance(SECURITY_REVIEW_STEP_ID)
                elif action == "edit":
                    draft.set_guidance(
                        SECURITY_REVIEW_STEP_ID,
                        "Review authorization instead.",
                    )
                else:
                    draft.clear_guidance(SECURITY_REVIEW_STEP_ID)

                validate_portable_workflow_for_apply(draft.workflow, catalog)


class WorkflowDraftTransformationTests(unittest.TestCase):
    def test_duplicate_copies_configuration_and_rewires_success_without_consumers(self) -> None:
        catalog = default_portable_component_catalog()
        draft = WorkflowDraft(default_portable_workflow(), catalog)
        draft.set_guidance(
            SECURITY_REVIEW_STEP_ID,
            "Review authentication boundaries.",
        )
        source = draft.workflow.step(SECURITY_REVIEW_STEP_ID)
        before_duplicate = draft.workflow
        original_successor = source.transitions[StepOutcome.SUCCEEDED]

        result = draft.duplicate(SECURITY_REVIEW_STEP_ID)

        duplicated = draft.workflow.step(result.step_instance_id)
        parsed_id = uuid.UUID(str(duplicated.instance_id))
        self.assertEqual(parsed_id.version, 4)
        self.assertNotEqual(duplicated.instance_id, source.instance_id)
        self.assertEqual(duplicated.display_name, "Security Review 2")
        self.assertEqual(duplicated.component_id, source.component_id)
        self.assertEqual(duplicated.execution_settings, source.execution_settings)
        self.assertEqual(duplicated.execution_budget, source.execution_budget)
        self.assertEqual(duplicated.capability_profile, source.capability_profile)
        self.assertEqual(duplicated.input_bindings, source.input_bindings)
        assert duplicated.guidance is not None
        self.assertEqual(duplicated.guidance.text, source.guidance.text)
        self.assertIs(
            duplicated.guidance.review_state,
            GuidanceReviewState.NEEDS_REVIEW,
        )
        self.assertEqual(
            {
                outcome: target
                for outcome, target in duplicated.transitions.items()
                if outcome is not StepOutcome.SUCCEEDED
            },
            {
                outcome: target
                for outcome, target in source.transitions.items()
                if outcome is not StepOutcome.SUCCEEDED
            },
        )
        self.assertEqual(
            draft.workflow.step(source.instance_id).transitions[StepOutcome.SUCCEEDED],
            duplicated.instance_id,
        )
        self.assertEqual(
            duplicated.transitions[StepOutcome.SUCCEEDED],
            original_successor,
        )
        self.assertFalse(
            any(
                binding.producer_step_id == duplicated.instance_id
                for step in draft.workflow.steps
                for binding in step.input_bindings.values()
            )
        )
        self.assertIn(
            "Security Review 2 output 'review' has no consumer and requires "
            "a deliberate consumer",
            result.warnings,
        )
        serialized_duplicate = next(
            step
            for step in draft.workflow.to_dict()["steps"]
            if step["instance_id"] == duplicated.instance_id
        )
        self.assertNotIn("attempts", serialized_duplicate)
        self.assertNotIn("evidence", serialized_duplicate)
        self.assertTrue(draft.undo())
        self.assertEqual(draft.workflow, before_duplicate)

    def test_apply_accepts_an_installed_workflow_scoped_analysis_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            catalog = build_portable_component_catalog(
                Path(raw),
                {
                    "custom-planner": {
                        "step_adapter": "analysis",
                        "component_id": "example.custom-planner",
                        "display_name": "Custom Planner",
                        "skills": [],
                        "agents": [],
                    }
                },
            )
            draft = WorkflowDraft(default_portable_workflow(), catalog)
            draft.change_type(
                ANALYSIS_STEP_ID,
                StepComponentId("example.custom-planner"),
            )

            applied = validate_portable_workflow_for_apply(
                draft.workflow,
                catalog,
            )

        self.assertEqual(
            applied.step(ANALYSIS_STEP_ID).component_id,
            StepComponentId("example.custom-planner"),
        )
        self.assertEqual(
            catalog.resolve(StepComponentId("example.custom-planner")).scope,
            StepScope.WORKFLOW,
        )

    def test_delete_previews_all_impacts_and_repairs_only_primary_success(self) -> None:
        catalog = default_portable_component_catalog()
        original = default_portable_workflow()
        draft = WorkflowDraft(original, catalog)

        preview = draft.preview_delete(DEVELOPMENT_STEP_ID)

        self.assertEqual(preview.step_display_name, "Development")
        self.assertEqual(len(preview.transition_impacts), 8)
        self.assertIn(
            (DEVELOPMENT_STEP_ID, StepOutcome.SUCCEEDED, SECURITY_REVIEW_STEP_ID),
            {
                (impact.source_step_id, impact.outcome, impact.target_step_id)
                for impact in preview.transition_impacts
            },
        )
        self.assertIn(
            (SECURITY_REVIEW_STEP_ID, StepOutcome.CHANGES_REQUESTED),
            {
                (impact.source_step_id, impact.outcome)
                for impact in preview.transition_impacts
                if impact.target_step_id == DEVELOPMENT_STEP_ID
            },
        )
        self.assertEqual(len(preview.binding_impacts), 3)
        self.assertEqual(
            {
                (impact.consumer_step_id, impact.input_port)
                for impact in preview.binding_impacts
            },
            {
                (SECURITY_REVIEW_STEP_ID, "implementation"),
                (FINAL_REVIEW_STEP_ID, "implementation"),
                (QA_STEP_ID, "implementation"),
            },
        )
        assert preview.primary_path_repair is not None
        self.assertEqual(
            preview.primary_path_repair.predecessor_step_id,
            original.start_step_id,
        )
        self.assertEqual(
            preview.primary_path_repair.successor_step_id,
            SECURITY_REVIEW_STEP_ID,
        )

        draft.delete(preview)

        self.assertNotIn(
            DEVELOPMENT_STEP_ID,
            {step.instance_id for step in draft.workflow.steps},
        )
        self.assertEqual(
            draft.workflow.step(original.start_step_id).transitions[
                StepOutcome.SUCCEEDED
            ],
            SECURITY_REVIEW_STEP_ID,
        )
        for consumer_id in (
            SECURITY_REVIEW_STEP_ID,
            FINAL_REVIEW_STEP_ID,
            QA_STEP_ID,
        ):
            self.assertEqual(
                draft.workflow.step(consumer_id)
                .input_bindings["implementation"]
                .producer_step_id,
                DEVELOPMENT_STEP_ID,
            )
        self.assertEqual(len(draft.workflow.steps), len(original.steps) - 1)
        self.assertEqual(
            draft.workflow.step(SECURITY_REVIEW_STEP_ID).transitions[
                StepOutcome.CHANGES_REQUESTED
            ],
            DEVELOPMENT_STEP_ID,
        )
        with self.assertRaisesRegex(ValueError, "unknown Step Instance ID"):
            validate_portable_workflow_for_apply(draft.workflow, catalog)

        self.assertTrue(draft.undo())
        self.assertEqual(draft.workflow, original)

    def test_delete_rejects_a_preview_after_the_draft_changes(self) -> None:
        catalog = default_portable_component_catalog()
        draft = WorkflowDraft(default_portable_workflow(), catalog)
        preview = draft.preview_delete(SECURITY_REVIEW_STEP_ID)
        draft.rename(FINAL_REVIEW_STEP_ID, "Release Review")

        with self.assertRaisesRegex(ValueError, "changed after the deletion preview"):
            draft.delete(preview)

        self.assertEqual(
            draft.workflow.step(SECURITY_REVIEW_STEP_ID).display_name,
            "Security Review",
        )

    def test_type_change_preserves_identity_and_position_while_resetting_type_state(
        self,
    ) -> None:
        catalog = default_portable_component_catalog()
        draft = WorkflowDraft(default_portable_workflow(), catalog)
        draft.set_guidance(
            SECURITY_REVIEW_STEP_ID,
            "Review authentication boundaries.",
        )
        before = draft.workflow
        source = before.step(SECURITY_REVIEW_STEP_ID)
        source_position = next(
            index
            for index, step in enumerate(before.primary_path(), start=1)
            if step.instance_id == source.instance_id
        )
        replacement_component = catalog.resolve(QA_COMPONENT_ID)

        draft.change_type(SECURITY_REVIEW_STEP_ID, QA_COMPONENT_ID)

        changed = draft.workflow.step(SECURITY_REVIEW_STEP_ID)
        changed_position = next(
            index
            for index, step in enumerate(draft.workflow.primary_path(), start=1)
            if step.instance_id == changed.instance_id
        )
        self.assertEqual(changed.instance_id, source.instance_id)
        self.assertEqual(changed.display_name, source.display_name)
        self.assertEqual(changed_position, source_position)
        self.assertEqual(changed.component_id, QA_COMPONENT_ID)
        self.assertEqual(
            changed.execution_settings,
            replacement_component.default_execution_settings,
        )
        self.assertEqual(
            changed.execution_budget,
            replacement_component.execution_budget_defaults,
        )
        self.assertEqual(
            changed.capability_profile,
            replacement_component.default_capability_profile(),
        )
        self.assertEqual(changed.input_bindings, {})
        self.assertEqual(
            set(changed.transitions),
            set(replacement_component.supported_outcomes),
        )
        self.assertEqual(
            changed.transitions[StepOutcome.SUCCEEDED],
            source.transitions[StepOutcome.SUCCEEDED],
        )
        self.assertTrue(
            all(
                target is None
                for outcome, target in changed.transitions.items()
                if outcome is not StepOutcome.SUCCEEDED
            )
        )
        assert changed.guidance is not None
        self.assertEqual(changed.guidance.text, source.guidance.text)
        self.assertIs(
            changed.guidance.review_state,
            GuidanceReviewState.NEEDS_REVIEW,
        )
        with self.assertRaisesRegex(ValueError, "NEEDS_REVIEW"):
            validate_portable_workflow_for_apply(draft.workflow, catalog)

        self.assertTrue(draft.undo())
        self.assertEqual(draft.workflow, before)

    def test_type_change_leaves_affected_consumer_binding_visible_until_repaired(
        self,
    ) -> None:
        catalog = default_portable_component_catalog()
        draft = WorkflowDraft(default_portable_workflow(), catalog)

        draft.change_type(FINAL_REVIEW_STEP_ID, DEVELOPMENT_COMPONENT_ID)

        qa_binding = draft.workflow.step(QA_STEP_ID).input_bindings["review_result"]
        self.assertEqual(qa_binding.producer_step_id, FINAL_REVIEW_STEP_ID)
        self.assertEqual(qa_binding.output_port, "review")
        with self.assertRaisesRegex(ValueError, "has no output port 'review'"):
            validate_portable_workflow_for_apply(draft.workflow, catalog)

    def test_type_change_cannot_run_a_workflow_scoped_step_per_issue(self) -> None:
        catalog = default_portable_component_catalog()
        draft = WorkflowDraft(default_portable_workflow(), catalog)

        draft.change_type(SECURITY_REVIEW_STEP_ID, ANALYSIS_COMPONENT_ID)

        for create_execution in (
            lambda: validate_portable_workflow_for_apply(draft.workflow, catalog),
            lambda: PortableWorkflowExecutor(draft.workflow, catalog, object()),
        ):
            with self.subTest(create_execution=create_execution):
                with self.assertRaisesRegex(
                    ValueError,
                    "Development.*ISSUE.*Security Review.*WORKFLOW.*once per Issue",
                ):
                    create_execution()

    def test_deleted_optional_producer_binding_requires_explicit_repair(self) -> None:
        builtin_catalog = default_portable_component_catalog()
        release_component = PortableStepComponent(
            component_id=StepComponentId("example.release-review"),
            default_display_name="Release Review",
            scope=StepScope.ISSUE,
            supported_outcomes=frozenset({StepOutcome.SUCCEEDED}),
            adapter=PortableRoleAdapter("reviewer"),
            input_ports={"implementation": IMPLEMENTATION_RESULT_CONTRACT},
            optional_input_ports={"review_context": REVIEW_RESULT_CONTRACT},
        )
        catalog = PortableStepComponentCatalog(
            (*builtin_catalog.components, release_component)
        )
        draft = WorkflowDraft(default_portable_workflow(), catalog)
        release_step_id = draft.add(release_component.component_id)
        deleted_binding = PortBinding(SECURITY_REVIEW_STEP_ID, "review")
        draft.set_binding(release_step_id, "review_context", deleted_binding)

        draft.delete(draft.preview_delete(SECURITY_REVIEW_STEP_ID))

        self.assertEqual(
            draft.workflow.step(release_step_id).input_bindings["review_context"],
            deleted_binding,
        )
        rendered = render_workflow_editor(
            draft.workflow,
            release_step_id,
            catalog,
            terminal_width=160,
            show_advanced=True,
        )
        self.assertIn(
            f"Current: [deleted Step Instance {SECURITY_REVIEW_STEP_ID}].review",
            rendered,
        )
        with self.assertRaisesRegex(ValueError, "unknown producer"):
            validate_portable_workflow_for_apply(draft.workflow, catalog)

        draft.set_binding(release_step_id, "review_context", None)

        validate_portable_workflow_for_apply(draft.workflow, catalog)


class WorkflowEditorFlowTests(unittest.TestCase):
    def test_duplicate_command_reports_unused_outputs_and_applies_the_copy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    [
                        "3",
                        "duplicate",
                        "apply",
                        "guidance",
                        "keep",
                        "apply",
                    ]
                ).read_line,
                write=output.append,
                terminal_width=120,
            )
            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        copied = next(
            step for step in stored.steps if step.display_name == "Security Review 2"
        )
        primary_ids = [step.instance_id for step in stored.primary_path()]
        source_position = primary_ids.index(SECURITY_REVIEW_STEP_ID)
        self.assertIs(result, EditorResult.APPLIED)
        assert copied.guidance is not None
        self.assertIs(copied.guidance.review_state, GuidanceReviewState.READY)
        self.assertEqual(primary_ids[source_position + 1], copied.instance_id)
        self.assertIn(
            "Warning: Security Review 2 output 'review' has no consumer",
            "\n".join(output),
        )

    def test_delete_command_previews_impacts_and_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []
            editor = FakeEditor(["3", "delete", "no", "apply"])

            result = run_workflow_editor(
                path,
                read_line=editor.read_line,
                write=output.append,
                terminal_width=120,
            )
            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.APPLIED)
        self.assertEqual(stored, default_portable_workflow())
        self.assertIn("Delete Preview — Security Review", rendered)
        self.assertIn("Development.SUCCEEDED -> Security Review", rendered)
        self.assertIn(
            "Security Review.implementation <- Development.implementation",
            rendered,
        )
        self.assertIn(
            "Bindings sourced from the deleted step will remain unresolved until",
            rendered,
        )
        self.assertIn(
            "Primary Path repair: Development.SUCCEEDED -> Final Review",
            rendered,
        )
        self.assertIn("No downstream Workflow Steps will be deleted.", rendered)
        self.assertIn("Type yes to delete 'Security Review': ", editor.prompts)

    def test_confirmed_delete_is_non_cascading_and_blocks_apply_for_broken_refs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    ["delete", "yes", "apply", "graph", "cancel"]
                ).read_line,
                write=output.append,
                terminal_width=120,
            )

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.CANCELLED)
        self.assertFalse(path.exists())
        self.assertIn("Security Review", rendered)
        self.assertIn("Final Review", rendered)
        self.assertIn("QA", rendered)
        self.assertIn(
            f"[deleted Step Instance {DEVELOPMENT_STEP_ID}]",
            rendered,
        )
        self.assertIn("targets unknown Step Instance ID", rendered)

    def test_deleted_producer_inputs_remain_inspectable_for_repair(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output: list[str] = []

            result = run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=FakeEditor(
                    ["delete", "yes", "select", "2", "advanced", "cancel"]
                ).read_line,
                write=output.append,
                terminal_width=160,
                terminal_height=50,
            )

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.CANCELLED)
        self.assertIn("Settings — Security Review", rendered)
        self.assertIn(str(DEVELOPMENT_STEP_ID), rendered)
        self.assertIn("binds unknown producer", rendered)

    def test_duplicate_delete_original_and_select_qa_keeps_draft_repairable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output: list[str] = []

            result = run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=FakeEditor(
                    [
                        "duplicate",
                        "2",
                        "delete",
                        "yes",
                        "5",
                        "advanced",
                        "cancel",
                    ]
                ).read_line,
                write=output.append,
                terminal_width=160,
                terminal_height=50,
            )

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.CANCELLED)
        self.assertIn("Settings — QA", rendered)
        self.assertIn(str(DEVELOPMENT_STEP_ID), rendered)
        self.assertIn("Advanced Port Bindings", rendered)

    def test_confirmed_unambiguous_delete_applies_without_cascading(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(["3", "delete", "yes", "apply"]).read_line,
                write=lambda _: None,
                terminal_width=120,
            )
            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        self.assertIs(result, EditorResult.APPLIED)
        self.assertNotIn(
            SECURITY_REVIEW_STEP_ID,
            {step.instance_id for step in stored.steps},
        )
        self.assertIn(FINAL_REVIEW_STEP_ID, {step.instance_id for step in stored.steps})
        self.assertIn(QA_STEP_ID, {step.instance_id for step in stored.steps})
        self.assertEqual(
            stored.step(DEVELOPMENT_STEP_ID).transitions[StepOutcome.SUCCEEDED],
            FINAL_REVIEW_STEP_ID,
        )

    def test_type_command_resets_configuration_and_preserves_identity_on_apply(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    ["5", "type", "2", "bind", "1", "1", "apply"]
                ).read_line,
                write=output.append,
                terminal_width=120,
            )
            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        changed = stored.step(QA_STEP_ID)
        self.assertIs(result, EditorResult.APPLIED)
        self.assertEqual(changed.instance_id, QA_STEP_ID)
        self.assertEqual(changed.display_name, "QA")
        self.assertEqual(changed.component_id, REVIEWER_COMPONENT_ID)
        self.assertEqual(changed.guidance, REVIEW_DEFAULT_GUIDANCE)
        self.assertEqual(stored.primary_path()[-1].instance_id, QA_STEP_ID)
        self.assertEqual(
            changed.input_bindings["implementation"].producer_step_id,
            DEVELOPMENT_STEP_ID,
        )
        self.assertIn("Type Change Preview — QA", "\n".join(output))
        self.assertIn(
            "Reset: Step Execution Settings, Execution Budget, capabilities, "
            "ports, bindings, and outcomes",
            "\n".join(output),
        )

    def test_type_change_guidance_must_be_resolved_before_apply(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    [
                        "5",
                        "guidance",
                        "edit",
                        "Check release evidence.",
                        ".",
                        "type",
                        "2",
                        "bind",
                        "1",
                        "1",
                        "apply",
                        "guidance",
                        "keep",
                        "apply",
                    ]
                ).read_line,
                write=output.append,
                terminal_width=120,
            )
            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        guidance = stored.step(QA_STEP_ID).guidance
        assert guidance is not None
        self.assertIs(result, EditorResult.APPLIED)
        self.assertEqual(guidance.text, "Check release evidence.")
        self.assertIs(guidance.review_state, GuidanceReviewState.READY)
        self.assertIn("has Step Guidance in NEEDS_REVIEW", "\n".join(output))

    def test_type_change_to_local_step_preserves_guidance_until_it_is_cleared(self) -> None:
        builtin_catalog = default_portable_component_catalog()
        local_component = PortableStepComponent(
            component_id=StepComponentId("example.local-check"),
            default_display_name="Local Check",
            scope=StepScope.ISSUE,
            supported_outcomes=frozenset({StepOutcome.SUCCEEDED}),
            adapter=None,
        )
        catalog = PortableStepComponentCatalog(
            (*builtin_catalog.components, local_component)
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    [
                        "3",
                        "guidance",
                        "edit",
                        "Review local policy.",
                        ".",
                        "type",
                        "5",
                        "apply",
                        "guidance",
                        "clear",
                        "apply",
                    ]
                ).read_line,
                write=output.append,
                terminal_width=120,
                catalog=catalog,
            )
            stored = WorkflowDefaultStore(path, catalog).load()

        changed = stored.step(SECURITY_REVIEW_STEP_ID)
        self.assertIs(result, EditorResult.APPLIED)
        self.assertEqual(changed.component_id, local_component.component_id)
        self.assertIsNone(changed.guidance)
        self.assertIn("has Step Guidance in NEEDS_REVIEW", "\n".join(output))

    def test_multiline_step_guidance_is_transactional_and_shows_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    [
                        "3",
                        "guidance",
                        "edit",
                        "Review authentication boundaries.",
                        "Focus on privilege escalation.",
                        ".",
                        "apply",
                    ]
                ).read_line,
                write=output.append,
                terminal_width=120,
            )
            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        guidance = stored.step(SECURITY_REVIEW_STEP_ID).guidance
        assert guidance is not None
        self.assertIs(result, EditorResult.APPLIED)
        self.assertEqual(
            guidance.text,
            "Review authentication boundaries.\nFocus on privilege escalation.",
        )
        self.assertIn("Review authentication boundaries.", "\n".join(output))
        self.assertIn("Enter Step Guidance one line at a time", "\n".join(output))

    def test_graph_preview_is_hidden_until_the_graph_command_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output: list[str] = []

            run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=FakeEditor(["cancel"]).read_line,
                write=output.append,
                terminal_width=120,
            )

        self.assertIn("Route map hidden", output[0])
        self.assertNotIn("Route Map", output[0])

    def test_graph_preview_lists_every_supported_outcome_and_explicit_terminals(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output: list[str] = []

            run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=FakeEditor(["graph", "cancel"]).read_line,
                write=output.append,
                terminal_width=120,
                terminal_height=40,
            )

        rendered = next(frame for frame in output if "Route Map" in frame)
        self.assertIn(
            "Development --BLOCKED--> Terminal",
            rendered,
        )
        self.assertIn("Development --BLOCKED--> Terminal", rendered)

    def test_advanced_bindings_show_typed_required_optional_and_compatible_sources(
        self,
    ) -> None:
        builtin_catalog = default_portable_component_catalog()
        custom_component = PortableStepComponent(
            component_id=StepComponentId("example.release-review"),
            default_display_name="Release Review",
            scope=StepScope.ISSUE,
            supported_outcomes=frozenset({StepOutcome.SUCCEEDED}),
            adapter=PortableRoleAdapter("reviewer"),
            input_ports={"implementation": IMPLEMENTATION_RESULT_CONTRACT},
            optional_input_ports={"review_context": REVIEW_RESULT_CONTRACT},
        )
        catalog = PortableStepComponentCatalog(
            (*builtin_catalog.components, custom_component)
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(["add", "5", "advanced", "apply"]).read_line,
                write=output.append,
                terminal_width=240,
                catalog=catalog,
            )
            stored = WorkflowDefaultStore(path, catalog).load()

        rendered = "\n".join(output)
        release_review = next(
            step for step in stored.steps if step.display_name == "Release Review"
        )
        self.assertIs(result, EditorResult.APPLIED)
        self.assertNotIn("review_context", release_review.input_bindings)
        self.assertIn(
            "implementation [required] devloop.implementation-result@1",
            rendered,
        )
        self.assertIn(
            "review_context [optional] devloop.review-result@1",
            rendered,
        )
        self.assertIn("Current: Development.implementation", rendered)
        self.assertIn(
            "Compatible: Security Review.review, Final Review.review",
            rendered,
        )

    def test_advanced_bindings_accept_and_display_a_failed_outcome_binding(
        self,
    ) -> None:
        self._assert_advanced_binding_accepts_outcome(StepOutcome.FAILED)

    def test_advanced_bindings_accept_and_display_a_blocked_outcome_binding(
        self,
    ) -> None:
        self._assert_advanced_binding_accepts_outcome(StepOutcome.BLOCKED)

    def _assert_advanced_binding_accepts_outcome(
        self,
        allowed_outcome: StepOutcome,
    ) -> None:
        catalog = default_portable_component_catalog()
        document = default_portable_workflow().to_dict()
        analysis, development, security_review = document["steps"][:3]
        development["transitions"] = {
            "SUCCEEDED": None,
            "BLOCKED": None,
            "FAILED": None,
            "CANCELLED": None,
        }
        development["transitions"][allowed_outcome.value] = str(
            SECURITY_REVIEW_STEP_ID
        )
        security_review["transitions"] = {
            outcome.value: None for outcome in StepOutcome
        }
        security_review["input_bindings"]["implementation"][
            "allowed_outcomes"
        ] = [allowed_outcome.value]
        document["steps"] = [analysis, development, security_review]
        workflow = load_portable_workflow(document, catalog)

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []
            WorkflowDefaultStore(path, catalog).replace(workflow)

            run_workflow_editor(
                path,
                read_line=FakeEditor(
                    ["select", "3", "advanced", "cancel"]
                ).read_line,
                write=output.append,
                terminal_width=240,
                catalog=catalog,
            )

        frame = next(
            rendered
            for rendered in reversed(output)
            if "Advanced Port Bindings" in rendered
            and "Settings — Security Review" in rendered
        )
        self.assertIn("Current: Development.implementation", frame)
        self.assertIn(f"Allowed outcomes: {allowed_outcome.value}", frame)
        self.assertIn("Compatible: None", frame)
        self.assertNotIn("incompatible or not definitely available", frame)

    def test_ambiguous_binding_requires_and_persists_an_explicit_producer_choice(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    ["add", "3", "bind", "2", "2", "apply"]
                ).read_line,
                write=lambda _: None,
                terminal_width=120,
            )

            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        added_qa = next(step for step in stored.steps if step.display_name == "QA 2")
        self.assertIs(result, EditorResult.APPLIED)
        self.assertEqual(
            added_qa.input_bindings["review_result"].producer_step_id,
            FINAL_REVIEW_STEP_ID,
        )

    def test_advanced_bindings_exclude_a_compatible_but_downstream_branch_producer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output: list[str] = []

            run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=FakeEditor(
                    [
                        "3",
                        "route",
                        "3",
                        "new",
                        "2",
                        "route",
                        "1",
                        "insert",
                        "1",
                        "select",
                        "6",
                        "advanced",
                        "cancel",
                    ]
                ).read_line,
                write=output.append,
                terminal_width=240,
            )

        branch_frame = next(
            frame
            for frame in reversed(output)
            if "Advanced Port Bindings" in frame and "Settings — Code Review" in frame
        )
        self.assertIn("Compatible: Development.implementation", branch_frame)
        self.assertNotIn("Development 2.implementation", branch_frame)

    def test_disallowed_outcome_route_is_not_displayed_or_automatically_bound(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    ["route", "2", "new", "2", "advanced", "apply", "cancel"]
                ).read_line,
                write=output.append,
                terminal_width=240,
            )

        branch_frame = next(
            frame
            for frame in reversed(output)
            if "Advanced Port Bindings" in frame
            and "Settings — Code Review" in frame
        )
        self.assertIs(result, EditorResult.CANCELLED)
        self.assertFalse(path.exists())
        self.assertIn("Current: Unbound", branch_frame)
        self.assertIn("Compatible: None", branch_frame)
        self.assertIn("missing required input bindings", "\n".join(output))

    def test_apply_rejects_a_binding_when_an_executable_path_bypasses_its_producer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    [
                        "3",
                        "route",
                        "3",
                        "existing",
                        "5",
                        "5",
                        "advanced",
                        "apply",
                        "cancel",
                    ]
                ).read_line,
                write=output.append,
                terminal_width=240,
                terminal_height=60,
            )

        qa_frame = next(
            frame
            for frame in reversed(output)
            if "Advanced Port Bindings" in frame and "Settings — QA" in frame
        )
        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.CANCELLED)
        self.assertFalse(path.exists())
        self.assertIn("Current: Final Review.review", qa_frame)
        self.assertIn("Compatible: None", qa_frame)
        self.assertRegex(
            rendered,
            r"QA.*review_result.*every executable path",
        )

    def test_route_outcome_to_existing_step_creates_loop_and_updates_graph_preview(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    ["3", "route", "3", "existing", "2", "graph", "apply"]
                ).read_line,
                write=output.append,
                terminal_width=120,
                terminal_height=40,
            )

            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        self.assertIs(result, EditorResult.APPLIED)
        self.assertEqual(
            stored.step(SECURITY_REVIEW_STEP_ID).transitions[StepOutcome.BLOCKED],
            DEVELOPMENT_STEP_ID,
        )
        self.assertIn(
            "Security Review --BLOCKED--> Development",
            "\n".join(output),
        )

    def test_succeeded_cycle_is_rejected_without_crashing_the_editor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output: list[str] = []

            result = run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=FakeEditor(
                    ["3", "route", "1", "existing", "2", "cancel"]
                ).read_line,
                write=output.append,
                terminal_width=120,
            )

        self.assertIs(result, EditorResult.CANCELLED)
        self.assertIn(
            "Cannot route outcome: The SUCCEEDED Primary Path cannot contain a loop",
            "\n".join(output),
        )

    def test_create_step_on_outcome_adds_an_auto_bound_branch_without_global_position(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    ["3", "route", "3", "new", "2", "apply"]
                ).read_line,
                write=output.append,
                terminal_width=120,
            )

            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        branch = next(step for step in stored.steps if step.display_name == "Code Review")
        self.assertIs(result, EditorResult.APPLIED)
        self.assertNotIn(branch, stored.primary_path())
        self.assertEqual(
            stored.step(SECURITY_REVIEW_STEP_ID).transitions[StepOutcome.BLOCKED],
            branch.instance_id,
        )
        self.assertEqual(
            branch.input_bindings["implementation"].producer_step_id,
            DEVELOPMENT_STEP_ID,
        )
        self.assertIn(
            "Position: branch-only",
            "\n".join(output),
        )

    def test_branch_step_move_command_is_rejected_without_assigning_a_position(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output: list[str] = []

            result = run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=FakeEditor(
                    ["3", "route", "3", "new", "2", "move-up", "cancel"]
                ).read_line,
                write=output.append,
                terminal_width=120,
            )

        self.assertIs(result, EditorResult.CANCELLED)
        self.assertIn(
            "Only Primary Path steps have an editable Position.",
            "\n".join(output),
        )

    def test_insert_step_on_route_preserves_destination_as_branch_local_successor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    ["3", "route", "2", "insert", "2", "apply"]
                ).read_line,
                write=lambda _: None,
                terminal_width=120,
            )

            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        inserted = next(step for step in stored.steps if step.display_name == "Code Review")
        self.assertIs(result, EditorResult.APPLIED)
        self.assertEqual(
            stored.step(SECURITY_REVIEW_STEP_ID).transitions[
                StepOutcome.CHANGES_REQUESTED
            ],
            inserted.instance_id,
        )
        self.assertEqual(
            inserted.transitions[StepOutcome.SUCCEEDED],
            DEVELOPMENT_STEP_ID,
        )
        self.assertNotIn(inserted, stored.primary_path())

    def test_route_outcome_to_explicit_terminal_persists_intent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    ["3", "route", "2", "terminal", "apply"]
                ).read_line,
                write=lambda _: None,
                terminal_width=120,
            )

            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        transitions = stored.step(SECURITY_REVIEW_STEP_ID).transitions
        self.assertIs(result, EditorResult.APPLIED)
        self.assertIn(StepOutcome.CHANGES_REQUESTED, transitions)
        self.assertIsNone(transitions[StepOutcome.CHANGES_REQUESTED])

    def test_select_command_keeps_branch_steps_keyboard_accessible(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    [
                        "3",
                        "route",
                        "3",
                        "new",
                        "2",
                        "2",
                        "select",
                        "6",
                        "rename",
                        "Branch Review",
                        "apply",
                    ]
                ).read_line,
                write=lambda _: None,
                terminal_width=120,
            )

            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        self.assertIs(result, EditorResult.APPLIED)
        branch_id = stored.step(SECURITY_REVIEW_STEP_ID).transitions[StepOutcome.BLOCKED]
        assert branch_id is not None
        self.assertEqual(stored.step(branch_id).display_name, "Branch Review")
        self.assertEqual(stored.step(DEVELOPMENT_STEP_ID).display_name, "Development")

    def test_apply_blocks_an_orphaned_branch_with_step_and_transition_guidance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    [
                        "3",
                        "route",
                        "3",
                        "new",
                        "2",
                        "3",
                        "route",
                        "3",
                        "terminal",
                        "apply",
                        "cancel",
                    ]
                ).read_line,
                write=output.append,
                terminal_width=120,
            )

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.CANCELLED)
        self.assertFalse(path.exists())
        self.assertIn("Cannot apply workflow: Step 'Code Review'", rendered)
        self.assertRegex(
            rendered,
            r"unreachable; repair an Outcome\s+Transition",
        )

    def test_invalid_step_selection_is_rejected_without_crashing(self) -> None:
        for selection in ("²", "9" * 10_000):
            with self.subTest(selection=selection[:10]), tempfile.TemporaryDirectory() as raw:
                output: list[str] = []

                result = run_workflow_editor(
                    Path(raw) / "devloop-plan.json",
                    read_line=FakeEditor([selection, "cancel"]).read_line,
                    write=output.append,
                    terminal_width=100,
                )

                self.assertIs(result, EditorResult.CANCELLED)
                self.assertIn("step number", "\n".join(output))

    def test_type_picker_lists_builtin_and_custom_portable_components(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            builtin_catalog = default_portable_component_catalog()
            custom_component = PortableStepComponent(
                component_id=StepComponentId("example.security-scan"),
                default_display_name="Security Scan",
                scope=StepScope.ISSUE,
                supported_outcomes=frozenset({StepOutcome.SUCCEEDED}),
                adapter=PortableRoleAdapter("reviewer"),
            )
            catalog = PortableStepComponentCatalog(
                (*builtin_catalog.components, custom_component)
            )
            output: list[str] = []

            run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=FakeEditor(["add", "cancel", "cancel"]).read_line,
                write=output.append,
                terminal_width=100,
                catalog=catalog,
            )

        rendered = "\n".join(output)
        self.assertIn("Workflow Step Types", rendered)
        self.assertIn("Development (ISSUE)", rendered)
        self.assertIn("Security Scan (ISSUE)", rendered)
        self.assertIn("example.security-scan", rendered)

    def test_invalid_component_selection_is_rejected_without_crashing(
        self,
    ) -> None:
        for selection in ("²", "9" * 10_000):
            with self.subTest(selection=selection[:10]), tempfile.TemporaryDirectory() as raw:
                output: list[str] = []

                result = run_workflow_editor(
                    Path(raw) / "devloop-plan.json",
                    read_line=FakeEditor(["add", selection, "cancel"]).read_line,
                    write=output.append,
                    terminal_width=100,
                )

                self.assertIs(result, EditorResult.CANCELLED)
                self.assertIn(
                    "Choose an installed Workflow Step Type by number",
                    "\n".join(output),
                )

    def test_add_appends_a_unique_default_instance_and_auto_binds_one_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(["add", "2", "apply"]).read_line,
                write=lambda _: None,
                terminal_width=100,
            )

            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        added = stored.primary_path()[-1]
        self.assertIs(result, EditorResult.APPLIED)
        self.assertEqual(added.display_name, "Code Review")
        self.assertEqual(uuid.UUID(str(added.instance_id)).version, 4)
        self.assertEqual(
            len({step.instance_id for step in stored.steps}),
            len(stored.steps),
        )
        self.assertEqual(
            added.input_bindings["implementation"].producer_step_id,
            DEVELOPMENT_STEP_ID,
        )
        self.assertIsNone(added.transitions[StepOutcome.SUCCEEDED])

    def test_add_retries_uuid_generation_when_an_instance_id_collides(self) -> None:
        replacement_id = uuid.UUID("c68fce09-f2c2-4a78-a74b-797d1f24ec4a")
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            generated_ids = (
                uuid.UUID(str(DEVELOPMENT_STEP_ID)),
                replacement_id,
            )

            with mock.patch(
                "devloop.workflow_editor.uuid.uuid4",
                side_effect=generated_ids,
            ):
                result = run_workflow_editor(
                    path,
                    read_line=FakeEditor(["add", "2", "apply", "cancel"]).read_line,
                    write=lambda _: None,
                    terminal_width=100,
                )

            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        reviewer_steps = [
            step
            for step in stored.steps
            if step.component_id == REVIEWER_COMPONENT_ID
        ]
        self.assertIs(result, EditorResult.APPLIED)
        self.assertIn(
            str(replacement_id),
            {step.instance_id for step in reviewer_steps},
        )
        self.assertEqual(
            len({step.instance_id for step in stored.steps}),
            len(stored.steps),
        )

    def test_insert_rewires_succeeded_order_without_changing_existing_ids(self) -> None:
        before = default_portable_workflow()
        before_ids = {step.instance_id for step in before.steps}
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"

            run_workflow_editor(
                path,
                read_line=FakeEditor(["insert", "2", "3", "apply"]).read_line,
                write=lambda _: None,
                terminal_width=100,
            )

            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        primary_path = stored.primary_path()
        self.assertEqual(
            [step.display_name for step in primary_path],
            [
                "Analysis",
                "Development",
                "Code Review",
                "Security Review",
                "Final Review",
                "QA",
            ],
        )
        self.assertTrue(before_ids.issubset({step.instance_id for step in stored.steps}))
        for index, step in enumerate(primary_path, start=1):
            expected_target = (
                primary_path[index].instance_id if index < len(primary_path) else None
            )
            self.assertEqual(
                step.transitions[StepOutcome.SUCCEEDED],
                expected_target,
            )

    def test_ambiguous_auto_binding_stays_unresolved_and_blocks_apply(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(["add", "3", "apply", "cancel"]).read_line,
                write=output.append,
                terminal_width=100,
            )

            self.assertFalse(path.exists())

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.CANCELLED)
        self.assertIn(
            "Input review_result: AMBIGUOUS (2 sources)",
            rendered,
        )
        self.assertIn("Cannot apply workflow", rendered)
        self.assertIn("missing required input bindings", rendered)

    def test_missing_auto_binding_stays_visible_for_deliberate_repair(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output: list[str] = []

            run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=FakeEditor(
                    ["insert", "2", "1", "apply", "cancel"]
                ).read_line,
                write=output.append,
                terminal_width=100,
            )

        rendered = "\n".join(output)
        self.assertIn(
            "Input implementation: MISSING (no source)",
            rendered,
        )
        self.assertIn("Cannot apply workflow", rendered)

    def test_move_up_reorders_succeeded_transitions_and_preserves_step_ids(self) -> None:
        original_ids = {
            step.display_name: step.instance_id
            for step in default_portable_workflow().steps
        }
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"

            run_workflow_editor(
                path,
                read_line=FakeEditor(["4", "move-up", "apply"]).read_line,
                write=lambda _: None,
                terminal_width=100,
            )

            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        primary_path = stored.primary_path()
        self.assertEqual(
            [step.display_name for step in primary_path],
            ["Analysis", "Development", "Final Review", "Security Review", "QA"],
        )
        self.assertEqual(
            {step.display_name: step.instance_id for step in stored.steps},
            original_ids,
        )

    def test_move_down_moves_the_selected_step_one_contiguous_position(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"

            run_workflow_editor(
                path,
                read_line=FakeEditor(["3", "move-down", "apply"]).read_line,
                write=lambda _: None,
                terminal_width=100,
            )

            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        self.assertEqual(
            [step.display_name for step in stored.primary_path()],
            ["Analysis", "Development", "Final Review", "Security Review", "QA"],
        )

    def test_moving_qa_before_its_review_producer_leaves_the_input_unresolved(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    ["5", "position", "3", "apply", "cancel"]
                ).read_line,
                write=output.append,
                terminal_width=100,
            )

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.CANCELLED)
        self.assertFalse(path.exists())
        self.assertIn("Input review_result: MISSING (no source)", rendered)
        self.assertIn("Cannot apply workflow", rendered)

    def test_moving_development_after_consumers_unresolves_their_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    ["2", "position", "4", "2", "apply", "cancel"]
                ).read_line,
                write=output.append,
                terminal_width=100,
            )

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.CANCELLED)
        self.assertFalse(path.exists())
        self.assertIn("Input implementation: MISSING (no source)", rendered)
        self.assertIn("Cannot apply workflow", rendered)

    def test_position_moves_directly_and_displays_contiguous_one_based_positions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            run_workflow_editor(
                path,
                read_line=FakeEditor(["4", "position", "3", "apply"]).read_line,
                write=output.append,
                terminal_width=100,
            )

            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        self.assertEqual(
            [step.display_name for step in stored.primary_path()],
            ["Analysis", "Development", "Final Review", "Security Review", "QA"],
        )
        moved_frame = next(
            frame for frame in output if "Position: 3/5" in frame
        )
        for position in range(1, 6):
            self.assertIn(f"{position}.", moved_frame)

    def test_invalid_and_oversized_positions_are_rejected_consistently(self) -> None:
        oversized = "9" * 10_000
        cases = (
            ("move", ["position"], "²"),
            ("move oversized", ["position"], oversized),
            ("insert", ["insert", "2"], "²"),
            ("insert oversized", ["insert", "2"], oversized),
        )

        for label, commands, invalid_position in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as raw:
                output: list[str] = []
                result = run_workflow_editor(
                    Path(raw) / "devloop-plan.json",
                    read_line=FakeEditor(
                        [*commands, invalid_position, "cancel"]
                    ).read_line,
                    write=output.append,
                    terminal_width=100,
                )

                rendered = "\n".join(output)
                self.assertIs(result, EditorResult.CANCELLED)
                self.assertIn(
                    "Primary Path Position must be a one-based number.",
                    rendered,
                )
                self.assertNotIn("invalid literal", rendered)
                self.assertNotIn("integer string conversion", rendered)

    def test_applied_inserted_primary_path_completes_a_portable_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = root / "devloop-plan.json"
            issue_path = root / "0004.md"
            issue_path.write_text("# Edited workflow\n", encoding="utf-8")
            issue = Issue("0004", "Edited workflow", issue_path, False)
            catalog = default_portable_component_catalog()

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(["insert", "2", "3", "apply"]).read_line,
                write=lambda _: None,
                terminal_width=100,
                catalog=catalog,
            )
            workflow = WorkflowDefaultStore(configuration_path, catalog).load()
            calls: list[str] = []

            class PassingRoleRunner:
                def run_role(self, **arguments: object) -> RoleResult:
                    calls.append(str(arguments["step_display_name"]))
                    return RoleResult(status="PASS")

            result = PortableWorkflowExecutor(
                workflow,
                catalog,
                PassingRoleRunner(),
            ).run(issue, pass_number=1)

        self.assertEqual(result.issue_status, IssueStatus.COMPLETED)
        self.assertEqual(
            calls,
            ["Development", "Code Review", "Security Review", "Final Review", "QA"],
        )

    def test_duplicated_workflow_round_trips_state_and_executes_independently(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = root / "devloop-plan.json"
            issue_path = root / "0008.md"
            issue_path.write_text("# Safe transformations\n", encoding="utf-8")
            issues_index = root / "README.md"
            issues_index.write_text(
                "[Safe transformations](./0008.md)\n",
                encoding="utf-8",
            )
            issue = Issue("0008", "Safe transformations", issue_path, False)
            catalog = default_portable_component_catalog()
            draft = WorkflowDraft(default_portable_workflow(), catalog)
            duplicate = draft.duplicate(SECURITY_REVIEW_STEP_ID)
            draft.keep_guidance(duplicate.step_instance_id)
            WorkflowDefaultStore(configuration_path, catalog).replace(draft.workflow)
            workflow = WorkflowDefaultStore(configuration_path, catalog).load()
            calls: list[tuple[str, str]] = []

            writer = LoopStateWriter(issues_index)
            writer.record_resolved_workflow(workflow, catalog)
            state_before_execution = json.loads(
                writer.state_path.read_text(encoding="utf-8")
            )

            class PassingRoleRunner:
                def run_role(self, **arguments: object) -> RoleResult:
                    calls.append(
                        (
                            str(arguments["step_instance_id"]),
                            str(arguments["step_display_name"]),
                        )
                    )
                    return RoleResult(status="PASS")

            execution = PortableWorkflowExecutor(
                workflow,
                catalog,
                PassingRoleRunner(),
            ).run(issue, pass_number=1)
            writer.record_portable_execution_result(issue, execution)
            restored = LoopStateWriter(issues_index)
            state_after_execution = json.loads(
                restored.state_path.read_text(encoding="utf-8")
            )

        self.assertNotIn("step_attempt_records", state_before_execution)
        self.assertEqual(restored.resolved_workflow(catalog), workflow)
        self.assertEqual(execution.issue_status, IssueStatus.COMPLETED)
        self.assertEqual(
            [display_name for _, display_name in calls],
            [
                "Development",
                "Security Review",
                "Security Review 2",
                "Final Review",
                "QA",
            ],
        )
        self.assertNotEqual(
            str(duplicate.step_instance_id),
            str(SECURITY_REVIEW_STEP_ID),
        )
        source_attempt = state_after_execution["step_attempt_records"][
            str(SECURITY_REVIEW_STEP_ID)
        ][issue.number][0]
        duplicate_attempt = state_after_execution["step_attempt_records"][
            str(duplicate.step_instance_id)
        ][issue.number][0]
        self.assertNotEqual(source_attempt["attempt_id"], duplicate_attempt["attempt_id"])
        self.assertEqual(
            duplicate_attempt["step_instance_id"],
            str(duplicate.step_instance_id),
        )

    def test_editor_configured_outcome_branch_completes_a_portable_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = root / "devloop-plan.json"
            issue_path = root / "0005-branch.md"
            issue_path.write_text("# Configured branch\n", encoding="utf-8")
            issue = Issue("0005", "Configured branch", issue_path, False)
            catalog = default_portable_component_catalog()

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(
                    ["3", "route", "3", "new", "2", "apply"]
                ).read_line,
                write=lambda _: None,
                terminal_width=120,
                catalog=catalog,
            )
            workflow = WorkflowDefaultStore(configuration_path, catalog).load()
            calls: list[str] = []

            class BranchingRoleRunner:
                def run_role(self, **arguments: object) -> RoleResult:
                    display_name = str(arguments["step_display_name"])
                    calls.append(display_name)
                    if display_name == "Security Review":
                        return RoleResult(status="BLOCKED", summary="Use branch")
                    return RoleResult(status="PASS")

            result = PortableWorkflowExecutor(
                workflow,
                catalog,
                BranchingRoleRunner(),
            ).run(issue, pass_number=1)

        self.assertEqual(result.issue_status, IssueStatus.COMPLETED)
        self.assertEqual(
            calls,
            ["Development", "Security Review", "Code Review"],
        )

    def test_editor_configured_changes_requested_route_executes_rework_loop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = root / "devloop-plan.json"
            issue_path = root / "0005-loop.md"
            issue_path.write_text("# Configured loop\n", encoding="utf-8")
            issue = Issue("0005", "Configured loop", issue_path, False)
            catalog = default_portable_component_catalog()

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(
                    ["3", "route", "2", "insert", "2", "apply"]
                ).read_line,
                write=lambda _: None,
                terminal_width=120,
                catalog=catalog,
            )
            workflow = WorkflowDefaultStore(configuration_path, catalog).load()
            calls: list[str] = []
            security_attempts = 0

            class ReworkingRoleRunner:
                def run_role(self, **arguments: object) -> RoleResult:
                    nonlocal security_attempts
                    display_name = str(arguments["step_display_name"])
                    calls.append(display_name)
                    if display_name == "Security Review":
                        security_attempts += 1
                        if security_attempts == 1:
                            return RoleResult(
                                status="FAIL",
                                fix_list=["Repair security finding"],
                            )
                    return RoleResult(status="PASS")

            result = PortableWorkflowExecutor(
                workflow,
                catalog,
                ReworkingRoleRunner(),
            ).run(issue, pass_number=1, max_passes=2)

        self.assertEqual(result.issue_status, IssueStatus.COMPLETED)
        self.assertEqual(
            calls,
            [
                "Development",
                "Security Review",
                "Code Review",
                "Development",
                "Security Review",
                "Final Review",
                "QA",
            ],
        )
        self.assertEqual(result.attempts[3].rework_attempt_id, result.attempts[1].attempt_id)

    def test_undoing_an_added_selected_step_restores_a_valid_selection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(["add", "2", "undo", "apply"]).read_line,
                write=lambda _: None,
                terminal_width=100,
            )

            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        self.assertIs(result, EditorResult.APPLIED)
        self.assertEqual(stored, default_portable_workflow())

    def test_apply_renames_a_selected_step_and_persists_the_workflow_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            fake = FakeEditor(["3", "rename", "Threat Review", "apply"])
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=fake.read_line,
                write=output.append,
                terminal_width=100,
            )

            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.APPLIED)
        self.assertIn("Workflow Steps", rendered)
        self.assertIn("Settings —", rendered)
        self.assertIn("Workflow Default (editable)", rendered)
        self.assertEqual(stored.step(SECURITY_REVIEW_STEP_ID).display_name, "Threat Review")

    def test_cancel_discards_the_whole_draft_without_a_persistence_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            fake = FakeEditor(["3", "rename", "Discarded Review", "cancel"])

            result = run_workflow_editor(
                path,
                read_line=fake.read_line,
                write=lambda _: None,
                terminal_width=100,
            )

            self.assertIs(result, EditorResult.CANCELLED)
            self.assertFalse(path.exists())

    def test_duplicate_display_name_is_rejected_inside_the_draft(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            fake = FakeEditor(["3", "rename", "development", "cancel"])
            output: list[str] = []

            run_workflow_editor(
                path,
                read_line=fake.read_line,
                write=output.append,
                terminal_width=100,
            )

        self.assertIn("requires unique display names", "\n".join(output))

    def test_unsafe_rename_never_reaches_terminal_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(
                    ["3", "rename", "\x1b[2JInjected\nHeading", "cancel"]
                ).read_line,
                write=output.append,
                terminal_width=100,
            )

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.CANCELLED)
        self.assertIn("Cannot rename step", rendered)
        self.assertIn("control", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("Injected\nHeading", rendered)

    def test_step_uuid_is_hidden_until_advanced_details_are_opened(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            run_workflow_editor(
                path,
                read_line=FakeEditor(["3", "advanced", "cancel"]).read_line,
                write=output.append,
                terminal_width=100,
            )

        step_id = str(SECURITY_REVIEW_STEP_ID)
        self.assertNotIn(step_id, output[0])
        self.assertIn(step_id, "\n".join(output[2:]))

    def test_undo_reverts_the_latest_draft_edit_before_apply(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"

            run_workflow_editor(
                path,
                read_line=FakeEditor(
                    ["3", "rename", "Temporary Review", "undo", "apply"]
                ).read_line,
                write=lambda _: None,
                terminal_width=100,
            )

            stored = WorkflowDefaultStore(
                path,
                default_portable_component_catalog(),
            ).load()

        self.assertEqual(stored.step(SECURITY_REVIEW_STEP_ID).display_name, "Security Review")

    def test_reset_step_restores_only_the_selected_builtin_step(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            catalog = default_portable_component_catalog()
            document = default_portable_workflow().to_dict()
            steps = {step["instance_id"]: step for step in document["steps"]}
            steps[str(SECURITY_REVIEW_STEP_ID)]["display_name"] = "Threat Review"
            steps[str(SECURITY_REVIEW_STEP_ID)]["guidance"] = {
                "text": "Temporary focus",
                "review_state": "READY",
            }
            steps[str(SECURITY_REVIEW_STEP_ID)]["capability_profile"][
                "agent_references"
            ] = []
            steps[str(FINAL_REVIEW_STEP_ID)]["display_name"] = "Assurance Review"
            WorkflowDefaultStore(path, catalog).replace(
                load_portable_workflow(document, catalog)
            )

            run_workflow_editor(
                path,
                read_line=FakeEditor(["3", "reset-step", "apply"]).read_line,
                write=lambda _: None,
                terminal_width=100,
            )

            stored = WorkflowDefaultStore(path, catalog).load()

        self.assertEqual(stored.step(SECURITY_REVIEW_STEP_ID).display_name, "Code Review")
        self.assertEqual(
            stored.step(SECURITY_REVIEW_STEP_ID).guidance,
            SECURITY_REVIEW_DEFAULT_GUIDANCE,
        )
        self.assertEqual(
            stored.step(SECURITY_REVIEW_STEP_ID).capability_profile,
            catalog.resolve(REVIEWER_COMPONENT_ID).default_capability_profile(),
        )
        self.assertEqual(stored.step(FINAL_REVIEW_STEP_ID).display_name, "Assurance Review")

    def test_reset_workflow_restores_the_complete_builtin_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            catalog = default_portable_component_catalog()
            document = default_portable_workflow().to_dict()
            security_review = next(
                step
                for step in document["steps"]
                if step["instance_id"] == SECURITY_REVIEW_STEP_ID
            )
            security_review["display_name"] = "Threat Review"
            WorkflowDefaultStore(path, catalog).replace(
                load_portable_workflow(document, catalog)
            )

            run_workflow_editor(
                path,
                read_line=FakeEditor(["reset-workflow", "apply"]).read_line,
                write=lambda _: None,
                terminal_width=100,
            )

            stored = WorkflowDefaultStore(path, catalog).load()

        self.assertEqual(stored, default_portable_workflow())

    def test_invalid_default_requires_explicit_reset_before_apply(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            original = json.dumps(
                {
                    "target_repo": "/repo",
                    "user_workflow_default": {
                        "schema": "devloop.portable-workflow/v1",
                    },
                    "user_workflow_default_hash": "invalid",
                },
                indent=2,
            )
            path.write_text(original, encoding="utf-8")
            output: list[str] = []

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(["apply", "cancel"]).read_line,
                write=output.append,
                terminal_width=100,
            )
            persisted = path.read_text(encoding="utf-8")

        self.assertIs(result, EditorResult.CANCELLED)
        self.assertEqual(persisted, original)
        rendered = "\n".join(output)
        self.assertIn("recovery mode", rendered)
        self.assertIn("reset-workflow", rendered)
        self.assertIn("must be reset before Apply", rendered)

    def test_invalid_default_opens_recovery_choices_in_application_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            path.write_text(
                json.dumps(
                    {
                        "user_workflow_default": {
                            "schema": "devloop.portable-workflow/v1",
                        },
                        "user_workflow_default_hash": "invalid",
                    }
                ),
                encoding="utf-8",
            )
            bridge = PortableRuntimeBridge()
            outcome: list[EditorResult] = []

            def run_editor() -> None:
                with portable_runtime_session(bridge):
                    outcome.append(
                        run_workflow_editor(
                            path,
                            read_line=FakeEditor([]).read_line,
                            write=lambda _: None,
                            terminal_width=100,
                        )
                    )

            worker = threading.Thread(target=run_editor)
            worker.start()
            first_event = bridge.next_event(timeout=1)
            screen_event = bridge.next_event(timeout=1)
            bridge.respond(first_event.request_id, "cancel")
            worker.join(timeout=1)

            self.assertIs(
                first_event.kind,
                PortableRuntimeEventKind.CHOICE_REQUESTED,
            )
            self.assertEqual(
                tuple(key for key, _label in first_event.options),
                ("reset-workflow", "cancel"),
            )
            self.assertIs(
                screen_event.kind,
                PortableRuntimeEventKind.SCREEN_UPDATED,
            )
            self.assertIn("recovery mode", screen_event.content)
            self.assertIn("superseded by", screen_event.content)
            self.assertNotIn("Workflow Default (editable)", screen_event.content)

        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, [EditorResult.CANCELLED])

    def test_current_run_is_read_only_while_workflow_default_remains_editable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            catalog = default_portable_component_catalog()
            current_document = default_portable_workflow().to_dict()
            current_security_review = next(
                step
                for step in current_document["steps"]
                if step["instance_id"] == SECURITY_REVIEW_STEP_ID
            )
            current_security_review["display_name"] = "Snapshot Review"
            current_workflow = load_portable_workflow(current_document, catalog)
            output: list[str] = []

            run_workflow_editor(
                path,
                read_line=FakeEditor(
                    [
                        "current",
                        "3",
                        "rename",
                        "future",
                        "3",
                        "rename",
                        "Future Review",
                        "apply",
                    ]
                ).read_line,
                write=output.append,
                terminal_width=100,
                current_workflow=current_workflow,
                catalog=catalog,
            )

            stored = WorkflowDefaultStore(path, catalog).load()

        rendered = "\n".join(output)
        self.assertIn("Current Run (read-only)", rendered)
        self.assertIn("Workflow Default (editable)", rendered)
        self.assertIn(
            "Apply: unfinished runs adopt all matching-step preferences, "
            "including the Execution Backend",
            rendered,
        )
        self.assertIn("Current Run (read-only)", rendered)
        self.assertIn("Current Run cannot be edited", rendered)
        self.assertIn("Snapshot Review", rendered)
        self.assertEqual(
            current_workflow.step(SECURITY_REVIEW_STEP_ID).display_name,
            "Snapshot Review",
        )
        self.assertEqual(stored.step(SECURITY_REVIEW_STEP_ID).display_name, "Future Review")

    def test_selection_preview_shows_the_backend_ahead_of_the_model_in_both_scopes(
        self,
    ) -> None:
        catalog = default_portable_component_catalog()
        current_document = default_portable_workflow().to_dict()
        current_security_review = next(
            step
            for step in current_document["steps"]
            if step["instance_id"] == SECURITY_REVIEW_STEP_ID
        )
        current_security_review["execution_settings"] = {
            "backend": "CLAUDE_CODE",
            "model": "claude-sonnet-5",
            "reasoning_effort": "high",
            "fast": "OFF",
        }
        current_workflow = load_portable_workflow(current_document, catalog)

        for scope, workflow, expected_backend, expected_model in (
            (
                EditorScope.CURRENT_RUN,
                current_workflow,
                "Claude Code",
                "claude-sonnet-5",
            ),
            (
                EditorScope.FUTURE_RUNS,
                default_portable_workflow(),
                "Codex CLI",
                "gpt-5.6-sol",
            ),
        ):
            with self.subTest(scope=scope.value):
                rendered = render_workflow_editor(
                    workflow,
                    SECURITY_REVIEW_STEP_ID,
                    catalog,
                    terminal_width=140,
                    terminal_height=40,
                    scope=scope,
                )

                self.assertIn(f"Backend: {expected_backend}", rendered)
                self.assertIn(f"Model: {expected_model}", rendered)
                self.assertLess(
                    rendered.index(f"Backend: {expected_backend}"),
                    rendered.index(f"Model: {expected_model}"),
                    rendered,
                )

    def test_the_execution_backend_action_is_offered_in_the_step_group(self) -> None:
        step_action_keys = tuple(
            action.command for action in WORKFLOW_ACTIONS if action.group == "Step"
        )
        self.assertEqual(
            step_action_keys,
            (
                "select",
                "rename",
                "type",
                "backend",
                "model",
                "reasoning",
                "fast",
                "budget",
                "guidance",
                "capabilities",
            ),
        )
        edit_step_commands = next(
            commands for group, commands in EDITOR_COMMAND_GROUPS if group == "Edit step"
        )
        self.assertIn("backend", edit_step_commands)

    def test_the_backend_menu_annotates_availability_and_moves_to_defaults(
        self,
    ) -> None:
        """Each backend is annotated, and choosing one resets model and effort."""
        component_catalog = default_portable_component_catalog()
        recorder = _MenuRecorder({"Execution Backend": "CLAUDE_CODE"})
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(["1", "backend", "apply"]).read_line,
                write=output.append,
                terminal_width=140,
                catalog=component_catalog,
                model_catalog_loader=_per_backend_catalog_loader(),
                verify_model=_RecordingVerifier().verify,
                backend_availability=_fake_availability(claude_installed=False),
                select_option=recorder.select,
            )
            stored = WorkflowDefaultStore(configuration_path, component_catalog).load()

        menu = recorder.menu("Execution Backend")
        labels = dict(menu.options)
        self.assertIn("Codex CLI · installed · current", labels["CODEX_CLI"])
        self.assertIn("Claude Code · not installed", labels["CLAUDE_CODE"])
        self.assertIn(menu.cancel_key, labels)
        expected = component_catalog.resolve(
            ANALYSIS_COMPONENT_ID
        ).execution_defaults_for(ExecutionBackendId.CLAUDE_CODE)
        self.assertEqual(
            stored.step(ANALYSIS_STEP_ID).execution_settings.as_tuple(),
            expected.as_tuple(),
        )

    def test_the_model_list_switches_backend_and_offers_that_backends_models(
        self,
    ) -> None:
        """The list's own entry names one backend, so it must deliver that backend.

        A Workflow Step is only ever offered its own backend's models, so someone
        looking for a Claude model in a Codex CLI list finds nothing and no
        indication that anything else exists. The entry that fixes that promises
        to run the step on the named backend and choose its model, so it must not
        stop at asking the same question again.
        """
        component_catalog = default_portable_component_catalog()
        verifier = _RecordingVerifier()
        recorder = _MenuRecorder(
            {
                "Model · Codex CLI": BACKEND_SWITCH_KEY,
                "Model · Claude Code": "1",
            }
        )
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(["1", "model", "apply"]).read_line,
                write=output.append,
                terminal_width=140,
                catalog=component_catalog,
                model_catalog_loader=_per_backend_catalog_loader(),
                verify_model=verifier.verify,
                backend_availability=_fake_availability(),
                select_option=recorder.select,
            )
            stored = WorkflowDefaultStore(configuration_path, component_catalog).load()

        codex_menu = recorder.menu("Model · Codex CLI")
        offered = dict(codex_menu.options)
        self.assertIn(BACKEND_SWITCH_KEY, offered)
        self.assertIn("Claude Code", offered[BACKEND_SWITCH_KEY])
        self.assertIn("Codex CLI", " ".join(codex_menu.description))
        self.assertEqual(
            [menu.title for menu in recorder.menus if menu.title.startswith("Model")],
            [
                "Model · Codex CLI · Analysis",
                "Model · Claude Code · Analysis",
            ],
        )
        # The intermediate chooser is what the label promised to skip.
        self.assertNotIn(
            "Execution Backend",
            [menu.title.split(" · ")[0] for menu in recorder.menus],
        )
        claude_model = _bundled_claude_catalog().models[0].model_id
        self.assertEqual(
            verifier.calls,
            [(ExecutionBackendId.CLAUDE_CODE, claude_model)],
        )
        self.assertEqual(
            stored.step(ANALYSIS_STEP_ID).execution_settings.backend,
            ExecutionBackendId.CLAUDE_CODE,
        )
        self.assertEqual(
            stored.step(ANALYSIS_STEP_ID).execution_settings.model,
            claude_model,
        )

    def test_the_model_list_route_names_a_backend_that_is_not_installed(self) -> None:
        """The direct route must not hide what the annotated chooser would show."""
        component_catalog = default_portable_component_catalog()
        recorder = _MenuRecorder({})
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(["1", "model", "cancel"]).read_line,
                write=output.append,
                terminal_width=140,
                catalog=component_catalog,
                model_catalog_loader=_per_backend_catalog_loader(),
                verify_model=_RecordingVerifier().verify,
                backend_availability=_fake_availability(claude_installed=False),
                select_option=recorder.select,
            )

        label = dict(recorder.menu("Model · Codex CLI").options)[BACKEND_SWITCH_KEY]
        self.assertIn("Claude Code", label)
        self.assertIn("not installed", label)

    def test_an_unavailable_catalog_still_points_at_the_execution_backend(self) -> None:
        """With no catalog the model list never opens, so the message must route.

        Someone whose Codex CLI cannot serve a catalog has no way through the
        model list at all, and that is exactly the person who needs to be told
        another Execution Backend exists.
        """
        component_catalog = default_portable_component_catalog()
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(["1", "model", "cancel"]).read_line,
                write=output.append,
                terminal_width=140,
                catalog=component_catalog,
                model_catalog_loader=lambda _backend: (_ for _ in ()).throw(
                    CatalogDiscoveryError("codex unavailable")
                ),
                verify_model=_RecordingVerifier().verify,
                backend_availability=_fake_availability(),
            )

        message = next(
            entry
            for entry in output
            if entry.startswith("No Codex CLI Model Catalog is available")
        )
        self.assertIn("Execution Backend", message)

    def test_the_non_tty_model_list_documents_the_backend_switch(self) -> None:
        """Line-by-line output must offer the same route, typed as a word."""
        component_catalog = default_portable_component_catalog()
        rendered_picker = render_model_picker(_codex_catalog(), terminal_width=100)
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(
                    ["1", "model", BACKEND_SWITCH_KEY, "1", "apply"]
                ).read_line,
                write=output.append,
                terminal_width=100,
                catalog=component_catalog,
                model_catalog_loader=_per_backend_catalog_loader(),
                verify_model=_RecordingVerifier().verify,
                backend_availability=_fake_availability(),
            )
            stored = WorkflowDefaultStore(configuration_path, component_catalog).load()

        rendered = "\n".join(output)
        self.assertIn(BACKEND_SWITCH_KEY, rendered_picker)
        self.assertIn("Execution Backend", rendered_picker)
        self.assertIn("Claude Code Models", rendered)
        self.assertEqual(
            stored.step(ANALYSIS_STEP_ID).execution_settings.as_tuple()[:2],
            (
                ExecutionBackendId.CLAUDE_CODE,
                _bundled_claude_catalog().models[0].model_id,
            ),
        )

    def test_opening_options_for_a_claude_step_costs_no_verification_call(self) -> None:
        """Browsing is free: the bundled entries are listed without a call."""
        component_catalog = default_portable_component_catalog()
        verifier = _RecordingVerifier()
        recorder = _MenuRecorder({})
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = _claude_backed_default(raw, component_catalog)
            output: list[str] = []

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(["1", "model", "cancel", "cancel"]).read_line,
                write=output.append,
                terminal_width=140,
                catalog=component_catalog,
                model_catalog_loader=_per_backend_catalog_loader(),
                verify_model=verifier.verify,
                backend_availability=_fake_availability(),
                select_option=recorder.select,
            )

        self.assertEqual(verifier.calls, [])
        menu = recorder.menu("Model · Claude Code")
        offered = dict(menu.options)
        self.assertIn("claude-opus-5", offered["1"])
        self.assertTrue(
            any("(alias)" in label for label in offered.values()),
            offered,
        )
        self.assertTrue(
            any("Enter another model identifier" in label for label in offered.values()),
            offered,
        )
        self.assertIn(FREE_TEXT_MODEL_KEY, offered)

    def test_selecting_a_short_alias_persists_the_resolved_pinned_identifier(
        self,
    ) -> None:
        """An alias is a picker convenience; only the concrete identifier is saved."""
        component_catalog = default_portable_component_catalog()
        verifier = _RecordingVerifier({"sonnet": "claude-sonnet-5-20260615"})
        catalog = _bundled_claude_catalog()
        alias_position = str(
            next(
                index
                for index, model in enumerate(catalog.models, start=1)
                if model.model_id == "sonnet"
            )
        )
        recorder = _MenuRecorder({"Model · Claude Code": alias_position})
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = _claude_backed_default(raw, component_catalog)
            output: list[str] = []

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(["1", "model", "apply"]).read_line,
                write=output.append,
                terminal_width=140,
                catalog=component_catalog,
                model_catalog_loader=_per_backend_catalog_loader(),
                verify_model=verifier.verify,
                backend_availability=_fake_availability(),
                select_option=recorder.select,
            )
            stored_document = configuration_path.read_text(encoding="utf-8")
            stored = WorkflowDefaultStore(configuration_path, component_catalog).load()

        self.assertEqual(
            verifier.calls,
            [(ExecutionBackendId.CLAUDE_CODE, "sonnet")],
        )
        self.assertEqual(
            stored.step(ANALYSIS_STEP_ID).execution_settings.model,
            "claude-sonnet-5-20260615",
        )
        self.assertNotIn('"model": "sonnet"', stored_document)

    def test_a_refused_model_is_reported_verbatim_and_not_saved(self) -> None:
        component_catalog = default_portable_component_catalog()
        refusal = "Invalid model name: made-up-model"
        verifier = _RecordingVerifier(refusals={"claude-opus-5": refusal})
        recorder = _MenuRecorder({"Model · Claude Code": "1"})
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = _claude_backed_default(raw, component_catalog)
            output: list[str] = []

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(["1", "model", "apply"]).read_line,
                write=output.append,
                terminal_width=140,
                catalog=component_catalog,
                model_catalog_loader=_per_backend_catalog_loader(),
                verify_model=verifier.verify,
                backend_availability=_fake_availability(),
                select_option=recorder.select,
            )
            stored = WorkflowDefaultStore(configuration_path, component_catalog).load()

        self.assertIn(refusal, "\n".join(output))
        self.assertEqual(
            stored.step(ANALYSIS_STEP_ID).execution_settings.model,
            "claude-sonnet-5",
        )

    def test_a_free_text_model_identifier_is_verified_before_it_is_saved(self) -> None:
        component_catalog = default_portable_component_catalog()
        verifier = _RecordingVerifier({"claude-opus-6": "claude-opus-6"})
        recorder = _MenuRecorder(
            {
                "Model · Claude Code": FREE_TEXT_MODEL_KEY,
                "Reasoning effort": "1",
            }
        )
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = _claude_backed_default(raw, component_catalog)
            output: list[str] = []

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(
                    ["1", "model", "claude-opus-6", "apply"]
                ).read_line,
                write=output.append,
                terminal_width=140,
                catalog=component_catalog,
                model_catalog_loader=_per_backend_catalog_loader(),
                verify_model=verifier.verify,
                backend_availability=_fake_availability(),
                select_option=recorder.select,
            )
            stored = WorkflowDefaultStore(configuration_path, component_catalog).load()

        self.assertEqual(
            verifier.calls,
            [(ExecutionBackendId.CLAUDE_CODE, "claude-opus-6")],
        )
        self.assertEqual(
            stored.step(ANALYSIS_STEP_ID).execution_settings.model,
            "claude-opus-6",
        )

    def test_claude_reasoning_choices_are_limited_to_supported_efforts(self) -> None:
        component_catalog = default_portable_component_catalog()
        recorder = _MenuRecorder({"Reasoning effort": "1"})
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = _claude_backed_default(raw, component_catalog)
            output: list[str] = []

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(["1", "reasoning", "apply"]).read_line,
                write=output.append,
                terminal_width=140,
                catalog=component_catalog,
                model_catalog_loader=_per_backend_catalog_loader(),
                verify_model=_RecordingVerifier().verify,
                backend_availability=_fake_availability(),
                select_option=recorder.select,
            )
            stored = WorkflowDefaultStore(configuration_path, component_catalog).load()

        menu = recorder.menu("Reasoning effort")
        offered = tuple(
            label for key, label in menu.options if key != menu.cancel_key
        )
        self.assertEqual(offered, _bundled_claude_catalog().reasoning_efforts)
        self.assertIn(
            stored.step(ANALYSIS_STEP_ID).execution_settings.reasoning_effort,
            offered,
        )

    def test_every_added_choice_offers_a_cancel_item_reachable_by_escape(self) -> None:
        """Esc maps to the cancel key, so every menu must actually offer one."""
        component_catalog = default_portable_component_catalog()
        recorder = _MenuRecorder(
            {
                "Execution Backend": "cancel",
                "Model · Claude Code": "cancel",
                "Reasoning effort": "cancel",
            }
        )
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = _claude_backed_default(raw, component_catalog)
            output: list[str] = []

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(
                    ["1", "backend", "model", "reasoning", "fast", "cancel"]
                ).read_line,
                write=output.append,
                terminal_width=140,
                catalog=component_catalog,
                model_catalog_loader=_per_backend_catalog_loader(),
                verify_model=_RecordingVerifier().verify,
                backend_availability=_fake_availability(),
                select_option=recorder.select,
            )
            stored = WorkflowDefaultStore(configuration_path, component_catalog).load()

        self.assertTrue(recorder.menus)
        for menu in recorder.menus:
            with self.subTest(menu=menu.title):
                self.assertIn(menu.cancel_key, dict(menu.options), menu.title)
        self.assertEqual(
            stored.step(ANALYSIS_STEP_ID).execution_settings.model,
            "claude-sonnet-5",
        )

    def test_the_non_tty_fallback_drives_backend_and_free_text_selection(self) -> None:
        """Without raw-key support the same choices stay reachable line by line."""
        component_catalog = default_portable_component_catalog()
        verifier = _RecordingVerifier({"claude-opus-5": "claude-opus-5"})
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(
                    [
                        "1",
                        "backend",
                        "CLAUDE_CODE",
                        "model",
                        FREE_TEXT_MODEL_KEY,
                        "claude-opus-5",
                        "reasoning",
                        "1",
                        "apply",
                    ]
                ).read_line,
                write=output.append,
                terminal_width=120,
                catalog=component_catalog,
                model_catalog_loader=_per_backend_catalog_loader(),
                verify_model=verifier.verify,
                backend_availability=_fake_availability(),
            )
            stored = WorkflowDefaultStore(configuration_path, component_catalog).load()

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.APPLIED)
        self.assertIn("Execution Backends", rendered)
        self.assertIn("CLAUDE_CODE. Claude Code — installed", rendered)
        self.assertIn("Claude Code Models — live", rendered)
        settings = stored.step(ANALYSIS_STEP_ID).execution_settings
        self.assertIs(settings.backend, ExecutionBackendId.CLAUDE_CODE)
        self.assertEqual(settings.model, "claude-opus-5")
        self.assertEqual(
            verifier.calls,
            [(ExecutionBackendId.CLAUDE_CODE, "claude-opus-5")],
        )

    def test_codex_model_selection_costs_no_verification_call(self) -> None:
        """The live Codex catalog is the account's own answer, so selection is free."""
        component_catalog = default_portable_component_catalog()
        verifier = _RecordingVerifier()
        recorder = _MenuRecorder({"Model · Codex CLI": "2", "Reasoning effort": "1"})
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(["1", "model", "apply"]).read_line,
                write=output.append,
                terminal_width=140,
                catalog=component_catalog,
                model_catalog_loader=_per_backend_catalog_loader(),
                verify_model=verifier.verify,
                backend_availability=_fake_availability(),
                select_option=recorder.select,
            )
            stored = WorkflowDefaultStore(configuration_path, component_catalog).load()

        self.assertEqual(verifier.calls, [])
        settings = stored.step(ANALYSIS_STEP_ID).execution_settings
        self.assertIs(settings.backend, ExecutionBackendId.CODEX_CLI)
        self.assertEqual(settings.model, "gpt-5.6-luna")

    def test_a_stale_claude_cache_cannot_change_a_step(self) -> None:
        """A cached catalog is display-only for the Claude backend too."""
        component_catalog = default_portable_component_catalog()
        verifier = _RecordingVerifier()
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = _claude_backed_default(raw, component_catalog)
            ModelCatalogCache(
                model_catalog_cache_path(
                    configuration_path,
                    ExecutionBackendId.CLAUDE_CODE,
                ),
                ExecutionBackendId.CLAUDE_CODE,
            ).replace(_bundled_claude_catalog())
            output: list[str] = []

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(["1", "model", "cancel"]).read_line,
                write=output.append,
                terminal_width=140,
                catalog=component_catalog,
                model_catalog_loader=lambda _backend: (_ for _ in ()).throw(
                    CatalogDiscoveryError("backend unavailable")
                ),
                verify_model=verifier.verify,
                backend_availability=_fake_availability(),
            )
            stored = WorkflowDefaultStore(configuration_path, component_catalog).load()

        rendered = "\n".join(output)
        self.assertIn(
            "A fresh live Claude Code Model Catalog is required",
            rendered,
        )
        self.assertEqual(verifier.calls, [])
        self.assertEqual(
            stored.step(ANALYSIS_STEP_ID).execution_settings.model,
            "claude-sonnet-5",
        )

    def test_the_model_picker_and_detail_line_name_the_step_backend(self) -> None:
        rendered_picker = render_model_picker(
            _bundled_claude_catalog(),
            terminal_width=120,
        )
        component_catalog = default_portable_component_catalog()
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = _claude_backed_default(raw, component_catalog)
            output: list[str] = []

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(["1", "cancel"]).read_line,
                write=output.append,
                terminal_width=140,
                catalog=component_catalog,
                model_catalog_loader=lambda _backend: (_ for _ in ()).throw(
                    CatalogDiscoveryError("backend unavailable")
                ),
                verify_model=_RecordingVerifier().verify,
                backend_availability=_fake_availability(),
            )

        self.assertIn("Claude Code Models — live", rendered_picker)
        self.assertIn("Claude Code Model Catalog: unavailable", "\n".join(output))

    def test_fast_is_refused_for_a_backend_that_advertises_no_fast_support(self) -> None:
        catalog = default_portable_component_catalog()
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = Path(raw) / "devloop-plan.json"
            document = default_portable_workflow().to_dict()
            document["steps"][0]["execution_settings"] = {
                "backend": "CLAUDE_CODE",
                "model": "claude-sonnet-5",
                "reasoning_effort": "high",
                "fast": "OFF",
            }
            WorkflowDefaultStore(configuration_path, catalog).replace(
                load_portable_workflow(document, catalog)
            )
            output: list[str] = []

            run_workflow_editor(
                configuration_path,
                read_line=FakeEditor(["1", "fast", "cancel"]).read_line,
                write=output.append,
                terminal_width=120,
                catalog=catalog,
                model_catalog_loader=_per_backend_catalog_loader(),
                verify_model=_RecordingVerifier().verify,
                backend_availability=_fake_availability(),
            )
            stored = WorkflowDefaultStore(configuration_path, catalog).load()

        self.assertIn(
            "Claude Code Backend advertises no Fast support",
            "\n".join(output),
        )
        self.assertIs(
            stored.step(ANALYSIS_STEP_ID).execution_settings.fast,
            FastPreference.OFF,
        )

    def test_rendered_editor_never_exceeds_terminal_height(self) -> None:
        height = 20
        with tempfile.TemporaryDirectory() as raw:
            output: list[str] = []

            run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=FakeEditor(["advanced", "graph", "cancel"]).read_line,
                write=output.append,
                terminal_width=120,
                terminal_height=height,
            )

        for frame in output:
            if "Available commands" not in frame:
                continue
            self.assertLessEqual(
                len(frame.splitlines()),
                height - 1,
                frame,
            )

    def test_narrow_layout_stacks_the_primary_path_above_selected_settings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output: list[str] = []

            run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=FakeEditor(["cancel"]).read_line,
                write=output.append,
                terminal_width=60,
            )

        first_frame = output[0]
        self.assertIn("Workflow Steps", first_frame)
        self.assertIn("Settings —", first_frame)
        self.assertLess(
            first_frame.index("Workflow Steps"),
            first_frame.index("Settings —"),
        )

    def test_wide_layout_uses_side_by_side_primary_and_selected_panes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output: list[str] = []

            run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=FakeEditor(["cancel"]).read_line,
                write=output.append,
                terminal_width=120,
            )

        self.assertIn("Workflow Steps", output[0])
        self.assertIn("┬ Settings —", output[0])

    def test_navigation_commands_change_the_highlighted_step_without_editing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output: list[str] = []

            run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=FakeEditor([]).read_line,
                read_command=FakeEditor(["__next_step__", "cancel"]).read_line,
                write=output.append,
                terminal_width=120,
            )

        self.assertIn("> 2. Development", output[0])
        self.assertIn("> 3. Security Review", output[1])

    def test_layout_never_emits_a_line_wider_than_the_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            width = 40
            output: list[str] = []

            run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=FakeEditor(
                    ["2", "rename", "A very long specialized review name" * 3, "advanced", "cancel"]
                ).read_line,
                write=output.append,
                terminal_width=width,
            )

        rendered_lines = "\n".join(output).splitlines()
        self.assertLessEqual(max(map(len, rendered_lines)), width)

    def test_capability_options_remain_reachable_from_the_workflow_editor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            opened: list[bool] = []

            run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=FakeEditor(["capabilities", "cancel"]).read_line,
                write=lambda _: None,
                terminal_width=100,
                open_capabilities=lambda _draft, _step_id: opened.append(True),
            )

        self.assertEqual(opened, [True])

    def test_capability_edit_applies_only_to_the_selected_review_instance(self) -> None:
        catalog = default_portable_component_catalog()
        replaceable = catalog.resolve(REVIEWER_COMPONENT_ID).default_capabilities[0]
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"

            result = run_workflow_editor(
                path,
                read_line=FakeEditor(["3", "capabilities", "apply"]).read_line,
                write=lambda _: None,
                terminal_width=100,
                catalog=catalog,
                open_capabilities=lambda draft, step_id: draft.toggle_capability(
                    step_id,
                    replaceable,
                ),
            )
            stored = WorkflowDefaultStore(path, catalog).load()

        self.assertIs(result, EditorResult.APPLIED)
        self.assertFalse(
            stored.step(SECURITY_REVIEW_STEP_ID).capability_profile.contains(
                replaceable
            )
        )
        self.assertTrue(
            stored.step(FINAL_REVIEW_STEP_ID).capability_profile.contains(replaceable)
        )


class WorkflowEditorWrapperTests(unittest.TestCase):
    def test_bash_and_powershell_planners_enter_the_same_portable_editor_module(self) -> None:
        root = Path(__file__).resolve().parents[1]
        bash = (root / "bin" / "devloop-plan.sh").read_text(encoding="utf-8")
        powershell = (root / "bin" / "devloop-plan.ps1").read_text(encoding="utf-8")

        self.assertIn("devloop.interactive_runner", bash)
        self.assertIn("devloop.interactive_runner", powershell)
        self.assertNotIn("codexcli", bash.casefold())
        self.assertNotIn("codexcli", powershell.casefold())


if __name__ == "__main__":
    unittest.main()
