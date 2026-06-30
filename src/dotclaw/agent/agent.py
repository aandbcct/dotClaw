"""Agent 角色抽象 —— Agent = AgentIdentity + AgentRuntime

v2 架构：
  Agent = Identity(声明式约束) + Runtime(纯执行设施)
  Agent 使用 Session，不持有 Session
  AgentLoop 只依赖 AgentRuntime，Identity 值由 Agent.run() 预解析传入

职责：
  - 持有 Identity + Runtime
  - 提供 run(session, msg) 纯函数 API
  - 桥接 Identity 与 Runtime（如用 allowed_tools 过滤 tool_executor）
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .identity import AgentIdentity
from .runtime import AgentRuntime

if TYPE_CHECKING:
    from ..config import Config
    from ..tools.base import ToolDefinition
    from ..session.session import Session as SessionType
    from ..session.agent_run import AgentRun


# ============================================================================
# LLMResponse — 单次 LLM 调用的完整结果
# ============================================================================

@dataclass
class LLMResponse:
    """一次 LLM 调用的完整返回。

    AgentLoop._invoke_llm() 返回此结构，Loop 用它判断下一步：
    有 tool_calls → 执行工具；没有 → 返回最终回复。
    """

    content: str = ""
    """LLM 返回的文本内容"""

    tool_calls: list = field(default_factory=list)
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

    v2 设计原则：
    - 使用 Session，不持有 Session
    - run(session, msg) 纯函数 API
    - 桥接方法组合 Identity 约束 + Runtime 能力
    """

    def __init__(
        self,
        identity: AgentIdentity,
        runtime: AgentRuntime,
        memory_dream: object = None,
        mcp_task: object = None,
        resume_manager: object = None,
    ) -> None:
        """构造 Agent。

        Args:
            identity: Agent 声明式约束（id/name/allowed_tools/system_prompt/...）
            runtime: Agent 纯执行设施（llm/tool_executor/conversation_mgr/...）
            memory_dream: DeepDream 记忆蒸馏实例（可选）
            mcp_task: MCP 后台初始化 task（可选）
            resume_manager: 中断恢复管理器（可选）
        """
        self._identity: AgentIdentity = identity
        self._runtime: AgentRuntime = runtime
        self._memory_dream: object = memory_dream
        self._mcp_task: object = mcp_task
        self._resume_manager: object = resume_manager

    # ======================== 只读属性 ========================

    @property
    def identity(self) -> AgentIdentity:
        """Agent 声明式约束。"""
        return self._identity

    @property
    def runtime(self) -> AgentRuntime:
        """Agent 纯执行设施。"""
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
    def config(self) -> "Config | None":
        """全局配置（从 config.yaml 加载）。"""
        return self._runtime.config

    @property
    def memory_dream(self) -> object:
        """DeepDream 记忆蒸馏实例。"""
        return self._memory_dream

    # ======================== 生命周期 ========================

    async def shutdown(self) -> None:
        """关闭 Agent 持有的所有运行时资源（MCP、后台 task 等）。"""
        if self._mcp_task is not None:
            import asyncio as _asyncio
            task = self._mcp_task
            if hasattr(task, 'done') and not task.done():  # type: ignore[union-attr]
                task.cancel()  # type: ignore[union-attr]
                try:
                    await task  # type: ignore[union-attr]
                except (_asyncio.CancelledError, Exception):
                    pass
        mcp = self._runtime.mcp_provider
        if mcp is not None and hasattr(mcp, 'shutdown'):
            await mcp.shutdown()  # type: ignore[union-attr]

    # ======================== 公开 API ========================

    async def run(self, session: "SessionType", user_message: str) -> "AgentRun":
        """处理一条用户消息（v2 纯函数 API）。

        Agent 使用 Session，不持有 Session。
        调用方负责 Session 生命周期管理。

        内部流程：
        1. 从 Identity 预解析 system_prompt / tool_definitions / model / max_loop_steps
        2. 创建 AgentLoop(self._runtime)
        3. 调用 loop.run(session, msg, ...) → AgentRun

        Args:
            session: 运行时上下文（load from conversation）
            user_message: 用户输入文本

        Returns:
            AgentRun（包装 AgentResult + 执行后的 Session）
        """
        from .loop import AgentLoop
        from ..session.agent_run import AgentRun as AR

        loop: AgentLoop = AgentLoop(self._runtime)
        return await loop.run(
            session=session,
            user_message=user_message,
            system_prompt=self._resolve_system_prompt(),
            tool_definitions=self._resolve_tool_definitions(),
            model=self._resolve_model(),
            max_loop_steps=self._identity.max_loop_steps,
        )

    # ======================== 配置解析（桥接方法） ========================

    def _resolve_model(self) -> str:
        """解析最终使用的模型名。

        优先级：Identity.model > config.llm.default_model

        Returns:
            模型名
        """
        if self._runtime.config is not None:
            return self._identity.resolve_model(self._runtime.config.llm.default_model)
        return self._identity.model

    def _resolve_system_prompt(self) -> str:
        """解析最终 system prompt。

        优先级：Identity.system_prompt_template > config.agent.system_prompt
        Identity 内部完成 {agent_name} / {workspace} 占位符替换。

        Returns:
            最终 system prompt 文本
        """
        resolved: str = self._identity.resolve_system_prompt()
        if resolved:
            return resolved
        if self._runtime.config is not None:
            return self._runtime.config.agent.system_prompt
        return ""

    def _resolve_tool_definitions(self) -> list["ToolDefinition"]:
        """Identity 约束 Runtime: 根据 Identity.allowed_tools 过滤工具定义。

        如果 allowed_tools 为空，返回所有已注册工具。
        如果 tool_executor 未初始化，返回空列表。

        Returns:
            过滤后的工具定义列表
        """
        executor = self._runtime.tool_executor
        if executor is None:
            return []

        all_defs: list["ToolDefinition"] = executor.get_definitions()

        allowed: list[str] = self._identity.allowed_tools
        if not allowed:
            return all_defs

        allowed_set: set[str] = set(allowed)
        return [d for d in all_defs if d.name in allowed_set]
