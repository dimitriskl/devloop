from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from unittest import mock

from devloop import cli, interactive_runner
from devloop.portable_sessions import (
    PortableSessionIntent,
    PortableSessionIntentKind,
    PortableSessionSnapshot,
    PortableSessionStatus,
    PortableWorkflowOperation,
)


class PortableSessionEntrypointTests(unittest.TestCase):
    def test_prd_command_starts_and_focuses_only_its_supplied_session(self) -> None:
        from devloop.portable_ui.app import PortableApplicationShell

        arguments = ["--prd", "feature.md"]
        supervisor = mock.Mock()
        supervisor.list_sessions.return_value = (
            PortableSessionSnapshot(
                session_id="unrelated-unavailable",
                checkout=Path.cwd(),
                status=PortableSessionStatus.UNAVAILABLE,
            ),
        )
        supervisor.list_saved_projects.return_value = ()
        supervisor.try_next_event.return_value = None

        def start(intent: PortableSessionIntent) -> PortableSessionSnapshot:
            self.assertEqual(intent.kind, PortableSessionIntentKind.START)
            assert intent.launch is not None
            self.assertEqual(intent.launch.arguments, tuple(arguments))
            return PortableSessionSnapshot(
                session_id=intent.launch.session_id,
                checkout=intent.launch.checkout,
                status=PortableSessionStatus.RUNNING,
            )

        supervisor.handle_intent.side_effect = start

        async def exercise(app: PortableApplicationShell) -> None:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                supervisor.handle_intent.assert_called_once()
                intent = supervisor.handle_intent.call_args.args[0]
                self.assertEqual(app._active_session_id, intent.launch.session_id)
                self.assertNotEqual(app._active_session_id, "unrelated-unavailable")

        with mock.patch.dict(os.environ, {"DEVLOOP_UI_MODE": "application"}), mock.patch(
            "devloop.portable_session_catalog.PortableSessionCatalog"
        ), mock.patch(
            "devloop.portable_ui.app.PortableSessionSupervisor", return_value=supervisor
        ), mock.patch.object(
            PortableApplicationShell, "run", lambda app: asyncio.run(exercise(app))
        ):
            self.assertEqual(cli.main(arguments), 130)

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

    def test_delivery_application_requests_immediate_session_start(self) -> None:
        from devloop.portable_ui.app import PortableSessionStartupAction

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
        self.assertEqual(
            run_application.call_args.kwargs["startup_action"],
            PortableSessionStartupAction.START_SUPPLIED_SESSION,
        )
        launch = run_application.call_args.args[0]
        self.assertEqual(launch.operation, PortableWorkflowOperation.DELIVERY)
        self.assertEqual(launch.arguments, tuple(arguments))
        self.assertEqual(launch.checkout, Path.cwd())


if __name__ == "__main__":
    unittest.main()
