"""Runtime —— Agent 编排引擎。

Runtime 是 dotClaw 的基础设施层，提供：
- Agent 调度与循环控制（AgentState 状态机）
- LLM 调用与工具执行协调
- Handoff 多 Agent 流转

架构关系：
    Runtime（编排引擎）
      ├── AgentState（状态机）→ 驱动 ReAct 循环
      ├── Task（内部问题拆解）→ Agent 的计划-执行子任务
      └── Agent（无状态配置）→ 作为 Runtime.run() 的入参

内部逻辑（原子操作封装为方法）：
1. run() — 公开入口，协调完整执行流程
2. _create_agent_state() — 创建 AgentState 实例
3. _invoke_llm() — 调用 LLM，返回 LLMResponse
4. _execute_tools() — 执行工具调用，返回结果
5. _build_messages() — 构建 LLM 输入消息
6. _build_slot_context() — 构建 SlotContext 给 Assembler
7. _build_agent_run() — 从 AgentState 构建 AgentRun
8. _handle_handoff() — 处理 Agent 间任务流转
"""

from __future__ import annotations

import asyncio
import json as _json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..llm.base import Message
from .agent_state import (
    AgentAction,
    AgentStatus,
    AgentState,
    AgentStartEvent,
    LLMResponseEvent,
    ToolsDoneEvent,
)

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
    from ..config import Config


def _find_project_root() -> Path:
    """从 dotClaw 模块位置向上找到项目根目录。"""
    import dotclaw
    return Path(dotclaw.__file__).parent.parent.parent


# ============================================================================
# Runtime —— 编排引擎
# ============================================================================

class Runtime:
    """Agent 编排引擎。

    Runtime 是单例/共享服务，不归 Agent 所有。
    Agent 是 Runtime 上调度的无状态"程序"（配置+Prompt+Tools）。

    核心循环：
        1. 创建 AgentState
        2. 发送 AgentStartEvent → 进入状态机循环
        3. 根据 AgentAction 执行 LLM 调用或工具执行
        4. Handoff 时递归创建新 Task
        5. 结束后保存 AgentRun

    Args:
        llm: LLM 代理，处理所有 LLM 调用
        tool_executor: 工具执行器
        assembler: 上下文组装器（构建 system_prompt）
        agent_registry: Agent 注册表（用于 handoff 路由）
        session_mgr: Session 管理器
        run_mgr: AgentRun 持久化管理器
        channel: 通信通道（CLI/API 等）
        memory_mgr: 记忆管理器
        skill_registry: 技能注册表
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
        self.channel: Channel | None = channel
        self.memory_mgr: MemoryManager | None = memory_mgr
        self.skill_registry: SkillRegistry | None = skill_registry
        self.mcp_provider: object = mcp_provider
        self.config: Config | None = config

    # ======================== 派生（多 Agent 隔离） ========================

    def derive(self, *, channel: Channel | None = None) -> Runtime:
        """派生 Runtime。共享 llm/skills/registry，隔离 channel。

        每个子 Agent 调用一次，开销极小（引用复制，不新建重量对象）。

        Args:
            channel: 覆盖的 channel（默认 NullChannel，避免子 Agent 输出到用户终端）

        Returns:
            新的 Runtime 实例，共享底层能力引用
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
            channel=channel,
            memory_mgr=self.memory_mgr,
            skill_registry=self.skill_registry,
            mcp_provider=self.mcp_provider,
            config=self.config,
        )

    # ======================== 公开入口 ========================

    async def run(
        self,
        thread_id: str,
        agent: AgentType,
        user_message: str,
    ) -> AgentRun:
        """执行一次 Agent 任务。

        完整流程：
        1. 创建 AgentState 并发送 AgentStartEvent
        2. 进入状态机循环：根据 AgentAction 执行对应操作
        3. Handoff 时递归调用 self.run(target_agent)
        4. 结束后保存 AgentRun

        Args:
            thread_id: Session ID
            agent: 要执行的 Agent 实例（配置+Prompt+Tools）
            user_message: 用户输入文本

        Returns:
            AgentRun（执行结果记录）
        """
        agent_id: str = agent.identity.agent_id
        model: str = agent._resolve_model()
        system_prompt: str = agent._resolve_system_prompt()
        tool_definitions: list[ToolDefinition] = agent._resolve_tool_definitions()
        max_iterations: int = agent.identity.max_loop_steps

        task: AgentState = self._create_agent_state(thread_id, agent_id, max_iterations)
        run_id: str = task.task_id
        started_at: str = datetime.now(timezone.utc).isoformat()
        start_time: float = time.time()

        # 发送启动事件
        action: AgentAction = task.handle_event(AgentStartEvent(user_message))

        # 本地消息历史（当前执行周期内累积的所有消息）
        all_messages: list[Message] = []
        tokens_in_total: int = 0
        tokens_out_total: int = 0

        try:
            # ── 组装 system_prompt（通过 Assembler） ──
            slot_ctx: SlotContext = self._build_slot_context(
                thread_id=thread_id,
                user_message=user_message,
                system_prompt=system_prompt,
                tool_definitions=tool_definitions,
            )

            if self.assembler is not None:
                self.assembler.on_new_request()
                system_prompt = await self.assembler.build_system_prompt(slot_ctx)

            # ── 状态机主循环 ──
            while not task.is_terminal:
                match action:
                    case AgentAction.INVOKE_LLM:
                        # 构建消息并调用 LLM
                        messages = self._build_messages(
                            user_input=user_message,
                            system_prompt=system_prompt,
                            history=all_messages,
                        )
                        resp: LLMResponse = await self._invoke_llm(
                            messages=messages,
                            model=model,
                            tool_definitions=tool_definitions,
                        )
                        tokens_in_total += resp.input_tokens
                        tokens_out_total += resp.output_tokens

                        # 记录 assistant 消息
                        asst_msg: Message = self._llm_response_to_message(resp)
                        all_messages.append(asst_msg)

                        # 喂入状态机
                        action = task.handle_event(LLMResponseEvent(resp))

                    case AgentAction.EXECUTE_TOOLS:
                        # 执行工具调用
                        results, stop_signal, handoff_signal, tool_error = (
                            await self._execute_tools(task)
                        )
                        all_messages.extend(results)

                        # 喂入状态机
                        action = task.handle_event(ToolsDoneEvent(
                            results=results,
                            stop_signal=stop_signal,
                            handoff_signal=handoff_signal,
                            tool_error=tool_error,
                        ))

                    case AgentAction.HANDOFF_TARGET:
                        # 递归执行 handoff
                        handoff_agent = task.handoff_target or ""
                        handoff_ctx = task.handoff_context or ""
                        return await self._handle_handoff(
                            thread_id=thread_id,
                            target_agent_id=handoff_agent,
                            context=handoff_ctx,
                            parent_run_id=run_id,
                        )

                    case AgentAction.FINALIZE:
                        break

                    case AgentAction.WAIT:
                        # WAIT 不应在循环中出现（TRUNCATED 在 _transition 中已处理）
                        break

        except Exception as e:
            task.end_status = AgentStatus.FAILED
            task.error_message = f"{type(e).__name__}: {e}"

        # ── 构建最终 AgentRun ──
        duration_ms: int = int((time.time() - start_time) * 1000)
        ended_at: str = datetime.now(timezone.utc).isoformat()

        agent_run = self._build_agent_run(
            task=task,
            run_id=run_id,
            agent_id=agent_id,
            all_messages=all_messages,
            tokens_in=tokens_in_total,
            tokens_out=tokens_out_total,
            duration_ms=duration_ms,
            ended_at=ended_at,
        )

        # 持久化 AgentRun
        await self.run_mgr.save(agent_run, thread_id)

        return agent_run

    # ======================== 原子操作：AgentState 创建 ========================

    def _create_agent_state(
        self,
        thread_id: str,
        agent_id: str,
        max_iterations: int,
    ) -> AgentState:
        """创建 AgentState 实例。

        原子操作：
        1. 生成 task_id
        2. 构造 AgentState

        Args:
            thread_id: Session ID
            agent_id: 执行此 AgentRun 的 Agent ID
            max_iterations: 最大迭代次数

        Returns:
            初始化好的 AgentState
        """
        task_id: str = uuid.uuid4().hex[:8]
        return AgentState(
            task_id=task_id,
            thread_id=thread_id,
            agent_id=agent_id,
            max_iterations=max_iterations,
        )

    # ======================== 原子操作：LLM 调用 ========================

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
            request_id=uuid.uuid4().hex[:8],
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
            journal=None,
        )

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
            history: 当前执行周期内累积的消息历史

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

    async def _invoke_llm(
        self,
        messages: list[Message],
        model: str,
        tool_definitions: list[ToolDefinition],
    ) -> LLMResponse:
        """调用 LLM 并返回完整响应。

        原子操作：
        1. 流式调用 llm.chat()
        2. 聚合 content、tool_calls、tokens
        3. 返回 LLMResponse

        Args:
            messages: LLM 输入消息列表
            model: 模型名
            tool_definitions: 工具定义列表

        Returns:
            LLMResponse（content + tool_calls + finish_reason + tokens）
        """
        from ..agent.agent import LLMResponse as LR

        current_content: str = ""
        tool_calls: list[object] = []
        finish_reason: str = "stop"
        input_tokens: int = 0
        output_tokens: int = 0

        stream_enabled: bool = False
        if self.config is not None:
            stream_enabled = self.config.llm.stream

        async for chunk in self.llm.chat(
            messages=messages,
            tools=tool_definitions if tool_definitions else None,
            model=model,
            purpose="chat",
            stream=stream_enabled,
        ):
            if chunk.content:
                current_content += chunk.content
                if self.channel is not None:
                    await self.channel.stream(chunk.content)

            if chunk.tool_call:
                tool_calls.append(chunk.tool_call)

            if chunk.is_final:
                finish_reason = chunk.finish_reason or "stop"
                input_tokens = getattr(chunk, "input_tokens", 0)
                output_tokens = getattr(chunk, "output_tokens", len(current_content))
                break

        return LR(
            content=current_content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    # ======================== 原子操作：工具执行 ========================

    async def _execute_tools(
        self,
        task: AgentState,
    ) -> tuple[list[Message], bool, bool, bool]:
        """执行当前 LLM 响应中的工具调用。

        原子操作：
        1. 从 task.current_llm_response 提取 tool_calls
        2. 逐个执行工具（并发）
        3. 分析结果中的 stop/handoff 信号

        Args:
            task: 当前 AgentState

        Returns:
            (results, stop_signal, handoff_signal, tool_error)
        """
        resp = task.current_llm_response
        if resp is None or not resp.tool_calls:
            return ([], False, False, False)

        # 并发执行所有工具调用
        tool_messages: list[Message] = list(await asyncio.gather(*[
            self._execute_single_tool(tc)
            for tc in resp.tool_calls
        ]))

        # 分析信号
        stop_signal: bool = self._detect_stop_signal(tool_messages)
        handoff_signal: bool = self._detect_handoff_signal(tool_messages)
        tool_error: bool = any("错误" in tm.content for tm in tool_messages)

        return (tool_messages, stop_signal, handoff_signal, tool_error)

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

        # 解析参数
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

    # ======================== 原子操作：信号检测 ========================

    def _detect_stop_signal(self, tool_messages: list[Message]) -> bool:
        """检测工具结果中是否包含停止信号。

        目前检测规则：工具结果内容包含 "TASK_COMPLETE"。
        未来可扩展为从 ToolResult.metadata 中读取。

        Args:
            tool_messages: 工具执行结果列表

        Returns:
            True 表示应停止循环
        """
        for msg in tool_messages:
            if msg.content and "TASK_COMPLETE" in msg.content:
                return True
        return False

    def _detect_handoff_signal(self, tool_messages: list[Message]) -> bool:
        """检测工具结果中是否包含 handoff 信号。

        目前检测规则：工具结果内容包含 "HANDOFF"。
        未来可扩展为从 ToolResult.metadata 中读取。

        Args:
            tool_messages: 工具执行结果列表

        Returns:
            True 表示应执行 handoff
        """
        for msg in tool_messages:
            if msg.content and "HANDOFF" in msg.content:
                return True
        return False

    # ======================== 原子操作：Handoff ========================

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
        2. 创建子 Agent 实例
        3. 递归调用 self.run()

        Args:
            thread_id: Session ID（同一 Session）
            target_agent_id: 目标 Agent ID
            context: handoff 上下文信息
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

        # 创建子 Agent（共享 Runtime 的底层能力）
        from ..agent.agent import Agent as AgentCls

        child_agent: AgentCls = AgentCls(
            identity=target_identity,
            runtime=self,
        )

        # 递归执行（同一 thread_id）
        return await self.run(
            thread_id=thread_id,
            agent=child_agent,
            user_message=context,
        )

    # ======================== 原子操作：AgentRun 构建 ========================

    @staticmethod
    def _llm_response_to_message(resp: LLMResponse) -> Message:
        """将 LLMResponse 转换为 assistant Message。

        Args:
            resp: LLM 响应

        Returns:
            role="assistant" 的 Message
        """
        return Message(
            role="assistant",
            content=resp.content or "",
            tool_calls=list(resp.tool_calls) if resp.tool_calls else None,
        )

    @staticmethod
    def _build_agent_run(
        task: AgentState,
        run_id: str,
        agent_id: str,
        all_messages: list[Message],
        tokens_in: int,
        tokens_out: int,
        duration_ms: int,
        ended_at: str,
    ) -> AgentRun:
        """从 AgentState 构建 AgentRun。

        原子操作：
        1. 映射 AgentStatus → AgentRun.end_status
        2. 填充所有字段

        Args:
            task: 完成的 AgentState
            run_id: Run ID
            agent_id: Agent ID
            all_messages: 所有累积的消息
            tokens_in: 总输入 tokens
            tokens_out: 总输出 tokens
            duration_ms: 总耗时（毫秒）
            ended_at: 结束时间

        Returns:
            AgentRun 实例
        """
        from ..session.agent_run import AgentRun as AR

        status_mapping: dict[AgentStatus, str] = {
            AgentStatus.COMPLETED: "completed",
            AgentStatus.HANDOFF: "handoff",
            AgentStatus.FAILED: "failed",
            AgentStatus.RUNNING: "completed",
        }

        return AR(
            run_id=run_id,
            agent_id=agent_id,
            messages=list(all_messages),
            end_status=status_mapping.get(task.end_status, "completed"),
            tool_calls=task.tool_calls_total,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            iterations=task.iteration,
            duration_ms=duration_ms,
            error=task.error_message,
            ended_at=ended_at,
        )
