from __future__ import annotations

import unittest
from unittest import mock

from devloop import subprocess_utils


class ActiveProcessTreeTests(unittest.TestCase):
    def test_registered_process_is_terminated_during_application_shutdown(
        self,
    ) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        subprocess_utils.register_process_tree(process)
        try:
            with mock.patch.object(
                subprocess_utils,
                "terminate_process",
            ) as terminate:
                subprocess_utils.terminate_active_process_trees()

            terminate.assert_called_once_with(process)
        finally:
            subprocess_utils.unregister_process_tree(process)

    @unittest.skipUnless(
        subprocess_utils.os.name == "nt",
        "requires the Windows process-tree fallback",
    )
    def test_failed_taskkill_falls_back_to_the_owned_process_handle(self) -> None:
        process = mock.Mock()
        process.pid = 123
        taskkill_result = mock.Mock(returncode=1)

        with mock.patch.object(
            subprocess_utils.subprocess,
            "run",
            return_value=taskkill_result,
        ) as taskkill:
            subprocess_utils._signal_process_tree(process, force=False)

        taskkill.assert_called_once()
        process.terminate.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
