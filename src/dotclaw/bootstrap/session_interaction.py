"""按 Session 路由 Identity 的最小交互入口（阶段 1 + 门面移除修复）。

SessionInteractionService 是必要的最小 Session 入口，不是泛化的 ChatService。
它读取 ``session.agent_id``，在 ``AgentRegistry`` 中验证 Identity 后，以冻结的
``RunRequest`` 直接提交共享 ``SessionRunCoordinator``；不构造任何运行时 Agent 门面。
未知或空 Identity 必须返回明确错误，不能回退到默认 Identity
（开发计划阶段 1 + 总体设计 §4.2 + agent_remove_fix）。
"""

from __future__ import annotations

from pathlib import Path

from ..agent.identity import AgentIdentity
from ..orchestration.registry import AgentRegistry
from ..runtime.adapters.approval_repository import ApprovalRepositoryAdapter
from ..runtime.adapters.run_repository import RunRepositoryAdapter
from ..runtime.application.dto import RunRequest, RunResult
from ..runtime.application.ports import ContextPort, LLMOutputPort
from ..runtime.application.request_factory import create_run_request
from ..runtime.application.session_run_coordinator import SessionRunCoordinator
from ..runtime.domain.context import ContextOwner
from ..runtime.domain.facts import RunErrorCode
from ..session.session import Session, SessionManager


class UnknownIdentityError(ValueError):
    """Session 绑定的 Identity 未注册或为空。"""


class SessionDeletionRejected(RuntimeError):
    """存在非终态 Run 时拒绝删除 Session（开发计划阶段 5）。"""


class SessionInteractionService:
    """按 Session 路由 Identity 的最小交互入口。

    允许依赖：SessionManager、AgentRegistry、Coordinator。
    禁止依赖具体 LLM、工具、MCP 或 Channel 实现。
    """

    def __init__(
        self,
        session_manager: SessionManager,
        agent_registry: AgentRegistry,
        coordinator: SessionRunCoordinator,
        default_agent_id: str | None = None,
        run_repository: RunRepositoryAdapter | None = None,
        approval_repository: ApprovalRepositoryAdapter | None = None,
        context_port: ContextPort | None = None,
        preferred_model: str = "",
        chat_models: tuple[str, ...] = (),
    ) -> None:
        """绑定路由所需的会话管理与身份目录。

        ``run_repository`` / ``approval_repository`` / ``context_port`` 为应用级
        Session 删除协调流程所需；缺省为 None 时对应步骤被跳过（兼容既有仅做
        交互路由的构造场景）。
        """
        self._session_manager: SessionManager = session_manager
        self._agent_registry: AgentRegistry = agent_registry
        self._coordinator: SessionRunCoordinator = coordinator
        self._default_agent_id: str | None = default_agent_id
        self._run_repository: RunRepositoryAdapter | None = run_repository
        self._approval_repository: ApprovalRepositoryAdapter | None = approval_repository
        self._context_port: ContextPort | None = context_port
        self._preferred_model: str = preferred_model
        self._chat_models: tuple[str, ...] = chat_models or (
            (preferred_model,) if preferred_model else ()
        )

    # ── 创建 ──

    def _resolve_default_agent_id(self) -> str:
        """显式优先、默认兜底的 Identity 选择（总体设计 §5.1）。"""
        if self._default_agent_id and self._agent_registry.get(self._default_agent_id):
            return self._default_agent_id
        if self._agent_registry.get("default"):
            return "default"
        identities = self._agent_registry.list_all()
        if len(identities) == 1:
            return identities[0].agent_id
        raise UnknownIdentityError("无法确定默认 Identity，请显式指定 agent_id")

    async def create_session(self, agent_id: str | None = None, title: str = "新对话") -> Session:
        """创建绑定显式 Identity 的 Session；未指定时使用默认 Identity 并落盘。"""
        resolved: str = agent_id or self._resolve_default_agent_id()
        if self._agent_registry.get(resolved) is None:
            raise UnknownIdentityError(f"未知 Identity: {resolved}")
        if not self._preferred_model:
            raise ValueError("创建 Session 前必须配置 chat 用途的首选模型")
        return await self._session_manager.create(
            agent_id=resolved,
            title=title,
            model=self._preferred_model,
        )

    # ── 路由 ──

    def _require_identity(self, session: Session) -> AgentIdentity:
        """校验 ``session.agent_id`` 为已注册 Identity；未知或空必须明确报错。"""
        agent_id: str = session.agent_id
        if not agent_id or self._agent_registry.get(agent_id) is None:
            raise UnknownIdentityError(f"Session 绑定了未知或空 Identity: {agent_id}")
        return self._agent_registry.get(agent_id)  # type: ignore[return-value]

    def get_identity(self, session: Session) -> AgentIdentity:
        """只读校验入口：取得绑定 Identity 供 CLI Banner 与 ``/model`` 展示。

        不创建任何运行时 Agent 对象；提交路由仍以 ``session.agent_id`` 为权威。
        """
        return self._require_identity(session)

    @property
    def chat_models(self) -> tuple[str, ...]:
        """返回可供 Session 绑定的全部 chat active 模型。"""
        return self._chat_models

    async def switch_model(self, session: Session | str, model: str) -> Session:
        """校验并持久化 Session 的模型绑定，仅影响后续新 Run。"""
        if model not in self._chat_models:
            raise ValueError(f"模型不存在或未启用: {model}")
        if isinstance(session, str):
            loaded = await self._session_manager.load(session)
            if loaded is None:
                raise ValueError(f"Session 不存在: {session}")
            session = loaded
        self._require_identity(session)
        if session.model == model:
            return session
        session.model = model
        await self._session_manager.save(session)
        return session

    # ── 提交与控制 ──

    async def submit(self, session: Session | str, user_message: str, output_port: LLMOutputPort | None = None) -> RunResult:
        """提交一次普通消息，按 Session 路由到对应 Identity 并冻结 RunRequest。

        ``output_port`` 为本提交的运行级输出端口，透传至 Runtime 执行参数。
        冻结请求在 Coordinator 取得 Session 租约后重新加载最新 Session 并创建
        （见 ``submit_prepared``），避免入口持有的旧对象遗漏上一轮成功投影。
        """
        session_id: str = session if isinstance(session, str) else session.id
        initial_session: Session | None = await self._session_manager.load(session_id)
        if initial_session is None:
            raise UnknownIdentityError(f"Session 不存在: {session_id}")
        # 保留入口快速拒绝契约；租约内仍会再次校验，防止两次读取之间绑定发生变化。
        self._require_identity(initial_session)

        async def _make_request() -> RunRequest:
            latest_session: Session | None = await self._session_manager.load(session_id)
            if latest_session is None:
                raise UnknownIdentityError(f"Session 不存在: {session_id}")
            identity: AgentIdentity = self._require_identity(latest_session)
            if not latest_session.model:
                if not self._preferred_model:
                    raise ValueError("Session 缺少模型绑定，且未配置 chat 用途的首选模型")
                latest_session.model = self._preferred_model
                await self._session_manager.save(latest_session)
            return create_run_request(latest_session, identity.agent_id, user_message)

        return await self._coordinator.submit_prepared(session_id, _make_request, output_port)

    async def resolve_approval(self, approval_id: str, approved: bool, output_port: LLMOutputPort | None = None) -> RunResult:
        """提交审批决定并返回恢复后的结构化结果；透传运行级输出端口。"""
        return await self._coordinator.resolve_approval(approval_id, approved, output_port)

    async def resume_delegation(self, child_run_id: str, output_port: LLMOutputPort | None = None) -> RunResult:
        """提交 delegation 子运行结果回灌并恢复父运行，返回结构化结果；透传运行级输出端口。"""
        return await self._coordinator.resume_delegation(child_run_id, output_port)

    async def cancel(self, run_id: str, reason: str) -> None:
        """将取消请求交由运行协调器处理。"""
        await self._coordinator.cancel(run_id, reason)

    async def cancel_session(self, session_id: str, reason: str) -> str | None:
        """按 Session 取消唯一活动 Run，并返回其标识。

        GUI 只持有当前 Session，不应读取 Runtime 私有仓储或猜测 ``run_id``。
        当前 Session 串行契约要求最多存在一个活动 Run；若持久化事实违反该
        不变量则明确失败，避免取消错误目标。
        """
        if self._run_repository is None:
            raise RuntimeError("按 Session 取消需要运行仓储")
        active_runs = await self._run_repository.list_active_runs(session_id)
        if not active_runs:
            return None
        if len(active_runs) != 1:
            raise RuntimeError(f"Session {session_id} 存在多个活动 Run，拒绝猜测取消目标")
        run_id: str = active_runs[0].run_id
        await self.cancel(run_id, reason)
        return run_id

    async def resume_run(self, run_id: str, output_port: LLMOutputPort | None = None) -> RunResult:
        """恢复未结束 Run，并返回结构化结果；透传运行级输出端口。"""
        return await self._coordinator.resume_run(run_id, output_port)

    async def abandon_run(self, run_id: str) -> RunResult:
        """显式放弃未结束 Run，并返回结构化结果。"""
        return await self._coordinator.abandon_run(run_id)

    # ── 删除协调 ──

    async def delete_session(self, session_id: str) -> None:
        """应用级 Session 删除协调流程（开发计划阶段 5 + 总体设计 §5.2）。

        删除是应用级流程，不是只删单个 JSON 文件：

        1. 拒绝活动 Run：若 Session 仍存在非终态 Run，明确拒绝删除，要求先取消、
           重试或放弃，避免产生部分删除与孤儿数据；
        2. 清理该 Session 的待审批记录（审批仓库根与 Host 同源，布局由适配器独占）；
        3. 删除完整 Session 存储目录（session.json + agent_runs + 消息/事件/checkpoint）；
        4. 释放 Session 与 Run 范围的 Context 缓存。

        Agent 范围缓存按 Identity 共享、跨 Session 复用，不在此随单 Session 删除
        清空，否则会误伤其他绑定同一 Identity 的 Session（设计 §5.2 的“释放 Agent
        缓存”指身份级生命周期，而非单 Session 删除的副作用）。
        """
        session_dir: Path = self._session_manager.session_directory(session_id)
        if not session_dir.is_dir():
            return  # 幂等：目录已不存在则不操作、不抛错

        # 1) 拒绝活动 Run
        if self._run_repository is not None:
            active_runs = await self._run_repository.list_active_runs(session_id)
            if active_runs:
                run_ids: str = ", ".join(run.run_id for run in active_runs)
                raise SessionDeletionRejected(
                    f"Session {session_id} 仍有非终态 Run（{run_ids}），"
                    f"请先取消、重试或放弃这些运行后再删除"
                )

        # 收集本 Session 曾经拥有的 Run id，用于 Run 范围缓存释放（删目录前读取）。
        run_ids = self._run_ids_in(session_dir)

        # 2) 清理该 Session 的待审批记录
        if self._approval_repository is not None:
            await self._approval_repository.delete_by_session(session_id)

        # 3) 删除完整 Session 目录（session_manager 同时触发删除处理器释放 SESSION 缓存）
        await self._session_manager.delete(session_id)

        # 4) 释放 Context 缓存：SESSION（删除处理器已释放一次，此处显式兜底）
        #    + RUN 范围（按本 Session 拥有的 Run id，run 级缓存为会话私有）。
        if self._context_port is not None:
            for run_id in run_ids:
                await self._context_port.release_scope(ContextOwner.RUN, run_id)
            await self._context_port.release_scope(ContextOwner.SESSION, session_id)

    @staticmethod
    def _run_ids_in(session_dir: Path) -> tuple[str, ...]:
        """读取 Session 目录下 agent_runs 的各 Run 子目录名（即 run_id）。"""
        agent_runs: Path = session_dir / "agent_runs"
        if not agent_runs.is_dir():
            return ()
        return tuple(child.name for child in agent_runs.iterdir() if child.is_dir())


def format_run_result(result: RunResult) -> str:
    """将 Runtime 领域结果收敛为 Channel 可直接展示的文本（不含流式收尾决策）。

    CLI/Channel 的渲染函数基于本文本决定 Markdown 输出；运行时已通过输出端口
    流式呈现的内容不应再由本函数重复输出。
    """
    if result.final_message is not None:
        return result.final_message.content
    if result.state.is_waiting_approval():
        return f"运行等待审批：{result.run_id}"
    if result.state.is_abandoned():
        return f"运行已放弃：{result.run_id}"
    if result.error is not None:
        if result.error.code is RunErrorCode.SESSION_BUSY:
            return "当前会话仍有未完成运行，请先完成审批、重试或取消后再发送消息。"
        return f"执行失败：{result.error.message}"
    return f"执行未完成：{result.state.describe()}"
