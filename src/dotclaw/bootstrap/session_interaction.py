"""按 Session 路由 Identity 的最小交互入口（阶段 1）。

SessionInteractionService 是必要的最小 Session 入口，不是泛化的 ChatService。
它读取 ``session.agent_id``，在 ``AgentRegistry`` 中验证 Identity 后取得/创建轻量
Agent 门面，委托其提交共享 Coordinator。未知或空 Identity 必须返回明确错误，
不能回退到默认 Identity（开发计划阶段 1 + 总体设计 §4.2）。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..agent.agent import Agent, _display_result
from ..agent.identity import AgentIdentity
from ..orchestration.registry import AgentRegistry
from ..runtime.adapters.approval_repository import ApprovalRepositoryAdapter
from ..runtime.adapters.run_repository import RunRepositoryAdapter
from ..runtime.application.ports import ContextPort, TextStreamPort
from ..runtime.application.session_run_coordinator import SessionRunCoordinator
from ..runtime.domain.context import ContextOwner
from ..session.session import Session, SessionManager

if TYPE_CHECKING:
    from ..config.settings import Config


class UnknownIdentityError(ValueError):
    """Session 绑定的 Identity 未注册或为空。"""


class SessionDeletionRejected(RuntimeError):
    """存在非终态 Run 时拒绝删除 Session（开发计划阶段 5）。"""


class SessionInteractionService:
    """按 Session 路由 Identity 的最小交互入口。

    允许依赖：SessionManager、AgentRegistry、Coordinator/Agent 门面。
    禁止依赖具体 LLM、工具、MCP 或 Channel 实现。
    """

    def __init__(
        self,
        session_manager: SessionManager,
        agent_registry: AgentRegistry,
        coordinator: SessionRunCoordinator,
        config: Config | None = None,
        default_agent_id: str | None = None,
        run_repository: RunRepositoryAdapter | None = None,
        approval_repository: ApprovalRepositoryAdapter | None = None,
        context_port: ContextPort | None = None,
    ) -> None:
        """绑定路由所需的会话管理与身份目录。

        ``run_repository`` / ``approval_repository`` / ``context_port`` 为应用级
        Session 删除协调流程所需；缺省为 None 时对应步骤被跳过（兼容既有仅做
        交互路由的构造场景）。
        """
        self._session_manager: SessionManager = session_manager
        self._agent_registry: AgentRegistry = agent_registry
        self._coordinator: SessionRunCoordinator = coordinator
        self._config: Config | None = config
        self._default_agent_id: str | None = default_agent_id
        self._run_repository: RunRepositoryAdapter | None = run_repository
        self._approval_repository: ApprovalRepositoryAdapter | None = approval_repository
        self._context_port: ContextPort | None = context_port

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
        return await self._session_manager.create(agent_id=resolved, title=title)

    # ── 路由 ──

    def _require_identity(self, session: Session) -> AgentIdentity:
        """校验 ``session.agent_id`` 为已注册 Identity；未知或空必须明确报错。"""
        agent_id: str = session.agent_id
        if not agent_id or self._agent_registry.get(agent_id) is None:
            raise UnknownIdentityError(f"Session 绑定了未知或空 Identity: {agent_id}")
        return self._agent_registry.get(agent_id)  # type: ignore[return-value]

    async def get_agent(self, session: Session) -> Agent:
        """为 Session 取得绑定 Identity 的轻量 Agent 门面（路由权威）。

        调用方必须经由本服务取得 Agent，才能确保提交严格按 Session 绑定的
        Identity 路由，避免内部构造的 Agent 门面绕过 Session 权威。
        """
        identity: AgentIdentity = self._require_identity(session)
        return Agent(identity=identity, coordinator=self._coordinator, config=self._config)

    # ── 提交与控制 ──

    async def submit(self, session: Session | str, user_message: str, output_port: TextStreamPort | None = None) -> str:
        """提交一次普通消息，按 Session 路由到对应 Identity 的 Agent 门面。

        ``output_port`` 为本提交的运行级输出端口，透传至 Runtime 执行参数。
        """
        if isinstance(session, str):
            loaded: Session | None = await self._session_manager.load(session)
            if loaded is None:
                raise UnknownIdentityError(f"Session 不存在: {session}")
            session = loaded
        agent: Agent = await self.get_agent(session)
        return await agent.process(session, user_message, output_port)

    async def resolve_approval(self, approval_id: str, approved: bool, output_port: TextStreamPort | None = None) -> str:
        """提交审批决定并返回恢复后的展示文本；透传运行级输出端口。"""
        result = await self._coordinator.resolve_approval(approval_id, approved, output_port)
        return _display_result(result)

    async def cancel(self, run_id: str, reason: str) -> None:
        """将取消请求交由运行协调器处理。"""
        await self._coordinator.cancel(run_id, reason)

    async def retry_interrupted(self, run_id: str, output_port: TextStreamPort | None = None) -> str:
        """重试可恢复中断 Run，并返回展示结果；透传运行级输出端口。"""
        result = await self._coordinator.retry_interrupted(run_id, output_port)
        return _display_result(result)

    async def abandon_interrupted(self, run_id: str) -> str:
        """放弃可恢复中断 Run，并返回展示结果。"""
        result = await self._coordinator.abandon_interrupted(run_id)
        return _display_result(result)

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
