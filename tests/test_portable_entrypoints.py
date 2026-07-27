from __future__ import annotations

import threading
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from devloop import cli, interactive_runner
from devloop.portable_runtime import (
    PortableRuntimeBridge,
    PortableRuntimeEvent,
    PortableRuntimeEventKind,
    portable_plain_mode_active,
    portable_runtime_session,
)
from devloop.terminal_menu import WorkflowOptionsMenuState, read_workflow_command
from devloop.workflow_editor import EditorResult, WORKFLOW_ACTIONS, run_workflow_editor


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

    def test_workflow_options_have_seven_numbered_choices_inside_the_application(
        self,
    ) -> None:
        bridge = PortableRuntimeBridge()
        result: list[str] = []

        def choose() -> None:
            with portable_runtime_session(bridge):
                result.append(
                    read_workflow_command(
                        "Action: ",
                        fallback=lambda _prompt: "fallback",
                        actions=WORKFLOW_ACTIONS,
                    )
                )

        worker = threading.Thread(target=choose)
        worker.start()
        event = bridge.next_event(timeout=1)

        try:
            self.assertIs(event.kind, PortableRuntimeEventKind.CHOICE_REQUESTED)
            self.assertEqual(
                tuple(label for _key, label in event.options),
                (
                    "1. Previous step",
                    "2. Next step",
                    "3. Help",
                    "4. View options",
                    "5. Step options",
                    "6. Structure options",
                    "7. Save or reset options",
                ),
            )
            previous_key = event.options[0][0]
            bridge.respond(event.request_id, previous_key)
            worker.join(timeout=1)
        finally:
            if worker.is_alive():
                bridge.respond(event.request_id, "cancel")
                worker.join(timeout=1)

        self.assertEqual(result, ["__previous_step__"])

    def test_workflow_option_groups_open_focused_pages_with_back_navigation(
        self,
    ) -> None:
        bridge = PortableRuntimeBridge()
        result: list[str] = []

        def next_choice() -> PortableRuntimeEvent:
            while True:
                event = bridge.next_event(timeout=1)
                if event.kind is PortableRuntimeEventKind.CHOICE_REQUESTED:
                    return event

        def choose() -> None:
            with portable_runtime_session(bridge):
                result.append(
                    read_workflow_command(
                        "Action: ",
                        fallback=lambda _prompt: "fallback",
                        actions=WORKFLOW_ACTIONS,
                    )
                )

        worker = threading.Thread(target=choose)
        worker.start()
        try:
            root = next_choice()
            bridge.respond(root.request_id, root.options[3][0])
            view = next_choice()
            view_labels = tuple(label for _key, label in view.options)

            bridge.respond(view.request_id, view.options[-1][0])
            reopened_root = next_choice()
            bridge.respond(reopened_root.request_id, reopened_root.options[4][0])
            step = next_choice()
            step_labels = tuple(label for _key, label in step.options)

            bridge.respond(step.request_id, step.options[-1][0])
            reopened_root = next_choice()
            bridge.respond(reopened_root.request_id, reopened_root.options[5][0])
            structure = next_choice()
            structure_labels = tuple(label for _key, label in structure.options)

            bridge.respond(structure.request_id, structure.options[-1][0])
            reopened_root = next_choice()
            bridge.respond(reopened_root.request_id, reopened_root.options[6][0])
            save_or_reset = next_choice()
            save_or_reset_labels = tuple(
                label for _key, label in save_or_reset.options
            )

            apply_key = next(
                key
                for key, label in save_or_reset.options
                if "Apply workflow preferences" in label
            )
            bridge.respond(save_or_reset.request_id, apply_key)
            worker.join(timeout=1)
        finally:
            if worker.is_alive():
                bridge.request_stop()
                worker.join(timeout=1)

        self.assertEqual(
            view_labels,
            (
                "1. Inspect current run",
                "2. Edit workflow default",
                "3. Show or hide route map",
                "4. Show or hide technical details",
                "B. Back to Options",
            ),
        )
        self.assertEqual(
            step_labels,
            (
                "1. Select any workflow step",
                "2. Rename selected step",
                "3. Change component type",
                "4. Choose execution backend",
                "5. Choose model",
                "6. Choose reasoning effort",
                "7. Toggle Fast mode",
                "8. Edit execution budget",
                "9. Edit guidance",
                "10. Manage capabilities",
                "B. Back to Options",
            ),
        )
        self.assertEqual(
            structure_labels,
            (
                "1. Add step to the end",
                "2. Insert step at a position",
                "3. Duplicate selected step",
                "4. Delete selected step",
                "5. Move selected step earlier",
                "6. Move selected step later",
                "7. Move selected step to position",
                "8. Edit outcome route",
                "9. Edit input binding",
                "B. Back to Options",
            ),
        )
        self.assertEqual(
            save_or_reset_labels,
            (
                "1. Draft · Undo last edit",
                "2. Draft · Reset selected step",
                "3. Draft · Reset entire workflow",
                "4. Catalog · Retry model catalog",
                "5. Finish · Apply workflow preferences",
                "6. Finish · Cancel without saving",
                "B. Back to Options",
            ),
        )
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
            self.assertIn("Options…", labels)
            self.assertIn(("f2", "apply"), choice.shortcuts)
            self.assertIn(("f3", "graph"), choice.shortcuts)
            self.assertIn(("f9", "actions"), choice.shortcuts)

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

    def test_workflow_action_returns_to_its_submenu_after_it_runs(self) -> None:
        bridge = PortableRuntimeBridge()
        result: list[EditorResult] = []
        output: list[str] = []
        options_state = WorkflowOptionsMenuState()

        def next_choice() -> PortableRuntimeEvent:
            while True:
                event = bridge.next_event(timeout=1)
                if event.kind is PortableRuntimeEventKind.CHOICE_REQUESTED:
                    return event

        with tempfile.TemporaryDirectory() as raw:
            configuration_path = Path(raw) / "devloop-plan.json"

            def edit() -> None:
                with portable_runtime_session(bridge):
                    result.append(
                        run_workflow_editor(
                            configuration_path,
                            read_line=lambda _prompt: self.fail(
                                "unexpected line input"
                            ),
                            read_command=lambda prompt: read_workflow_command(
                                prompt,
                                fallback=lambda _prompt: self.fail(
                                    "unexpected fallback input"
                                ),
                                actions=WORKFLOW_ACTIONS,
                                state=options_state,
                            ),
                            write=output.append,
                            terminal_width=100,
                            terminal_height=30,
                        )
                    )

            worker = threading.Thread(target=edit)
            worker.start()
            try:
                workflow = next_choice()
                bridge.respond(workflow.request_id, "actions")
                options = next_choice()
                bridge.respond(options.request_id, options.options[3][0])
                view = next_choice()
                advanced_key = next(
                    key
                    for key, label in view.options
                    if "technical details" in label
                )
                bridge.respond(view.request_id, advanced_key)
                after_action = next_choice()
                after_action_labels = tuple(
                    label for _key, label in after_action.options
                )

                if "B. Back to Options" in after_action_labels:
                    bridge.respond(after_action.request_id, after_action.options[-1][0])
                    options = next_choice()
                    bridge.respond(options.request_id, options.cancel_key or "")
                    workflow = next_choice()
                    bridge.respond(workflow.request_id, "cancel")
                else:
                    bridge.respond(after_action.request_id, "cancel")
                worker.join(timeout=1)
            finally:
                if worker.is_alive():
                    bridge.request_stop()
                    worker.join(timeout=1)

        self.assertIn("4. Show or hide technical details", after_action_labels)
        self.assertIn("B. Back to Options", after_action_labels)
        self.assertTrue(
            any("Technical details shown" in line for line in output),
            output,
        )
        self.assertEqual(result, [EditorResult.CANCELLED])


if __name__ == "__main__":
    unittest.main()
