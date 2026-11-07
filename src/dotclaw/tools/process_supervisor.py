"""单次命令的进程树所有权和幂等清理，不持有其他调用的资源。"""

from __future__ import annotations

import asyncio
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

from ..common.resource_errors import ResourceCleanupError


@dataclass
class _OwnedProcess:
    """本次调用独占的进程及平台句柄。"""

    pid: int
    process: asyncio.subprocess.Process | None = None
    job_handle: int | None = None
    process_handle: int | None = None
    stdout_handle: int | None = None
    closed: bool = False


class ProcessSupervisor:
    """命令退出前收尾整个归属范围；取消与超时共用同一清理出口。"""

    def __init__(self, *, terminate_grace_seconds: float = 1.0) -> None:
        """宽限只用于 POSIX 的 SIGTERM；Windows 仍使用作业对象可靠终止。"""
        if isinstance(terminate_grace_seconds, bool) or not isinstance(terminate_grace_seconds, (int, float)) or not math.isfinite(terminate_grace_seconds) or terminate_grace_seconds < 0:
            raise ValueError("终止宽限必须为非负有限数")
        self._terminate_grace_seconds = float(terminate_grace_seconds)

    async def run(self, command: str, timeout_seconds: float) -> str:
        if timeout_seconds <= 0:
            raise ValueError("命令超时必须为正数")
        deadline = time.monotonic() + timeout_seconds
        creation = asyncio.create_task(self._spawn_owned(command))
        owned: _OwnedProcess | None = None
        communication: asyncio.Task[bytes] | None = None
        try:
            try:
                owned = await asyncio.wait_for(asyncio.shield(creation), timeout_seconds)
            except (asyncio.CancelledError, TimeoutError):
                # 创建线程或系统调用仍可能成功；必须取得所有权对象后再退出。
                owned = await _finish_owned_task(creation, propagate_cancel=False)
                raise
            communication = asyncio.create_task(self._communicate(owned))
            output = await asyncio.wait_for(asyncio.shield(communication), max(0, deadline - time.monotonic()))
            return output.decode("utf-8", errors="replace") or "(命令无输出)"
        finally:
            if owned is not None:
                cleanup = asyncio.create_task(self._cleanup(owned, communication))
                await _finish_owned_task(cleanup)

    async def _spawn_owned(self, command: str) -> _OwnedProcess:
        """先建立 OS 所有权，之后才允许用户命令运行。"""
        if os.name == "nt":
            return await asyncio.to_thread(_spawn_windows, command)
        process = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        return _OwnedProcess(process.pid, process=process)

    async def _communicate(self, owned: _OwnedProcess) -> bytes:
        if owned.process is not None:
            output, _ = await owned.process.communicate()
            return output
        # Windows 匿名管道的同步读只占本次调用的工作线程；终止作业会关闭所有写端。
        import msvcrt
        descriptor = msvcrt.open_osfhandle(owned.stdout_handle, os.O_RDONLY | os.O_BINARY)
        owned.stdout_handle = None
        def read() -> bytes:
            with os.fdopen(descriptor, "rb") as pipe:
                output = pipe.read()
            import _winapi
            _winapi.WaitForSingleObject(owned.process_handle, _winapi.INFINITE)
            return output
        return await asyncio.to_thread(read)

    async def _terminate_owned(self, owned: _OwnedProcess) -> None:
        """即使主 shell 已退出也终止其所有后代。"""
        if owned.closed:
            return
        try:
            if owned.process is None:
                await asyncio.to_thread(_terminate_windows, owned)
            else:
                try:
                    os.killpg(owned.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + self._terminate_grace_seconds
                while await asyncio.to_thread(_process_group_active, owned.pid):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        try:
                            os.killpg(owned.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        break
                    await asyncio.sleep(min(0.02, remaining))
                await owned.process.wait()
                # 主 shell 的退出不等于后代退出；确认整个归属范围已没有可运行成员。
                deadline = time.monotonic() + 3
                while await asyncio.to_thread(_process_group_active, owned.pid):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("进程组仍有活动成员")
                    await asyncio.sleep(0.01)
        except Exception as error:
            raise ResourceCleanupError("process_tree", "process_termination_failed", "命令进程树终止失败") from error

    async def _close_owned(self, owned: _OwnedProcess) -> None:
        """关闭尚未转移给读取者的句柄；重复清理无副作用。"""
        if owned.closed:
            return
        try:
            if owned.process is None:
                import _winapi
                for name in ("stdout_handle", "process_handle", "job_handle"):
                    handle = getattr(owned, name)
                    if handle is not None:
                        _winapi.CloseHandle(handle)
                        setattr(owned, name, None)
            owned.closed = True
        except Exception as error:
            raise ResourceCleanupError("process_tree", "process_handle_close_failed", "命令资源句柄关闭失败") from error

    async def _cleanup(self, owned: _OwnedProcess, communication: asyncio.Task[bytes] | None) -> None:
        failure: ResourceCleanupError | None = None
        try:
            try:
                await self._terminate_owned(owned)
            except ResourceCleanupError as error:
                failure = error
                if owned.process is None and owned.job_handle is not None:
                    # 作业关闭时杀死成员，读取线程仍持有有效的主进程等待句柄。
                    import _winapi
                    _winapi.CloseHandle(owned.job_handle)
                    owned.job_handle = None
                elif communication is not None:
                    communication.cancel()
            if communication is not None:
                try:
                    await communication
                except asyncio.CancelledError:
                    if failure is None:
                        raise
        finally:
            await self._close_owned(owned)
        if failure is not None:
            raise failure


async def _finish_owned_task[T](task: asyncio.Task[T], *, propagate_cancel: bool = True) -> T:
    """保留创建或清理任务，反复取消不能遗失本次调用的资源。"""
    interrupted = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            interrupted = True
            continue
        if interrupted and propagate_cancel:
            raise asyncio.CancelledError()
        return result


def _process_group_active(group_id: int) -> bool:
    """Linux 将已退出的僵尸成员与仍在执行的成员区分；其他 POSIX 使用内核组查询。"""
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    if not sys.platform.startswith("linux"):
        return True
    unknown = False
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(os.path.join(entry.path, "stat"), encoding="utf-8") as stream:
                    fields = stream.read().rsplit(") ", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                unknown = True
                continue
            if int(fields[2]) == group_id and fields[0] not in ("Z", "X"):
                return True
    return unknown


if os.name == "nt":
    import ctypes
    import _winapi
    from ctypes import wintypes

    class _BasicLimitInformation(ctypes.Structure):
        """Windows 作业基础限制布局。"""
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        """Windows 作业 I/O 统计布局。"""
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount",
            "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimitInformation(ctypes.Structure):
        """Windows 作业扩展限制布局。"""
        _fields_ = [("BasicLimitInformation", _BasicLimitInformation), ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _BasicAccountingInformation(ctypes.Structure):
        """Windows 作业活动进程统计布局。"""
        _fields_ = [(name, ctypes.c_longlong) for name in ("TotalUserTime", "TotalKernelTime",
            "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")] + [(name, wintypes.DWORD)
            for name in ("TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel.SetInformationJobObject.restype = wintypes.BOOL
    _kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel.TerminateJobObject.restype = wintypes.BOOL
    _kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    _kernel.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel.ResumeThread.restype = wintypes.DWORD

    def _check_windows(success) -> None:
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())

    def _spawn_windows(command: str) -> _OwnedProcess:
        """暂停创建、加入杀死后代的作业，最后恢复主线程。"""
        job = _kernel.CreateJobObjectW(None, None)
        _check_windows(job)
        owned = _OwnedProcess(0, job_handle=job)
        thread = write_handle = input_handle = None
        try:
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # 关闭作业句柄时杀死所有成员，不允许脱离。
            _check_windows(_kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            read_handle, write_handle = _winapi.CreatePipe(None, 0)
            owned.stdout_handle = read_handle
            os.set_handle_inheritable(read_handle, False)
            os.set_handle_inheritable(write_handle, True)
            input_handle = _winapi.CreateFile("NUL", 0x80000000, 3, 0, 3, 0, 0)
            os.set_handle_inheritable(input_handle, True)
            startup = subprocess.STARTUPINFO()
            startup.dwFlags = subprocess.STARTF_USESTDHANDLES
            startup.hStdInput, startup.hStdOutput, startup.hStdError = input_handle, write_handle, write_handle
            # 显式继承名单防止并发命令继承另一调用的管道写端，造成相互阻塞。
            startup.lpAttributeList = {"handle_list": [input_handle, write_handle]}
            shell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
            process, thread, pid, _ = _winapi.CreateProcess(shell, f'"{shell}" /d /s /c "{command}"',
                None, None, True, 0x4 | 0x08000000, None, None, startup)
            owned.pid, owned.process_handle = pid, process
            _check_windows(_kernel.AssignProcessToJobObject(job, process))
            if _kernel.ResumeThread(thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            return owned
        except BaseException:
            try:
                if owned.process_handle is not None:
                    _winapi.TerminateProcess(owned.process_handle, 1)
                    _winapi.WaitForSingleObject(owned.process_handle, _winapi.INFINITE)
                    _winapi.CloseHandle(owned.process_handle)
                if owned.stdout_handle is not None:
                    _winapi.CloseHandle(owned.stdout_handle)
                _winapi.CloseHandle(job)
            except Exception as error:
                raise ResourceCleanupError("process_tree", "spawn_cleanup_failed", "创建阶段的命令资源清理失败") from error
            raise
        finally:
            for handle in (thread, write_handle, input_handle):
                if handle is not None:
                    _winapi.CloseHandle(handle)

    def _terminate_windows(owned: _OwnedProcess) -> None:
        _check_windows(_kernel.TerminateJobObject(owned.job_handle, 1))
        deadline = time.monotonic() + 3
        accounting = _BasicAccountingInformation()
        while True:
            _check_windows(_kernel.QueryInformationJobObject(owned.job_handle, 1,
                ctypes.byref(accounting), ctypes.sizeof(accounting), None))
            if accounting.ActiveProcesses == 0:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("作业成员未在清理期限内退出")
            time.sleep(0.01)
