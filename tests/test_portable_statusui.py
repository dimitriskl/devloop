from __future__ import annotations

import unittest

from devloop.portable_runtime import (
    PortableRuntimeBridge,
    PortableRuntimeEventKind,
    portable_runtime_session,
)
from devloop.statusui import IssueDashboard, Stage


class PortableStatusUiTests(unittest.TestCase):
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
