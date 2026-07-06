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

if TYPE_CHECKING:
    from ..config import Config
    from ..tools.base import ToolDefinition
    from ..session.session import Session as SessionType
    from ..session.agent_run import AgentRun
    from ..orchestration.task import Task
    from ..orchestration.messaging import AgentMessaging
    from ..runtime.runtime import Runtime


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

    v3 设计原则：
    - 使用 Session，不持有 Session
    - 不持有 Runtime，Runtime 由外部传入
    - process(runtime, session, msg) 统一入口
    """

    def __init__(
        self,
        identity: AgentIdentity,
        runtime: Runtime | None = None,
        messaging: AgentMessaging | None = None,
        memory_dream: object = None,
        mcp_task: object = None,
        resume_manager: object = None,
    ) -> None:
        """构造 Agent。

        Args:
            identity: Agent 声明式约束（id/name/allowed_tools/system_prompt/...）
            runtime: Runtime 编排引擎（可选，process() 时也可传入）
            messaging: Agent 间通信层（可选，无则不启用 send）
            memory_dream: DeepDream 记忆蒸馏实例（可选）
            mcp_task: MCP 后台初始化 task（可选）
            resume_manager: 中断恢复管理器（可选）
        """
        self._identity: AgentIdentity = identity
        self._runtime: Runtime | None = runtime
        self._messaging: AgentMessaging | None = messaging
        self._memory_dream: object = memory_dream
        self._mcp_task: object = mcp_task
        self._resume_manager: object = resume_manager

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
        if self._runtime is not None:
            return self._runtime.config
        return None

    @property
    def memory_dream(self) -> object:
        """DeepDream 记忆蒸馏实例。"""
        return self._memory_dream

    # ======================== 生命周期 ========================

    async def shutdown(self) -> None:
        """关闭 Agent 持有的所有运行时资源（MCP、后台 task 等）。"""
        if self._mcp_task is not None:
            task = self._mcp_task
            if hasattr(task, 'done') and not task.done():  # type: ignore[union-attr]
                task.cancel()  # type: ignore[union-attr]
                try:
                    await task  # type: ignore[union-attr]
                except (asyncio.CancelledError, Exception):
                    pass
        if self._runtime is not None:
            mcp = self._runtime.mcp_provider
            if mcp is not None and hasattr(mcp, 'shutdown'):
                await mcp.shutdown()  # type: ignore[union-attr]

    # ======================== 公开 API ========================

    async def process(
        self,
        runtime: Runtime,
        session: SessionType,
        user_message: str,
        session_mgr: object,
    ) -> str:
        """处理一条用户消息。

        Args:
            runtime: Runtime 执行引擎
            session: 运行时上下文
            user_message: 用户输入文本
            session_mgr: SessionManager 实例

        Returns:
            Agent 最终回复文本
        """
        if runtime.journal is None:
            raise RuntimeError(
                "Agent.process() requires Runtime with Journal injected"
            )

        final_answer: str = await runtime.run(session, self, user_message)

        # 持久化对话记录
        session.add_conversation(
            user_query=user_message,
            final_answer=final_answer,
            agent_run_ids=[],
        )
        await session_mgr.save(session)  # type:ignore[union-attr]

        return final_answer

    async def execute(self, runtime: Runtime, task: Task) -> Task:
        """主从式入口 —— 内部创建独立 Session，执行后返回填充完成的 Task。

        使用 TurnLoop 执行子 Agent 的任务。

        Args:
            runtime: Runtime 编排引擎
            task: 父 Agent 创建的任务描述

        Returns:
            携带执行结果的 task（就地修改后的同一对象）
        """
        import uuid as _uuid

        if runtime.journal is None:
            raise RuntimeError(
                "Agent.execute() requires Runtime with Journal injected"
            )

        # 创建独立 Session
        child_session: SessionType = await runtime.session_mgr.create(
            title=f"sub-{self.agent_id}-{_uuid.uuid4().hex[:6]}",
            agent_id=self.agent_id,
            model=self._resolve_model(runtime),
        )

        task.mark_working()

        user_message: str = self._build_task_message(task)
        final_answer: str = await runtime.run(child_session, self, user_message)

        task.mark_completed(
            final_result=final_answer,
            sub_run_id="",
        )

        return task

    async def send(
        self,
        runtime: Runtime,
        target_agent_id: str,
        description: str,
        context: str = "",
        constraints: str = "",
        parent_run_id: str = "",
    ) -> Task:
        """发送 Task 给目标 Agent，阻塞等待执行完成。

        Agent 的一等通信能力。对标 A2A tasks/send。
        内部完成：路由 → 构造 Task → 创建子 Agent → TurnLoop 执行 → 等待结果。

        Args:
            runtime: Runtime 编排引擎
            target_agent_id: 接收方 agent_id
            description: 任务描述
            context: 父 Agent 传入的上下文摘要
            constraints: 约束条件
            parent_run_id: 父 AgentRun.run_id

        Returns:
            携带执行结果的 Task

        Raises:
            RuntimeError: 如果 Agent 未配置 AgentMessaging
        """
        import uuid as _uuid
        from ..orchestration.task import Task as _Task

        if self._messaging is None:
            raise RuntimeError(
                f"Agent '{self.agent_id}' has no messaging configured. "
                "Use factory.build_agent() to create a fully wired agent."
            )

        # 路由
        identity = self._messaging.route(target_agent_id)
        if identity is None:
            return self._build_failed_task(target_agent_id, description, parent_run_id)

        # 构造 Task
        task: Task = _Task(
            task_id=_uuid.uuid4().hex[:12],
            requester=target_agent_id,
            description=description,
            context=context,
            constraints=constraints,
            parent_run_id=parent_run_id,
        )

        # 注册追踪
        self._messaging.send(task, identity)

        # 创建子 Agent + 子 TurnLoop（共享 Runtime 底层能力）
        child_runtime = runtime.derive()
        child_agent: Agent = Agent(identity=identity, runtime=child_runtime)

        # 使用 execute 确保 TurnLoop 被正确创建
        return await child_agent.execute(child_runtime, task)

    @staticmethod
    def _build_failed_task(target_agent_id: str, description: str, parent_run_id: str) -> Task:
        """构造一个标记为 failed 的 Task（目标 Agent 不存在时使用）。"""
        import uuid as _uuid
        from ..orchestration.task import Task as _Task
        task: Task = _Task(
            task_id=_uuid.uuid4().hex[:12],
            requester=target_agent_id,
            description=description,
            parent_run_id=parent_run_id,
        )
        task.mark_failed(error=f"Agent '{target_agent_id}' not found in registry")
        return task

    def _build_task_message(self, task: Task) -> str:
        """将 Task 输入侧字段组装为子 Agent 的 user_message。

        description / context / constraints / input_artifacts 合并为一条消息。
        """
        parts: list[str] = [task.description]
        if task.context:
            parts.append(f"\n[上游上下文]\n{task.context}")
        if task.constraints:
            parts.append(f"\n[约束]\n{task.constraints}")
        if task.input_artifacts:
            refs: str = "\n".join(
                f"  - {a.name}: {a.uri or a.content[:200]}"
                for a in task.input_artifacts
            )
            parts.append(f"\n[输入产物]\n{refs}")
        return "\n\n".join(parts)

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
