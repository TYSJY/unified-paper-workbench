"""Windows Job Object process-tree runner for formal budgeted workloads."""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Sequence


CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
STILL_ACTIVE = 259
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
TREE_TERMINATION_EXIT_CODE = 124
POST_TERMINATION_VERIFY_MS = 10_000
POST_TERMINATION_VERIFY_SECONDS = 5.0


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
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


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
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


def _kernel32() -> Any:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _failure(code: str, *, started: bool, error_api: str) -> dict[str, Any]:
    return {
        "started": started,
        "returncode": None,
        "timed_out": False,
        "tree_guarded": False,
        "tree_terminated_verified": False,
        "error_code": code,
        "error_type": error_api,
    }


def _active_processes(kernel32: Any, job: wintypes.HANDLE) -> int | None:
    accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    returned = wintypes.DWORD()
    if not kernel32.QueryInformationJobObject(
        job,
        JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
        ctypes.byref(accounting),
        ctypes.sizeof(accounting),
        ctypes.byref(returned),
    ):
        return None
    return int(accounting.ActiveProcesses)


def _terminate_and_verify(kernel32: Any, job: wintypes.HANDLE) -> bool:
    active = _active_processes(kernel32, job)
    if active is None:
        return False
    if not active:
        return True
    if not kernel32.TerminateJobObject(job, TREE_TERMINATION_EXIT_CODE):
        return False
    deadline = time.monotonic() + POST_TERMINATION_VERIFY_SECONDS
    while time.monotonic() < deadline:
        active = _active_processes(kernel32, job)
        if active is None:
            return False
        if active == 0:
            return True
        time.sleep(0.02)
    return _active_processes(kernel32, job) == 0


def run_in_kill_on_close_job(
    argv: Sequence[str],
    *,
    cwd: str | Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Create suspended, bind to KILL_ON_JOB_CLOSE, resume, and verify cleanup."""

    if os.name != "nt":
        return _failure(
            "RUNTIME_BUDGET_PROCESS_TREE_GUARD_UNAVAILABLE",
            started=False,
            error_api="WindowsJobObjectUnavailable",
        )
    if not argv or not Path(argv[0]).is_absolute():
        return _failure(
            "RUNTIME_BUDGET_PROCESS_TREE_GUARD_UNAVAILABLE",
            started=False,
            error_api="AbsoluteExecutableRequired",
        )

    kernel32 = _kernel32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return _failure(
            "RUNTIME_BUDGET_PROCESS_TREE_GUARD_UNAVAILABLE",
            started=False,
            error_api="CreateJobObjectW",
        )

    process_info = PROCESS_INFORMATION()
    process_created = False
    process_assigned = False
    process_resumed = False
    try:
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            return _failure(
                "RUNTIME_BUDGET_PROCESS_TREE_GUARD_UNAVAILABLE",
                started=False,
                error_api="SetInformationJobObject",
            )

        startup = STARTUPINFOW()
        startup.cb = ctypes.sizeof(startup)
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(argv)))
        if not kernel32.CreateProcessW(
            str(argv[0]),
            command_line,
            None,
            None,
            False,
            CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
            None,
            str(Path(cwd)),
            ctypes.byref(startup),
            ctypes.byref(process_info),
        ):
            return _failure(
                "RUNTIME_BUDGET_LAUNCH_FAILED",
                started=False,
                error_api="CreateProcessW",
            )
        process_created = True

        if not kernel32.AssignProcessToJobObject(job, process_info.hProcess):
            kernel32.TerminateProcess(process_info.hProcess, TREE_TERMINATION_EXIT_CODE)
            kernel32.WaitForSingleObject(process_info.hProcess, POST_TERMINATION_VERIFY_MS)
            return _failure(
                "RUNTIME_BUDGET_PROCESS_TREE_GUARD_UNAVAILABLE",
                started=False,
                error_api="AssignProcessToJobObject",
            )
        process_assigned = True

        if kernel32.ResumeThread(process_info.hThread) == 0xFFFFFFFF:
            kernel32.TerminateJobObject(job, TREE_TERMINATION_EXIT_CODE)
            kernel32.WaitForSingleObject(process_info.hProcess, POST_TERMINATION_VERIFY_MS)
            return _failure(
                "RUNTIME_BUDGET_LAUNCH_FAILED",
                started=False,
                error_api="ResumeThread",
            )
        process_resumed = True

        timeout_ms = int(timeout_seconds) * 1000
        wait_result = kernel32.WaitForSingleObject(process_info.hProcess, timeout_ms)
        timed_out = wait_result == WAIT_TIMEOUT
        wait_failed = wait_result == WAIT_FAILED

        exit_code = wintypes.DWORD(STILL_ACTIVE)
        if not timed_out and not wait_failed:
            if not kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(exit_code)):
                wait_failed = True

        tree_verified = _terminate_and_verify(kernel32, job)
        if not tree_verified:
            return {
                "started": True,
                "returncode": None if timed_out or wait_failed else int(exit_code.value),
                "timed_out": timed_out,
                "tree_guarded": True,
                "tree_terminated_verified": False,
                "error_code": "RUNTIME_BUDGET_PROCESS_TREE_TERMINATION_UNVERIFIED",
                "error_type": "QueryInformationJobObject",
            }
        if wait_failed:
            return {
                "started": True,
                "returncode": None,
                "timed_out": False,
                "tree_guarded": True,
                "tree_terminated_verified": True,
                "error_code": "RUNTIME_BUDGET_LAUNCH_FAILED",
                "error_type": "WaitForSingleObject",
            }
        return {
            "started": True,
            "returncode": None if timed_out else int(exit_code.value),
            "timed_out": timed_out,
            "tree_guarded": True,
            "tree_terminated_verified": True,
            "error_code": "RUNTIME_BUDGET_LAUNCH_TIMEOUT" if timed_out else "",
            "error_type": "TimeoutExpired" if timed_out else "",
        }
    finally:
        if process_created:
            if process_assigned:
                # KILL_ON_JOB_CLOSE is the final backstop even if verification
                # above failed because the query API was unavailable.
                kernel32.TerminateJobObject(job, TREE_TERMINATION_EXIT_CODE)
            elif not process_resumed:
                kernel32.TerminateProcess(process_info.hProcess, TREE_TERMINATION_EXIT_CODE)
            kernel32.CloseHandle(process_info.hThread)
            kernel32.CloseHandle(process_info.hProcess)
        kernel32.CloseHandle(job)
