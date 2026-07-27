from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from devloop import cli, interactive_runner
from devloop.portable_sessions import PortableWorkflowOperation


class PortableSessionEntrypointTests(unittest.TestCase):
    def test_planning_application_starts_with_a_passive_session_launch(self) -> None:
        arguments = ["--repo", str(Path.cwd())]

        with mock.patch.dict(
            os.environ,
            {"DEVLOOP_UI_MODE": "application"},
        ), mock.patch(
            "devloop.portable_ui.app.run_portable_sessions_application",
            return_value=17,
        ) as run_application:
            result = interactive_runner.main(arguments)

        self.assertEqual(result, 17)
        launch = run_application.call_args.args[0]
        self.assertEqual(launch.operation, PortableWorkflowOperation.PLANNING)
        self.assertEqual(launch.arguments, tuple(arguments))
        self.assertEqual(launch.checkout, Path.cwd())

    def test_delivery_application_starts_with_a_passive_session_launch(self) -> None:
        arguments = ["--prd", "feature.md", "--issues", "issues/README.md"]

        with mock.patch.dict(
            os.environ,
            {"DEVLOOP_UI_MODE": "application"},
        ), mock.patch(
            "devloop.portable_ui.app.run_portable_sessions_application",
            return_value=19,
        ) as run_application:
            result = cli.main(arguments)

        self.assertEqual(result, 19)
        launch = run_application.call_args.args[0]
        self.assertEqual(launch.operation, PortableWorkflowOperation.DELIVERY)
        self.assertEqual(launch.arguments, tuple(arguments))
        self.assertEqual(launch.checkout, Path.cwd())


if __name__ == "__main__":
    unittest.main()
