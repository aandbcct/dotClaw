"""Agent 角色抽象 —— Agent = AgentIdentity + AgentRuntime

v3 架构（Runtime 重构）：
  Agent = Identity（声明式约束） + Runtime（外部编排引擎）
  Agent 不持有 Runtime，Runtime 是顶层基础设施。
  Agent.process() 接收 Runtime 作为参数。

职责：
  - 持有 Identity
  - 提供 process(runtime, session, msg) 入口
  - 桥接 Identity 约束 + Runtime 能力
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .identity import AgentIdentity
from ..mcp.provider import MCPToolProvider

if TYPE_CHECKING:
    from ..config import Config
    from ..tools.base import ToolDefinition
    from ..session.session import Session as SessionType
    from ..session.agent_run import AgentRun
    from ..orchestration.task import Task
    from ..orchestration.dispatcher import AgentDispatcher
    from ..runtime import Runtime
    from ..runtime.application.session_run_coordinator import SessionRunCoordinator
    from ..runtime.domain.models import RunResult
    from ..tools.executor import ToolExecutor
    from ..skills.registry import SkillRegistry
    from ..memory.dream import DeepDream
    from .resume import ResumeManager
    from ..llm.base import ToolCall as LegacyToolCall


# ============================================================================
# LLMResponse — 单次 LLM 调用的完整结果
# ============================================================================

@dataclass
class LLMResponse:
    """一次 LLM 调用的完整返回。

    Runtime._invoke_llm() 返回此结构，用它判断下一步：
    有 tool_calls → 执行工具；没有 → 返回最终回复。
    """

    content: str = ""
    """LLM 返回的文本内容"""

    tool_calls: list[LegacyToolCall] = field(default_factory=list)
    """LLM 返回的工具调用列表（ToolCall 对象）"""

    finish_reason: str = "stop"
    """停止原因：stop / tool_calls / length / error"""

    input_tokens: int = 0
    """本次调用消耗的输入 token 数"""

    output_tokens: int = 0
    """本次调用产生的输出 token 数"""


# ============================================================================
# Agent — 角色管理类
# ============================================================================

class Agent:
    """Agent 角色抽象 —— Identity + Runtime 的桥梁。

    v3 设计原则：
    - 使用 Session，不持有 Session
    - 不持有 Runtime，Runtime 由外部传入
    - process(runtime, session, msg) 统一入口
    """

    def __init__(
        self,
        identity: AgentIdentity,
        runtime: Runtime | None = None,
        coordinator: SessionRunCoordinator | None = None,
        config: Config | None = None,
        tool_executor: ToolExecutor | None = None,
        mcp_provider: MCPToolProvider | None = None,
        skill_registry: SkillRegistry | None = None,
        dispatcher: AgentDispatcher | None = None,
        memory_dream: DeepDream | None = None,
        mcp_task: asyncio.Task[None] | None = None,
        resume_manager: ResumeManager | None = None,
    ) -> None:
        """构造 Agent。

        Args:
            identity: Agent 声明式约束（id/name/allowed_tools/system_prompt/...）
            runtime: Runtime 编排引擎（可选，process() 时也可传入）
            dispatcher: Agent 委托调度器（可选，无则不启用 delegation）
            memory_dream: DeepDream 记忆蒸馏实例（可选）
            mcp_task: MCP 后台初始化 task（可选）
            resume_manager: 中断恢复管理器（可选）
        """
        self._identity: AgentIdentity = identity
        self._runtime: Runtime | None = runtime
        self._coordinator: SessionRunCoordinator | None = coordinator
        self._config: Config | None = config
        self._tool_executor: ToolExecutor | None = tool_executor
        self._mcp_provider: MCPToolProvider | None = mcp_provider
        self._skill_registry: SkillRegistry | None = skill_registry
        self._last_run_result: RunResult | None = None
        self._dispatcher: AgentDispatcher | None = dispatcher
        self._memory_dream: DeepDream | None = memory_dream
        self._mcp_task: asyncio.Task[None] | None = mcp_task
        self._resume_manager: ResumeManager | None = resume_manager

    # ======================== 只读属性 ========================

    @property
    def identity(self) -> AgentIdentity:
        """Agent 声明式约束。"""
        return self._identity

    @property
    def runtime(self) -> Runtime | None:
        """Runtime 编排引擎（可能为 None，外部传入时使用）。"""
        return self._runtime

    @property
    def agent_id(self) -> str:
        """Agent 唯一标识。"""
        return self._identity.agent_id

    @property
    def agent_name(self) -> str:
        """Agent 显示名称。"""
        return self._identity.agent_name

    @property
    def config(self) -> Config | None:
        """全局配置（从 Runtime 获取）。"""
        if self._config is not None:
            return self._config
        if self._runtime is not None:
            return self._runtime.config
        return None

    @property
    def last_run_result(self) -> "RunResult | None":
        """最近一次普通消息执行结果，供 CLI 处理审批或取消。"""
        return self._last_run_result

    @property
    def tool_executor(self) -> "ToolExecutor | None":
        """仅供 CLI 展示工具清单的既有执行器。"""
        return self._tool_executor

    @property
    def mcp_provider(self) -> "MCPToolProvider | None":
        """仅供 CLI 展示 MCP 状态的提供者。"""
        return self._mcp_provider

    @property
    def skill_registry(self) -> "SkillRegistry | None":
        """仅供 CLI 展示 Skill 的注册表。"""
        return self._skill_registry

    @property
    def memory_dream(self) -> DeepDream | None:
        """DeepDream 记忆蒸馏实例。"""
        return self._memory_dream

    @property
    def dispatcher(self) -> "AgentDispatcher | None":
        """返回当前 Agent 装配的 delegation 门面。"""
        return self._dispatcher

    # ======================== 生命周期 ========================

    async def shutdown(self) -> None:
        """关闭 Agent 持有的所有运行时资源（MCP、后台 task 等）。"""
        if self._mcp_task is not None:
            task: asyncio.Task[None] = self._mcp_task
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self._runtime is not None:
            mcp = self._runtime.mcp_provider
            if isinstance(mcp, MCPToolProvider):
                await mcp.shutdown()
        elif self._mcp_provider is not None:
            await self._mcp_provider.shutdown()

    # ======================== 公开 API ========================

    async def process(
        self,
        session: SessionType,
        user_message: str,
    ) -> str:
        """处理一条用户消息。

        Args:
            session: 运行时上下文
            user_message: 用户输入文本

        Returns:
            Agent 最终回复文本
        """
        if self._coordinator is None:
            raise RuntimeError("Agent.process() 需要注入 SessionRunCoordinator")
        from ..runtime.application.request_factory import create_run_request
        request = create_run_request(session, self.agent_id, user_message)
        result = await self._coordinator.submit(request)
        self._last_run_result = result
        if result.final_message is not None:
            return result.final_message.content
        if result.error is not None:
            return f"执行失败：{result.error.message}"
        if result.status.value == "waiting_approval":
            return f"运行等待审批：{result.run_id}"
        return f"执行未完成：{result.status.value}"

    async def resolve_approval(self, approval_id: str, approved: bool) -> str:
        """提交有限审批决定，并返回恢复运行的标准结果文本。"""
        if self._coordinator is None:
            raise RuntimeError("Agent.resolve_approval() 需要注入 SessionRunCoordinator")
        result = await self._coordinator.resolve_approval(approval_id, approved)
        self._last_run_result = result
        if result.final_message is not None:
            return result.final_message.content
        if result.error is not None:
            return f"执行失败：{result.error.message}"
        if result.status.value == "waiting_approval":
            return f"运行等待审批：{result.run_id}"
        return f"执行未完成：{result.status.value}"

    async def cancel_run(self, run_id: str, reason: str) -> None:
        """将取消请求转交协调器，不读写运行内部状态。"""
        if self._coordinator is None:
            raise RuntimeError("Agent.cancel_run() 需要注入 SessionRunCoordinator")
        await self._coordinator.cancel(run_id, reason)

    async def execute_in_session(
        self,
        runtime: Runtime,
        session: SessionType,
        task: Task,
    ) -> str:
        """在 Dispatcher 已创建的独立 target Session 中执行 Task。

        TaskSpecification 只作为首条 user 消息传入，避免材料或任务正文污染
        target Identity 的 system prompt。
        """
        if runtime.journal is None:
            raise RuntimeError("Agent.execute_in_session() 需要 Journal")
        legacy_run = getattr(runtime, "run")
        answer, _ = await legacy_run(session, self, task.specification.render_user_message())
        return answer

    # ======================== 配置解析（桥接方法） ========================

    def _resolve_model(self, runtime: Runtime | None = None) -> str:
        """解析最终使用的模型名。

        优先级：Identity.model > Config.llm.default_model

        Args:
            runtime: Runtime 实例（用于获取 Config）

        Returns:
            模型名
        """
        rt: Runtime | None = runtime or self._runtime
        if rt is not None and rt.config is not None:
            return self._identity.resolve_model(rt.config.llm.default_model)
        return self._identity.model

    def _resolve_system_prompt(self, runtime: Runtime | None = None) -> str:
        """解析最终 system prompt。

        优先级：Identity.system_prompt_template > Config.agent.system_prompt
        Identity 内部完成 {agent_name} / {workspace} 占位符替换。

        Args:
            runtime: Runtime 实例（用于获取 Config）

        Returns:
            最终 system prompt 文本
        """
        resolved: str = self._identity.resolve_system_prompt()
        if resolved:
            return resolved
        rt: Runtime | None = runtime or self._runtime
        if rt is not None and rt.config is not None:
            return rt.config.agent.system_prompt
        return ""

    def _resolve_tool_definitions(self, runtime: Runtime | None = None) -> list[ToolDefinition]:
        """Identity 约束 Runtime: 根据 Identity.allowed_tools 过滤工具定义。

        如果 allowed_tools 为空，返回所有已注册工具。
        如果 tool_executor 未初始化，返回空列表。

        Args:
            runtime: Runtime 实例（用于获取 tool_executor）

        Returns:
            过滤后的工具定义列表
        """
        rt: Runtime | None = runtime or self._runtime
        if rt is None or rt.tool_executor is None:
            return []

        all_defs: list[ToolDefinition] = rt.tool_executor.get_definitions()

        allowed: list[str] = self._identity.allowed_tools
        if not allowed:
            return all_defs

        allowed_set: set[str] = set(allowed)
        return [d for d in all_defs if d.name in allowed_set]








