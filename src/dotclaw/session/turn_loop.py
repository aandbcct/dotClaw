"""TurnLoop —— Session 级持久事件循环。

TurnLoop 是 Session 和 AgentRun 之间的事件循环层：
- Session：宏观会话容器
- TurnLoop：per-Session 后台协程，监听触发源，驱动 AgentRun
- AgentRun：TurnLoop 内的一次原子 LLM 执行

TurnLoop 使用 asyncio.Queue 实现事件驱动模式。
触发源包括：USER_INPUT / TOOL_RESULT / RESUME。

控制中枢：AgentState 状态机。
TurnLoop 不直接判断 "有 tool_call 就执行工具"，
而是把事件喂给 AgentState.handle_event()，由状态机返回 AgentAction：
    INVOKE_LLM → EXECUTE_TOOLS → INVOKE_LLM → ... → FINALIZE / HANDOFF_TARGET

内部逻辑（原子操作封装为方法）：
1. run_forever() — 主事件循环入口
2. push_trigger() — 向事件队列推送触发事件
3. _step() — 单次 AgentRun 执行（AgentState 驱动完整 ReAct 循环）
4. _build_system_prompt() — 构建 system prompt（含 Slot 解析）
5. _build_context_msgs() — 组装 LLM 上下文消息
6. _invoke_llm() — 调用 LLM 返回 LLMResponse
7. _save_agent_run() — 持久化 AgentRun 记录
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..journal.events import EventType
from ..llm.base import Message
from ..session.agent_run import RunEndStatus, TriggerType
from ..runtime.agent_state import (
    AgentState, AgentPhase, AgentAction, AgentStatus,
    AgentStartEvent as ASStartEvent,
    LLMResponseEvent as ASLLMResponseEvent,
    ToolsDoneEvent as ASToolsDoneEvent,
)

if TYPE_CHECKING:
    from ..runtime.runtime import Runtime
    from ..runtime.state_store import StateStore, StateSnapshot
    from ..agent.agent import Agent as AgentType, LLMResponse
    from ..tools.base import ToolDefinition
    from ..journal.journal import Journal
    from ..channel.base import Channel


# ============================================================================
# 触发事件
# ============================================================================

@dataclass
class TriggerEvent:
    """TurnLoop 的触发事件。"""

    trigger_type: TriggerType
    data: object = None
    agent_id: str = ""


# ============================================================================
# TurnLoop
# ============================================================================

class TurnLoop:
    """Session 级持久事件循环。

    控制流：TurnLoop 等待触发 → AgentState 驱动 → Runtime 执行原子方法。
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
        if not self._active:
            return
        await self._queue.put(event)

    async def run_forever(self, initial_message: str) -> str:
        self._active = True
        final_response: str = ""

        await self._queue.put(TriggerEvent(
            trigger_type=TriggerType.USER_INPUT,
            data=initial_message,
        ))

        try:
            while self._active:
                trigger: TriggerEvent = await self._queue.get()
                final_response = await self._step(
                    user_message=str(trigger.data) if trigger.data else "",
                    trigger=trigger.trigger_type,
                    tool_results=trigger.data
                        if trigger.trigger_type == TriggerType.TOOL_RESULT
                        else None,
                )
                if final_response:
                    self._active = False
        except Exception:
            self._active = False
            raise
        finally:
            self._journal.finalize()

        return final_response

    async def stop(self) -> None:
        self._active = False

    @property
    def run_ids(self) -> list[str]:
        return list(self._run_ids)

    # ======================== 原子操作：单步执行 ========================

    async def _step(
        self,
        user_message: str,
        trigger: TriggerType,
        tool_results: object = None,
    ) -> str:
        """AgentState 驱动的单次 AgentRun 执行。

        控制流：
        1. 创建 AgentRun + agentrun_start
        2. 记录触发消息到 trace
        3. 构建 system_prompt（仅 AgentRun.messages）
        4. AgentState.handle_event(StartEvent) → INVOKE_LLM
        5. 循环：INVOKE_LLM → LLMResponseEvent → next action
                 EXECUTE_TOOLS → ToolsDoneEvent → next action
                 FINALIZE → 构建结果返回
                 HANDOFF_TARGET → 执行 handoff
        6. agentrun_end + 持久化 AgentRun
        """
        agentrun_id: str = uuid.uuid4().hex[:8]
        self._run_ids.append(agentrun_id)
        run_messages: list[Message] = []
        self._journal.agentrun_start(agentrun_id, trigger.value)

        started_at: str = datetime.now(timezone.utc).isoformat()
        start_time: float = time.time()
        tokens_in_total: int = 0
        tokens_out_total: int = 0

        # 1. 记录触发消息
        if trigger == TriggerType.USER_INPUT and user_message:
            user_msg: Message = Message(role="user", content=user_message)
            self._journal.record_message(user_msg)
            self._context_messages.append(user_msg)
            run_messages.append(user_msg)
        elif trigger == TriggerType.TOOL_RESULT and tool_results is not None:
            tool_list: list[Message] = _ensure_list(tool_results)
            for tr in tool_list:
                self._journal.record_message(tr)
                self._context_messages.append(tr)
                run_messages.append(tr)

        # 2. system_prompt（仅 AgentRun.messages，不 trace）
        system_prompt: str = await self._build_system_prompt(user_message)
        run_messages.append(Message(role="system", content=system_prompt))

        # 3. 创建 AgentState → 启动
        max_iterations: int = 10
        if hasattr(self._agent, '_resolve_max_loop_steps'):
            max_iterations = self._agent._resolve_max_loop_steps(self._runtime)

        state: AgentState = AgentState(
            task_id=agentrun_id,
            thread_id=self._session_id,
            agent_id=self._agent.agent_id,
            max_iterations=max_iterations,
        )
        action: AgentAction = state.handle_event(ASStartEvent(user_message=user_message))

        # 4. AgentState 驱动的执行循环
        final_answer: str = ""
        end_status_str: str = RunEndStatus.COMPLETED.value

        try:
            while not state.is_terminal:
                if action == AgentAction.INVOKE_LLM:
                    context_msgs: list[Message] = await self._build_context_msgs(
                        user_message="",
                        system_prompt=system_prompt,
                    )
                    resp: LLMResponse = await self._invoke_llm(
                        agentrun_id=agentrun_id,
                        context_msgs=context_msgs,
                    )
                    tokens_in_total += resp.input_tokens
                    tokens_out_total += resp.output_tokens

                    asst_msg: Message = Message(
                        role="assistant",
                        content=resp.content or "",
                        tool_calls=list(resp.tool_calls) if resp.tool_calls else None,
                    )
                    self._journal.record_message(asst_msg)
                    self._context_messages.append(asst_msg)
                    run_messages.append(asst_msg)

                    action = state.handle_event(ASLLMResponseEvent(response=resp))

                elif action == AgentAction.EXECUTE_TOOLS:
                    llm_resp: LLMResponse | None = state.current_llm_response
                    if llm_resp is None or not llm_resp.tool_calls:
                        action = AgentAction.FINALIZE
                        continue

                    if self._runtime.tool_executor is None:
                        raise RuntimeError("工具执行器未初始化")

                    tool_msgs: list[Message] = await self._runtime._execute_tools(
                        list(llm_resp.tool_calls),
                    )
                    for tm in tool_msgs:
                        self._journal.record_message(tm)
                        self._context_messages.append(tm)
                        run_messages.append(tm)

                    done_event: ASToolsDoneEvent = ASToolsDoneEvent(results=tool_msgs)
                    action = state.handle_event(done_event)

                elif action == AgentAction.FINALIZE:
                    if state.current_llm_response is not None:
                        final_answer = state.current_llm_response.content or ""
                    if state.end_status == AgentStatus.HANDOFF:
                        end_status_str = RunEndStatus.HANDOFF.value
                    elif state.end_status == AgentStatus.FAILED:
                        end_status_str = RunEndStatus.FAILED.value
                    else:
                        end_status_str = RunEndStatus.COMPLETED.value
                    break

                elif action == AgentAction.HANDOFF_TARGET:
                    handoff_target: str = state.handoff_target or ""
                    if not handoff_target:
                        handoff_target = _extract_handoff_target(state.current_tool_results)

                    await self._runtime._handle_handoff(
                        thread_id=self._session_id,
                        target_agent_id=handoff_target,
                        context=state.handoff_context or "",
                        parent_run_id=agentrun_id,
                    )
                    end_status_str = RunEndStatus.HANDOFF.value
                    break

                elif action == AgentAction.WAIT:
                    # 跨状态自动转换，不执行实际操作
                    pass

                else:
                    break

        except Exception as e:
            end_status_str = RunEndStatus.FAILED.value
            self._journal.error("ERROR", "turnloop.step", f"{type(e).__name__}: {e}")
            if not state.is_terminal:
                state.end_status = AgentStatus.FAILED
                state.error_message = str(e)
        finally:
            # 5. 结束 AgentRun
            duration_ms: int = int((time.time() - start_time) * 1000)
            ended_at: str = datetime.now(timezone.utc).isoformat()
            self._journal.agentrun_end(end_status_str)

            state_snapshot: dict | None = state.snapshot() if state is not None else None
            await self._save_agent_run(
                agentrun_id=agentrun_id,
                agent_id=self._agent.agent_id,
                end_status=end_status_str,
                trigger=trigger.value,
                tokens_in=tokens_in_total,
                tokens_out=tokens_out_total,
                duration_ms=duration_ms,
                started_at=started_at,
                ended_at=ended_at,
                state_snapshot=state_snapshot,
                trace_ids=self._collect_trace_ids(),
                error=state.error_message if state.error_message else None,
                messages=run_messages,
            )

        return final_answer

    # ======================== 原子操作：System Prompt ========================

    async def _build_system_prompt(self, user_message: str) -> str:
        system_prompt: str = self._agent._resolve_system_prompt(self._runtime)
        tool_definitions: list[ToolDefinition] = self._agent._resolve_tool_definitions(
            self._runtime
        )
        from ..agent.slotContext import SlotContext as SCtx

        project_root: Path = self._runtime._find_project_root() if hasattr(
            self._runtime, '_find_project_root'
        ) else Path.cwd()

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

    # ======================== 原子操作：上下文消息 ========================

    async def _build_context_msgs(
        self,
        user_message: str,
        system_prompt: str,
    ) -> list[Message]:
        history_msgs: list[Message] = list(self._context_messages)
        if user_message:
            return self._runtime._build_messages(
                user_input=user_message,
                system_prompt=system_prompt,
                history=history_msgs,
            )
        else:
            return [Message(role="system", content=system_prompt)] + history_msgs

    # ======================== 原子操作：LLM 调用 ========================

    async def _invoke_llm(
        self,
        agentrun_id: str,
        context_msgs: list[Message],
    ) -> LLMResponse:
        from ..agent.agent import LLMResponse as LR

        model: str = self._agent._resolve_model(self._runtime)
        tool_definitions: list[ToolDefinition] = self._agent._resolve_tool_definitions(
            self._runtime
        )

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
            if self._runtime.config is not None
            else False
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

    # ======================== 原子操作：持久化 ========================

    async def _save_agent_run(
        self,
        agentrun_id: str,
        agent_id: str,
        end_status: str,
        trigger: str,
        tokens_in: int,
        tokens_out: int,
        duration_ms: int,
        started_at: str,
        ended_at: str,
        state_snapshot: dict | None = None,
        trace_ids: list[str] | None = None,
        error: str | None = None,
        messages: list[Message] | None = None,
    ) -> None:
        from ..session.agent_run import AgentRun
        ar: AgentRun = AgentRun(
            run_id=agentrun_id,
            agent_id=agent_id,
            end_status=end_status,
            state_snapshot=state_snapshot,
            trace_ids=trace_ids or [],
            trigger=trigger,
            sequence=self._journal._agentrun_sequence,
            tool_calls=0,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_ms=duration_ms,
            error=error,
            started_at=started_at,
            ended_at=ended_at,
            messages=messages or [],
        )
        await self._runtime.run_mgr.save(ar, self._session_id)

    def _collect_trace_ids(self) -> list[str]:
        return [
            f"{evt.event_type}:{evt.created_at}"
            for evt in self._journal._events
            if evt.data.get("agentrun_id") == self._journal._agentrun_id
        ]


# ============================================================================
# 辅助函数
# ============================================================================

def _ensure_list(obj: object) -> list[Message]:
    if isinstance(obj, list):
        return obj  # type: ignore[return-value]
    return []


def _extract_handoff_target(tool_results: list[Message]) -> str:
    for msg in tool_results:
        if msg.name == "handoff_to_agent" and msg.content:
            return msg.content
    return ""
