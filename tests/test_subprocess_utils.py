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


if __name__ == "__main__":
    unittest.main()
