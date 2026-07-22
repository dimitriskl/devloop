from __future__ import annotations

import io
import os
import unittest

from devloop.portable_runtime import (
    PortableRuntimeBridge,
    PortableRuntimeEventKind,
    portable_plain_mode_session,
    portable_runtime_session,
)
from devloop.portable_workflow import (
    DEVELOPMENT_STEP_ID,
    StepRuntimeState,
    StepRuntimeStatus,
    default_portable_component_catalog,
    default_portable_workflow,
)
from devloop.statusui import (
    IssueDashboard,
    Stage,
    WaitingIndicator,
    WorkflowProgress,
    project_workflow_progress,
)


class InteractiveBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class PortableStatusUiTests(unittest.TestCase):
    @staticmethod
    def delivery_progress() -> WorkflowProgress:
        return project_workflow_progress(
            default_portable_workflow(),
            default_portable_component_catalog(),
            (
                StepRuntimeState(
                    step_instance_id=DEVELOPMENT_STEP_ID,
                    issue_id="0001",
                    status=StepRuntimeStatus.RUNNING,
                    pass_number=1,
                ),
            ),
            (),
            issue_id="0001",
            issue_title="Readable progress",
            issue_position=1,
            issue_total=8,
        )

    def test_plain_mode_disables_terminal_animation_on_an_interactive_stream(self) -> None:
        output = InteractiveBuffer()
        with portable_plain_mode_session():
            dashboard = IssueDashboard(
                issue_number="0001",
                issue_title="Plain output",
                position=1,
                total=1,
                stream=output,
            )
            waiting = WaitingIndicator(stream=output)
            waiting.start()

        self.assertFalse(dashboard.enabled)
        self.assertIsNone(waiting._thread)
        self.assertNotIn("\r", output.getvalue())

    def test_issue_dashboard_replaces_the_application_detail_view(self) -> None:
        bridge = PortableRuntimeBridge()
        with portable_runtime_session(bridge):
            dashboard = IssueDashboard(
                issue_number="0001",
                issue_title="Persistent terminal shell",
                position=1,
                total=3,
                frame_seconds=60,
            )
            dashboard.begin_role(Stage.DEVELOPMENT, 1)
            event = bridge.next_event(timeout=1)
            dashboard.close()

        self.assertTrue(dashboard.enabled)
        self.assertIs(event.kind, PortableRuntimeEventKind.SCREEN_UPDATED)
        self.assertIn("CURRENT ISSUE", event.content)
        self.assertIn("Persistent terminal shell", event.content)

    def test_application_dashboard_uses_pane_width_and_one_pass_summary_row(self) -> None:
        bridge = PortableRuntimeBridge()
        bridge.set_content_size(140, 24)
        progress = self.delivery_progress()

        with portable_runtime_session(bridge):
            dashboard = IssueDashboard(
                issue_number="0001",
                issue_title="Readable progress",
                position=1,
                total=8,
                frame_seconds=60,
                terminal_size=lambda **_: os.terminal_size((41, 10)),
            )
            dashboard.show_workflow_progress(progress)
            event = bridge.next_event(timeout=1)
            dashboard.close()

        rules = [
            line
            for line in event.content.splitlines()
            if line and len(set(line)) == 1 and line[0] in {"─", "-"}
        ]
        summary = next(
            line
            for line in event.content.splitlines()
            if "DEVELOPMENT WORKING pass 1" in line
        )

        self.assertTrue(rules)
        self.assertTrue(all(len(rule) == 140 for rule in rules))
        self.assertIn("SECURITY WAITING pass 1", summary)
        self.assertIn("FINAL WAITING pass 1", summary)
        self.assertIn("QA WAITING pass 1", summary)

    def test_application_dashboard_keeps_vertical_steps_in_a_narrow_pane(self) -> None:
        bridge = PortableRuntimeBridge()
        bridge.set_content_size(60, 24)

        with portable_runtime_session(bridge):
            dashboard = IssueDashboard(
                issue_number="0001",
                issue_title="Readable progress",
                position=1,
                total=8,
                frame_seconds=60,
            )
            dashboard.show_workflow_progress(self.delivery_progress())
            event = bridge.next_event(timeout=1)
            dashboard.close()

        lines = event.content.splitlines()
        development_row = next(
            index for index, line in enumerate(lines) if "Development" in line
        )
        security_row = next(
            index for index, line in enumerate(lines) if "Security Review" in line
        )

        self.assertNotEqual(development_row, security_row)


if __name__ == "__main__":
    unittest.main()
