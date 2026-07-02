"""Agent 角色抽象 —— Agent = AgentIdentity + AgentRuntime

v2 架构：
  Agent = Identity(声明式约束) + Runtime(纯执行设施)
  Agent 使用 Session，不持有 Session
  Agent 使用 Session，不持有 Session
  AgentRuntime 是纯执行引擎，Identity 值由 Agent 预解析传入

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
    from ..orchestration.task import Task
    from ..orchestration.messaging import AgentMessaging


# ============================================================================
# LLMResponse — 单次 LLM 调用的完整结果
# ============================================================================

@dataclass
class LLMResponse:
    """一次 LLM 调用的完整返回。

    AgentRuntime._invoke_llm() 返回此结构，用它判断下一步：
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
        messaging: "AgentMessaging | None" = None,
        memory_dream: object = None,
        mcp_task: object = None,
        resume_manager: object = None,
    ) -> None:
        """构造 Agent。

        Args:
            identity: Agent 声明式约束（id/name/allowed_tools/system_prompt/...）
            runtime: Agent 纯执行设施（llm/tool_executor/conversation_mgr/...）
            messaging: Agent 间通信层（可选，无则不启用 send）
            memory_dream: DeepDream 记忆蒸馏实例（可选）
            mcp_task: MCP 后台初始化 task（可选）
            resume_manager: 中断恢复管理器（可选）
        """
        self._identity: AgentIdentity = identity
        self._runtime: AgentRuntime = runtime
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
        """处理一条用户消息（纯执行，不持久化）。

        Args:
            session: 运行时上下文
            user_message: 用户输入文本

        Returns:
            AgentRun（一次原子调用的完整记录）
        """
        return await self._runtime.run(
            session=session,
            user_message=user_message,
            system_prompt=self._resolve_system_prompt(),
            tool_definitions=self._resolve_tool_definitions(),
            model=self._resolve_model(),
            max_loop_steps=self._identity.max_loop_steps,
        )

    async def execute(self, task: "Task") -> "Task":
        """主从式入口 —— 内部创建独立 Session，执行后返回填充完成的 Task。

        输入 task 的 description/context/constraints/input_artifacts 已由父 Agent 填充。
        执行完毕后填充 task 的 status/final_result/output_artifacts/error/sub_run_id。

        Args:
            task: 父 Agent 创建的任务描述

        Returns:
            携带执行结果的 task（就地修改后的同一对象）
        """
        import uuid as _uuid

        # 创建独立 Session（与父 Agent 完全隔离）
        child_session: "SessionType" = await self._runtime.session_mgr.create(
            title=f"sub-{self.agent_id}-{_uuid.uuid4().hex[:6]}",
            agent_id=self.agent_id,
            model=self._resolve_model(),
        )

        task.mark_working()

        # 组装 user_message
        user_message: str = self._build_task_message(task)

        try:
            sub_run: "AgentRun" = await self._runtime.run(
                session=child_session,
                user_message=user_message,
                system_prompt=self._resolve_system_prompt(),
                tool_definitions=self._resolve_tool_definitions(),
                model=self._resolve_model(),
                max_loop_steps=self._identity.max_loop_steps,
            )

            if sub_run.end_status == "completed":
                task.mark_completed(
                    final_result=sub_run.final_output or "",
                    sub_run_id=sub_run.run_id,
                )
            else:
                task.mark_failed(error=sub_run.error or sub_run.end_status)
                task.sub_run_id = sub_run.run_id
        except Exception as e:
            task.mark_failed(error=str(e))

        return task

    async def send(
        self,
        target_agent_id: str,
        description: str,
        context: str = "",
        constraints: str = "",
        parent_run_id: str = "",
    ) -> "Task":
        """发送 Task 给目标 Agent，阻塞等待执行完成。

        Agent 的一等通信能力。对标 A2A tasks/send。
        内部完成：路由 → 构造 Task → 创建子Agent → execute → 等待结果。

        Args:
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
        task: "Task" = _Task(
            task_id=_uuid.uuid4().hex[:12],
            requester=target_agent_id,
            description=description,
            context=context,
            constraints=constraints,
            parent_run_id=parent_run_id,
        )

        # 注册追踪
        self._messaging.send(task, identity)

        # 创建子 Agent（独立 Runtime + Session）
        child_runtime = self._runtime.derive()
        child_agent: Agent = Agent(identity=identity, runtime=child_runtime)

        # 执行并等待
        return await child_agent.execute(task)

    @staticmethod
    def _build_failed_task(target_agent_id: str, description: str, parent_run_id: str) -> "Task":
        """构造一个标记为 failed 的 Task（目标 Agent 不存在时使用）。"""
        import uuid as _uuid
        from ..orchestration.task import Task as _Task
        task = _Task(
            task_id=_uuid.uuid4().hex[:12],
            requester=target_agent_id,
            description=description,
            parent_run_id=parent_run_id,
        )
        task.mark_failed(error=f"Agent '{target_agent_id}' not found in registry")
        return task

    def _build_task_message(self, task: "Task") -> str:
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

    async def process(
        self,
        session: "SessionType",
        user_message: str,
        session_mgr: "object",
    ) -> "AgentRun":
        """处理一条用户消息（完整流程：执行 + 状态记录 + 持久化）。

        调度器职责：
        1. 创建 AgentState 并开始计时
        2. 调用 run() 执行
        3. 累加 AgentState
        4. 成功时追加 Conversation 记录并保存 Session

        Args:
            session: 运行时上下文
            user_message: 用户输入文本
            session_mgr: SessionManager 实例

        Returns:
            AgentRun（执行结果，含 end_status）
        """
        import uuid
        from ..session.agent_state import AgentState

        state: AgentState = AgentState(
            request_id=uuid.uuid4().hex[:8],
        )

        agent_run: "AgentRun" = await self.run(session, user_message)
        state.accumulate(agent_run)

        if agent_run.end_status == "completed":
            final: str | None = agent_run.final_output
            session.add_conversation(
                user_query=user_message,
                final_answer=final or "",
                agent_run_ids=state.agent_run_ids,
            )
            await session_mgr.save(session)  # type:ignore[union-attr]
            state.finish("completed")
        else:
            state.finish("failed", agent_run.error)

        return agent_run

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
