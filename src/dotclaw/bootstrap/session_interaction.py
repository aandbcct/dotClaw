"""按 Session 路由 Identity 的最小交互入口（阶段 1）。

SessionInteractionService 是必要的最小 Session 入口，不是泛化的 ChatService。
它读取 ``session.agent_id``，在 ``AgentRegistry`` 中验证 Identity 后取得/创建轻量
Agent 门面，委托其提交共享 Coordinator。未知或空 Identity 必须返回明确错误，
不能回退到默认 Identity（开发计划阶段 1 + 总体设计 §4.2）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..agent.agent import Agent, _display_result
from ..agent.identity import AgentIdentity
from ..orchestration.registry import AgentRegistry
from ..runtime.application.session_run_coordinator import SessionRunCoordinator
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
    ) -> None:
        """绑定路由所需的会话管理与身份目录。"""
        self._session_manager: SessionManager = session_manager
        self._agent_registry: AgentRegistry = agent_registry
        self._coordinator: SessionRunCoordinator = coordinator
        self._config: Config | None = config
        self._default_agent_id: str | None = default_agent_id

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

    async def submit(self, session: Session | str, user_message: str, output_port=None) -> str:
        """提交一次普通消息，按 Session 路由到对应 Identity 的 Agent 门面。

        ``output_port`` 当前仅接收，阶段 3 才线程到 Runtime 执行参数（运行级输出端口）。
        """
        if isinstance(session, str):
            loaded: Session | None = await self._session_manager.load(session)
            if loaded is None:
                raise UnknownIdentityError(f"Session 不存在: {session}")
            session = loaded
        agent: Agent = await self.get_agent(session)
        return await agent.process(session, user_message)

    async def resolve_approval(self, approval_id: str, approved: bool) -> str:
        """提交审批决定并返回恢复后的展示文本。"""
        result = await self._coordinator.resolve_approval(approval_id, approved)
        return _display_result(result)

    async def cancel(self, run_id: str, reason: str) -> None:
        """将取消请求交由运行协调器处理。"""
        await self._coordinator.cancel(run_id, reason)

    async def retry_interrupted(self, run_id: str) -> str:
        """重试可恢复中断 Run，并返回展示结果。"""
        result = await self._coordinator.retry_interrupted(run_id)
        return _display_result(result)

    async def abandon_interrupted(self, run_id: str) -> str:
        """放弃可恢复中断 Run，并返回展示结果。"""
        result = await self._coordinator.abandon_interrupted(run_id)
        return _display_result(result)
