"""按 Session 串行提交 RuntimeEngine 请求的协调器。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from ..domain.facts import AgentRun, RunError, RunErrorCode, RunStatus
from .dto import RunRequest, RunResult
from .ports import LLMOutputPort


class RuntimeExecutionPort(Protocol):
    """协调器依赖的最小执行接口。"""

    async def execute(self, request: RunRequest, output_port: LLMOutputPort | None = None) -> RunResult:
        """执行已获得 Session 租约的请求；output_port 为本提交的运行级输出端口。"""


class RuntimeControlPort(RuntimeExecutionPort, Protocol):
    """由协调器串行化的审批恢复和取消控制接口。"""

    async def get_approval_session_id(self, approval_id: str) -> str | None:
        """定位待处理审批所属的 Session。"""

    async def resolve_approval(self, approval_id: str, approved: bool, output_port: LLMOutputPort | None = None) -> RunResult:
        """恢复指定审批关联的运行；output_port 为本恢复的运行级输出端口。"""

    async def get_run_session_id(self, run_id: str) -> str | None:
        """定位运行所属的 Session。"""

    async def cancel(self, run_id: str, reason: str) -> None:
        """取消指定运行。"""

    async def recover_session(self, session_id: str) -> None:
        """恢复进程重启遗留的 Session 运行状态。"""

    async def active_run(self, session_id: str) -> AgentRun | None:
        """读取持久化的当前 Session 占用。"""

    async def retry_interrupted(self, run_id: str, output_port: LLMOutputPort | None = None) -> RunResult:
        """重试可恢复中断 Run；output_port 为本重试的运行级输出端口。"""

    async def abandon_interrupted(self, run_id: str) -> RunResult:
        """放弃可恢复中断 Run。"""


class SessionRunCoordinator:
    """同一 Session FIFO、不同 Session 可并行的轻量租约协调器。"""

    def __init__(self, engine: RuntimeControlPort) -> None:
        """绑定纯运行执行入口。"""
        self._engine: RuntimeControlPort = engine
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard: asyncio.Lock = asyncio.Lock()

    async def submit(self, request: RunRequest, output_port: LLMOutputPort | None = None) -> RunResult:
        """按 Session 获取 FIFO 租约后执行请求；output_port 为本提交运行级端口。"""
        lock: asyncio.Lock = await self._get_lock(request.session_id)
        async with lock:
            occupied: RunResult | None = await self._prepare_new_request(request.session_id)
            if occupied is not None:
                return occupied
            return await self._engine.execute(request, output_port)

    async def submit_prepared(
        self,
        session_id: str,
        request_factory: Callable[[], Awaitable[RunRequest]],
        output_port: LLMOutputPort | None = None,
    ) -> RunResult:
        """在同一 Session 租约内准备冻结请求并执行，避免压缩版本并发覆盖。"""
        lock: asyncio.Lock = await self._get_lock(session_id)
        async with lock:
            occupied: RunResult | None = await self._prepare_new_request(session_id)
            if occupied is not None:
                return occupied
            request: RunRequest = await request_factory()
            if request.session_id != session_id:
                raise ValueError("冻结请求的 Session 与租约不一致")
            return await self._engine.execute(request, output_port)

    async def resolve_approval(self, approval_id: str, approved: bool, output_port: LLMOutputPort | None = None) -> RunResult:
        """在审批所属 Session 的租约内恢复原运行；output_port 为本恢复运行级端口。"""
        session_id: str | None = await self._engine.get_approval_session_id(approval_id)
        if session_id is None:
            return await self._engine.resolve_approval(approval_id, approved, output_port)
        lock: asyncio.Lock = await self._get_lock(session_id)
        async with lock:
            return await self._engine.resolve_approval(approval_id, approved, output_port)

    async def cancel(self, run_id: str, reason: str) -> None:
        """立即发送取消信号，避免等待运行自身占用的 Session 租约。

        活动运行只会更新其局部取消令牌；等待审批的运行由 Engine 依据终态检查
        原子收口。因此取消不能像普通请求一样等待同一把 Session 锁，否则会与
        正在执行且等待外部结果的 Run 相互等待。
        """
        await self._engine.cancel(run_id, reason)

    async def retry_interrupted(self, run_id: str, output_port: LLMOutputPort | None = None) -> RunResult:
        """在所属 Session 锁内重试中断 Run，避免与普通请求并发；output_port 为本重试运行级端口。"""
        session_id: str | None = await self._engine.get_run_session_id(run_id)
        if session_id is None:
            return await self._engine.retry_interrupted(run_id, output_port)
        lock: asyncio.Lock = await self._get_lock(session_id)
        async with lock:
            return await self._engine.retry_interrupted(run_id, output_port)

    async def abandon_interrupted(self, run_id: str) -> RunResult:
        """在所属 Session 锁内放弃中断 Run。"""
        session_id: str | None = await self._engine.get_run_session_id(run_id)
        if session_id is None:
            return await self._engine.abandon_interrupted(run_id)
        lock: asyncio.Lock = await self._get_lock(session_id)
        async with lock:
            return await self._engine.abandon_interrupted(run_id)

    async def _prepare_new_request(self, session_id: str) -> RunResult | None:
        """以持久化占用表保证跨进程 Session 串行，并在新请求前放弃旧中断。"""
        await self._engine.recover_session(session_id)
        active: AgentRun | None = await self._engine.active_run(session_id)
        if active is None:
            return None
        if active.status is RunStatus.INTERRUPTED:
            await self._engine.abandon_interrupted(active.run_id)
            return None
        return RunResult(
            active.run_id,
            RunStatus.FAILED,
            error=RunError(RunErrorCode.SESSION_BUSY, "Session 存在未终态 Run，暂不接受普通请求"),
        )

    async def _get_lock(self, session_id: str) -> asyncio.Lock:
        """为 Session 创建或返回唯一的异步租约锁。"""
        async with self._locks_guard:
            lock: asyncio.Lock | None = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock
