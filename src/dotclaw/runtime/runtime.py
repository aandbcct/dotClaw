"""Runtime —— Agent 执行引擎。

Runtime 是 dotClaw 的基础设施层，提供原子执行能力：
- LLM 调用（_invoke_llm）
- 工具执行（_execute_tools / _execute_single_tool）
- Handoff 多 Agent 流转
- 上下文消息构建

v2 变更：
- 控制循环移至 TurnLoop
- Runtime 退化为纯能力提供者（原子方法集）
- 注入 Journal 用于观测，注入 StateStore 用于持久化

内部逻辑（原子操作封装为方法）：
1. _invoke_llm() — 调用 LLM，返回 LLMResponse
2. _execute_tools() — 并发执行工具调用
3. _execute_single_tool() — 执行单个工具
4. _build_messages() — 构建 LLM 输入消息
5. _build_slot_context() — 构建 SlotContext 供 Assembler
6. _handle_handoff() — 处理 Agent 间流转
"""

from __future__ import annotations

import asyncio
import json as _json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..llm.base import Message

if TYPE_CHECKING:
    from ..llm.proxy import LLMProxy
    from ..tools.executor import ToolExecutor
    from ..tools.base import ToolDefinition
    from ..memory.manager import MemoryManager
    from ..skills.registry import SkillRegistry
    from ..agent.agent import Agent as AgentType, LLMResponse
    from ..agent.slotContext import ContextAssembler, SlotContext
    from ..channel.base import Channel
    from ..session.session import SessionManager
    from ..session.agent_run import AgentRun, AgentRunManager
    from ..orchestration.registry import AgentRegistry
    from ..journal.journal import Journal
    from ..config import Config
    from .state_store import StateStore


# ============================================================================
# Runtime —— 执行引擎
# ============================================================================

class Runtime:
    """Agent 执行引擎。

    Runtime 是共享服务，聚合 LLM/Tool/Config 等能力引用。
    TurnLoop 是控制循环的持有者，Runtime 提供原子执行方法。

    Args:
        llm: LLM 代理
        tool_executor: 工具执行器
        assembler: 上下文组装器
        agent_registry: Agent 注册表
        session_mgr: Session 管理器
        run_mgr: AgentRun 持久化管理器
        journal: 统一观测模块
        state_store: 状态持久化
        channel: 通信通道
        memory_mgr: 记忆管理器
        skill_registry: 技能注册表
        mcp_provider: MCP 连接器
        config: 全局配置
    """

    def __init__(
        self,
        llm: LLMProxy,
        tool_executor: ToolExecutor | None,
        assembler: ContextAssembler | None,
        agent_registry: AgentRegistry,
        session_mgr: SessionManager,
        run_mgr: AgentRunManager,
        journal: Journal | None = None,
        state_store: StateStore | None = None,
        channel: Channel | None = None,
        memory_mgr: MemoryManager | None = None,
        skill_registry: SkillRegistry | None = None,
        mcp_provider: object = None,
        config: Config | None = None,
    ) -> None:
        self.llm: LLMProxy = llm
        self.tool_executor: ToolExecutor | None = tool_executor
        self.assembler: ContextAssembler | None = assembler
        self.agent_registry: AgentRegistry = agent_registry
        self.session_mgr: SessionManager = session_mgr
        self.run_mgr: AgentRunManager = run_mgr
        self.journal: Journal | None = journal
        self.state_store: StateStore | None = state_store
        self.channel: Channel | None = channel
        self.memory_mgr: MemoryManager | None = memory_mgr
        self.skill_registry: SkillRegistry | None = skill_registry
        self.mcp_provider: object = mcp_provider
        self.config: Config | None = config

    # ======================== 派生（多 Agent 隔离） ========================

    def derive(self, *, channel: Channel | None = None) -> Runtime:
        """派生 Runtime。共享 llm/skills/registry/journal/state_store，隔离 channel。

        Args:
            channel: 覆盖的 channel

        Returns:
            新的 Runtime 实例
        """
        if channel is None:
            from ..channel.null import NullChannel
            channel = NullChannel()

        return Runtime(
            llm=self.llm,
            tool_executor=self.tool_executor,
            assembler=self.assembler,
            agent_registry=self.agent_registry,
            session_mgr=self.session_mgr,
            run_mgr=self.run_mgr,
            journal=self.journal,
            state_store=self.state_store,
            channel=channel,
            memory_mgr=self.memory_mgr,
            skill_registry=self.skill_registry,
            mcp_provider=self.mcp_provider,
            config=self.config,
        )

    # ======================== 原子操作：SlotContext 构建 ========================

    def _build_slot_context(
        self,
        thread_id: str,
        user_message: str,
        system_prompt: str,
        tool_definitions: list[ToolDefinition],
    ) -> SlotContext:
        """构建 SlotContext 供 Assembler 使用。

        Args:
            thread_id: Session ID
            user_message: 用户输入
            system_prompt: system prompt 模板
            tool_definitions: 工具定义列表

        Returns:
            SlotContext 实例
        """
        from ..agent.slotContext import SlotContext as SCtx

        project_root: Path = _find_project_root()
        max_ctx_tokens: int = 8000
        if self.config is not None:
            max_ctx_tokens = self.config.agent.max_context_tokens

        return SCtx(
            query=user_message,
            request_id=_new_hex_id(),
            session_id=thread_id,
            project_root=project_root,
            max_context_tokens=max_ctx_tokens,
            system_prompt=system_prompt,
            tool_definitions=tool_definitions,
            skill_registry=self.skill_registry,
            memory_manager=self.memory_mgr,
            agent_registry=self.agent_registry,
            knowledge_base=None,
            user_profile=None,
            journal=self.journal,
        )

    # ======================== 原子操作：消息构建 ========================

    def _build_messages(
        self,
        user_input: str,
        system_prompt: str,
        history: list[Message],
    ) -> list[Message]:
        """构建 LLM 调用消息列表。

        原子操作：
        1. 创建 system message
        2. 裁剪历史消息以适应 token 预算
        3. 追加 user message

        Args:
            user_input: 用户输入文本
            system_prompt: system prompt 文本
            history: 上下文历史消息

        Returns:
            LLM 输入消息列表
        """
        from ..agent.message_utils import trim as msg_trim, _msg_tokens

        system_msg: Message = Message(role="system", content=system_prompt)
        user_msg: Message = Message(role="user", content=user_input)

        max_ctx_tokens: int = 8000
        if self.config is not None:
            max_ctx_tokens = self.config.agent.max_context_tokens

        budget: int = max_ctx_tokens - _msg_tokens(system_msg) - _msg_tokens(user_msg)

        if budget > 0:
            trimmed_history: list[Message] = msg_trim(list(history), budget)
        else:
            trimmed_history = []

        return [system_msg] + trimmed_history + [user_msg]

    # ======================== 原子操作：工具执行 ========================

    async def _execute_tools(
        self,
        tool_calls: list[object],
    ) -> list[Message]:
        """并发执行工具调用列表。

        原子操作：
        1. 对每个 tool_call 并发执行 _execute_single_tool
        2. 返回所有 tool result Message 列表

        Args:
            tool_calls: ToolCall 对象列表

        Returns:
            role="tool" 的 Message 列表
        """
        if not tool_calls:
            return []

        return list(await asyncio.gather(*[
            self._execute_single_tool(tc)
            for tc in tool_calls
        ]))

    async def _execute_single_tool(self, tc: object) -> Message:
        """执行单个工具调用。

        原子操作：
        1. 解析参数
        2. 通过 tool_executor 执行
        3. 返回 tool result Message

        Args:
            tc: ToolCall 对象

        Returns:
            role="tool" 的 Message
        """
        name: str = getattr(tc, "name", "")
        tool_id: str = getattr(tc, "id", "")

        try:
            args: dict[str, object] = _json.loads(getattr(tc, "arguments", "{}"))
        except (_json.JSONDecodeError, TypeError):
            args = {}

        if self.tool_executor is None:
            return Message(
                role="tool",
                content="错误：工具执行器未初始化",
                tool_call_id=tool_id,
            )

        if self.channel is not None:
            self.channel.print_info(
                f"\n🔧 调用工具: {name}({_json.dumps(args, ensure_ascii=False)})"
            )

        result = await self.tool_executor.execute(
            name=name,
            arguments=args,
            channel=self.channel,
        )

        if self.channel is not None:
            content_preview: str = result.output[:100]
            if len(result.output) > 100:
                content_preview += "..."
            self.channel.print_info(f"  结果: {content_preview}")

        return Message(
            role="tool",
            content=result.output,
            tool_call_id=tool_id,
        )

    # ======================== 原子操作：Handoff 处理 ========================

    async def _handle_handoff(
        self,
        thread_id: str,
        target_agent_id: str,
        context: str,
        parent_run_id: str,
    ) -> AgentRun:
        """处理 Agent 间任务流转。

        原子操作：
        1. 从 agent_registry 查找目标 Agent
        2. 创建子 Agent + 子 TurnLoop
        3. 推送触发事件并等待结果

        Args:
            thread_id: Session ID
            target_agent_id: 目标 Agent ID
            context: handoff 上下文
            parent_run_id: 父 AgentRun ID

        Returns:
            子 Agent 的 AgentRun

        Raises:
            RuntimeError: 目标 Agent 不存在
        """
        target_identity = self.agent_registry.get(target_agent_id)
        if target_identity is None:
            raise RuntimeError(
                f"Handoff 失败：Agent '{target_agent_id}' 不在注册表中"
            )

        from ..agent.agent import Agent as AgentCls
        child_agent: AgentCls = AgentCls(
            identity=target_identity,
            runtime=self,
        )

        # 创建子 TurnLoop（共享 Runtime + Journal + StateStore）
        from ..session.turn_loop import TurnLoop, TriggerEvent as TE
        from ..session.agent_run import TriggerType

        if self.journal is None or self.state_store is None:
            raise RuntimeError("Handoff 需要 Runtime 注入 Journal 和 StateStore")

        child_loop: TurnLoop = TurnLoop(
            session_id=thread_id,
            agent=child_agent,
            runtime=self.derive(),
            state_store=self.state_store,
            journal=self.journal,
            channel=self.channel,
        )

        # 推送 handoff 触发
        trigger: TE = TE(
            trigger_type=TriggerType.USER_INPUT,
            data=context,
            agent_id=target_agent_id,
        )
        await child_loop.push_trigger(trigger)

        # 异步执行（当前版本等待完成）
        await child_loop.run_forever(context)

        # 返回占位 AgentRun（子 Agent 的 AgentRun 已在 TurnLoop 中持久化）
        from ..session.agent_run import AgentRun as AR
        return AR(
            run_id="",
            agent_id=target_agent_id,
            parent_run_id=parent_run_id,
            end_status="completed",
        )


# ============================================================================
# 辅助函数
# ============================================================================

def _find_project_root() -> Path:
    """从 dotClaw 模块位置向上找到项目根目录。"""
    import dotclaw
    return Path(dotclaw.__file__).parent.parent.parent


def _new_hex_id() -> str:
    """生成 8 位 hex ID。"""
    import uuid
    return uuid.uuid4().hex[:8]
