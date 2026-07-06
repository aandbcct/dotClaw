"""TurnLoop —— Runtime 执行层事件循环。

每轮：收到触发事件 → _step() 创建 AgentRun → AgentState 驱动完整 ReAct 循环 → 返回结果。
内部 think-act 循环由 AgentState 状态机完成，不跨 AgentRun 持久化。

handoff 时 TurnLoop 协程挂起等待子 Agent，
同一事件循环可并发运行多个 Session 的 TurnLoop。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..llm.base import Message
from ..session.agent_run import RunEndStatus, TriggerType
from .agent_state import (
    AgentState, AgentPhase, AgentAction, AgentStatus,
    AgentStartEvent as ASStartEvent,
    LLMResponseEvent as ASLLMResponseEvent,
    ToolsDoneEvent as ASToolsDoneEvent,
)

if TYPE_CHECKING:
    from .runtime import Runtime
    from .state_store import StateStore
    from ..agent.agent import Agent as AgentType, LLMResponse
    from ..tools.base import ToolDefinition
    from ..journal.journal import Journal
    from ..channel.base import Channel


@dataclass
class TriggerEvent:
    """外部触发事件。"""
    trigger_type: TriggerType
    data: object


class TurnLoop:
    """Runtime 执行层事件循环。

    职责：
    - run_forever() 等待外部触发（用户输入 / handoff 流转）
    - _step() 执行一次完整 AgentRun（多轮 think-act）
    - handoff 时协程挂起，不阻塞其他 Session
    """

    def __init__(
        self,
        session_id: str,
        agent: AgentType,
        runtime: Runtime,
        state_store: StateStore,
        journal: Journal,
        channel: Channel | None = None,
    ) -> None:
        self._session_id: str = session_id
        self._agent: AgentType = agent
        self._runtime: Runtime = runtime
        self._state_store: StateStore = state_store
        self._journal: Journal = journal
        self._channel: Channel | None = channel

        self._queue: asyncio.Queue[TriggerEvent] = asyncio.Queue()
        self._active: bool = False
        self._context_messages: list[Message] = []
        self._run_ids: list[str] = []

    # ======================== 公开入口 ========================

    async def push_trigger(self, event: TriggerEvent) -> None:
        """推送外部触发到事件队列。handoff 返回时使用。"""
        if not self._active:
            return
        await self._queue.put(event)

    async def run_forever(self, initial_message: str) -> str:
        """启动事件循环。

        原子操作：
        1. 推送初始 USER_INPUT
        2. 阻塞等待触发事件
        3. 执行 _step() 完成一轮对话
        4. 返回最终回复

        注意：不调用 journal.finalize()，由调用方管理 Journal 生命周期。
        """
        self._active = True
        final_response: str = ""

        await self._queue.put(TriggerEvent(
            trigger_type=TriggerType.USER_INPUT,
            data=initial_message,
        ))

        try:
            while self._active:
                trigger: TriggerEvent = await self._queue.get()
                user_message: str = str(trigger.data) if trigger.data else ""

                final_response = await self._step(user_message)

                # 只要不是 handoff 场景，就退出循环
                # 注意：不能用 if final_response: 判空 —— _step() 异常时
                # 返回 ""，会导致 queue.get() 永久阻塞
                self._active = False
        except Exception:
            self._active = False
            raise

        return final_response

    async def stop(self) -> None:
        """优雅关闭。"""
        self._active = False

    @property
    def run_ids(self) -> list[str]:
        """本次 conversation 中产生的所有 AgentRun ID。"""
        return list(self._run_ids)

    # ======================== 单次 AgentRun 执行 ========================

    async def _step(self, user_message: str) -> str:
        """执行一次完整 AgentRun（一次用户一问一答）。

        AgentState 驱动内部多轮 think-act：
        IDLE → THINKING → ACTING → THINKING → ... → RESPONDING → DONE

        原子操作：
        1. 创建 AgentRun + agentrun_start
        2. 记录 user message + system_prompt
        3. AgentState.handle_event(StartEvent) → INVOKE_LLM
        4. 状态机循环：
           INVOKE_LLM  → 调用 LLM → 记录 → LLMResponseEvent → next action
           EXECUTE_TOOLS → 执行工具 → 记录 → ToolsDoneEvent → next action
           FINALIZE → 结束
           HANDOFF_TARGET → 执行 handoff → 挂起等待 → 结束
        5. agentrun_end + 持久化
        """
        agentrun_id: str = uuid.uuid4().hex[:8]
        self._run_ids.append(agentrun_id)
        run_messages: list[Message] = []
        self._journal.agentrun_start(agentrun_id, TriggerType.USER_INPUT.value)

        started_at: str = datetime.now(timezone.utc).isoformat()
        start_time: float = time.time()
        tokens_in_total: int = 0
        tokens_out_total: int = 0

        # 记录 user message
        user_msg: Message = Message(role="user", content=user_message)
        self._journal.record_message(user_msg)
        self._context_messages.append(user_msg)
        run_messages.append(user_msg)

        # system_prompt 仅记录到 AgentRun.messages
        system_prompt: str = await self._build_system_prompt(user_message)
        run_messages.append(Message(role="system", content=system_prompt))

        # 创建 AgentState → StartEvent → INVOKE_LLM
        state: AgentState = self._create_agent_state(agentrun_id)
        action: AgentAction = state.handle_event(ASStartEvent(user_message=user_message))

        final_answer: str = ""
        end_status: RunEndStatus = RunEndStatus.COMPLETED

        try:
            while not state.is_terminal:
                if action == AgentAction.INVOKE_LLM:
                    context_msgs: list[Message] = self._build_context_msgs(system_prompt)
                    resp: LLMResponse = await self._invoke_llm(context_msgs)
                    tokens_in_total += resp.input_tokens
                    tokens_out_total += resp.output_tokens

                    asst_msg: Message = _build_assistant_message(resp)
                    self._journal.record_message(asst_msg)
                    self._context_messages.append(asst_msg)
                    run_messages.append(asst_msg)

                    action = state.handle_event(ASLLMResponseEvent(response=resp))

                elif action == AgentAction.EXECUTE_TOOLS:
                    tool_msgs: list[Message] = await self._execute_tools_for_state(state)
                    for tm in tool_msgs:
                        self._journal.record_message(tm)
                        self._context_messages.append(tm)
                        run_messages.append(tm)

                    action = state.handle_event(ASToolsDoneEvent(results=tool_msgs))

                elif action == AgentAction.WAIT:
                    # Should never reach here in normal flow.
                    # TRUNCATED auto-transitions to DONE → FINALIZE via _transition().
                    continue

                else:
                    # FINALIZE / HANDOFF_TARGET / unexpected → exit loop.
                    # AgentState may have auto-transitioned to DONE during
                    # handle_event(), making is_terminal=True. We process
                    # FINALIZE/HANDOFF outside the loop below.
                    break

            # ── 处理循环退出后的终态动作 ──
            # handle_event() 内部 _transition() 会自动将 RESPONDING/TRUNCATED/
            # HANDOFF 转为 DONE 并返回 FINALIZE/HANDOFF_TARGET。
            # 由于 elif 链的短路特性，这些终态动作需要在循环外处理。
            if action == AgentAction.FINALIZE:
                if state.current_llm_response is not None:
                    final_answer = state.current_llm_response.content or ""
                end_status = _agent_status_to_run_end(state.end_status)
            elif action == AgentAction.HANDOFF_TARGET:
                await self._handle_handoff_for_state(state, agentrun_id)
                end_status = RunEndStatus.HANDOFF

        except Exception as e:
            end_status = RunEndStatus.FAILED
            final_answer = f"[执行异常] {type(e).__name__}: {e}"
            self._journal.error("ERROR", "turnloop.step", f"{type(e).__name__}: {e}")

        finally:
            duration_ms: int = int((time.time() - start_time) * 1000)
            ended_at: str = datetime.now(timezone.utc).isoformat()
            self._journal.agentrun_end(end_status.value)

            try:
                await self._save_agent_run(
                    agentrun_id=agentrun_id,
                    agent_id=self._agent.agent_id,
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

    async def _invoke_llm(self, context_msgs: list[Message]) -> LLMResponse:
        """调用 LLM，返回 LLMResponse。"""
        from ..agent.agent import LLMResponse as LR

        model: str = self._agent._resolve_model(self._runtime)
        tool_definitions: list[ToolDefinition] = self._agent._resolve_tool_definitions(self._runtime)

        self._journal.prompt_built(
            message_count=len(context_msgs),
            context_length=sum(len(str(m.content or "")) for m in context_msgs),
            system_prompt="",
            tool_count=len(tool_definitions),
        )
        self._journal.llm_call_start(attempt=1)

        current_content: str = ""
        tool_calls: list[object] = []
        finish_reason: str = "stop"
        input_tokens: int = 0
        output_tokens: int = 0
        stream_enabled: bool = (
            self._runtime.config.llm.stream
            if self._runtime.config is not None else False
        )

        async for chunk in self._runtime.llm.chat(
            messages=context_msgs,
            tools=tool_definitions if tool_definitions else None,
            model=model,
            purpose="chat",
            stream=stream_enabled,
        ):
            if chunk.content:
                current_content += chunk.content
                if self._channel is not None:
                    await self._channel.stream(chunk.content)
            if chunk.tool_call:
                tool_calls.append(chunk.tool_call)
            if chunk.is_final:
                finish_reason = chunk.finish_reason or "stop"
                input_tokens = getattr(chunk, "input_tokens", 0)
                output_tokens = getattr(chunk, "output_tokens", len(current_content))
                break

        self._journal.llm_response_end(
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

    async def _execute_tools_for_state(self, state: AgentState) -> list[Message]:
        """执行 AgentState 中当前待执行的工具调用。

        Args:
            state: 当前 AgentState（phase=ACTING，current_llm_response 含 tool_calls）

        Returns:
            role="tool" 的 Message 列表

        Raises:
            RuntimeError: 工具执行器未初始化
        """
        llm_resp: LLMResponse | None = state.current_llm_response
        if llm_resp is None or not llm_resp.tool_calls:
            return []
        if self._runtime.tool_executor is None:
            raise RuntimeError("工具执行器未初始化")
        return await self._runtime._execute_tools(list(llm_resp.tool_calls))

    # ======================== Handoff ========================

    async def _handle_handoff_for_state(self, state: AgentState, parent_run_id: str) -> None:
        """执行 handoff：创建子 TurnLoop，协程挂起等待完成。

        AgentState 指示 HANDOFF_TARGET 时调用。
        子 TurnLoop 的 run_forever() 挂起当前协程，不阻塞事件循环。
        """
        handoff_target: str = state.handoff_target or _extract_handoff_target(state.current_tool_results)
        if not handoff_target:
            return

        await self._runtime._handle_handoff(
            thread_id=self._session_id,
            target_agent_id=handoff_target,
            context=state.handoff_context or "",
            parent_run_id=parent_run_id,
        )

    # ======================== System Prompt / Context ========================

    async def _build_system_prompt(self, user_message: str) -> str:
        """构建 system prompt（含 Slot 解析）。"""
        system_prompt: str = self._agent._resolve_system_prompt(self._runtime)
        tool_definitions: list[ToolDefinition] = self._agent._resolve_tool_definitions(self._runtime)
        from ..agent.slotContext import SlotContext as SCtx

        project_root: Path = self._runtime._find_project_root() if hasattr(
            self._runtime, '_find_project_root') else Path.cwd()

        slot_ctx: SCtx = SCtx(
            query=user_message,
            request_id=uuid.uuid4().hex[:8],
            session_id=self._session_id,
            project_root=project_root,
            max_context_tokens=8000 if self._runtime.config is None
                else self._runtime.config.agent.max_context_tokens,
            system_prompt=system_prompt,
            tool_definitions=tool_definitions,
            skill_registry=self._runtime.skill_registry,
            memory_manager=getattr(self._runtime, 'memory_mgr', None),
            agent_registry=self._runtime.agent_registry,
            knowledge_base=None,
            user_profile=None,
            journal=self._journal,
        )
        if self._runtime.assembler is not None:
            self._runtime.assembler.on_new_request()
            system_prompt = await self._runtime.assembler.build_system_prompt(slot_ctx)
        return system_prompt

    def _build_context_msgs(self, system_prompt: str) -> list[Message]:
        """构建 LLM 上下文消息列表。"""
        history_msgs: list[Message] = list(self._context_messages)
        return [Message(role="system", content=system_prompt)] + history_msgs

    # ======================== AgentState 工厂 ========================

    def _create_agent_state(self, agentrun_id: str) -> AgentState:
        """创建 AgentState 实例。"""
        max_iterations: int = 10
        if hasattr(self._agent, '_resolve_max_loop_steps'):
            max_iterations = self._agent._resolve_max_loop_steps(self._runtime)
        return AgentState(
            task_id=agentrun_id,
            thread_id=self._session_id,
            agent_id=self._agent.agent_id,
            max_iterations=max_iterations,
        )

    # ======================== 持久化 ========================

    async def _save_agent_run(
        self,
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
            sequence=self._journal._agentrun_sequence,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_ms=duration_ms,
            started_at=started_at,
            ended_at=ended_at,
            messages=messages or [],
        )
        await self._runtime.run_mgr.save(ar, self._session_id)

    def _collect_trace_ids(self) -> list[str]:
        """收集当前 AgentRun 关联的 trace event IDs。"""
        return [
            f"{evt.event_type}:{evt.created_at}"
            for evt in self._journal._events
            if evt.data.get("agentrun_id") == self._journal._agentrun_id
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
