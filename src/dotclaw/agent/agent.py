"""Runtime v2 的 Agent 门面。

模块只保存 Agent 身份与展示所需依赖，将普通执行、审批恢复和取消委托给
SessionRunCoordinator，禁止持有旧 Runtime、Session 级状态或 delegation runner。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..mcp.provider import MCPToolProvider
from .identity import AgentIdentity

if TYPE_CHECKING:
    from ..config import Config
    from ..memory.dream import DeepDream
    from ..runtime.application.session_run_coordinator import SessionRunCoordinator
    from ..runtime.domain.models import RunResult
    from ..session.session import Session
    from ..skills.registry import SkillRegistry
    from ..tools.executor import ToolExecutor


class Agent:
    """以声明式身份驱动 Runtime v2 协调器的轻量门面。"""

    def __init__(
        self,
        identity: AgentIdentity,
        coordinator: SessionRunCoordinator,
        config: Config,
        tool_executor: ToolExecutor | None = None,
        mcp_provider: MCPToolProvider | None = None,
        skill_registry: SkillRegistry | None = None,
        memory_dream: DeepDream | None = None,
        mcp_task: asyncio.Task[None] | None = None,
    ) -> None:
        """绑定执行协调器与仅供展示或关闭的基础设施依赖。"""
        self._identity: AgentIdentity = identity
        self._coordinator: SessionRunCoordinator = coordinator
        self._config: Config = config
        self._tool_executor: ToolExecutor | None = tool_executor
        self._mcp_provider: MCPToolProvider | None = mcp_provider
        self._skill_registry: SkillRegistry | None = skill_registry
        self._last_run_result: RunResult | None = None
        self._memory_dream: DeepDream | None = memory_dream
        self._mcp_task: asyncio.Task[None] | None = mcp_task

    @property
    def identity(self) -> AgentIdentity:
        """返回不可变的 Agent 身份约束。"""
        return self._identity

    @property
    def agent_id(self) -> str:
        """返回 Agent 唯一标识。"""
        return self._identity.agent_id

    @property
    def agent_name(self) -> str:
        """返回 Agent 显示名称。"""
        return self._identity.agent_name

    @property
    def config(self) -> Config:
        """返回当前 Agent 使用的全局配置。"""
        return self._config

    @property
    def last_run_result(self) -> RunResult | None:
        """返回最近一次运行结果，供 Channel 展示审批或错误信息。"""
        return self._last_run_result

    @property
    def tool_executor(self) -> ToolExecutor | None:
        """返回仅供 CLI 展示的工具执行器。"""
        return self._tool_executor

    @property
    def mcp_provider(self) -> MCPToolProvider | None:
        """返回仅供 CLI 展示和关闭的 MCP 提供者。"""
        return self._mcp_provider

    @property
    def skill_registry(self) -> SkillRegistry | None:
        """返回仅供 CLI 展示的技能目录。"""
        return self._skill_registry

    @property
    def memory_dream(self) -> DeepDream | None:
        """返回可选的记忆蒸馏服务。"""
        return self._memory_dream

    async def shutdown(self) -> None:
        """关闭 Agent 持有的后台 MCP 初始化任务和提供者。"""
        if self._mcp_task is not None and not self._mcp_task.done():
            self._mcp_task.cancel()
            try:
                await self._mcp_task
            except asyncio.CancelledError:
                pass
        if self._mcp_provider is not None:
            await self._mcp_provider.shutdown()

    async def process(self, session: Session, user_message: str) -> str:
        """提交普通用户消息，并将标准 RunResult 转换为 Channel 文本。"""
        from ..runtime.application.request_factory import create_run_request

        request = create_run_request(session, self.agent_id, user_message)
        result: RunResult = await self._coordinator.submit(request)
        self._last_run_result = result
        return _display_result(result)

    async def resolve_approval(self, approval_id: str, approved: bool) -> str:
        """提交审批决定并返回同一运行恢复后的展示文本。"""
        result: RunResult = await self._coordinator.resolve_approval(approval_id, approved)
        self._last_run_result = result
        return _display_result(result)

    async def cancel_run(self, run_id: str, reason: str) -> None:
        """将取消请求交由运行协调器处理。"""
        await self._coordinator.cancel(run_id, reason)


def _display_result(result: RunResult) -> str:
    """将 Runtime 领域结果收敛为 Channel 可直接展示的文本。"""
    if result.final_message is not None:
        return result.final_message.content
    if result.error is not None:
        return f"执行失败：{result.error.message}"
    if result.status.value == "waiting_approval":
        return f"运行等待审批：{result.run_id}"
    return f"执行未完成：{result.status.value}"
