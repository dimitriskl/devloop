from __future__ import annotations

import ctypes
import subprocess
import sys
import tempfile
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

from devloop import subprocess_utils


class ActiveProcessTreeTests(unittest.TestCase):
    def test_windows_identity_mismatch_is_not_reported_alive(self) -> None:
        process = mock.Mock()
        process.pid = 123
        original = subprocess_utils.ProcessIdentity(pid=123, creation_time=100)
        replacement = subprocess_utils.ProcessIdentity(pid=123, creation_time=200)

        with (
            mock.patch.object(subprocess_utils.os, "name", "nt"),
            mock.patch.object(
                subprocess_utils,
                "_refresh_windows_process_tree",
                return_value={original},
            ),
            mock.patch.object(
                subprocess_utils,
                "_windows_process_identity",
                return_value=replacement,
            ),
        ):
            self.assertFalse(subprocess_utils._process_tree_is_alive(process))

    def test_windows_identity_mismatch_is_never_terminated(self) -> None:
        process = mock.Mock()
        process.pid = 123
        original = subprocess_utils.ProcessIdentity(pid=123, creation_time=100)
        replacement = subprocess_utils.ProcessIdentity(pid=123, creation_time=200)

        with (
            mock.patch.object(subprocess_utils.os, "name", "nt"),
            mock.patch.object(
                subprocess_utils,
                "_refresh_windows_process_tree",
                return_value={original},
            ),
            mock.patch.object(
                subprocess_utils,
                "_windows_process_identity",
                return_value=replacement,
            ),
            mock.patch.object(
                subprocess_utils,
                "_terminate_windows_process_tree",
            ) as terminate,
            mock.patch.object(subprocess_utils, "_signal_process") as signal_root,
        ):
            confirmed = subprocess_utils._signal_process_tree(process, force=True)

        self.assertFalse(confirmed)
        terminate.assert_not_called()
        signal_root.assert_not_called()

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
            ) as terminate, mock.patch.object(
                subprocess_utils,
                "_process_tree_is_alive",
                return_value=True,
            ):
                subprocess_utils.terminate_active_process_trees()

            terminate.assert_called_once_with(process)
        finally:
            subprocess_utils.unregister_process_tree(process)

    @unittest.skipUnless(
        subprocess_utils.os.name == "nt",
        "requires the Windows process-tree fallback",
    )
    def test_windows_cleanup_never_falls_back_to_pid_only_taskkill(self) -> None:
        process = mock.Mock()
        process.pid = 123
        with (
            mock.patch.object(
                subprocess_utils,
                "_terminate_windows_process_tree",
                return_value=False,
            ),
            mock.patch.object(
                subprocess_utils.subprocess,
                "run",
            ) as taskkill,
        ):
            subprocess_utils._signal_process_tree(process, force=False)

        taskkill.assert_not_called()
        process.terminate.assert_not_called()

    @unittest.skipUnless(
        subprocess_utils.os.name == "nt",
        "requires Windows descendant retention",
    )
    def test_registered_tree_stops_child_after_root_exits_first(self) -> None:
        child_source = (
            "import os,time;"
            "from pathlib import Path;"
            "Path('child.pid').write_text(str(os.getpid()),encoding='utf-8');"
            "time.sleep(60)"
        )
        root_source = (
            "import subprocess,sys,time;"
            "from pathlib import Path;"
            f"subprocess.Popen([sys.executable,'-u','-c',{child_source!r}]);"
            "p=Path('child.pid');"
            "\nwhile not p.is_file(): time.sleep(0.01)"
        )

        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", root_source],
                cwd=directory,
                **subprocess_utils.process_tree_creation_kwargs(),
            )
            subprocess_utils.register_process_tree(process)
            child_pid = self._wait_for_pid(Path(directory) / "child.pid")
            process.wait(timeout=5)

            try:
                self.assertTrue(self._windows_process_is_alive(child_pid))
                results = subprocess_utils.terminate_active_process_trees()
                self.assertTrue(all(result.tree_terminated for result in results))
                self.assertTrue(self._wait_for_windows_process_dead(child_pid))
            finally:
                if self._windows_process_is_alive(child_pid):
                    subprocess_utils._terminate_windows_process_tree(child_pid)
                subprocess_utils.unregister_process_tree(process)

    def _wait_for_pid(self, path: Path) -> int:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.is_file():
                return int(path.read_text(encoding="utf-8"))
            time.sleep(0.01)
        self.fail(f"Child process identity was not written: {path}")

    def _wait_for_windows_process_dead(self, process_id: int) -> bool:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not self._windows_process_is_alive(process_id):
                return True
            time.sleep(0.01)
        return False

    @staticmethod
    def _windows_process_is_alive(process_id: int) -> bool:
        open_process = ctypes.windll.kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        wait = ctypes.windll.kernel32.WaitForSingleObject
        wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait.restype = wintypes.DWORD
        close = ctypes.windll.kernel32.CloseHandle
        close.argtypes = (wintypes.HANDLE,)
        close.restype = wintypes.BOOL
        handle = open_process(
            subprocess_utils.WINDOWS_SYNCHRONIZE,
            False,
            process_id,
        )
        if not handle:
            return False
        try:
            return wait(handle, 0) == subprocess_utils.WINDOWS_WAIT_TIMEOUT
        finally:
            close(handle)


if __name__ == "__main__":
    unittest.main()
