"""唯一拥有能力子任务的取消控制服务，不依赖仓储。"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace

from ...common.resource_errors import ResourceCleanupError
from ..domain.cancellation import CancellationRequest
from .execution import CancellationToken
from .operation import RunOperationCancelled, RunOperationCleanupError, RunOperationHandle, RunOperationKind


class CancellationService:
    """持有取消投影、短临界区和能力任务，不写业务事实。"""

    def __init__(self) -> None:
        self._tokens: dict[str, CancellationToken] = {}
        self._requests: dict[str, CancellationRequest] = {}
        self._operations: dict[tuple[str, str], RunOperationHandle] = {}
        self._gates: dict[str, asyncio.Lock] = {}
        self._drivers: set[str] = set()

    def gate(self, run_id: str) -> asyncio.Lock:
        """返回唯一的非重入运行锁，生命周期随宿主。"""
        return self._gates.setdefault(run_id, asyncio.Lock())

    def register_driver(self, run_id: str, token: CancellationToken, request: CancellationRequest | None) -> None:
        """调用方已持 gate，并从仓储读出首次请求。"""
        if run_id in self._drivers:
            raise ValueError("同一运行不能存在两个驱动")
        if request is None:
            self.assert_resources_settled(run_id)
        self._drivers.add(run_id)
        self._tokens[run_id] = token
        if request is not None:
            self.signal(request)

    def unregister_driver(self, run_id: str) -> None:
        """调用方已持 gate；退出驱动不抹去取消和清理证据。"""
        self._drivers.discard(run_id)
        self._tokens.pop(run_id, None)

    def signal(self, request: CancellationRequest) -> None:
        """调用方已持 gate；只发送一次取消，避免重复打断 finally。"""
        request = self._requests.setdefault(request.run_id, request)
        token = self._tokens.get(request.run_id)
        if token is not None:
            token.request(request)
        for handle in self._operations.values():
            if handle.run_id == request.run_id and not handle.task.done() and not handle.cancel_sent:
                handle.cancel_sent = True
                handle.task.cancel()

    def pending_operations(self, run_id: str) -> tuple[RunOperationHandle, ...]:
        """返回句柄副本；任务退出但清理失败的证据仍然保留。"""
        return tuple(replace(handle) for handle in self._operations.values() if handle.run_id == run_id)

    def assert_resources_settled(self, run_id: str) -> None:
        """调用方已持 gate；残留资源证据阻止恢复业务或提交成功。"""
        for handle in self._operations.values():
            if handle.run_id == run_id:
                raise RunOperationCleanupError(handle.operation_id, handle.cleanup_error or "operation_pending", "运行仍有未收尾的能力资源")

    def clear_settled(self, run_id: str) -> None:
        """终态落盘后清理控制投影，不能隐藏残留能力。"""
        if self.pending_operations(run_id):
            raise ValueError("运行仍有能力任务或清理失败证据")
        self._requests.pop(run_id, None)
        self._tokens.pop(run_id, None)

    async def run_operation[T](self, run_id: str, kind: RunOperationKind, factory: Callable[[], Awaitable[T]], *, call_id: str | None = None) -> T:
        """子任务响应用户取消，驱动自身的取消保留系统中断语义。"""
        operation_id = uuid.uuid4().hex
        async with self.gate(run_id):
            request = self._requests.get(run_id)
            if request is not None:
                raise RunOperationCancelled(run_id, operation_id, request.request_id)
            self.assert_resources_settled(run_id)
            async def invoke() -> T:
                return await factory()
            task = asyncio.create_task(invoke())
            handle = RunOperationHandle(operation_id, run_id, kind, call_id, task, started_monotonic=time.monotonic())
            self._operations[(run_id, operation_id)] = handle
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            driver = asyncio.current_task()
            if driver is not None and driver.cancelling():
                if not task.done() and not handle.cancel_sent:
                    handle.cancel_sent = True
                    task.cancel()
                try:
                    await asyncio.shield(task)
                except ResourceCleanupError as error:
                    handle.cleanup_error = error.diagnostic_code
                except BaseException:
                    pass
                raise
            request = self._requests.get(run_id)
            if request is not None and task.cancelled():
                raise RunOperationCancelled(run_id, operation_id, request.request_id) from None
            raise RuntimeError("能力子任务无取消请求却自行取消") from None
        except ResourceCleanupError as error:
            handle.cleanup_error = error.diagnostic_code
            raise RunOperationCleanupError(operation_id, error.diagnostic_code, str(error)) from error
        finally:
            async with self.gate(run_id):
                if task.done() and handle.cleanup_error is None:
                    self._operations.pop((run_id, operation_id), None)

    def has_driver(self, run_id: str) -> bool:
        """查询唯一驱动登记，不以任务异常推断运行终态。"""
        return run_id in self._drivers

    def confirm_cleanup(self, run_id: str) -> None:
        """调用方已持 gate 且持久化人工确认；活任务或驱动存在时严格拒绝。"""
        if self.has_driver(run_id) or any(handle.run_id == run_id and not handle.task.done() for handle in self._operations.values()):
            raise ValueError("运行仍有驱动或活任务，不能人工确认清理")
        for key in tuple(self._operations):
            if key[0] == run_id:
                del self._operations[key]
