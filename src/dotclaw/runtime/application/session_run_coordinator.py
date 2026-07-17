"""按 Session 串行提交 RuntimeEngine 请求的协调器。"""

from __future__ import annotations

import asyncio
from typing import Protocol

from ..domain.models import RunRequest, RunResult


class RuntimeExecutionPort(Protocol):
    """协调器依赖的最小执行接口。"""

    async def execute(self, request: RunRequest) -> RunResult:
        """执行已获得 Session 租约的请求。"""


class RuntimeControlPort(RuntimeExecutionPort, Protocol):
    """由协调器串行化的审批恢复和取消控制接口。"""

    async def get_approval_session_id(self, approval_id: str) -> str | None:
        """定位待处理审批所属的 Session。"""

    async def resolve_approval(self, approval_id: str, approved: bool) -> RunResult:
        """恢复指定审批关联的运行。"""

    async def get_run_session_id(self, run_id: str) -> str | None:
        """定位运行所属的 Session。"""

    async def cancel(self, run_id: str, reason: str) -> None:
        """取消指定运行。"""


class SessionRunCoordinator:
    """同一 Session FIFO、不同 Session 可并行的轻量租约协调器。"""

    def __init__(self, engine: RuntimeControlPort) -> None:
        """绑定纯运行执行入口。"""
        self._engine: RuntimeControlPort = engine
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard: asyncio.Lock = asyncio.Lock()

    async def submit(self, request: RunRequest) -> RunResult:
        """按 Session 获取 FIFO 租约后执行请求。"""
        lock: asyncio.Lock = await self._get_lock(request.session_id)
        async with lock:
            return await self._engine.execute(request)

    async def resolve_approval(self, approval_id: str, approved: bool) -> RunResult:
        """在审批所属 Session 的租约内恢复原运行。"""
        session_id: str | None = await self._engine.get_approval_session_id(approval_id)
        if session_id is None:
            return await self._engine.resolve_approval(approval_id, approved)
        lock: asyncio.Lock = await self._get_lock(session_id)
        async with lock:
            return await self._engine.resolve_approval(approval_id, approved)

    async def cancel(self, run_id: str, reason: str) -> None:
        """立即发送取消信号，避免等待运行自身占用的 Session 租约。

        活动运行只会更新其局部取消令牌；等待审批的运行由 Engine 依据终态检查
        原子收口。因此取消不能像普通请求一样等待同一把 Session 锁，否则会与
        正在执行且等待外部结果的 Run 相互等待。
        """
        await self._engine.cancel(run_id, reason)

    async def _get_lock(self, session_id: str) -> asyncio.Lock:
        """为 Session 创建或返回唯一的异步租约锁。"""
        async with self._locks_guard:
            lock: asyncio.Lock | None = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock
