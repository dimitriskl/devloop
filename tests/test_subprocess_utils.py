from __future__ import annotations

import ctypes
import subprocess
import sys
import tempfile
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devloop import subprocess_utils


class ActiveProcessTreeTests(unittest.TestCase):
    def test_windows_tracked_launch_registers_before_resuming(self) -> None:
        process = mock.Mock()
        process.pid = 123
        process._handle = 99
        events: list[str] = []
        ownership = subprocess_utils.WindowsJobOwnership(
            state=subprocess_utils.WindowsJobAssignmentState.ASSIGNED,
            handle=41,
        )

        def launch(
            command: object,
            popen_kwargs: object,
            *,
            creationflags: int,
        ) -> object:
            del command, popen_kwargs
            self.assertEqual(creationflags, 16)
            events.append("register")
            subprocess_utils._ACTIVE_WINDOWS_JOBS[process] = ownership
            return process

        def resume(candidate: object) -> bool:
            self.assertIs(candidate, process)
            events.append("resume")
            return True

        try:
            with (
                mock.patch.object(subprocess_utils.os, "name", "nt"),
                mock.patch.object(
                    subprocess_utils,
                    "_launch_suspended_windows_process",
                    side_effect=launch,
                ),
                mock.patch.object(
                    subprocess_utils,
                    "_resume_windows_process",
                    side_effect=resume,
                ),
            ):
                launched = subprocess_utils.launch_process_tree(
                    ["backend"],
                    creationflags=16,
                    stdin=subprocess.PIPE,
                )
        finally:
            subprocess_utils._ACTIVE_WINDOWS_JOBS.pop(process, None)

        self.assertIs(launched, process)
        self.assertEqual(events, ["register", "resume"])

    def test_windows_access_denied_retries_suspended_launch_with_breakaway(
        self,
    ) -> None:
        first = mock.Mock()
        second = mock.Mock()
        failed = subprocess_utils.WindowsJobOwnership(
            state=subprocess_utils.WindowsJobAssignmentState.ASSIGNMENT_FAILED,
            failure_stage="assign",
            error_code=subprocess_utils.WINDOWS_ERROR_ACCESS_DENIED,
        )
        assigned = subprocess_utils.WindowsJobOwnership(
            state=subprocess_utils.WindowsJobAssignmentState.ASSIGNED,
            handle=42,
        )
        launches: list[int] = []

        def launch(
            command: object,
            popen_kwargs: object,
            *,
            creationflags: int,
        ) -> object:
            del command, popen_kwargs
            process = first if not launches else second
            launches.append(creationflags)
            subprocess_utils._ACTIVE_WINDOWS_JOBS[process] = (
                failed if process is first else assigned
            )
            return process

        try:
            with (
                mock.patch.object(subprocess_utils.os, "name", "nt"),
                mock.patch.object(
                    subprocess_utils,
                    "_launch_suspended_windows_process",
                    side_effect=launch,
                ),
                mock.patch.object(
                    subprocess_utils,
                    "_discard_suspended_windows_process",
                ) as discard,
                mock.patch.object(
                    subprocess_utils,
                    "_resume_windows_process",
                    return_value=True,
                ),
            ):
                launched = subprocess_utils.launch_process_tree(["backend"])
        finally:
            subprocess_utils._ACTIVE_WINDOWS_JOBS.pop(first, None)
            subprocess_utils._ACTIVE_WINDOWS_JOBS.pop(second, None)

        self.assertIs(launched, second)
        self.assertEqual(
            launches,
            [0, subprocess_utils.WINDOWS_CREATE_BREAKAWAY_FROM_JOB],
        )
        discard.assert_called_once_with(first)

    def test_confirmed_stopped_process_is_not_registered_again(self) -> None:
        process = mock.Mock()
        subprocess_utils._mark_process_tree_stopped(process)

        with mock.patch.object(
            subprocess_utils,
            "register_process_tree",
        ) as register:
            result = subprocess_utils.terminate_process(process)

        self.assertIs(result.state, subprocess_utils.ProcessTreeState.STOPPED)
        register.assert_not_called()

    def test_windows_job_membership_query_reports_active_processes(self) -> None:
        def query_information(
            handle: object,
            information_class: int,
            information: object,
            information_size: int,
            return_length: object,
        ) -> bool:
            del handle, information_class, information_size, return_length
            information._obj.active_processes = 2  # type: ignore[attr-defined]
            return True

        kernel32 = SimpleNamespace(
            QueryInformationJobObject=mock.Mock(side_effect=query_information),
        )
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel32):
            active_processes = (
                subprocess_utils._query_windows_job_active_processes(41)
            )

        self.assertEqual(active_processes, 2)

    def test_active_windows_job_member_keeps_tree_running(self) -> None:
        process = mock.Mock()
        ownership = subprocess_utils.WindowsJobOwnership(
            state=subprocess_utils.WindowsJobAssignmentState.ASSIGNED,
            handle=41,
        )
        with subprocess_utils._ACTIVE_PROCESS_TREES_LOCK:
            subprocess_utils._ACTIVE_PROCESS_TREES.add(process)
            subprocess_utils._ACTIVE_WINDOWS_JOBS[process] = ownership
        try:
            with (
                mock.patch.object(subprocess_utils.os, "name", "nt"),
                mock.patch.object(
                    subprocess_utils,
                    "_query_windows_job_active_processes",
                    return_value=1,
                ),
                mock.patch.object(
                    subprocess_utils,
                    "_refresh_windows_process_tree",
                    return_value=set(),
                ),
            ):
                state = subprocess_utils._process_tree_state(process)

            self.assertIs(state, subprocess_utils.ProcessTreeState.RUNNING)
        finally:
            subprocess_utils.unregister_process_tree(process)

    def test_empty_authoritatively_queried_windows_job_is_stopped(self) -> None:
        process = mock.Mock()
        ownership = subprocess_utils.WindowsJobOwnership(
            state=subprocess_utils.WindowsJobAssignmentState.ASSIGNED,
            handle=42,
        )
        with subprocess_utils._ACTIVE_PROCESS_TREES_LOCK:
            subprocess_utils._ACTIVE_PROCESS_TREES.add(process)
            subprocess_utils._ACTIVE_WINDOWS_JOBS[process] = ownership
        try:
            with (
                mock.patch.object(subprocess_utils.os, "name", "nt"),
                mock.patch.object(
                    subprocess_utils,
                    "_query_windows_job_active_processes",
                    return_value=0,
                ) as query,
            ):
                released = subprocess_utils.release_process_tree_if_stopped(
                    process,
                )

            self.assertTrue(released)
            query.assert_called_once_with(42)
            self.assertNotIn(process, subprocess_utils._ACTIVE_PROCESS_TREES)
        finally:
            subprocess_utils.unregister_process_tree(process)

    def test_windows_job_query_failure_is_unknown_and_retains_ownership(self) -> None:
        process = mock.Mock()
        ownership = subprocess_utils.WindowsJobOwnership(
            state=subprocess_utils.WindowsJobAssignmentState.ASSIGNED,
            handle=43,
        )
        with subprocess_utils._ACTIVE_PROCESS_TREES_LOCK:
            subprocess_utils._ACTIVE_PROCESS_TREES.add(process)
            subprocess_utils._ACTIVE_WINDOWS_JOBS[process] = ownership
        try:
            with (
                mock.patch.object(subprocess_utils.os, "name", "nt"),
                mock.patch.object(
                    subprocess_utils,
                    "_query_windows_job_active_processes",
                    return_value=None,
                ),
                mock.patch.object(
                    subprocess_utils,
                    "_close_windows_handle",
                ) as close_handle,
            ):
                state = subprocess_utils._process_tree_state(process)
                released = subprocess_utils.release_process_tree_if_stopped(
                    process,
                )

            self.assertIs(state, subprocess_utils.ProcessTreeState.UNKNOWN)
            self.assertFalse(released)
            self.assertIn(process, subprocess_utils._ACTIVE_PROCESS_TREES)
            self.assertIn(process, subprocess_utils._ACTIVE_WINDOWS_JOBS)
            close_handle.assert_not_called()
        finally:
            subprocess_utils.unregister_process_tree(process)

    def test_windows_job_assignment_failure_is_unknown_and_retained(self) -> None:
        process = mock.Mock()
        process.pid = 123
        failed_ownership = subprocess_utils.WindowsJobOwnership(
            state=subprocess_utils.WindowsJobAssignmentState.ASSIGNMENT_FAILED,
        )
        with (
            mock.patch.object(subprocess_utils.os, "name", "nt"),
            mock.patch.object(
                subprocess_utils,
                "_process_identity",
                return_value=subprocess_utils.ProcessIdentity(123, 100),
            ),
            mock.patch.object(
                subprocess_utils,
                "_create_windows_kill_on_close_job",
                return_value=failed_ownership,
            ),
            mock.patch.object(
                subprocess_utils,
                "_refresh_windows_process_tree",
                return_value=set(),
            ),
        ):
            subprocess_utils.register_process_tree(process)
            try:
                state = subprocess_utils._process_tree_state(process)
                released = subprocess_utils.release_process_tree_if_stopped(
                    process,
                )

                self.assertIs(state, subprocess_utils.ProcessTreeState.UNKNOWN)
                self.assertFalse(released)
                self.assertEqual(
                    subprocess_utils._ACTIVE_WINDOWS_JOBS[process],
                    failed_ownership,
                )
            finally:
                subprocess_utils.unregister_process_tree(process)

    def test_windows_job_assignment_failure_returns_typed_failure(self) -> None:
        process = mock.Mock()
        process._handle = 99
        kernel32 = SimpleNamespace(
            CreateJobObjectW=mock.Mock(return_value=45),
            SetInformationJobObject=mock.Mock(return_value=True),
            AssignProcessToJobObject=mock.Mock(return_value=False),
            CloseHandle=mock.Mock(return_value=True),
        )
        with (
            mock.patch.object(ctypes, "WinDLL", return_value=kernel32),
            mock.patch.object(ctypes, "get_last_error", return_value=5),
        ):
            ownership = subprocess_utils._create_windows_kill_on_close_job(
                process,
            )

        self.assertIs(
            ownership.state,
            subprocess_utils.WindowsJobAssignmentState.ASSIGNMENT_FAILED,
        )
        self.assertIsNone(ownership.handle)
        self.assertEqual(ownership.failure_stage, "assign")
        self.assertEqual(ownership.error_code, 5)
        kernel32.CloseHandle.assert_called_once_with(45)

    def test_windows_job_termination_waits_for_zero_before_handle_release(
        self,
    ) -> None:
        active_counts = iter((2, 1, 0))

        def query_information(
            handle: object,
            information_class: int,
            information: object,
            information_size: int,
            return_length: object,
        ) -> bool:
            del handle, information_class, information_size, return_length
            information._obj.active_processes = next(  # type: ignore[attr-defined]
                active_counts,
            )
            return True

        kernel32 = SimpleNamespace(
            TerminateJobObject=mock.Mock(return_value=True),
            QueryInformationJobObject=mock.Mock(side_effect=query_information),
            CloseHandle=mock.Mock(return_value=True),
        )
        ownership = subprocess_utils.WindowsJobOwnership(
            state=subprocess_utils.WindowsJobAssignmentState.ASSIGNED,
            handle=44,
        )
        with (
            mock.patch.object(ctypes, "WinDLL", return_value=kernel32),
            mock.patch.object(subprocess_utils.time, "sleep"),
        ):
            state = subprocess_utils._terminate_windows_job(ownership)

        self.assertIs(state, subprocess_utils.ProcessTreeState.STOPPED)
        kernel32.TerminateJobObject.assert_called_once_with(44, 1)
        self.assertEqual(kernel32.QueryInformationJobObject.call_count, 3)
        kernel32.CloseHandle.assert_not_called()

    def test_unregistered_process_is_captured_and_its_root_is_signalled(self) -> None:
        class UnregisteredProcess:
            returncode: int | None = None

            def __init__(self) -> None:
                self.signals: list[str] = []

            def poll(self) -> int | None:
                return self.returncode

            def wait(self, timeout: float | None = None) -> int:
                if not self.signals:
                    raise subprocess.TimeoutExpired(["backend"], timeout)
                self.returncode = -15
                return self.returncode

            def terminate(self) -> None:
                self.signals.append("terminate")

            def kill(self) -> None:
                self.signals.append("kill")

        process = UnregisteredProcess()

        result = subprocess_utils.terminate_process(process)  # type: ignore[arg-type]

        self.assertEqual(process.signals, ["terminate"])
        self.assertIs(result.state, subprocess_utils.ProcessTreeState.STOPPED)
        self.assertNotIn(process, subprocess_utils._ACTIVE_PROCESS_TREES)

    def test_permanent_unknown_reaper_is_bounded_and_keeps_ownership(self) -> None:
        process = mock.Mock()
        subprocess_utils.register_process_tree(process)
        try:
            with (
                mock.patch.object(
                    subprocess_utils,
                    "PROCESS_TREE_REAPER_INTERVAL_SECONDS",
                    0.01,
                ),
                mock.patch.object(
                    subprocess_utils,
                    "PROCESS_TREE_REAPER_MAX_ATTEMPTS",
                    2,
                ),
                mock.patch.object(
                    subprocess_utils,
                    "_process_tree_state",
                    return_value=subprocess_utils.ProcessTreeState.UNKNOWN,
                ),
                mock.patch.object(
                    subprocess_utils,
                    "_signal_process_tree",
                    return_value=True,
                ),
            ):
                subprocess_utils._ensure_process_tree_reaper()
                reaper = subprocess_utils._PROCESS_TREE_REAPER_THREAD
                self.assertIsNotNone(reaper)
                assert reaper is not None
                reaper.join(timeout=1)

            self.assertFalse(reaper.is_alive())
            self.assertIn(process, subprocess_utils._ACTIVE_PROCESS_TREES)
        finally:
            subprocess_utils.unregister_process_tree(process)

    @unittest.skipUnless(
        subprocess_utils.os.name == "nt",
        "requires Windows process probing",
    )
    def test_windows_open_process_failure_is_unknown(self) -> None:
        kernel32 = SimpleNamespace(
            OpenProcess=mock.Mock(return_value=0),
            WaitForSingleObject=mock.Mock(),
            CloseHandle=mock.Mock(),
        )
        identity = subprocess_utils.ProcessIdentity(pid=123, creation_time=100)

        with (
            mock.patch.object(ctypes, "WinDLL", return_value=kernel32),
            mock.patch.object(ctypes, "get_last_error", return_value=5),
        ):
            state = subprocess_utils._windows_identity_state(identity)

        self.assertIs(state, subprocess_utils.ProcessTreeState.UNKNOWN)
        kernel32.WaitForSingleObject.assert_not_called()

    @unittest.skipUnless(
        subprocess_utils.os.name == "nt",
        "requires Windows process probing",
    )
    def test_windows_process_time_query_failure_is_unknown(self) -> None:
        kernel32 = SimpleNamespace(
            OpenProcess=mock.Mock(return_value=12),
            WaitForSingleObject=mock.Mock(),
            CloseHandle=mock.Mock(return_value=True),
        )
        identity = subprocess_utils.ProcessIdentity(pid=123, creation_time=100)

        with (
            mock.patch.object(ctypes, "WinDLL", return_value=kernel32),
            mock.patch.object(
                subprocess_utils,
                "_windows_process_times",
                return_value=None,
            ),
        ):
            state = subprocess_utils._windows_identity_state(identity)

        self.assertIs(state, subprocess_utils.ProcessTreeState.UNKNOWN)
        kernel32.WaitForSingleObject.assert_not_called()
        kernel32.CloseHandle.assert_called_once_with(12)

    def test_windows_unknown_probe_keeps_tree_ownership(self) -> None:
        process = mock.Mock()
        identity = subprocess_utils.ProcessIdentity(pid=123, creation_time=100)

        with (
            mock.patch.object(subprocess_utils.os, "name", "nt"),
            mock.patch.object(
                subprocess_utils,
                "_refresh_windows_process_tree",
                return_value={identity},
            ),
            mock.patch.object(
                subprocess_utils,
                "_windows_identity_state",
                return_value=subprocess_utils.ProcessTreeState.UNKNOWN,
            ),
        ):
            state = subprocess_utils._process_tree_state(process)
            alive = subprocess_utils._process_tree_is_alive(process)

        self.assertIs(state, subprocess_utils.ProcessTreeState.UNKNOWN)
        self.assertTrue(alive)

    def test_posix_retained_leader_recovers_late_group_without_overlap(self) -> None:
        process = mock.Mock()
        process.pid = 101
        setattr(process, subprocess_utils.PROCESS_TREE_ATTRIBUTE, 101)
        leader = subprocess_utils.ProcessIdentity(pid=101, creation_time=10)
        child = subprocess_utils.ProcessIdentity(pid=102, creation_time=11)
        group = subprocess_utils.PosixProcessGroupIdentity(
            group_id=101,
            leader=leader,
            leader_retained=True,
        )
        with subprocess_utils._ACTIVE_PROCESS_TREES_LOCK:
            subprocess_utils._ACTIVE_PROCESS_TREES.add(process)
            subprocess_utils._ACTIVE_PROCESS_TREE_IDENTITIES[process] = set()
            subprocess_utils._ACTIVE_POSIX_PROCESS_GROUPS[process] = group
        try:
            with (
                mock.patch.object(subprocess_utils.os, "name", "posix"),
                mock.patch.object(
                    subprocess_utils,
                    "_linux_process_group_identities",
                    return_value={leader, child},
                ),
                mock.patch.object(
                    subprocess_utils,
                    "_linux_process_state",
                    side_effect=lambda pid: "Z" if pid == leader.pid else "S",
                ),
                mock.patch.object(
                    subprocess_utils,
                    "_posix_process_identity",
                    return_value=leader,
                ),
                mock.patch.object(
                    subprocess_utils.os,
                    "killpg",
                    create=True,
                ) as kill_group,
            ):
                state = subprocess_utils._process_tree_state(process)
                signalled = subprocess_utils._signal_process_tree(
                    process,
                    force=False,
                )

            self.assertIs(state, subprocess_utils.ProcessTreeState.RUNNING)
            self.assertTrue(signalled)
            kill_group.assert_called_once_with(101, subprocess_utils.signal.SIGTERM)
            self.assertIn(
                child,
                subprocess_utils._ACTIVE_PROCESS_TREE_IDENTITIES[process],
            )
        finally:
            subprocess_utils.unregister_process_tree(process)

    def test_posix_reused_visible_group_leader_is_never_signalled(self) -> None:
        process = mock.Mock()
        process.pid = 101
        setattr(process, subprocess_utils.PROCESS_TREE_ATTRIBUTE, 101)
        leader = subprocess_utils.ProcessIdentity(pid=101, creation_time=10)
        replacement = subprocess_utils.ProcessIdentity(pid=101, creation_time=20)
        group = subprocess_utils.PosixProcessGroupIdentity(
            group_id=101,
            leader=leader,
            leader_retained=True,
        )
        with subprocess_utils._ACTIVE_PROCESS_TREES_LOCK:
            subprocess_utils._ACTIVE_PROCESS_TREES.add(process)
            subprocess_utils._ACTIVE_PROCESS_TREE_IDENTITIES[process] = {leader}
            subprocess_utils._ACTIVE_POSIX_PROCESS_GROUPS[process] = group
        try:
            with (
                mock.patch.object(subprocess_utils.os, "name", "posix"),
                mock.patch.object(
                    subprocess_utils,
                    "_linux_process_group_identities",
                    return_value={replacement},
                ),
                mock.patch.object(
                    subprocess_utils,
                    "_posix_process_identity",
                    return_value=replacement,
                ),
                mock.patch.object(
                    subprocess_utils.os,
                    "killpg",
                    create=True,
                ) as kill_group,
            ):
                state = subprocess_utils._process_tree_state(process)
                signalled = subprocess_utils._signal_process_tree(
                    process,
                    force=True,
                )

            self.assertIs(state, subprocess_utils.ProcessTreeState.STOPPED)
            self.assertFalse(signalled)
            kill_group.assert_not_called()
        finally:
            subprocess_utils.unregister_process_tree(process)

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

    def test_windows_identity_mismatch_skips_pid_tree_but_signals_popen_root(
        self,
    ) -> None:
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
            mock.patch.object(
                subprocess_utils,
                "_signal_process",
                return_value=True,
            ) as signal_root,
        ):
            confirmed = subprocess_utils._signal_process_tree(process, force=True)

        self.assertTrue(confirmed)
        terminate.assert_not_called()
        signal_root.assert_called_once_with(process, force=True)

    def test_registered_process_is_terminated_during_application_shutdown(
        self,
    ) -> None:
        process = subprocess_utils.launch_process_tree(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; time.sleep(60)",
            ],
        )
        try:
            results = subprocess_utils.terminate_active_process_trees()

            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].tree_terminated)
            self.assertIsNotNone(process.poll())
            self.assertNotIn(process, subprocess_utils._ACTIVE_PROCESS_TREES)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            subprocess_utils.release_process_tree_if_stopped(process)

    @unittest.skipUnless(
        subprocess_utils.os.name == "nt",
        "requires the Windows process-tree fallback",
    )
    def test_windows_cleanup_uses_popen_handle_not_pid_only_taskkill(self) -> None:
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
        process.terminate.assert_called_once_with()

    def test_windows_job_guard_is_retained_until_confirmed_cleanup(self) -> None:
        process = mock.Mock()
        process.pid = 123
        identity = subprocess_utils.ProcessIdentity(pid=123, creation_time=100)
        with (
            mock.patch.object(subprocess_utils.os, "name", "nt"),
            mock.patch.object(
                subprocess_utils,
                "_process_identity",
                return_value=identity,
            ),
            mock.patch.object(
                subprocess_utils,
                "_create_windows_kill_on_close_job",
                return_value=subprocess_utils.WindowsJobOwnership(
                    state=(
                        subprocess_utils.WindowsJobAssignmentState.ASSIGNED
                    ),
                    handle=41,
                ),
            ),
            mock.patch.object(
                subprocess_utils,
                "_refresh_windows_process_tree",
                return_value={identity},
            ),
            mock.patch.object(
                subprocess_utils,
                "_close_windows_handle",
            ) as close_handle,
        ):
            subprocess_utils.register_process_tree(process)
            self.assertEqual(
                subprocess_utils._ACTIVE_WINDOWS_JOBS[process],
                subprocess_utils.WindowsJobOwnership(
                    state=(
                        subprocess_utils.WindowsJobAssignmentState.ASSIGNED
                    ),
                    handle=41,
                ),
            )
            close_handle.assert_not_called()

            subprocess_utils.unregister_process_tree(process)

        close_handle.assert_called_once_with(41)
        self.assertNotIn(
            process,
            subprocess_utils._ACTIVE_WINDOWS_JOBS,
        )

    def test_windows_job_guard_closes_at_interpreter_fail_safe(self) -> None:
        process = mock.Mock()
        with subprocess_utils._ACTIVE_PROCESS_TREES_LOCK:
            subprocess_utils._ACTIVE_WINDOWS_JOBS[process] = (
                subprocess_utils.WindowsJobOwnership(
                    state=(
                        subprocess_utils.WindowsJobAssignmentState.ASSIGNED
                    ),
                    handle=42,
                )
            )
        with mock.patch.object(
            subprocess_utils,
            "_close_windows_handle",
        ) as close_handle:
            subprocess_utils._close_active_windows_job_handles()

        close_handle.assert_called_once_with(42)
        self.assertNotIn(
            process,
            subprocess_utils._ACTIVE_WINDOWS_JOBS,
        )

    @unittest.skipUnless(
        subprocess_utils.os.name == "nt",
        "requires Windows descendant retention",
    )
    def test_windows_job_stops_child_and_grandchild_after_root_exits(self) -> None:
        grandchild_source = (
            "import os,time;"
            "from pathlib import Path;"
            "Path('grandchild.pid').write_text("
            "str(os.getpid()),encoding='utf-8');"
            "time.sleep(60)"
        )
        child_source = (
            "import os,subprocess,sys,time;"
            "from pathlib import Path;"
            "Path('child.pid').write_text(str(os.getpid()),encoding='utf-8');"
            f"subprocess.Popen([sys.executable,'-u','-c',{grandchild_source!r}]);"
            "p=Path('grandchild.pid');"
            "\nwhile not p.is_file(): time.sleep(0.01)"
            "\ntime.sleep(60)"
        )
        root_source = (
            "import subprocess,sys,time;"
            "from pathlib import Path;"
            "go=Path('go');"
            "\nwhile not go.is_file(): time.sleep(0.01)"
            f"\nsubprocess.Popen([sys.executable,'-u','-c',{child_source!r}]);"
            "\npaths=(Path('child.pid'),Path('grandchild.pid'));"
            "\nwhile not all(path.is_file() for path in paths): time.sleep(0.01)"
        )

        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            process = subprocess_utils.launch_process_tree(
                [sys.executable, "-u", "-c", root_source],
                cwd=directory,
            )
            ownership = subprocess_utils._ACTIVE_WINDOWS_JOBS[process]
            self.assertIs(
                ownership.state,
                subprocess_utils.WindowsJobAssignmentState.ASSIGNED,
            )
            directory_path.joinpath("go").write_text("go", encoding="utf-8")
            child_pid = self._wait_for_pid(directory_path / "child.pid")
            grandchild_pid = self._wait_for_pid(
                directory_path / "grandchild.pid",
            )
            process.wait(timeout=5)

            try:
                self.assertTrue(self._windows_process_is_alive(child_pid))
                self.assertTrue(self._windows_process_is_alive(grandchild_pid))
                self.assertEqual(
                    subprocess_utils._query_windows_job_active_processes(
                        ownership.handle,
                    ),
                    2,
                )
                results = subprocess_utils.terminate_active_process_trees()
                self.assertTrue(all(result.tree_terminated for result in results))
                self.assertTrue(self._wait_for_windows_process_dead(child_pid))
                self.assertTrue(
                    self._wait_for_windows_process_dead(grandchild_pid),
                )
            finally:
                if self._windows_process_is_alive(child_pid):
                    subprocess_utils._terminate_windows_process_tree(child_pid)
                if self._windows_process_is_alive(grandchild_pid):
                    subprocess_utils._terminate_windows_process_tree(
                        grandchild_pid,
                    )
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
