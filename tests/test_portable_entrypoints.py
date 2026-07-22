from __future__ import annotations

import threading
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from devloop import cli, interactive_runner
from devloop.portable_runtime import (
    PortableRuntimeBridge,
    PortableRuntimeEventKind,
    portable_plain_mode_active,
    portable_runtime_session,
)
from devloop.terminal_menu import MenuAction, read_workflow_command
from devloop.workflow_editor import EditorResult, run_workflow_editor


class PortableEntrypointTests(unittest.TestCase):
    def test_nested_development_handoff_reuses_the_application_session(self) -> None:
        bridge = PortableRuntimeBridge()
        plain_mode_seen: list[bool] = []

        def run_development(*_args: object) -> int:
            plain_mode_seen.append(portable_plain_mode_active())
            return 0

        with portable_runtime_session(bridge), mock.patch.dict(
            "os.environ",
            {"DEVLOOP_UI_MODE": "application"},
        ), mock.patch.object(cli, "_run_devloop", side_effect=run_development):
            result = cli.main(["--prd", "prd.md", "--issues", "issues.md"])

        self.assertEqual(result, 0)
        self.assertEqual(plain_mode_seen, [False])

    def test_both_entrypoints_accept_plain_mode(self) -> None:
        planning = interactive_runner.build_parser().parse_args(["--plain"])
        delivery = cli.build_parser().parse_args(
            ["--prd", "prd.md", "--issues", "issues.md", "--plain"]
        )

        self.assertTrue(planning.plain)
        self.assertTrue(delivery.plain)

    def test_workflow_commands_are_selected_inside_the_application(self) -> None:
        bridge = PortableRuntimeBridge()
        result: list[str] = []

        def choose() -> None:
            with portable_runtime_session(bridge):
                result.append(
                    read_workflow_command(
                        "Action: ",
                        fallback=lambda _prompt: "fallback",
                        actions=(
                            MenuAction("Workflow", "Apply changes", "apply"),
                        ),
                    )
                )

        worker = threading.Thread(target=choose)
        worker.start()
        event = bridge.next_event(timeout=1)

        self.assertIs(event.kind, PortableRuntimeEventKind.CHOICE_REQUESTED)
        self.assertIn(("apply", "Workflow · Apply changes"), event.options)
        bridge.respond(event.request_id, "apply")
        worker.join(timeout=1)

        self.assertEqual(result, ["apply"])

    def test_workflow_editor_exposes_steps_as_the_application_navigation(self) -> None:
        bridge = PortableRuntimeBridge()
        result: list[EditorResult] = []
        with tempfile.TemporaryDirectory() as raw:
            configuration_path = Path(raw) / "devloop-plan.json"

            def edit() -> None:
                with portable_runtime_session(bridge):
                    result.append(
                        run_workflow_editor(
                            configuration_path,
                            read_line=lambda _prompt: self.fail("unexpected line input"),
                            read_command=lambda _prompt: self.fail(
                                "unexpected action input"
                            ),
                            write=lambda _line: None,
                            terminal_width=100,
                            terminal_height=30,
                        )
                    )

            worker = threading.Thread(target=edit)
            worker.start()
            choice = bridge.next_event(timeout=1)

            self.assertIs(choice.kind, PortableRuntimeEventKind.CHOICE_REQUESTED)
            labels = [label for _key, label in choice.options]
            self.assertIn("1. Analysis", labels)
            self.assertIn("2. Development", labels)
            self.assertIn(("f2", "apply"), choice.shortcuts)
            self.assertIn(("f3", "graph"), choice.shortcuts)

            development_key = next(
                key for key, label in choice.options if label == "2. Development"
            )
            bridge.preview(choice.request_id, development_key)
            previews = [bridge.next_event(timeout=1), bridge.next_event(timeout=1)]
            self.assertTrue(
                any("Development" in preview.content for preview in previews)
            )
            bridge.respond(choice.request_id, "cancel")
            worker.join(timeout=1)

        self.assertEqual(result, [EditorResult.CANCELLED])


if __name__ == "__main__":
    unittest.main()
