from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

from devloop import cli, interactive_runner
from devloop.portable_execution_backend import BackendModelCatalogAccess
from devloop.portable_runtime import portable_plain_mode_active
from devloop.portable_workflow import PortableStepComponentCatalog
from devloop.workflow_defaults import portable_planner_configuration_path


class OptionsCommandTests(unittest.TestCase):
    """`devloop options` opens Dev Loop Options without a PRD or a run."""

    def _isolated_configuration(self, root: Path) -> mock._patch_dict:
        return mock.patch.dict(
            os.environ,
            {"APPDATA": str(root), "XDG_CONFIG_HOME": str(root)},
        )

    def test_options_command_opens_dev_loop_options_without_a_prd(self) -> None:
        with tempfile.TemporaryDirectory() as raw, self._isolated_configuration(
            Path(raw)
        ), mock.patch.object(interactive_runner, "run_options_menu") as run_options_menu:
            result = cli.main(["options"])
            expected_state_path = portable_planner_configuration_path()

        self.assertEqual(result, 0)
        run_options_menu.assert_called_once()
        bundle_root, _selection, state_path = run_options_menu.call_args.args
        self.assertEqual(bundle_root, Path(__file__).resolve().parents[1])
        self.assertEqual(state_path, expected_state_path)
        kwargs = run_options_menu.call_args.kwargs
        self.assertIsInstance(kwargs["component_catalog"], PortableStepComponentCatalog)
        catalog_access = kwargs["catalog_access"]
        self.assertIsInstance(catalog_access, BackendModelCatalogAccess)
        self.assertEqual(catalog_access.cwd, Path.cwd().resolve())
        self.assertEqual(catalog_access.codex, "codex")

    def test_slash_options_is_accepted_as_the_same_command(self) -> None:
        with tempfile.TemporaryDirectory() as raw, self._isolated_configuration(
            Path(raw)
        ), mock.patch.object(interactive_runner, "run_options_menu") as run_options_menu:
            result = cli.main(["/options"])

        self.assertEqual(result, 0)
        run_options_menu.assert_called_once()

    def test_codex_flag_reaches_model_catalog_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as raw, self._isolated_configuration(
            Path(raw)
        ), mock.patch.object(interactive_runner, "run_options_menu") as run_options_menu:
            cli.main(["options", "--codex", "custom-codex"])

        catalog_access = run_options_menu.call_args.kwargs["catalog_access"]
        self.assertEqual(catalog_access.codex, "custom-codex")

    def test_plain_flag_drives_the_menu_through_line_input(self) -> None:
        plain_mode_seen: list[bool] = []

        def record_mode(*_args: object, **_kwargs: object) -> None:
            plain_mode_seen.append(portable_plain_mode_active())

        with tempfile.TemporaryDirectory() as raw, self._isolated_configuration(
            Path(raw)
        ), mock.patch.object(
            interactive_runner, "run_options_menu", side_effect=record_mode
        ):
            result = cli.main(["options", "--plain"])

        self.assertEqual(result, 0)
        self.assertEqual(plain_mode_seen, [True])

    def test_options_command_rejects_run_flags(self) -> None:
        error = StringIO()

        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            cli.main(["options", "--prd", "prd.md"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments: --prd", error.getvalue())

    def test_keyboard_interrupt_leaves_the_menu_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as raw, self._isolated_configuration(
            Path(raw)
        ), mock.patch.object(
            interactive_runner, "run_options_menu", side_effect=KeyboardInterrupt
        ):
            result = cli.main(["options"])

        self.assertEqual(result, 130)

    def test_run_help_points_at_the_options_command(self) -> None:
        help_text = cli.build_parser().format_help()

        self.assertIn("devloop options", help_text)
        self.assertIn("reasoning effort", help_text)

    def test_options_help_describes_the_menu_and_its_flags(self) -> None:
        help_text = cli.build_options_parser().format_help()

        self.assertIn("devloop options", help_text)
        self.assertIn("--codex", help_text)
        self.assertIn("--plain", help_text)
        self.assertIn("Execution Backend, model, and reasoning effort", help_text)


if __name__ == "__main__":
    unittest.main()
