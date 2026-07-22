from __future__ import annotations

import io
import unittest

from devloop.portable_runtime import (
    PortableRuntimeBridge,
    PortableRuntimeEventKind,
    portable_plain_mode_session,
    portable_runtime_session,
)
from devloop.statusui import IssueDashboard, Stage, WaitingIndicator


class InteractiveBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class PortableStatusUiTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
