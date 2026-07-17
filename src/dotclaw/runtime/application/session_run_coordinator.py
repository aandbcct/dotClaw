"""按 Session 串行提交 RuntimeEngine 请求的协调器。"""

from __future__ import annotations

import asyncio

from ..domain.models import RunRequest, RunResult


class RuntimeExecutionPort:
    """协调器依赖的最小执行接口。"""

    async def execute(self, request: RunRequest) -> RunResult:
        """执行已获得 Session 租约的请求。"""


class SessionRunCoordinator:
    """同一 Session FIFO、不同 Session 可并行的轻量租约协调器。"""

    def __init__(self, engine: RuntimeExecutionPort) -> None:
        """绑定纯运行执行入口。"""
        self._engine: RuntimeExecutionPort = engine
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard: asyncio.Lock = asyncio.Lock()

    async def submit(self, request: RunRequest) -> RunResult:
        """按 Session 获取 FIFO 租约后执行请求。"""
        lock: asyncio.Lock = await self._get_lock(request.session_id)
        async with lock:
            return await self._engine.execute(request)

    async def _get_lock(self, session_id: str) -> asyncio.Lock:
        """为 Session 创建或返回唯一的异步租约锁。"""
        async with self._locks_guard:
            lock: asyncio.Lock | None = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock
