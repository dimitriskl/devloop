from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devloop import cli
from devloop.codex_runner import RoleResult
from devloop.issue_pack import Issue
from devloop.model_catalog import (
    CatalogDiscoveryError,
    CatalogSource,
    CodexModel,
    CodexModelCatalog,
    CodexModelCatalogCache,
    model_catalog_cache_path,
)
from devloop.portable_workflow import (
    ANALYSIS_STEP_ID,
    CodexExecutionSettings,
    DEVELOPMENT_STEP_ID,
    ExecutionBudget,
    FINAL_REVIEW_STEP_ID,
    QA_STEP_ID,
    SECURITY_REVIEW_STEP_ID,
    FastPreference,
    PortableStepComponent,
    PortableStepComponentCatalog,
    PortableWorkflowExecutor,
    StepComponentId,
    StepInstanceId,
    StepOutcome,
    StepScope,
    default_portable_component_catalog,
    default_portable_workflow,
    load_portable_workflow,
    preflight_codex_execution_settings,
)
from devloop.workflow_editor import render_workflow_editor
from devloop.workflow_defaults import WorkflowDefaultStore
from devloop.workflow_editor import EditorResult, run_workflow_editor
from devloop.state import LoopStateWriter
from devloop.terminal_text import has_unsafe_terminal_controls
from tests.terminal_safety import (
    HOSTILE_TERMINAL_TEXT,
    assert_terminal_text_is_safe,
)


class _FakeEditor:
    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)

    def read_line(self, _prompt: str) -> str:
        return next(self._responses)


class CodexExecutionSettingsTests(unittest.TestCase):
    @staticmethod
    def _live_catalog(*, sol_fast: bool = True) -> CodexModelCatalog:
        return CodexModelCatalog(
            models=(
                CodexModel("gpt-5.6-luna", "Luna", "", ("high",)),
                CodexModel(
                    "gpt-5.6-sol",
                    "Sol",
                    "",
                    ("high", "xhigh"),
                    advertises_fast=sol_fast,
                ),
                CodexModel("gpt-5.6-terra", "Terra", "", ("high",)),
            ),
            fetched_at="2026-07-16T12:00:00",
            source=CatalogSource.LIVE,
        )

    def test_builtin_roles_use_the_approved_model_effort_and_fast_defaults(self) -> None:
        workflow = default_portable_workflow()

        self.assertEqual(
            workflow.step(ANALYSIS_STEP_ID).codex_settings.as_tuple(),
            ("gpt-5.6-sol", "xhigh", FastPreference.OFF),
        )
        self.assertEqual(workflow.start_step_id, ANALYSIS_STEP_ID)
        self.assertEqual(
            workflow.step(DEVELOPMENT_STEP_ID).codex_settings.as_tuple(),
            ("gpt-5.6-luna", "high", FastPreference.OFF),
        )
        for step_id in (SECURITY_REVIEW_STEP_ID, FINAL_REVIEW_STEP_ID):
            self.assertEqual(
                workflow.step(step_id).codex_settings.as_tuple(),
                ("gpt-5.6-sol", "xhigh", FastPreference.OFF),
            )
        self.assertEqual(
            workflow.step(QA_STEP_ID).codex_settings.as_tuple(),
            ("gpt-5.6-terra", "high", FastPreference.OFF),
        )

    def test_model_and_effort_reject_c1_and_bidirectional_controls(self) -> None:
        for field_name, model, effort in (
            ("model", "gpt-safe\x9b2Junsafe", "high"),
            ("model", "gpt-safe\x9d0;unsafe\x9c", "high"),
            ("model", "gpt-safe\u202eunsafe", "high"),
            ("reasoning effort", "gpt-safe", "hi\x9b2Jgh"),
            ("reasoning effort", "gpt-safe", "hi\x9d0;unsafe\x9cgh"),
            ("reasoning effort", "gpt-safe", "hi\u2066gh"),
        ):
            with self.subTest(field_name=field_name, value=(model, effort)):
                with self.assertRaisesRegex(ValueError, field_name):
                    CodexExecutionSettings(model, effort)

    def test_each_step_round_trips_an_independent_execution_choice(self) -> None:
        document = default_portable_workflow().to_dict()
        security_review = next(
            step
            for step in document["steps"]
            if step["instance_id"] == SECURITY_REVIEW_STEP_ID
        )
        security_review["codex_settings"] = {
            "model": "gpt-5.6-terra",
            "reasoning_effort": "medium",
            "fast": "ON",
        }

        restored = load_portable_workflow(
            document,
            default_portable_component_catalog(),
        )

        self.assertEqual(
            restored.step(SECURITY_REVIEW_STEP_ID).codex_settings.as_tuple(),
            ("gpt-5.6-terra", "medium", FastPreference.ON),
        )
        self.assertEqual(
            restored.step(FINAL_REVIEW_STEP_ID).codex_settings.as_tuple(),
            ("gpt-5.6-sol", "xhigh", FastPreference.OFF),
        )
        self.assertEqual(restored.to_dict(), document)

    def test_execution_budget_round_trips_independently_from_codex_settings(self) -> None:
        document = default_portable_workflow().to_dict()
        development = next(
            step
            for step in document["steps"]
            if step["instance_id"] == DEVELOPMENT_STEP_ID
        )
        original_codex_settings = dict(development["codex_settings"])
        development["execution_budget"] = {
            "timeout_seconds": 2400,
            "checkpoint_seconds": 420,
        }

        restored = load_portable_workflow(
            document,
            default_portable_component_catalog(),
        )

        self.assertEqual(
            restored.step(DEVELOPMENT_STEP_ID).execution_budget,
            ExecutionBudget(timeout_seconds=2400, checkpoint_seconds=420),
        )
        self.assertEqual(
            restored.step(DEVELOPMENT_STEP_ID).codex_settings.to_dict(),
            original_codex_settings,
        )
        self.assertEqual(restored.to_dict(), document)

    def test_local_deterministic_steps_explain_that_codex_settings_do_not_apply(self) -> None:
        local_component = PortableStepComponent(
            component_id=StepComponentId("example.local-check"),
            default_display_name="Local Check",
            scope=StepScope.ISSUE,
            supported_outcomes=frozenset({StepOutcome.SUCCEEDED}),
            adapter=None,
        )
        catalog = PortableStepComponentCatalog((local_component,))
        step_id = StepInstanceId("54de2be8-5f1a-4be6-9b8c-0d7e6f5a4b03")
        workflow = load_portable_workflow(
            {
                "schema": "devloop.portable-workflow/v2",
                "start_step_id": step_id,
                "steps": [
                    {
                        "instance_id": step_id,
                        "display_name": "Local Check",
                        "component_id": "example.local-check",
                        "transitions": {"SUCCEEDED": None},
                        "input_bindings": {},
                    }
                ],
            },
            catalog,
        )

        rendered = render_workflow_editor(
            workflow,
            step_id,
            catalog,
            terminal_width=120,
        )

        self.assertIn("Local deterministic execution", rendered)
        self.assertNotIn("Model:", rendered)

    def test_preflight_names_the_exact_step_and_unsupported_fast_setting(self) -> None:
        document = default_portable_workflow().to_dict()
        security_review = next(
            step
            for step in document["steps"]
            if step["instance_id"] == SECURITY_REVIEW_STEP_ID
        )
        security_review["codex_settings"]["fast"] = "ON"
        workflow = load_portable_workflow(
            document,
            default_portable_component_catalog(),
        )

        with self.assertRaisesRegex(
            ValueError,
            "Security Review.*Fast ON.*gpt-5.6-sol.*Retry Catalog.*options",
        ):
            preflight_codex_execution_settings(
                workflow,
                default_portable_component_catalog(),
                self._live_catalog(sol_fast=False),
            )

    def test_editor_constrains_and_persists_model_effort_and_fast_per_step(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                configuration_path,
                read_line=_FakeEditor(
                    ["1", "model", "2", "reasoning", "2", "fast", "on", "apply"]
                ).read_line,
                write=output.append,
                terminal_width=120,
                model_catalog_loader=self._live_catalog,
            )
            stored = WorkflowDefaultStore(
                configuration_path,
                default_portable_component_catalog(),
            ).load()

        self.assertIs(result, EditorResult.APPLIED)
        self.assertEqual(
            stored.step(ANALYSIS_STEP_ID).codex_settings.as_tuple(),
            ("gpt-5.6-sol", "xhigh", FastPreference.ON),
        )
        self.assertEqual(
            stored.step(DEVELOPMENT_STEP_ID).codex_settings.as_tuple(),
            ("gpt-5.6-luna", "high", FastPreference.OFF),
        )
        rendered = "\n".join(output)
        self.assertIn("Codex Models — live", rendered)
        self.assertIn("2. Sol — gpt-5.6-sol", rendered)

    def test_editor_persists_execution_budget_without_changing_codex_settings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_workflow_editor(
                configuration_path,
                read_line=_FakeEditor(
                    ["1", "budget", "1200", "150", "apply"]
                ).read_line,
                write=output.append,
                terminal_width=120,
                model_catalog_loader=self._live_catalog,
            )
            stored = WorkflowDefaultStore(
                configuration_path,
                default_portable_component_catalog(),
            ).load()

        self.assertIs(result, EditorResult.APPLIED)
        self.assertEqual(
            stored.step(ANALYSIS_STEP_ID).execution_budget,
            ExecutionBudget(timeout_seconds=1200, checkpoint_seconds=150),
        )
        self.assertEqual(
            stored.step(ANALYSIS_STEP_ID).codex_settings.as_tuple(),
            ("gpt-5.6-sol", "xhigh", FastPreference.OFF),
        )
        self.assertIn("Timeout:", "\n".join(output))

    def test_retry_catalog_replaces_visible_stale_cache_after_discovery_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = Path(raw) / "devloop-plan.json"
            cache = CodexModelCatalogCache(
                model_catalog_cache_path(configuration_path)
            )
            cache.replace(self._live_catalog())
            attempts = 0

            def discover() -> CodexModelCatalog:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise CatalogDiscoveryError("temporary backend failure")
                return self._live_catalog()

            output: list[str] = []
            run_workflow_editor(
                configuration_path,
                read_line=_FakeEditor(["retry-catalog", "cancel"]).read_line,
                write=output.append,
                terminal_width=120,
                model_catalog_loader=discover,
            )

        rendered = "\n".join(output)
        self.assertIn("Codex Model Catalog refreshed from the live backend.", rendered)
        self.assertNotIn("Codex Model Catalog: STALE", output[-1])

    def test_editor_sanitizes_backend_catalog_errors(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output: list[str] = []

            run_workflow_editor(
                Path(raw) / "devloop-plan.json",
                read_line=_FakeEditor(["cancel"]).read_line,
                write=output.append,
                terminal_width=120,
                model_catalog_loader=lambda: (_ for _ in ()).throw(
                    CatalogDiscoveryError(HOSTILE_TERMINAL_TEXT)
                ),
            )

        assert_terminal_text_is_safe(
            self,
            "\n".join(output),
            redirected=True,
        )

    def test_editor_rejects_hostile_live_catalog_metadata_before_picker_surfaces(
        self,
    ) -> None:
        hostile_values = (
            ("model_id", "gpt-5.6-luna\nspoofed-model"),
            ("display_name", "Luna\x1b]52;c;dGVybWluYWw=\x07"),
            ("reasoning_efforts", ("high\x1b[2J",)),
            ("fetched_at", "2026-07-16T12:00:00\nspoofed-status"),
        )
        for field_name, hostile_value in hostile_values:
            with self.subTest(field_name=field_name), tempfile.TemporaryDirectory() as raw:
                output: list[str] = []

                def load_catalog() -> CodexModelCatalog:
                    model_values = {
                        "model_id": "gpt-5.6-luna",
                        "display_name": "Luna",
                        "description": "Focused implementation model",
                        "reasoning_efforts": ("high",),
                    }
                    fetched_at = "2026-07-16T12:00:00"
                    if field_name == "fetched_at":
                        fetched_at = str(hostile_value)
                    else:
                        model_values[field_name] = hostile_value
                    return CodexModelCatalog(
                        models=(CodexModel(**model_values),),
                        fetched_at=fetched_at,
                    )

                run_workflow_editor(
                    Path(raw) / "devloop-plan.json",
                    read_line=_FakeEditor(["model", "reasoning", "cancel"]).read_line,
                    write=output.append,
                    terminal_width=120,
                    model_catalog_loader=load_catalog,
                )

                rendered = "\n".join(output)
                self.assertFalse(
                    any(
                        character != "\n"
                        and has_unsafe_terminal_controls(character)
                        for character in rendered
                    )
                )
                self.assertNotIn("Codex Models —", rendered)
                self.assertNotIn("Reasoning Efforts —", rendered)

    def test_editor_rejects_hostile_cached_catalog_metadata_before_display(
        self,
    ) -> None:
        hostile_values = (
            ("model_id", "gpt-5.6-luna\nspoofed-model"),
            ("display_name", "Luna\x1b]52;c;dGVybWluYWw=\x07"),
            ("reasoning_efforts", ["high\x1b[2J"]),
            ("fetched_at", "2026-07-16T12:00:00\nspoofed-status"),
        )
        for field_name, hostile_value in hostile_values:
            with self.subTest(field_name=field_name), tempfile.TemporaryDirectory() as raw:
                configuration_path = Path(raw) / "devloop-plan.json"
                cache_document = {
                    "fetched_at": "2026-07-16T12:00:00",
                    "models": [
                        {
                            "model_id": "gpt-5.6-luna",
                            "display_name": "Luna",
                            "description": "Focused implementation model",
                            "reasoning_efforts": ["high"],
                            "service_tier_ids": [],
                            "supports_fast": False,
                        }
                    ],
                }
                if field_name == "fetched_at":
                    cache_document["fetched_at"] = hostile_value
                else:
                    cache_document["models"][0][field_name] = hostile_value
                model_catalog_cache_path(configuration_path).write_text(
                    json.dumps(cache_document),
                    encoding="utf-8",
                )
                output: list[str] = []

                run_workflow_editor(
                    configuration_path,
                    read_line=_FakeEditor(["model", "reasoning", "cancel"]).read_line,
                    write=output.append,
                    terminal_width=120,
                )

                rendered = "\n".join(output)
                self.assertFalse(
                    any(
                        character != "\n"
                        and has_unsafe_terminal_controls(character)
                        for character in rendered
                    )
                )
                self.assertNotIn("Codex Models —", rendered)
                self.assertNotIn("Reasoning Efforts —", rendered)

    def test_cached_only_model_choice_cannot_be_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = Path(raw) / "devloop-plan.json"
            cached_catalog = CodexModelCatalog(
                models=(
                    CodexModel(
                        "cached-only-model",
                        "Cached Only",
                        "",
                        ("xhigh",),
                    ),
                ),
                fetched_at="2026-07-15T12:00:00",
                source=CatalogSource.LIVE,
            )
            CodexModelCatalogCache(
                model_catalog_cache_path(configuration_path)
            ).replace(cached_catalog)
            output: list[str] = []

            result = run_workflow_editor(
                configuration_path,
                read_line=_FakeEditor(["1", "model", "1", "apply"]).read_line,
                write=output.append,
                terminal_width=120,
                model_catalog_loader=lambda: (_ for _ in ()).throw(
                    CatalogDiscoveryError("backend unavailable")
                ),
            )
            stored = WorkflowDefaultStore(
                configuration_path,
                default_portable_component_catalog(),
            ).load()

        self.assertIs(result, EditorResult.APPLIED)
        self.assertEqual(
            stored.step(ANALYSIS_STEP_ID).codex_settings.model,
            "gpt-5.6-sol",
        )
        self.assertIn(
            "fresh live Codex Model Catalog is required",
            "\n".join(output),
        )

    def test_run_preflight_fails_before_snapshotting_an_unavailable_combination(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = root / "devloop-plan.json"
            issue_index = root / "README.md"
            issue_index.write_text("", encoding="utf-8")
            store = WorkflowDefaultStore(
                configuration_path,
                default_portable_component_catalog(),
            )
            document = default_portable_workflow().to_dict()
            development = next(
                step
                for step in document["steps"]
                if step["instance_id"] == DEVELOPMENT_STEP_ID
            )
            development["codex_settings"]["reasoning_effort"] = "xhigh"
            store.replace(
                load_portable_workflow(
                    document,
                    default_portable_component_catalog(),
                )
            )
            writer = LoopStateWriter(issue_index)

            with self.assertRaisesRegex(
                ValueError,
                "Development.*reasoning effort.*xhigh.*gpt-5.6-luna.*options",
            ):
                cli.resolve_run_workflow(
                    writer,
                    default_portable_component_catalog(),
                    user_workflow_path=configuration_path,
                    live_model_catalog=self._live_catalog(),
                    require_codex_preflight=True,
                )

        self.assertNotIn("resolved_workflow", writer.state)
        self.assertNotIn("resolved_workflow_hash", writer.state)

    def test_rerun_preflight_keeps_current_preferences_when_refresh_is_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = root / "devloop-plan.json"
            issue_index = root / "README.md"
            issue_index.write_text("", encoding="utf-8")
            catalog = default_portable_component_catalog()
            current_workflow = default_portable_workflow()
            writer = LoopStateWriter(issue_index)
            writer.record_resolved_workflow(current_workflow, catalog)
            original_hash = writer.state["resolved_workflow_hash"]

            unavailable_document = current_workflow.to_dict()
            development = next(
                step
                for step in unavailable_document["steps"]
                if step["instance_id"] == DEVELOPMENT_STEP_ID
            )
            development["codex_settings"]["model"] = "missing-model"
            WorkflowDefaultStore(configuration_path, catalog).replace(
                load_portable_workflow(unavailable_document, catalog)
            )

            with self.assertRaisesRegex(
                ValueError,
                "Development.*model.*missing-model.*options",
            ):
                cli.resolve_run_workflow(
                    writer,
                    catalog,
                    user_workflow_path=configuration_path,
                    live_model_catalog=self._live_catalog(),
                    require_codex_preflight=True,
                )

            restored = LoopStateWriter(issue_index)

        self.assertEqual(
            restored.resolved_workflow(catalog),
            current_workflow,
        )
        self.assertEqual(restored.state["resolved_workflow_hash"], original_hash)

    def test_cli_preflight_repair_can_open_options_and_retry_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = root / "devloop-plan.json"
            issue_index = root / "README.md"
            issue_index.write_text("", encoding="utf-8")
            catalog = default_portable_component_catalog()
            invalid_document = default_portable_workflow().to_dict()
            invalid_document["steps"][0]["codex_settings"]["model"] = "missing-model"
            store = WorkflowDefaultStore(configuration_path, catalog)
            store.replace(load_portable_workflow(invalid_document, catalog))
            writer = LoopStateWriter(issue_index)
            discoveries = iter(
                (
                    CatalogDiscoveryError("temporary failure one"),
                    CatalogDiscoveryError("temporary failure two"),
                    self._live_catalog(),
                )
            )
            discovery_count = 0

            def discover() -> CodexModelCatalog:
                nonlocal discovery_count
                discovery_count += 1
                result = next(discoveries)
                if isinstance(result, Exception):
                    raise result
                return result

            actions = iter(("/options", "retry-catalog"))

            def repair(*_args: object, **_kwargs: object) -> EditorResult:
                store.replace(default_portable_workflow())
                return EditorResult.APPLIED

            with mock.patch.object(
                cli,
                "run_workflow_editor",
                side_effect=repair,
            ) as editor:
                workflow = cli.resolve_run_workflow_with_repair(
                    writer,
                    catalog,
                    user_workflow_path=configuration_path,
                    model_catalog_loader=discover,
                    read_line=lambda _prompt: next(actions),
                    write=lambda _message: None,
                )

        self.assertEqual(workflow, default_portable_workflow())
        editor.assert_called_once()
        self.assertEqual(discovery_count, 3)
        self.assertIn("resolved_workflow", writer.state)

    def test_existing_run_preflight_repair_adopts_edited_execution_preferences(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = root / "devloop-plan.json"
            issue_index = root / "README.md"
            issue_index.write_text("", encoding="utf-8")
            catalog = default_portable_component_catalog()
            current_workflow = default_portable_workflow()
            writer = LoopStateWriter(issue_index)
            writer.record_resolved_workflow(current_workflow, catalog)

            invalid_document = current_workflow.to_dict()
            invalid_development = next(
                step
                for step in invalid_document["steps"]
                if step["instance_id"] == DEVELOPMENT_STEP_ID
            )
            invalid_development["codex_settings"]["model"] = "missing-model"
            store = WorkflowDefaultStore(configuration_path, catalog)
            store.replace(load_portable_workflow(invalid_document, catalog))

            repaired_document = current_workflow.to_dict()
            repaired_development = next(
                step
                for step in repaired_document["steps"]
                if step["instance_id"] == DEVELOPMENT_STEP_ID
            )
            repaired_development["codex_settings"] = {
                "model": "gpt-5.6-sol",
                "reasoning_effort": "xhigh",
                "fast": "OFF",
            }
            repaired_workflow = load_portable_workflow(
                repaired_document,
                catalog,
            )

            def repair(*_args: object, **_kwargs: object) -> EditorResult:
                store.replace(repaired_workflow)
                return EditorResult.APPLIED

            with mock.patch.object(
                cli,
                "run_workflow_editor",
                side_effect=repair,
            ) as editor:
                resolved = cli.resolve_run_workflow_with_repair(
                    writer,
                    catalog,
                    user_workflow_path=configuration_path,
                    model_catalog_loader=self._live_catalog,
                    read_line=lambda _prompt: "/options",
                    write=lambda _message: None,
                )

        self.assertEqual(
            resolved.step(DEVELOPMENT_STEP_ID).codex_settings.as_tuple(),
            ("gpt-5.6-sol", "xhigh", FastPreference.OFF),
        )
        self.assertEqual(
            editor.call_args.kwargs["current_workflow"],
            current_workflow,
        )

    def test_cli_preflight_sanitizes_backend_catalog_errors(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_index = root / "README.md"
            issue_index.write_text("", encoding="utf-8")
            output: list[str] = []

            workflow = cli.resolve_run_workflow_with_repair(
                LoopStateWriter(issue_index),
                default_portable_component_catalog(),
                user_workflow_path=root / "devloop-plan.json",
                model_catalog_loader=lambda: (_ for _ in ()).throw(
                    CatalogDiscoveryError(HOSTILE_TERMINAL_TEXT)
                ),
                read_line=lambda _prompt: "/quit",
                write=output.append,
            )

        self.assertIsNone(workflow)
        assert_terminal_text_is_safe(
            self,
            "\n".join(output),
            redirected=True,
        )

    def test_cli_repair_resets_and_applies_a_schema_v1_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = root / "devloop-plan.json"
            configuration_path.write_text(
                '{"target_repo":"/repo","user_workflow_default":'
                '{"schema":"devloop.portable-workflow/v1"},'
                '"user_workflow_default_hash":"invalid"}',
                encoding="utf-8",
            )
            issue_index = root / "README.md"
            issue_index.write_text("", encoding="utf-8")
            catalog = default_portable_component_catalog()
            actions = iter(("/options", "reset-workflow", "apply"))

            workflow = cli.resolve_run_workflow_with_repair(
                LoopStateWriter(issue_index),
                catalog,
                user_workflow_path=configuration_path,
                model_catalog_loader=self._live_catalog,
                read_line=lambda _prompt: next(actions),
                write=lambda _message: None,
            )
            persisted = WorkflowDefaultStore(
                configuration_path,
                catalog,
            ).load()

        self.assertEqual(workflow, default_portable_workflow())
        self.assertEqual(persisted, default_portable_workflow())

    def test_cli_repair_cancel_preserves_a_malformed_v2_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            configuration_path = root / "devloop-plan.json"
            original = (
                '{"target_repo":"/repo","user_workflow_default":'
                '{"schema":"devloop.portable-workflow/v2","steps":[]},'
                '"user_workflow_default_hash":"invalid"}'
            )
            configuration_path.write_text(original, encoding="utf-8")
            issue_index = root / "README.md"
            issue_index.write_text("", encoding="utf-8")
            actions = iter(("/options", "cancel", "/quit"))

            workflow = cli.resolve_run_workflow_with_repair(
                LoopStateWriter(issue_index),
                default_portable_component_catalog(),
                user_workflow_path=configuration_path,
                model_catalog_loader=self._live_catalog,
                read_line=lambda _prompt: next(actions),
                write=lambda _message: None,
            )
            persisted = configuration_path.read_text(encoding="utf-8")

        self.assertIsNone(workflow)
        self.assertEqual(persisted, original)

    def test_executor_forwards_each_snapshotted_setting_without_pass_adaptation(self) -> None:
        calls: list[dict[str, object]] = []

        class PassingRunner:
            def run_role(self, **arguments: object) -> RoleResult:
                calls.append(arguments)
                return RoleResult(status="PASS")

        workflow = default_portable_workflow()

        PortableWorkflowExecutor(
            workflow,
            default_portable_component_catalog(),
            PassingRunner(),
        ).run(
            Issue("0001", "Settings", Path("0001.md"), False),
            pass_number=3,
            max_passes=3,
        )

        self.assertEqual(
            [call["codex_settings"] for call in calls],
            [
                step.codex_settings
                for step in workflow.primary_path()
                if default_portable_component_catalog().resolve(step.component_id).scope
                is StepScope.ISSUE
            ],
        )
        self.assertEqual(
            [call["execution_budget"] for call in calls],
            [
                step.execution_budget
                for step in workflow.primary_path()
                if default_portable_component_catalog().resolve(step.component_id).scope
                is StepScope.ISSUE
            ],
        )
        self.assertTrue(all(call["pass_number"] == 3 for call in calls))

    def test_loop_state_round_trips_the_current_per_step_settings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            issue_index = root / "README.md"
            issue_index.write_text("", encoding="utf-8")
            document = default_portable_workflow().to_dict()
            final_review = next(
                step
                for step in document["steps"]
                if step["instance_id"] == FINAL_REVIEW_STEP_ID
            )
            final_review["codex_settings"] = {
                "model": "gpt-5.6-terra",
                "reasoning_effort": "high",
                "fast": "ON",
            }
            workflow = load_portable_workflow(
                document,
                default_portable_component_catalog(),
            )
            writer = LoopStateWriter(issue_index)
            writer.record_resolved_workflow(
                workflow,
                default_portable_component_catalog(),
            )

            restored = LoopStateWriter(issue_index).resolved_workflow(
                default_portable_component_catalog()
            )

        self.assertEqual(
            restored.step(FINAL_REVIEW_STEP_ID).codex_settings.as_tuple(),
            ("gpt-5.6-terra", "high", FastPreference.ON),
        )
        self.assertEqual(restored, workflow)


if __name__ == "__main__":
    unittest.main()
