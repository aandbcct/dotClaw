"""按 Session 串行提交 RuntimeEngine 请求的协调器。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Protocol

from ..domain.cancellation import CancellationReceipt, CancellationDisposition, CancellationOrigin, CancellationRecord
from ..domain.facts import AgentRun, RunError, RunErrorCode
from ..domain.state import AgentRunState, Ended, RunOutcome
from .dto import RunRequest, RunResult
from .ports import LLMOutputPort
from .run_change_notifier import RunChangeNotifier


class RuntimeExecutionPort(Protocol):
    """协调器依赖的最小执行接口。"""

    async def prepare_run(self, request: RunRequest) -> str: ...
    async def _prepare_run(self, request: RunRequest) -> str: ...
    async def start_prepared(self, run_id: str, output_port: LLMOutputPort | None = None) -> RunResult: ...
    async def get_run_result(self, run_id: str) -> RunResult | None: ...
    async def list_runs(self) -> tuple[AgentRun, ...]: ...
    async def recover_cancellations(self) -> tuple[str, ...]: ...
    async def confirm_cancellation_cleanup(self, run_id: str, reason: str) -> RunResult: ...

    async def execute(self, request: RunRequest, output_port: LLMOutputPort | None = None) -> RunResult:
        """执行已获得 Session 租约的请求；output_port 为本提交的运行级输出端口。"""


class RuntimeControlPort(RuntimeExecutionPort, Protocol):
    """由协调器串行化的审批恢复和取消控制接口。"""

    async def get_approval_session_id(self, approval_id: str) -> str | None:
        """定位待处理审批所属的 Session。"""

    async def resolve_approval(self, approval_id: str, approved: bool, output_port: LLMOutputPort | None = None) -> RunResult:
        """恢复指定审批关联的运行；output_port 为本恢复的运行级输出端口。"""

    async def get_delegation_session_id(self, child_run_id: str) -> str | None:
        """定位 delegation 子运行所属的 Session（即父运行 Session）。"""

    async def resume_delegation(self, child_run_id: str, output_port: LLMOutputPort | None = None) -> RunResult:
        """恢复指定 delegation 子运行关联的父运行；output_port 为本恢复的运行级输出端口。"""

    async def get_run_session_id(self, run_id: str) -> str | None:
        """定位运行所属的 Session。"""

    async def request_cancel(self, run_id: str, reason: str, origin: CancellationOrigin = CancellationOrigin.USER, parent_request_id: str | None = None) -> CancellationReceipt:
        """持久化取消请求，不等待 Session 租约。"""

    async def complete_cancellation(self, run_id: str) -> RunResult:
        """持租约补齐取消收尾。"""

    async def refresh_cancellation_progress(self, run_id: str) -> CancellationRecord | None:
        """刷新清理进度，不追加运行事件。"""

    async def active_run(self, session_id: str) -> AgentRun | None:
        """读取持久化的当前 Session 占用。"""

    async def resume_run(self, run_id: str, output_port: LLMOutputPort | None = None) -> RunResult:
        """恢复未结束 Run；output_port 为本恢复的运行级输出端口。"""

    async def abandon_run(self, run_id: str) -> RunResult:
        """显式放弃未结束 Run。"""


class SessionBusyError(ValueError):
    """会话执行租约已被占用，编辑操作不得排队覆盖运行结果。"""


class RunNotRecoverableError(ValueError):
    """目标不是当前可恢复运行，拒绝严格放弃。"""


class SessionRunCoordinator:
    """同一 Session FIFO、不同 Session 可并行的轻量租约协调器。"""

    def __init__(self, engine: RuntimeControlPort, *, run_change_notifier: RunChangeNotifier) -> None:
        """绑定纯运行执行入口。"""
        self._engine: RuntimeControlPort = engine
        self._notifier: RunChangeNotifier = run_change_notifier
        self._driver_tasks: dict[str, asyncio.Task[RunResult]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard: asyncio.Lock = asyncio.Lock()
        self._cancellation_jobs: dict[str, asyncio.Task[None]] = {}
        self._accepting_submissions: bool = True

    @asynccontextmanager
    async def edit_session(self, session_id: str) -> AsyncIterator[None]:
        """在执行租约内编辑会话元数据，运行中立即拒绝而不排队等待。"""
        lock: asyncio.Lock = await self._get_lock(session_id)
        if lock.locked():
            raise SessionBusyError("会话正在运行，请结束后再修改")
        async with lock:
            yield

    async def is_session_executing(self, session_id: str) -> bool:
        """判断当前进程是否正持有 Session 执行租约。"""
        lock: asyncio.Lock = await self._get_lock(session_id)
        return lock.locked()

    async def submit(self, request: RunRequest, output_port: LLMOutputPort | None = None) -> RunResult:
        """按 Session 获取 FIFO 租约后执行请求；output_port 为本提交运行级端口。"""
        lock: asyncio.Lock = await self._get_lock(request.session_id)
        async with lock:
            self._assert_accepting_business()
            occupied: RunResult | None = await self._prepare_new_request(request.session_id)
            if occupied is not None:
                return occupied
            return await self._execute_owned(request, output_port)

    async def submit_prepared(
        self,
        session_id: str,
        request_factory: Callable[[], Awaitable[RunRequest]],
        output_port: LLMOutputPort | None = None,
    ) -> RunResult:
        """在同一 Session 租约内准备冻结请求并执行，避免压缩版本并发覆盖。"""
        lock: asyncio.Lock = await self._get_lock(session_id)
        async with lock:
            self._assert_accepting_business()
            occupied: RunResult | None = await self._prepare_new_request(session_id)
            if occupied is not None:
                return occupied
            request: RunRequest = await request_factory()
            if request.session_id != session_id:
                raise ValueError("冻结请求的 Session 与租约不一致")
            return await self._execute_owned(request, output_port)

    async def _execute_owned(self, request: RunRequest, output_port: LLMOutputPort | None) -> RunResult:
        self._assert_accepting_business()
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("运行提交缺少当前驱动任务")
        key = "submission:" + str(id(task))
        self._driver_tasks[key] = task
        try:
            result = await self._engine.execute(request, output_port)
            if result.control_status == "cancelling":
                self._schedule_cancellation_drain(result.run_id)
            return result
        finally:
            if self._driver_tasks.get(key) is task:
                self._driver_tasks.pop(key, None)

    async def prepare_run(self, request: RunRequest) -> str:
        self._assert_accepting_business()
        lock = await self._get_lock(request.session_id)
        async with lock:
            self._assert_accepting_business()
            occupied = await self._engine.active_run(request.session_id)
            if occupied is not None and occupied.run_id != request.run_id:
                raise SessionBusyError("准备的 Session 已存在其他活动运行")
            self._assert_accepting_business()
            return await self._engine.prepare_run(request)

    async def _prepare_child_run_locked(self, request: RunRequest) -> str:
        """委派已持父gate；仅准备新目标Session，绝不排队等待活跃子Session。"""
        if not request.parent_run_id:
            raise ValueError("子运行准备缺少父运行归属")
        self._assert_accepting_business()
        lock = await self._get_lock(request.session_id)
        if lock.locked():
            raise SessionBusyError("不能持父gate等待已有子Session运行租约")
        async with lock:
            self._assert_accepting_business()
            occupied = await self._engine.active_run(request.session_id)
            if occupied is not None:
                raise SessionBusyError("委派只能在空闲的新目标Session准备")
            self._assert_accepting_business()
            return await self._engine._prepare_run(request)

    def _assert_accepting_business(self) -> None:
        """获取租约后再次检查关闭屏障；控制收尾和事实观察不经过此检查。"""
        if not self._accepting_submissions:
            raise RuntimeError("宿主已停止接受新运行和业务恢复")

    def schedule_prepared(self, run_id: str) -> None:
        """同步登记唯一驱动；适配器不创建或持有任务。"""
        existing = self._driver_tasks.get(run_id)
        if existing is not None:
            return
        self._assert_accepting_business()
        task = asyncio.create_task(self._start_prepared_serial(run_id))
        self._driver_tasks[run_id] = task
        task.add_done_callback(lambda completed: self._driver_done(run_id, completed))

    def _driver_done(self, run_id: str, task: asyncio.Task[RunResult]) -> None:
        if self._driver_tasks.get(run_id) is task:
            self._driver_tasks.pop(run_id, None)
        if not task.cancelled() and task.exception() is not None:
            logging.getLogger(__name__).error("运行驱动失败 run_id=%s", run_id, exc_info=task.exception())
        self._notifier.notify(run_id)

    async def _start_prepared_serial(self, run_id: str) -> RunResult:
        session_id = await self._engine.get_run_session_id(run_id)
        if session_id is None:
            raise ValueError("准备运行不存在")
        lock = await self._get_lock(session_id)
        async with lock:
            self._assert_accepting_business()
            result = await self._engine.start_prepared(run_id)
            if result.control_status == "cancelling":
                self._schedule_cancellation_drain(run_id)
            return result

    async def start_prepared(self, run_id: str) -> RunResult:
        self.schedule_prepared(run_id)
        task = self._driver_tasks[run_id]
        return await asyncio.shield(task)

    async def wait_terminal(self, run_id: str) -> RunResult:
        """先登记通知版本再读结果；观察者取消不影响子驱动。"""
        while True:
            version = self._notifier.version(run_id)
            result = await self._engine.get_run_result(run_id)
            if result is None:
                raise ValueError("等待的运行不存在")
            if result.state.is_ended():
                return result
            await self._notifier.wait(run_id, version, 15)

    async def recover_cancellations(self) -> None:
        """先注册所有可观察的恢复作业；资源未知的运行不会阻塞宿主启动。"""
        for run_id in await self._engine.recover_cancellations():
            self._schedule_cancellation_drain(run_id)

    async def confirm_cancellation_cleanup(self, run_id: str, reason: str) -> RunResult:
        session_id = await self._engine.get_run_session_id(run_id)
        if session_id is None:
            raise ValueError("运行不存在")
        lock = await self._get_lock(session_id)
        if lock.locked():
            raise SessionBusyError("仍有运行驱动或收尾作业，不接受人工确认")
        async with lock:
            return await self._engine.confirm_cancellation_cleanup(run_id, reason)

    async def shutdown(self, grace_seconds: float = 5.0) -> None:
        """先拒绝新提交，再请求停止；超时只保留阻塞证据，不杀驱动或伪造终态。"""
        self._accepting_submissions = False
        for run in await self._engine.list_runs():
            if not run.state.is_ended():
                await self.cancel(run.run_id, "宿主关闭", origin=CancellationOrigin.HOST_SHUTDOWN)
        tasks = tuple(set(self._driver_tasks.values()) | set(self._cancellation_jobs.values()))
        current = asyncio.current_task()
        tasks = tuple(task for task in tasks if task is not current and not task.done())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
            if pending:
                for run in await self._engine.list_runs():
                    if not run.state.is_ended():
                        await self._engine.refresh_cancellation_progress(run.run_id)

    async def resolve_approval(self, approval_id: str, approved: bool, output_port: LLMOutputPort | None = None) -> RunResult:
        """在审批所属 Session 的租约内恢复原运行；output_port 为本恢复运行级端口。"""
        session_id: str | None = await self._engine.get_approval_session_id(approval_id)
        if session_id is None:
            self._assert_accepting_business()
            result = await self._engine.resolve_approval(approval_id, approved, output_port)
            if result.control_status == "cancelling":
                self._schedule_cancellation_drain(result.run_id)
            return result
        lock: asyncio.Lock = await self._get_lock(session_id)
        async with lock:
            self._assert_accepting_business()
            result = await self._engine.resolve_approval(approval_id, approved, output_port)
            if result.control_status == "cancelling":
                self._schedule_cancellation_drain(result.run_id)
            return result

    async def resume_delegation(self, child_run_id: str, output_port: LLMOutputPort | None = None) -> RunResult:
        """在 delegation 父运行所属 Session 的租约内恢复父运行；output_port 为本恢复运行级端口。"""
        session_id: str | None = await self._engine.get_delegation_session_id(child_run_id)
        if session_id is None:
            self._assert_accepting_business()
            result = await self._engine.resume_delegation(child_run_id, output_port)
            if result.control_status == "cancelling":
                self._schedule_cancellation_drain(result.run_id)
            return result
        lock: asyncio.Lock = await self._get_lock(session_id)
        async with lock:
            self._assert_accepting_business()
            result = await self._engine.resume_delegation(child_run_id, output_port)
            if result.control_status == "cancelling":
                self._schedule_cancellation_drain(result.run_id)
            return result

    async def cancel(self, run_id: str, reason: str, *, origin: CancellationOrigin = CancellationOrigin.USER,
                     parent_request_id: str | None = None) -> CancellationReceipt:
        """受理立即返回，协调器只持有补收尾作业。"""
        receipt = await self._engine.request_cancel(run_id, reason, origin, parent_request_id)
        if receipt.disposition in (CancellationDisposition.ACCEPTED, CancellationDisposition.ALREADY_REQUESTED):
            self._schedule_cancellation_drain(run_id)
        return receipt

    def _schedule_cancellation_drain(self, run_id: str) -> None:
        """去重登记取消收尾；作业会在驱动释放 Session 租约后继续。"""
        current = self._cancellation_jobs.get(run_id)
        if current is not None and not current.done():
            return
        job = asyncio.create_task(self._drain_cancelled_run(run_id))
        self._cancellation_jobs[run_id] = job
        job.add_done_callback(lambda completed: self._cancellation_job_done(run_id, completed))

    def _cancellation_job_done(self, run_id: str, job: asyncio.Task[None]) -> None:
        if self._cancellation_jobs.get(run_id) is job:
            self._cancellation_jobs.pop(run_id, None)
        if not job.cancelled() and job.exception() is not None:
            logging.getLogger(__name__).error("取消补收尾失败 run_id=%s", run_id, exc_info=job.exception())

    async def _drain_cancelled_run(self, run_id: str) -> None:
        """等待既有驱动释放租约，再重读并接管；超时仅更新阻塞证据。"""
        session_id = await self._engine.get_run_session_id(run_id)
        if session_id is None:
            return
        lock = await self._get_lock(session_id)
        waiter = asyncio.create_task(lock.acquire())
        acquired = False
        try:
            while not waiter.done():
                completed, _ = await asyncio.wait((waiter,), timeout=5)
                if not completed:
                    await self._engine.refresh_cancellation_progress(run_id)
            acquired = await waiter
            await self._engine.complete_cancellation(run_id)
        finally:
            if not waiter.done():
                waiter.cancel()
                try:
                    await waiter
                except asyncio.CancelledError:
                    pass
            elif not acquired and not waiter.cancelled() and waiter.exception() is None:
                acquired = waiter.result()
            if acquired:
                lock.release()

    async def resume_run(self, run_id: str, output_port: LLMOutputPort | None = None) -> RunResult:
        """在所属 Session 锁内恢复未结束 Run，避免与普通请求并发；output_port 为本恢复运行级端口。"""
        session_id: str | None = await self._engine.get_run_session_id(run_id)
        if session_id is None:
            self._assert_accepting_business()
            result = await self._engine.resume_run(run_id, output_port)
            if result.control_status == "cancelling":
                self._schedule_cancellation_drain(run_id)
            return result
        lock: asyncio.Lock = await self._get_lock(session_id)
        async with lock:
            self._assert_accepting_business()
            result = await self._engine.resume_run(run_id, output_port)
            if result.control_status == "cancelling":
                self._schedule_cancellation_drain(run_id)
            return result

    async def abandon_run(self, run_id: str, *, only_recoverable: bool = False) -> RunResult:
        """在所属 Session 锁内显式放弃未结束 Run。"""
        session_id: str | None = await self._engine.get_run_session_id(run_id)
        if session_id is None:
            if only_recoverable:
                raise RunNotRecoverableError("运行不存在或不可恢复")
            result = await self._engine.abandon_run(run_id)
            if result.control_status == "cancelling":
                self._schedule_cancellation_drain(run_id)
            return result
        lock: asyncio.Lock = await self._get_lock(session_id)
        if only_recoverable and lock.locked():
            raise SessionBusyError("会话正在运行，不能放弃")
        async with lock:
            if only_recoverable:
                active = await self._engine.active_run(session_id)
                if active is None or active.run_id != run_id or active.state.is_ended() or active.state.is_waiting_approval() or active.state.is_waiting_delegation():
                    raise RunNotRecoverableError("只能放弃当前可恢复运行")
            result = await self._engine.abandon_run(run_id)
            if result.control_status == "cancelling":
                self._schedule_cancellation_drain(run_id)
            return result

    async def _prepare_new_request(self, session_id: str) -> RunResult | None:
        """以持久化占用表保证跨进程 Session 串行；任意非终态 Run 拒绝普通请求。"""
        if not self._accepting_submissions:
            raise RuntimeError("宿主已停止接受新运行")
        active: AgentRun | None = await self._engine.active_run(session_id)
        if active is None:
            return None
        return RunResult(
            active.run_id,
            AgentRunState(mode=Ended(RunOutcome.FAILED)),
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
