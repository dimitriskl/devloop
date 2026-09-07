from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_workflow_editor import (
    FakeEditor,
    _codex_catalog,
    _fake_availability,
    _per_backend_catalog_loader,
    _RecordingVerifier,
)

from devloop.cli_ui import format_menu_entry
from devloop.model_catalog import CatalogDiscoveryError, ModelCatalog
from devloop.portable_execution_backend import ExecutionBackendId
from devloop.portable_workflow import (
    ANALYSIS_STEP_ID,
    PORTABLE_WORKFLOW_SCHEMA,
    FastPreference,
    StepExecutionSettings,
    default_portable_component_catalog,
    default_portable_workflow,
    load_portable_workflow,
)
from devloop.workflow_defaults import WorkflowDefaultStore
from devloop.workflow_editor import (
    EditorResult,
    SelectionMenu,
    run_options_menu_editor,
)


def _stored_workflow(path: Path):
    return WorkflowDefaultStore(path, default_portable_component_catalog()).load()


class OptionsMenuTopLevelTests(unittest.TestCase):
    def test_exit_shows_the_numbered_top_menu_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_options_menu_editor(
                path,
                read_line=FakeEditor(["0"]).read_line,
                write=output.append,
                terminal_width=120,
            )
            written = path.exists()

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.CANCELLED)
        self.assertFalse(written)
        self.assertIn("Dev Loop Options", rendered)
        self.assertIn("1. Models per role", rendered)
        self.assertIn("2. Save", rendered)
        self.assertIn("0. Exit", rendered)

    def test_models_per_role_lists_every_agent_step_with_its_current_settings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []

            result = run_options_menu_editor(
                path,
                read_line=FakeEditor(["1", "0", "0"]).read_line,
                write=output.append,
                terminal_width=120,
            )

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.CANCELLED)
        self.assertIn("Models per role", rendered)
        self.assertRegex(rendered, r"1\. Analysis\s+gpt-5\.6-sol / xhigh\s+Codex CLI")
        self.assertRegex(rendered, r"2\. Development\s+gpt-5\.6-luna / high\s+Codex CLI")
        self.assertRegex(rendered, r"3\. Security Review\s+gpt-5\.6-sol / xhigh\s+Codex CLI")
        self.assertRegex(rendered, r"4\. Final Review\s+gpt-5\.6-sol / xhigh\s+Codex CLI")
        self.assertRegex(rendered, r"5\. QA\s+gpt-5\.6-terra / high\s+Codex CLI")
        self.assertIn("0. Back", rendered)


class OptionsMenuModelSelectionTests(unittest.TestCase):
    def test_backend_model_effort_path_updates_the_row_and_save_persists_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []
            # roles, Analysis, Codex CLI, Luna, high, back to top, Save, Exit
            editor = FakeEditor(["1", "1", "1", "2", "1", "0", "2", "0"])

            result = run_options_menu_editor(
                path,
                read_line=editor.read_line,
                write=output.append,
                terminal_width=120,
                model_catalog_loader=_per_backend_catalog_loader(),
                backend_availability=_fake_availability(),
            )
            analysis = _stored_workflow(path).step(ANALYSIS_STEP_ID)

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.APPLIED)
        self.assertEqual(
            analysis.execution_settings,
            StepExecutionSettings(
                ExecutionBackendId.CODEX_CLI,
                "gpt-5.6-luna",
                "high",
                FastPreference.OFF,
            ),
        )
        self.assertIn("Backend for Analysis", rendered)
        self.assertRegex(rendered, r"1\. Codex CLI\s+installed.*\(current\)")
        self.assertRegex(rendered, r"2\. Claude Code\s+installed")
        self.assertIn("Model for Analysis", rendered)
        self.assertRegex(rendered, r"1\. Sol — gpt-5\.6-sol.*\(current\)")
        self.assertIn("2. Luna — gpt-5.6-luna", rendered)
        self.assertIn("Effort for Analysis", rendered)
        self.assertIn("1. high", rendered)
        self.assertRegex(rendered, r"1\. Analysis\s+gpt-5\.6-luna / high\s+Codex CLI")

    def test_back_at_every_level_leaves_the_step_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []
            # roles, Analysis, Codex CLI, Luna, then 0 on effort, model, backend,
            # roles, and the top menu
            editor = FakeEditor(["1", "1", "1", "2", "0", "0", "0", "0", "0"])

            result = run_options_menu_editor(
                path,
                read_line=editor.read_line,
                write=output.append,
                terminal_width=120,
                model_catalog_loader=_per_backend_catalog_loader(),
                backend_availability=_fake_availability(),
            )
            written = path.exists()

        def screens(title: str) -> int:
            return sum(1 for block in output if block.startswith(title))

        self.assertIs(result, EditorResult.CANCELLED)
        self.assertFalse(written)
        self.assertEqual(screens("Effort for Analysis"), 1)
        self.assertEqual(screens("Model for Analysis"), 2)
        self.assertEqual(screens("Backend for Analysis"), 2)
        self.assertEqual(screens("Models per role"), 2)
        self.assertEqual(screens("Dev Loop Options"), 2)
        self.assertNotIn("Unsaved changes", "\n".join(output))


class OptionsMenuSaveTests(unittest.TestCase):
    def test_exit_with_unsaved_changes_discards_them_with_a_notice(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []
            # roles, Analysis, Codex CLI, Luna, high, back to top, Exit without Save
            editor = FakeEditor(["1", "1", "1", "2", "1", "0", "0"])

            result = run_options_menu_editor(
                path,
                read_line=editor.read_line,
                write=output.append,
                terminal_width=120,
                model_catalog_loader=_per_backend_catalog_loader(),
                backend_availability=_fake_availability(),
            )
            written = path.exists()

        self.assertIs(result, EditorResult.CANCELLED)
        self.assertFalse(written)
        self.assertIn("Unsaved changes discarded", "\n".join(output))

    def test_exit_after_save_does_not_warn(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []
            editor = FakeEditor(["1", "1", "1", "2", "1", "0", "2", "0"])

            result = run_options_menu_editor(
                path,
                read_line=editor.read_line,
                write=output.append,
                terminal_width=120,
                model_catalog_loader=_per_backend_catalog_loader(),
                backend_availability=_fake_availability(),
            )

        self.assertIs(result, EditorResult.APPLIED)
        self.assertNotIn("Unsaved changes", "\n".join(output))


class OptionsMenuClaudeTests(unittest.TestCase):
    def test_claude_model_is_verified_once_and_stored_with_its_backend(self) -> None:
        verifier = _RecordingVerifier()
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []
            # roles, Analysis, Claude Code, Claude Sonnet 5, high, back, Save, Exit
            editor = FakeEditor(["1", "1", "2", "3", "3", "0", "2", "0"])

            result = run_options_menu_editor(
                path,
                read_line=editor.read_line,
                write=output.append,
                terminal_width=120,
                model_catalog_loader=_per_backend_catalog_loader(),
                backend_availability=_fake_availability(),
                verify_model=verifier.verify,
            )
            analysis = _stored_workflow(path).step(ANALYSIS_STEP_ID)

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.APPLIED)
        self.assertEqual(
            analysis.execution_settings,
            StepExecutionSettings(
                ExecutionBackendId.CLAUDE_CODE,
                "claude-sonnet-5",
                "high",
                FastPreference.OFF,
            ),
        )
        self.assertEqual(
            verifier.calls,
            [(ExecutionBackendId.CLAUDE_CODE, "claude-sonnet-5")],
        )
        self.assertIn("Model for Analysis — Claude Code", rendered)
        self.assertIn("1. Claude Fable 5.1 — claude-fable-5-1", rendered)
        self.assertIn("3. Claude Sonnet 5 — claude-sonnet-5", rendered)
        self.assertRegex(rendered, r"1\. Analysis\s+claude-sonnet-5 / high\s+Claude Code")

    def test_refused_claude_model_is_reported_and_the_step_stays_unchanged(
        self,
    ) -> None:
        verifier = _RecordingVerifier(refusals={"claude-sonnet-5": "not on this account"})
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []
            # roles, Analysis, Claude Code, Sonnet, high -> refused, then 0 back
            # through model, backend, roles, top
            editor = FakeEditor(["1", "1", "2", "3", "3", "0", "0", "0", "0"])

            result = run_options_menu_editor(
                path,
                read_line=editor.read_line,
                write=output.append,
                terminal_width=120,
                model_catalog_loader=_per_backend_catalog_loader(),
                backend_availability=_fake_availability(),
                verify_model=verifier.verify,
            )
            written = path.exists()

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.CANCELLED)
        self.assertFalse(written)
        self.assertIn("refused model 'claude-sonnet-5'", rendered)
        self.assertIn("not on this account", rendered)
        self.assertEqual(
            sum(1 for block in output if block.startswith("Model for Analysis")),
            2,
        )

    def test_alias_is_stored_as_the_pinned_identifier_it_resolves_to(self) -> None:
        verifier = _RecordingVerifier(resolutions={"opus": "claude-opus-5"})
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []
            # roles, Analysis, Claude Code, Opus (latest) alias, high, back, Save, Exit
            editor = FakeEditor(["1", "1", "2", "5", "3", "0", "2", "0"])

            result = run_options_menu_editor(
                path,
                read_line=editor.read_line,
                write=output.append,
                terminal_width=120,
                model_catalog_loader=_per_backend_catalog_loader(),
                backend_availability=_fake_availability(),
                verify_model=verifier.verify,
            )
            analysis = _stored_workflow(path).step(ANALYSIS_STEP_ID)

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.APPLIED)
        assert analysis.execution_settings is not None
        self.assertEqual(analysis.execution_settings.model, "claude-opus-5")
        self.assertIn("opus resolved to claude-opus-5", rendered)


class OptionsMenuCatalogTests(unittest.TestCase):
    def test_failed_catalog_is_reported_and_picking_the_backend_again_retries(
        self,
    ) -> None:
        attempts: list[ExecutionBackendId] = []

        def loader(backend: ExecutionBackendId) -> ModelCatalog:
            attempts.append(backend)
            if len(attempts) <= 2:
                raise CatalogDiscoveryError("codex offline")
            return _codex_catalog()

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            output: list[str] = []
            # roles, Analysis, Codex CLI (fails), Codex CLI again (works), Luna,
            # high, back, Save, Exit
            editor = FakeEditor(["1", "1", "1", "1", "2", "1", "0", "2", "0"])

            result = run_options_menu_editor(
                path,
                read_line=editor.read_line,
                write=output.append,
                terminal_width=120,
                model_catalog_loader=loader,
                backend_availability=_fake_availability(),
            )
            analysis = _stored_workflow(path).step(ANALYSIS_STEP_ID)

        rendered = "\n".join(output)
        self.assertIs(result, EditorResult.APPLIED)
        self.assertIn("Live Codex CLI Model Catalog unavailable: codex offline", rendered)
        self.assertEqual(
            sum(1 for block in output if block.startswith("Backend for Analysis")),
            2,
        )
        assert analysis.execution_settings is not None
        self.assertEqual(analysis.execution_settings.model, "gpt-5.6-luna")


def _write_corrupt_default(path: Path) -> str:
    original = json.dumps(
        {
            "target_repo": "/repo",
            "user_workflow_default": {"schema": PORTABLE_WORKFLOW_SCHEMA, "steps": []},
            "user_workflow_default_hash": "invalid",
        },
        indent=2,
    )
    path.write_text(original, encoding="utf-8")
    return original


class OptionsMenuRecoveryTests(unittest.TestCase):
    def test_corrupt_default_offers_reset_and_save_restores_the_built_in(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            _write_corrupt_default(path)
            output: list[str] = []

            result = run_options_menu_editor(
                path,
                read_line=FakeEditor(["1", "2", "0"]).read_line,
                write=output.append,
                terminal_width=120,
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))
            stored = _stored_workflow(path)

        first_screen = output[0]
        self.assertIs(result, EditorResult.APPLIED)
        self.assertIn("could not be loaded", first_screen)
        self.assertIn("1. Reset to the built-in workflow default", first_screen)
        self.assertIn("2. Save", first_screen)
        self.assertNotIn("Models per role", first_screen)
        self.assertEqual(stored, default_portable_workflow())
        self.assertEqual(persisted["target_repo"], "/repo")

    def test_corrupt_default_exit_preserves_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            original = _write_corrupt_default(path)
            output: list[str] = []

            result = run_options_menu_editor(
                path,
                read_line=FakeEditor(["0"]).read_line,
                write=output.append,
                terminal_width=120,
            )
            persisted = path.read_text(encoding="utf-8")

        self.assertIs(result, EditorResult.CANCELLED)
        self.assertEqual(persisted, original)
        self.assertNotIn("Unsaved changes", "\n".join(output))

    def test_save_before_reset_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            original = _write_corrupt_default(path)
            output: list[str] = []

            result = run_options_menu_editor(
                path,
                read_line=FakeEditor(["2", "0"]).read_line,
                write=output.append,
                terminal_width=120,
            )
            persisted = path.read_text(encoding="utf-8")

        self.assertIs(result, EditorResult.CANCELLED)
        self.assertEqual(persisted, original)
        self.assertIn("must be reset before Apply", "\n".join(output))


class OptionsMenuApplicationModeTests(unittest.TestCase):
    def test_application_menus_carry_bare_labels_and_zero_as_cancel(self) -> None:
        menus: list[SelectionMenu] = []
        answers = iter(["1", "0", "0"])

        def select(menu: SelectionMenu) -> str:
            menus.append(menu)
            return next(answers)

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"

            result = run_options_menu_editor(
                path,
                read_line=FakeEditor([]).read_line,
                write=lambda _line: None,
                terminal_width=120,
                select_option=select,
            )

        self.assertIs(result, EditorResult.CANCELLED)
        top, roles = menus[0], menus[1]
        self.assertEqual(top.title, "Dev Loop Options")
        self.assertEqual(
            top.options,
            (("1", "Models per role"), ("2", "Save"), ("0", "Exit")),
        )
        self.assertEqual(top.cancel_key, "0")
        self.assertEqual(roles.title, "Models per role")
        self.assertEqual([key for key, _ in roles.options], ["1", "2", "3", "4", "5", "0"])
        self.assertTrue(roles.options[0][1].startswith("Analysis"))
        self.assertEqual(roles.options[-1], ("0", "Back"))

    def test_application_menu_labels_carry_no_number_of_their_own(self) -> None:
        """The application renderer prefixes each key, so a numbered label doubles it.

        Regression: the Models per role screen once showed `1. 1. Analysis …`
        because the label repeated the key that `format_menu_entry` already prints.
        """
        menus: list[SelectionMenu] = []
        answers = iter(["1", "0", "0"])

        def select(menu: SelectionMenu) -> str:
            menus.append(menu)
            return next(answers)

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            run_options_menu_editor(
                path,
                read_line=FakeEditor([]).read_line,
                write=lambda _line: None,
                terminal_width=120,
                select_option=select,
            )

        self.assertEqual(len(menus), 3)
        for menu in menus:
            for key, label in menu.options:
                with self.subTest(menu=menu.title, key=key):
                    self.assertNotRegex(label, r"^\d+\. ")
                    self.assertNotRegex(format_menu_entry(key, label), r"\d+\. \d+\. ")
        self.assertEqual(format_menu_entry("1", menus[0].options[0][1]), "  1. Models per role")
        self.assertRegex(format_menu_entry("1", menus[1].options[0][1]), r"^  1\. Analysis\s")


def _default_with_fast_on(path: Path) -> None:
    catalog = default_portable_component_catalog()
    document = default_portable_workflow().to_dict()
    document["steps"][0]["execution_settings"]["fast"] = "ON"
    WorkflowDefaultStore(path, catalog).replace(load_portable_workflow(document, catalog))


class OptionsMenuFastTests(unittest.TestCase):
    def _run(self, path: Path, inputs: list[str]) -> tuple[EditorResult, str]:
        output: list[str] = []
        result = run_options_menu_editor(
            path,
            read_line=FakeEditor(inputs).read_line,
            write=output.append,
            terminal_width=120,
            model_catalog_loader=_per_backend_catalog_loader(),
            backend_availability=_fake_availability(),
            verify_model=_RecordingVerifier().verify,
        )
        return result, "\n".join(output)

    def test_fast_is_kept_when_backend_and_model_still_support_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            _default_with_fast_on(path)
            # roles, Analysis, Codex CLI, Sol, xhigh, back, Save, Exit
            result, rendered = self._run(path, ["1", "1", "1", "1", "1", "0", "2", "0"])
            analysis = _stored_workflow(path).step(ANALYSIS_STEP_ID)

        self.assertIs(result, EditorResult.APPLIED)
        assert analysis.execution_settings is not None
        self.assertIs(analysis.execution_settings.fast, FastPreference.ON)
        self.assertNotIn("Fast was set to Off", rendered)

    def test_fast_is_turned_off_when_the_new_model_does_not_advertise_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            _default_with_fast_on(path)
            # roles, Analysis, Codex CLI, Luna, high, back, Save, Exit
            result, rendered = self._run(path, ["1", "1", "1", "2", "1", "0", "2", "0"])
            analysis = _stored_workflow(path).step(ANALYSIS_STEP_ID)

        self.assertIs(result, EditorResult.APPLIED)
        assert analysis.execution_settings is not None
        self.assertEqual(analysis.execution_settings.model, "gpt-5.6-luna")
        self.assertIs(analysis.execution_settings.fast, FastPreference.OFF)
        self.assertIn("Fast was set to Off", rendered)

    def test_fast_is_turned_off_when_the_backend_changes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "devloop-plan.json"
            _default_with_fast_on(path)
            # roles, Analysis, Claude Code, Claude Sonnet 5, high, back, Save, Exit
            result, rendered = self._run(path, ["1", "1", "2", "2", "3", "0", "2", "0"])
            analysis = _stored_workflow(path).step(ANALYSIS_STEP_ID)

        self.assertIs(result, EditorResult.APPLIED)
        assert analysis.execution_settings is not None
        self.assertIs(analysis.execution_settings.backend, ExecutionBackendId.CLAUDE_CODE)
        self.assertIs(analysis.execution_settings.fast, FastPreference.OFF)
        self.assertIn("Fast was set to Off", rendered)
