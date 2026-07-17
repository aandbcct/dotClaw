"""Runtime —— Agent 执行引擎。

Runtime 是 dotClaw 的执行引擎 + 依赖容器，统一负责：
- 依赖注入（LLM / Tool / Journal / Config / ...）
- 执行入口：run(session, agent, user_message) → final_answer
- 内部 ReAct 循环由 AgentState 状态机驱动

v3 变更：
- TurnLoop 合并回 Runtime，消除架空抽象层
- _context_messages / _run_ids 变为 run() 内部局部变量
- Journal 生命周期由 run() 内部管理
"""

from __future__ import annotations

import asyncio
import json as _json
import time
import uuid
from datetime import datetime, timedelta, timezone

CHINA_TZ = timezone(timedelta(hours=8))
from pathlib import Path
from typing import TYPE_CHECKING

from ..llm.base import Message
from ..journal.journal import Journal
from ..session.agent_run import RunEndStatus, TriggerType
from .agent_state import (
    AgentState, AgentPhase, AgentAction, AgentStatus,
    AgentStartEvent as ASStartEvent,
    LLMResponseEvent as ASLLMResponseEvent,
    ToolsDoneEvent as ASToolsDoneEvent,
    ContinueEvent,
)


class DelegationProtocolError(RuntimeError):
    """source Run 在活动 Task 未终态时试图结束。"""

if TYPE_CHECKING:
    from ..llm.proxy import LLMProxy
    from ..tools.executor import ToolExecutor
    from ..tools.base import ToolDefinition
    from ..memory.manager import MemoryManager
    from ..skills.registry import SkillRegistry
    from ..agent.agent import Agent as AgentType, LLMResponse
    from ..agent.slotContext import ContextAssembler, SlotContext
    from ..channel.base import Channel
    from ..session.session import Session as SessionType, SessionManager
    from ..session.agent_run import AgentRun, AgentRunManager
    from ..orchestration.registry import AgentRegistry
    from ..config import Config
    from .state_store import StateStore


# ============================================================================
# Runtime —— 执行引擎
# ============================================================================

class Runtime:
    """Agent 执行引擎 + 依赖容器。

    run(session, agent, user_message) 是一次完整的一问一答入口。
    内部 ReAct 循环由 AgentState 状态机驱动，所有运行时状态
    （_context_messages, _run_ids）都是栈上的局部变量。

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
        delegation_endpoint: str = "",
        delegation_task_id: str = "",
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
        self.delegation_endpoint: str = delegation_endpoint
        self.delegation_task_id: str = delegation_task_id
        # per-_step 上下文，由 _step() 在进入时设置，_execute_single_tool() 消费
        self._current_agent: AgentType | None = None
        self._current_session_id: str = ""
        self._current_agentrun_id: str = ""
        self._current_allowed_tools: frozenset[str] = frozenset()

    # ======================== 派生（多 Agent 隔离） ========================

    def derive(self, *, channel: Channel | None = None, delegation_endpoint: str = "", delegation_task_id: str = "") -> Runtime:
        """派生 Runtime。共享 llm/skills/registry/state_store，隔离 channel 和 journal。

        子 Agent 获得全新 Journal 实例，避免父子并发执行时 trace/session/AgentRun
        互相覆盖。channel 默认使用 NullChannel，阻止子 Agent 向用户终端输出。

        Args:
            channel: 覆盖的 channel

        Returns:
            新的 Runtime 实例
        """
        if channel is None:
            from ..channel.null import NullChannel
            channel = NullChannel()

        child_journal: Journal = Journal()

        child_assembler: ContextAssembler | None = (
            self.assembler.clone() if self.assembler is not None else None
        )
        return Runtime(
            llm=self.llm,
            tool_executor=self.tool_executor,
            assembler=child_assembler,
            agent_registry=self.agent_registry,
            session_mgr=self.session_mgr,
            run_mgr=self.run_mgr,
            journal=child_journal,
            state_store=self.state_store,
            channel=channel,
            memory_mgr=self.memory_mgr,
            skill_registry=self.skill_registry,
            mcp_provider=self.mcp_provider,
            config=self.config,
            delegation_endpoint=delegation_endpoint,
            delegation_task_id=delegation_task_id,
        )

    # ======================== 公开入口：run() ========================

    # WAIT 哨兵值（人工审批等长等待场景，AgentRun 以 WAITING 结束，
    # 调用方再次 run() 时 Runtime 自动检测并恢复，无需传递额外参数）
    WAIT_SENTINEL: str = "__DOTCLAW_WAIT__"

    async def run(
        self,
        session: SessionType,
        agent: AgentType,
        user_message: str,
    ) -> tuple[str, list[str]]:
        """执行一次用户消息 → Agent 回复。

        Runtime 自动检测 session 下是否有 WAITING 状态的 AgentRun：
        - 无 → 新建 AgentRun，从 IDLE 开始
        - 有 → 从 WAITING 恢复，自动重建状态和上下文

        短等待（RETRYING / TRUNCATED / 子Agent）在单次 AgentRun 内自动完成，
        调用方无感知。只有人工审批等长等待场景返回 WAIT_SENTINEL。

        Args:
            session: 当前 Session
            agent: 执行 Agent
            user_message: 用户输入文本

        Returns:
            (final_answer, run_ids): 最终回复 + 本次产生的 AgentRun ID 列表
        """
        if self.journal is None or self.state_store is None:
            raise RuntimeError(
                "Runtime.run() requires Journal and StateStore injected"
            )

        # Journal 生命周期
        conversation_id: str = uuid.uuid4().hex[:8]
        model: str = agent._resolve_model(self)
        self.journal.session_start(
            session_id=session.id,
            model=model,
            config=self.config.journal if self.config else None,
            conversation_id=conversation_id,
        )

        context_messages: list[Message] = []
        run_ids: list[str] = []

        # 自动检测 WAITING 状态 → 内部 resume
        waiting_runs: list[object] = await self.run_mgr.list(
            session.id, end_status="waiting",
        )
        waiting_run: object | None = waiting_runs[-1] if waiting_runs else None

        try:
            final_answer = await self._step(
                session_id=session.id,
                agent=agent,
                user_message=user_message,
                context_messages=context_messages,
                run_ids=run_ids,
                waiting_run=waiting_run,
            )
        except Exception:
            self.journal.finalize()
            raise

        try:
            await self._ensure_source_task_closed(agent, session.id)
            return final_answer, list(run_ids)
        finally:
            self.journal.finalize()

    async def _ensure_source_task_closed(self, agent: AgentType, session_id: str) -> None:
        """阻止 source Session 在活动 Task 存在时提交最终回答。

        MVP 的普通等待由 wait_task 在当前 Run 内完成。若模型绕过等待工具直接
        结束，本守卫会拒绝提交该回答，确保不会留下后台 delegation。
        """
        dispatcher = agent.dispatcher
        if dispatcher is None:
            return
        active_task = await dispatcher.broker.active_task_for_source(session_id)
        if active_task is not None:
            error: DelegationProtocolError = DelegationProtocolError(
                f"source Session 仍有活动 Task：{active_task.task_id}；必须 wait_task 或 cancel_task"
            )
            self.journal.task_event(
                event_type="protocol_violation",
                task_id=active_task.task_id,
                endpoint="source",
                status=active_task.status.value,
                sequence=active_task.result_message.sequence if active_task.result_message is not None else 0,
            )
            raise error

    # ======================== 单次 AgentRun 执行 ========================

    async def _step(
        self,
        session_id: str,
        agent: AgentType,
        user_message: str,
        context_messages: list[Message],
        run_ids: list[str],
        *,
        waiting_run: object | None = None,
    ) -> str:
        """执行一次 AgentRun（新建或从 WAITING 恢复）。

        AgentState 驱动内部 think-act 循环：
        IDLE → THINKING → ACTING → THINKING → ... → RESPONDING → DONE

        waiting_run 为 None 时新建 AgentRun，否则从 WAITING AgentRun 恢复。
        此参数由 run() 内部自动检测填充，调用方无需关心。

        Args:
            waiting_run: WAITING 状态的 AgentRun 对象（内部使用，调用方不传）。
        """
        agentrun_id: str = uuid.uuid4().hex[:8]
        run_ids.append(agentrun_id)
        run_messages: list[Message] = []

        # 设置 per-step 上下文（供工具执行时使用）
        self._current_agent = agent
        self._current_session_id = session_id
        self._current_agentrun_id = agentrun_id
        self._current_allowed_tools = frozenset(
            definition.name for definition in self._resolve_runtime_tools(agent)
        )

        # system_prompt
        system_prompt: str = await self._build_system_prompt(
            session_id, agent, user_message or "",
        )
        run_messages.append(Message(role="system", content=system_prompt))

        # ── 确定触发类型并启动 Journal 追踪 ──
        trigger: TriggerType = (
            TriggerType.RESUME if waiting_run is not None
            else TriggerType.USER_INPUT
        )
        self.journal.agentrun_start(agentrun_id, trigger.value)

        # ── 分支：resume 或 fresh ──
        if waiting_run is not None:
            action, state = await self._init_resume(
                waiting_run, agentrun_id, session_id, agent,
                user_message, context_messages, run_messages,
            )
        else:
            action, state = self._init_fresh(
                agentrun_id, session_id, agent,
                user_message, context_messages, run_messages,
            )

        started_at: str = datetime.now(CHINA_TZ).isoformat()
        start_time: float = time.time()
        tokens_in_total: int = 0
        tokens_out_total: int = 0

        final_answer: str = ""
        end_status: RunEndStatus = RunEndStatus.COMPLETED

        try:
            while not state.is_terminal:
                if action == AgentAction.INVOKE_LLM:
                    context_msgs: list[Message] = self._build_context_msgs(
                        system_prompt, context_messages,
                    )
                    resp: LLMResponse = await self._invoke_llm(
                        agent, context_msgs,
                    )
                    tokens_in_total += resp.input_tokens
                    tokens_out_total += resp.output_tokens

                    asst_msg: Message = _build_assistant_message(resp)
                    self.journal.record_message(asst_msg)
                    context_messages.append(asst_msg)
                    run_messages.append(asst_msg)

                    action = state.handle_event(ASLLMResponseEvent(response=resp))

                elif action == AgentAction.EXECUTE_TOOLS:
                    tool_msgs, needs_approval = await self._execute_tools_for_state(state)
                    for tm in tool_msgs:
                        self.journal.record_message(tm)
                        context_messages.append(tm)
                        run_messages.append(tm)

                    action = state.handle_event(ASToolsDoneEvent(
                        results=tool_msgs,
                        needs_approval=needs_approval,
                    ))

                elif action == AgentAction.WAIT:
                    # TRUNCATED: 注入 "continue" 提示后自动续跑
                    if state.phase == AgentPhase.TRUNCATED:
                        truncated_continue: bool = (
                            self.config.agent.truncated_continue
                            if self.config is not None else False
                        )
                        if truncated_continue:
                            state._truncated_continue_allowed = True
                            inject_msg: Message = Message(
                                role="user",
                                content="[系统] 上一轮回复被截断，请继续完成。",
                            )
                            self.journal.record_message(inject_msg)
                            context_messages.append(inject_msg)
                            run_messages.append(inject_msg)
                        action = state.handle_event(ContinueEvent())

                    # RETRYING: 自动推送 ContinueEvent
                    elif state.phase == AgentPhase.RETRYING:
                        action = state.handle_event(ContinueEvent())

                    else:
                        # WAITING_APPROVAL 等长等待：持久化 + WAIT_SENTINEL
                        # 等待状态已由 _save_waiting_state 写入，finally 中不得再次以完成态覆盖。
                        end_status = RunEndStatus.WAITING
                        await self._save_waiting_state(state, agentrun_id, session_id,
                                                       tokens_in_total, tokens_out_total,
                                                       start_time, started_at, run_messages)
                        return self.WAIT_SENTINEL

                else:
                    # FINALIZE / HANDOFF_TARGET / unexpected → exit loop.
                    break

            # 处理循环退出后的终态动作
            if action == AgentAction.FINALIZE:
                if state.current_llm_response is not None:
                    final_answer = state.current_llm_response.content or ""
                end_status = _agent_status_to_run_end(state.end_status)
            elif action == AgentAction.HANDOFF_TARGET:
                await self._handle_handoff_for_state(
                    state, agentrun_id, session_id,
                )
                end_status = RunEndStatus.HANDOFF

        except Exception as e:
            end_status = RunEndStatus.FAILED
            final_answer = f"[执行异常] {type(e).__name__}: {e}"
            self.journal.error("ERROR", "runtime.step", f"{type(e).__name__}: {e}")

        finally:
            duration_ms: int = int((time.time() - start_time) * 1000)
            ended_at: str = datetime.now(CHINA_TZ).isoformat()
            self.journal.agentrun_end(end_status.value)

            if end_status != RunEndStatus.WAITING:
                try:
                    await self._save_agent_run(
                        session_id=session_id,
                        agentrun_id=agentrun_id,
                        agent_id=agent.agent_id,
                        end_status=end_status,
                        tokens_in=tokens_in_total,
                        tokens_out=tokens_out_total,
                        duration_ms=duration_ms,
                        started_at=started_at,
                        ended_at=ended_at,
                        state_snapshot=state.snapshot(),
                        trace_ids=self._collect_trace_ids(),
                        messages=run_messages,
                    )
                except Exception:
                    pass

        return final_answer

    # ======================== LLM 调用 ========================

    async def _invoke_llm(
        self,
        agent: AgentType,
        context_msgs: list[Message],
    ) -> LLMResponse:
        """调用 LLM，返回 LLMResponse。"""
        from ..agent.agent import LLMResponse as LR

        model: str = agent._resolve_model(self)
        tool_definitions: list[ToolDefinition] = agent._resolve_tool_definitions(self)

        self.journal.prompt_built(
            message_count=len(context_msgs),
            context_length=sum(len(str(m.content or "")) for m in context_msgs),
            system_prompt="",
            tool_count=len(tool_definitions),
        )
        self.journal.llm_call_start(attempt=1)

        current_content: str = ""
        tool_calls: list[object] = []
        finish_reason: str = "stop"
        input_tokens: int = 0
        output_tokens: int = 0
        stream_enabled: bool = (
            self.config.llm.stream
            if self.config is not None else False
        )

        async for chunk in self.llm.chat(
            messages=context_msgs,
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

        self.journal.llm_response_end(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tps=(output_tokens / 1.0) if output_tokens > 0 else 0.0,
            status="success",
            stop_reason=finish_reason,
        )

        return LR(
            content=current_content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    # ======================== 工具执行 ========================

    async def _execute_tools(
        self,
        tool_calls: list[object],
    ) -> list[Message]:
        """并发执行工具调用列表。"""
        if not tool_calls:
            return []

        return list(await asyncio.gather(*[
            self._execute_single_tool(tc)
            for tc in tool_calls
        ]))

    async def _execute_single_tool(self, tc: object) -> Message:
        """执行单个工具调用。"""
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

        if name not in self._current_allowed_tools:
            return Message(
                role="tool",
                content=f"错误：当前 Identity 无权调用工具 '{name}'",
                tool_call_id=tool_id,
            )

        if self.channel is not None:
            self.channel.print_info(
                f"\n🔧 调用工具: {name}({_json.dumps(args, ensure_ascii=False)})"
            )

        from ..tools.base import ToolExecutionContext

        exec_ctx: ToolExecutionContext | None = None
        if self._current_agent is not None:
            exec_ctx = ToolExecutionContext(
                agent=self._current_agent,
                runtime=self,
                session_id=self._current_session_id,
                agentrun_id=self._current_agentrun_id,
                task_id=self.delegation_task_id,
                channel=self.channel,
            )

        result = await self.tool_executor.execute(
            name=name,
            arguments=args,
            channel=self.channel,
            execution_context=exec_ctx,
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

    async def _execute_tools_for_state(
        self,
        state: AgentState,
    ) -> tuple[list[Message], bool]:
        """执行 AgentState 中当前待执行的工具调用。

        Returns:
            (tool_msgs, needs_approval): 工具结果列表 + 是否需要审批
        """
        llm_resp: LLMResponse | None = state.current_llm_response
        if llm_resp is None or not llm_resp.tool_calls:
            return [], False

        # 检查是否有工具命中审批列表
        approval_commands: list[str] = (
            self.config.tools.approval_commands
            if self.config is not None else []
        )
        tool_calls: list[object] = list(llm_resp.tool_calls)
        for tc in tool_calls:
            name: str = getattr(tc, "name", "")
            if name in approval_commands:
                return [], True  # 需要审批，不执行工具

        if self.tool_executor is None:
            raise RuntimeError("工具执行器未初始化")
        return await self._execute_tools(tool_calls), False

    # ======================== Handoff ========================

    async def _handle_handoff_for_state(
        self,
        state: AgentState,
        parent_run_id: str,
        session_id: str,
    ) -> None:
        """执行 handoff：创建子 Agent，通过 derived Runtime.run() 完成流转。"""
        handoff_target: str = state.handoff_target or _extract_handoff_target(
            state.current_tool_results,
        )
        if not handoff_target:
            return

        await self._handle_handoff(
            thread_id=session_id,
            target_agent_id=handoff_target,
            context=state.handoff_context or "",
            parent_run_id=parent_run_id,
        )

    async def _handle_handoff(
        self,
        thread_id: str,
        target_agent_id: str,
        context: str,
        parent_run_id: str,
    ) -> AgentRun:
        """处理 Agent 间任务流转。

        创建子 Agent + derived Runtime → 递归调用 run()。
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

        if self.journal is None:
            raise RuntimeError("Handoff 需要 Runtime 注入 Journal")

        child_runtime: Runtime = self.derive()
        _, handoff_run_ids = await child_runtime.run(
            session=_dummy_session(thread_id),
            agent=child_agent,
            user_message=context,
        )

        from ..session.agent_run import AgentRun as AR
        return AR(
            run_id=handoff_run_ids[0] if handoff_run_ids else "",
            agent_id=target_agent_id,
            parent_run_id=parent_run_id,
            end_status="completed",
        )

    # ======================== System Prompt / Context ========================

    async def _build_system_prompt(
        self,
        session_id: str,
        agent: AgentType,
        user_message: str,
    ) -> str:
        """构建 system prompt（含 Slot 解析）。"""
        system_prompt: str = agent._resolve_system_prompt(self)
        tool_definitions: list[ToolDefinition] = self._resolve_runtime_tools(agent)
        from ..agent.slotContext import SlotContext as SCtx

        project_root: Path = _find_project_root()

        slot_ctx: SCtx = SCtx(
            query=user_message,
            request_id=uuid.uuid4().hex[:8],
            session_id=session_id,
            project_root=project_root,
            max_context_tokens=(
                self.config.agent.max_context_tokens
                if self.config is not None else 8000
            ),
            system_prompt=system_prompt,
            tool_definitions=tool_definitions,
            skill_registry=self.skill_registry,
            memory_manager=self.memory_mgr,
            agent_registry=self.agent_registry,
            knowledge_base=None,
            user_profile=None,
            journal=self.journal,
        )
        if self.assembler is not None:
            self.assembler.on_new_request()
            excluded_slots: frozenset[str] = (
                frozenset({"available_agents"})
                if self.delegation_endpoint == "target" else frozenset()
            )
            system_prompt = await self.assembler.build_system_prompt(slot_ctx, excluded_slots)
        return system_prompt

    def _resolve_runtime_tools(self, agent: AgentType) -> list[ToolDefinition]:
        """按 Identity 与 delegation 角色过滤可见且可执行的工具。"""
        definitions: list[ToolDefinition] = agent._resolve_tool_definitions(self)
        if self.delegation_endpoint != "target":
            return definitions
        forbidden: frozenset[str] = frozenset({"delegate", "cancel_task"})
        return [definition for definition in definitions if definition.name not in forbidden]

    def _build_context_msgs(
        self,
        system_prompt: str,
        context_messages: list[Message],
    ) -> list[Message]:
        """构建 LLM 上下文消息列表。"""
        history_msgs: list[Message] = list(context_messages)
        return [Message(role="system", content=system_prompt)] + history_msgs

    # ======================== 原子操作：AgentState 初始化 ========================

    def _init_fresh(
        self,
        agentrun_id: str,
        session_id: str,
        agent: AgentType,
        user_message: str,
        context_messages: list[Message],
        run_messages: list[Message],
    ) -> tuple[AgentAction, AgentState]:
        """新建 AgentRun：记录 user message + 创建 AgentState + StartEvent。

        Args:
            agentrun_id: 本次 AgentRun ID
            session_id: Session ID
            agent: 执行 Agent
            user_message: 用户输入
            context_messages: 上下文消息列表（会追加 user message）
            run_messages: run 内消息列表（会追加 user message）

        Returns:
            (action, state): 下一步动作 + 新建的 AgentState
        """
        user_msg: Message = Message(role="user", content=user_message)
        self.journal.record_message(user_msg)
        context_messages.append(user_msg)
        run_messages.append(user_msg)

        state: AgentState = self._create_agent_state(agentrun_id, session_id, agent)
        action: AgentAction = state.handle_event(ASStartEvent(user_message=user_message))
        return action, state

    async def _init_resume(
        self,
        waiting_run: object,
        agentrun_id: str,
        session_id: str,
        agent: AgentType,
        user_message: str,
        context_messages: list[Message],
        run_messages: list[Message],
    ) -> tuple[AgentAction, AgentState]:
        """从 WAITING AgentRun 恢复：重建状态 + 恢复上下文 + 推 ContinueEvent。

        原子操作：
        1. 从 waiting_run.state_snapshot 重建 AgentState
        2. 从 waiting_run.messages 恢复对话历史（跳过 system）
        3. 追记 user_message（如果非空）作为审批反馈
        4. 推 ContinueEvent 恢复状态机执行

        Args:
            waiting_run: WAITING 状态的 AgentRun 记录
            agentrun_id: 本次 AgentRun ID
            session_id: Session ID
            agent: 执行 Agent
            user_message: 审批反馈文本（可为空）
            context_messages: 上下文消息列表（会追加恢复的 messages）
            run_messages: run 内消息列表（同上）

        Returns:
            (action, state): 下一步动作 + 恢复的 AgentState
        """
        # 1. 从 state_snapshot 重建 AgentState
        snapshot_data: dict = getattr(waiting_run, "state_snapshot", {}) or {}
        state: AgentState = AgentState.restore(snapshot_data)
        # restore() 不恢复 phase，需手动设置
        phase_str: str = snapshot_data.get("phase", "")
        if phase_str:
            try:
                state.phase = AgentPhase(phase_str)
            except ValueError:
                pass

        # 2. 从 messages 恢复对话历史（跳过 system，由 _build_system_prompt 重建）
        messages: list[Message] = getattr(waiting_run, "messages", []) or []
        for msg in messages:
            if msg.role == "system":
                continue
            context_messages.append(msg)
            run_messages.append(msg)

        # 3. 追记审批反馈（如果用户提供了消息）
        if user_message:
            resume_msg: Message = Message(role="user", content=user_message)
            self.journal.record_message(resume_msg)
            context_messages.append(resume_msg)
            run_messages.append(resume_msg)

        # 4. 推 ContinueEvent（WAITING_APPROVAL 等挂起阶段通过此事件恢复）
        action: AgentAction = state.handle_event(ContinueEvent())
        return action, state

    # ======================== WAIT/RESUME ========================

    async def _save_waiting_state(
        self,
        state: AgentState,
        agentrun_id: str,
        session_id: str,
        tokens_in: int,
        tokens_out: int,
        start_time: float,
        started_at: str,
        run_messages: list[Message],
    ) -> None:
        """挂起状态：持久化 AgentState + 写入 WAITING AgentRun 记录。

        Args:
            state: 当前 AgentState（处于 WAIT 阶段）
            agentrun_id: 当前 AgentRun ID
            session_id: Session ID
            tokens_in: 累计输入 tokens
            tokens_out: 累计输出 tokens
            start_time: AgentRun 开始时间
            started_at: AgentRun 开始时间（ISO 格式）
            run_messages: 当前 AgentRun 的消息列表
        """
        duration_ms: int = int((time.time() - start_time) * 1000)
        ended_at: str = datetime.now(CHINA_TZ).isoformat()

        # 持久化 AgentState 快照（AgentState → StateSnapshot → 写入）
        if self.state_store is not None:
            from .state_store import StateSnapshot
            snapshot: StateSnapshot = StateSnapshot.from_agent_state(state)
            await self.state_store.save_legacy(session_id, snapshot)

        # 写入 WAITING 状态的 AgentRun 记录
        await self._save_agent_run(
            session_id=session_id,
            agentrun_id=agentrun_id,
            agent_id=state.agent_id,
            end_status=RunEndStatus.WAITING,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_ms=duration_ms,
            started_at=started_at,
            ended_at=ended_at,
            state_snapshot=state.snapshot(),
            trace_ids=self._collect_trace_ids(),
            messages=run_messages,
        )

    # ======================== AgentState 工厂 ========================

    def _create_agent_state(
        self,
        agentrun_id: str,
        session_id: str,
        agent: AgentType,
    ) -> AgentState:
        """创建 AgentState 实例。"""
        max_iterations: int = 10
        if hasattr(agent, '_resolve_max_loop_steps'):
            max_iterations = agent._resolve_max_loop_steps(self)
        return AgentState(
            task_id=agentrun_id,
            thread_id=session_id,
            agent_id=agent.agent_id,
            max_iterations=max_iterations,
        )

    # ======================== 持久化 ========================

    async def _save_agent_run(
        self,
        session_id: str,
        agentrun_id: str,
        agent_id: str,
        end_status: RunEndStatus,
        tokens_in: int,
        tokens_out: int,
        duration_ms: int,
        started_at: str,
        ended_at: str,
        state_snapshot: dict | None = None,
        trace_ids: list[str] | None = None,
        messages: list[Message] | None = None,
    ) -> None:
        """持久化单个 AgentRun 记录。"""
        from ..session.agent_run import AgentRun
        ar: AgentRun = AgentRun(
            run_id=agentrun_id,
            agent_id=agent_id,
            end_status=end_status.value,
            state_snapshot=state_snapshot,
            trace_ids=trace_ids or [],
            trigger=TriggerType.USER_INPUT.value,
            sequence=self.journal._agentrun_sequence,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_ms=duration_ms,
            started_at=started_at,
            ended_at=ended_at,
            messages=messages or [],
        )
        # 当前 Runtime 仍是旧兼容执行器，保持其旧持久化契约；Runtime v2
        # 只会经 FileRunRepository 写入分离的运行容器。
        await self.run_mgr.save(ar, session_id)

    def _collect_trace_ids(self) -> list[str]:
        """收集当前 AgentRun 关联的 trace event IDs。"""
        return [
            f"{evt.event_type}:{evt.created_at}"
            for evt in self.journal._events
            if evt.data.get("agentrun_id") == self.journal._agentrun_id
        ]


# ============================================================================
# 辅助函数
# ============================================================================

def _build_assistant_message(resp: LLMResponse) -> Message:
    """从 LLMResponse 构建 assistant Message。"""
    return Message(
        role="assistant",
        content=resp.content or "",
        tool_calls=list(resp.tool_calls) if resp.tool_calls else None,
    )


def _agent_status_to_run_end(agent_status: AgentStatus) -> RunEndStatus:
    """AgentStatus 到 RunEndStatus 的映射。"""
    mapping: dict[AgentStatus, RunEndStatus] = {
        AgentStatus.COMPLETED: RunEndStatus.COMPLETED,
        AgentStatus.HANDOFF: RunEndStatus.HANDOFF,
        AgentStatus.FAILED: RunEndStatus.FAILED,
    }
    return mapping.get(agent_status, RunEndStatus.COMPLETED)


def _extract_handoff_target(tool_results: list[Message]) -> str:
    """从工具结果中提取 handoff 目标。"""
    for msg in tool_results:
        if msg.name == "handoff_to_agent" and msg.content:
            return msg.content
    return ""


def _dummy_session(session_id: str) -> SessionType:
    """创建一个最小 Session 占位对象，供 handoff 场景使用。"""
    from ..session.session import Session
    return Session(
        id=session_id,
        title=f"handoff-{session_id[:6]}",
        agent_id="__handoff__",
        model="",
    )


def _find_project_root() -> Path:
    """从 dotClaw 模块位置向上找到项目根目录。"""
    import dotclaw
    return Path(dotclaw.__file__).parent.parent.parent
