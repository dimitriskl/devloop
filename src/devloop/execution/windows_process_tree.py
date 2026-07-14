from __future__ import annotations

import ctypes
import os
import subprocess
import time
from ctypes import wintypes

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_CREATE_SUSPENDED = 0x00000004
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_RESUME_FAILED = 0xFFFFFFFF
_PROCESS_STOP_GRACE_SECONDS = 1.0


class WindowsProcessTreeError(RuntimeError):
    pass


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobObjectBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class WindowsProcessTree:
    """Own one Windows process tree through a kill-on-close Job Object."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsProcessTreeError("Windows process ownership is unavailable.")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise WindowsProcessTreeError("Unable to create the App Server process tree.")
        self._handle: int | None = int(handle)
        limits = _JobObjectExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self._handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            self.close()
            raise WindowsProcessTreeError(
                "Unable to configure the App Server process tree."
            )

    @staticmethod
    def creation_flags() -> int:
        return _CREATE_SUSPENDED

    def assign_and_resume(self, process: subprocess.Popen[bytes]) -> None:
        """Assign the suspended child before allowing its first instruction."""

        process_handle = getattr(process, "_handle", None)
        if process_handle is None or not self._kernel32.AssignProcessToJobObject(
            self._require_handle(),
            int(process_handle),
        ):
            raise WindowsProcessTreeError(
                "Unable to assign Codex App Server to its owned process tree."
            )
        thread_handle = self._open_process_thread(process.pid)
        try:
            if self._kernel32.ResumeThread(thread_handle) == _RESUME_FAILED:
                raise WindowsProcessTreeError(
                    "Unable to resume Codex App Server inside its owned process tree."
                )
        finally:
            self._kernel32.CloseHandle(thread_handle)

    def stop(self, process: subprocess.Popen[bytes]) -> bool:
        """Stop every owned descendant and verify that the Job Object is empty."""

        try:
            process.wait(timeout=_PROCESS_STOP_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if self._active_processes() == 0:
            self._reap(process)
            self.close()
            return True

        handle = self._require_handle()
        terminated = bool(self._kernel32.TerminateJobObject(handle, 1))
        empty = terminated and self._wait_until_empty()
        self._reap(process)
        self.close()
        return empty

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            self._kernel32.CloseHandle(handle)

    def _wait_until_empty(self) -> bool:
        deadline = time.monotonic() + _PROCESS_STOP_GRACE_SECONDS
        while True:
            if self._active_processes() == 0:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    def _active_processes(self) -> int:
        return int(self._accounting().ActiveProcesses)

    def _accounting(self) -> _JobObjectBasicAccountingInformation:
        accounting = _JobObjectBasicAccountingInformation()
        if not self._kernel32.QueryInformationJobObject(
            self._require_handle(),
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            raise WindowsProcessTreeError(
                "Unable to verify App Server process-tree cleanup."
            )
        return accounting

    @staticmethod
    def _reap(process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=_PROCESS_STOP_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _require_handle(self) -> int:
        if self._handle is None:
            raise WindowsProcessTreeError("App Server process-tree ownership is closed.")
        return self._handle

    def _open_process_thread(self, process_id: int) -> int:
        snapshot = self._kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if not snapshot or int(snapshot) == invalid_handle:
            raise WindowsProcessTreeError(
                "Unable to inspect the suspended App Server process."
            )
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            found = self._kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == process_id:
                    thread_handle = self._kernel32.OpenThread(
                        _THREAD_SUSPEND_RESUME,
                        False,
                        entry.th32ThreadID,
                    )
                    if thread_handle:
                        return int(thread_handle)
                found = self._kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self._kernel32.CloseHandle(snapshot)
        raise WindowsProcessTreeError(
            "Unable to find the suspended App Server primary thread."
        )

    def _configure_signatures(self) -> None:
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.CreateToolhelp32Snapshot.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self._kernel32.Thread32First.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ThreadEntry32),
        ]
        self._kernel32.Thread32First.restype = wintypes.BOOL
        self._kernel32.Thread32Next.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ThreadEntry32),
        ]
        self._kernel32.Thread32Next.restype = wintypes.BOOL
        self._kernel32.OpenThread.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._kernel32.OpenThread.restype = wintypes.HANDLE
        self._kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        self._kernel32.ResumeThread.restype = wintypes.DWORD
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL


def create_windows_process_tree() -> WindowsProcessTree:
    return WindowsProcessTree()
