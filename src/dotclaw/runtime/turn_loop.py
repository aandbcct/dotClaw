"""TurnLoop —— Runtime 执行层事件循环。

每个 AgentRun = 一次状态机转换（one event → one action）。
工具调用时 AgentRun 以 TOOL_WAIT 结束，AgentState 快照持久化；
工具结果通过事件队列推回，新 AgentRun 恢复状态继续。

控制中枢：AgentState 状态机跨 AgentRun 持久化。
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
from .agent_state import (
    AgentState, AgentPhase, AgentAction, AgentStatus,
    AgentStartEvent as ASStartEvent,
    LLMResponseEvent as ASLLMResponseEvent,
    ToolsDoneEvent as ASToolsDoneEvent,
)

if TYPE_CHECKING:
    from .runtime import Runtime
    from .state_store import StateStore, StateSnapshot
    from ..agent.agent import Agent as AgentType, LLMResponse
    from ..tools.base import ToolDefinition
    from ..journal.journal import Journal
    from ..channel.base import Channel


@dataclass
class TriggerEvent:
    trigger_type: TriggerType
    data: object = None
    agent_id: str = ""


class TurnLoop:
    """Runtime 执行层事件循环。

    核心原则：工具调用时 AgentRun 结束（TOOL_WAIT），AgentState 跨 AgentRun 持久化。
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

        # 跨 AgentRun 持久化的 AgentState（TOOL_WAIT 时保存，TOOL_RESULT 时恢复）
        self._pending_state: AgentState | None = None

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

                if trigger.trigger_type == TriggerType.USER_INPUT:
                    final_response = await self._step_user_input(
                        user_message=str(trigger.data) if trigger.data else "",
                    )
                elif trigger.trigger_type == TriggerType.TOOL_RESULT:
                    final_response = await self._step_tool_result(
                        tool_results=trigger.data,
                    )
                else:
                    final_response = await self._step_user_input(
                        user_message=str(trigger.data) if trigger.data else "",
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

    # ======================== USER_INPUT AgentRun ========================

    async def _step_user_input(self, user_message: str) -> str:
        """用户输入触发的 AgentRun。

        AgentState IDLE → StartEvent → INVOKE_LLM → call LLM:
          - 文本回复 → FINALIZE → 返回最终文本
          - 工具调用 → EXECUTE_TOOLS → 保存状态 → TOOL_WAIT → 执行工具 → 推 TOOL_RESULT
        """
        agentrun_id: str = uuid.uuid4().hex[:8]
        self._run_ids.append(agentrun_id)
        run_messages: list[Message] = []
        self._journal.agentrun_start(agentrun_id, TriggerType.USER_INPUT.value)

        started_at: str = datetime.now(timezone.utc).isoformat()
        start_time: float = time.time()

        # 记录 user message
        user_msg: Message = Message(role="user", content=user_message)
        self._journal.record_message(user_msg)
        self._context_messages.append(user_msg)
        run_messages.append(user_msg)

        # system_prompt（仅 AgentRun，不 trace）
        system_prompt: str = await self._build_system_prompt(user_message)
        run_messages.append(Message(role="system", content=system_prompt))

        # AgentState 创建 → StartEvent → INVOKE_LLM
        state: AgentState = self._create_agent_state(agentrun_id)
        action: AgentAction = state.handle_event(ASStartEvent(user_message=user_message))

        # 调用 LLM
        context_msgs: list[Message] = await self._build_context_msgs(
            user_message=user_message,
            system_prompt=system_prompt,
        )
        resp: LLMResponse = await self._invoke_llm(agentrun_id, context_msgs)

        asst_msg: Message = Message(
            role="assistant",
            content=resp.content or "",
            tool_calls=list(resp.tool_calls) if resp.tool_calls else None,
        )
        self._journal.record_message(asst_msg)
        self._context_messages.append(asst_msg)
        run_messages.append(asst_msg)

        # LLMResponseEvent → FINALIZE or EXECUTE_TOOLS
        action = state.handle_event(ASLLMResponseEvent(response=resp))

        duration_ms: int = int((time.time() - start_time) * 1000)
        ended_at: str = datetime.now(timezone.utc).isoformat()

        if action == AgentAction.FINALIZE:
            return await self._finalize_run(
                agentrun_id=agentrun_id,
                state=state,
                run_messages=run_messages,
                end_status=RunEndStatus.COMPLETED,
                tokens_in=resp.input_tokens,
                tokens_out=resp.output_tokens,
                duration_ms=duration_ms,
                started_at=started_at,
                ended_at=ended_at,
                trigger=TriggerType.USER_INPUT.value,
                final_answer=resp.content or "",
            )

        elif action == AgentAction.EXECUTE_TOOLS:
            snapshot: dict = state.snapshot()
            self._pending_state = state
            await self._save_state_snapshot(snapshot)

            self._journal.agentrun_end(RunEndStatus.TOOL_WAIT.value)
            await self._save_agent_run(
                agentrun_id=agentrun_id,
                agent_id=self._agent.agent_id,
                end_status=RunEndStatus.TOOL_WAIT.value,
                trigger=TriggerType.USER_INPUT.value,
                tokens_in=resp.input_tokens,
                tokens_out=resp.output_tokens,
                duration_ms=duration_ms,
                started_at=started_at,
                ended_at=ended_at,
                state_snapshot=snapshot,
                trace_ids=self._collect_trace_ids(),
                messages=run_messages,
            )

            tool_msgs: list[Message] = await self._execute_current_tools(state)
            await self._queue.put(TriggerEvent(
                trigger_type=TriggerType.TOOL_RESULT,
                data=tool_msgs,
            ))

            return ""

        else:
            return ""

    # ======================== TOOL_RESULT AgentRun ========================

    async def _step_tool_result(self, tool_results: object) -> str:
        """工具结果触发的 AgentRun。

        恢复 AgentState（phase=ACTING）→ ToolsDoneEvent → INVOKE_LLM / FINALIZE / HANDOFF_TARGET。
        如果 INVOKE_LLM 后又遇到工具调用，重复 TOOL_WAIT → push 循环。
        """
        agentrun_id: str = uuid.uuid4().hex[:8]
        self._run_ids.append(agentrun_id)
        run_messages: list[Message] = []
        self._journal.agentrun_start(agentrun_id, TriggerType.TOOL_RESULT.value)

        started_at: str = datetime.now(timezone.utc).isoformat()
        start_time: float = time.time()

        state: AgentState = self._restore_or_create_state(agentrun_id)
        if state is None:
            return ""

        tool_list: list[Message] = _ensure_list(tool_results)
        for tr in tool_list:
            self._journal.record_message(tr)
            self._context_messages.append(tr)
            run_messages.append(tr)

        system_prompt: str = await self._build_system_prompt("")
        run_messages.append(Message(role="system", content=system_prompt))

        done_event: ASToolsDoneEvent = ASToolsDoneEvent(results=tool_list)
        action: AgentAction = state.handle_event(done_event)

        tokens_in_total: int = 0
        tokens_out_total: int = 0

        if action == AgentAction.INVOKE_LLM:
            context_msgs: list[Message] = await self._build_context_msgs(
                user_message="",
                system_prompt=system_prompt,
            )
            resp: LLMResponse = await self._invoke_llm(agentrun_id, context_msgs)
            tokens_in_total = resp.input_tokens
            tokens_out_total = resp.output_tokens

            asst_msg: Message = Message(
                role="assistant",
                content=resp.content or "",
                tool_calls=list(resp.tool_calls) if resp.tool_calls else None,
            )
            self._journal.record_message(asst_msg)
            self._context_messages.append(asst_msg)
            run_messages.append(asst_msg)

            action = state.handle_event(ASLLMResponseEvent(response=resp))

        duration_ms: int = int((time.time() - start_time) * 1000)
        ended_at: str = datetime.now(timezone.utc).isoformat()

        if action == AgentAction.FINALIZE:
            return await self._finalize_run(
                agentrun_id=agentrun_id, state=state, run_messages=run_messages,
                end_status=RunEndStatus.COMPLETED,
                tokens_in=tokens_in_total, tokens_out=tokens_out_total,
                duration_ms=duration_ms, started_at=started_at, ended_at=ended_at,
                trigger=TriggerType.TOOL_RESULT.value,
                final_answer=(state.current_llm_response and state.current_llm_response.content) or "",
            )

        elif action == AgentAction.EXECUTE_TOOLS:
            snapshot: dict = state.snapshot()
            self._pending_state = state
            await self._save_state_snapshot(snapshot)

            self._journal.agentrun_end(RunEndStatus.TOOL_WAIT.value)
            await self._save_agent_run(
                agentrun_id=agentrun_id, agent_id=self._agent.agent_id,
                end_status=RunEndStatus.TOOL_WAIT.value,
                trigger=TriggerType.TOOL_RESULT.value,
                tokens_in=tokens_in_total, tokens_out=tokens_out_total,
                duration_ms=duration_ms, started_at=started_at, ended_at=ended_at,
                state_snapshot=snapshot,
                trace_ids=self._collect_trace_ids(),
                messages=run_messages,
            )

            tool_msgs: list[Message] = await self._execute_current_tools(state)
            await self._queue.put(TriggerEvent(
                trigger_type=TriggerType.TOOL_RESULT,
                data=tool_msgs,
            ))
            return ""

        elif action == AgentAction.HANDOFF_TARGET:
            handoff_target: str = state.handoff_target or _extract_handoff_target(state.current_tool_results)
            await self._runtime._handle_handoff(
                thread_id=self._session_id,
                target_agent_id=handoff_target,
                context=state.handoff_context or "",
                parent_run_id=agentrun_id,
            )
            return await self._finalize_run(
                agentrun_id=agentrun_id, state=state, run_messages=run_messages,
                end_status=RunEndStatus.HANDOFF,
                tokens_in=tokens_in_total, tokens_out=tokens_out_total,
                duration_ms=duration_ms, started_at=started_at, ended_at=ended_at,
                trigger=TriggerType.TOOL_RESULT.value,
                final_answer="",
            )

        return ""

    # ======================== 原子操作：结束 AgentRun ========================

    async def _finalize_run(
        self,
        agentrun_id: str,
        state: AgentState,
        run_messages: list[Message],
        end_status: RunEndStatus,
        tokens_in: int,
        tokens_out: int,
        duration_ms: int,
        started_at: str,
        ended_at: str,
        trigger: str,
        final_answer: str,
    ) -> str:
        self._pending_state = None
        self._journal.agentrun_end(end_status.value)

        await self._save_agent_run(
            agentrun_id=agentrun_id, agent_id=self._agent.agent_id,
            end_status=end_status.value, trigger=trigger,
            tokens_in=tokens_in, tokens_out=tokens_out,
            duration_ms=duration_ms, started_at=started_at, ended_at=ended_at,
            state_snapshot=state.snapshot(),
            trace_ids=self._collect_trace_ids(),
            messages=run_messages,
        )
        return final_answer

    # ======================== AgentState 管理 ========================

    def _create_agent_state(self, agentrun_id: str) -> AgentState:
        max_iterations: int = 10
        if hasattr(self._agent, '_resolve_max_loop_steps'):
            max_iterations = self._agent._resolve_max_loop_steps(self._runtime)
        return AgentState(
            task_id=agentrun_id,
            thread_id=self._session_id,
            agent_id=self._agent.agent_id,
            max_iterations=max_iterations,
        )

    def _restore_or_create_state(self, agentrun_id: str) -> AgentState | None:
        if self._pending_state is not None:
            return self._pending_state
        return self._create_agent_state(agentrun_id)

    async def _save_state_snapshot(self, snapshot: dict) -> None:
        try:
            from .state_store import StateSnapshot
            s: StateSnapshot = StateSnapshot.from_dict(snapshot)
            await self._state_store.save(self._session_id, s)
        except Exception:
            pass

    # ======================== 工具执行 ========================

    async def _execute_current_tools(self, state: AgentState) -> list[Message]:
        llm_resp: LLMResponse | None = state.current_llm_response
        if llm_resp is None or not llm_resp.tool_calls:
            return []
        if self._runtime.tool_executor is None:
            raise RuntimeError("工具执行器未初始化")
        return await self._runtime._execute_tools(list(llm_resp.tool_calls))

    # ======================== System Prompt / Context ========================

    async def _build_system_prompt(self, user_message: str) -> str:
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

    async def _build_context_msgs(
        self, user_message: str, system_prompt: str,
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

    # ======================== LLM 调用 ========================

    async def _invoke_llm(
        self, agentrun_id: str, context_msgs: list[Message],
    ) -> LLMResponse:
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
            input_tokens=input_tokens, output_tokens=output_tokens,
            tps=(output_tokens / 1.0) if output_tokens > 0 else 0.0,
            status="success", stop_reason=finish_reason,
        )
        return LR(
            content=current_content, tool_calls=tool_calls, finish_reason=finish_reason,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )

    # ======================== 持久化 ========================

    async def _save_agent_run(
        self,
        agentrun_id: str, agent_id: str,
        end_status: str, trigger: str,
        tokens_in: int, tokens_out: int,
        duration_ms: int, started_at: str, ended_at: str,
        state_snapshot: dict | None = None,
        trace_ids: list[str] | None = None,
        error: str | None = None,
        messages: list[Message] | None = None,
    ) -> None:
        from ..session.agent_run import AgentRun
        ar: AgentRun = AgentRun(
            run_id=agentrun_id, agent_id=agent_id,
            end_status=end_status,
            state_snapshot=state_snapshot,
            trace_ids=trace_ids or [],
            trigger=trigger,
            sequence=self._journal._agentrun_sequence,
            tool_calls=0,
            tokens_in=tokens_in, tokens_out=tokens_out,
            duration_ms=duration_ms,
            error=error,
            started_at=started_at, ended_at=ended_at,
            messages=messages or [],
        )
        await self._runtime.run_mgr.save(ar, self._session_id)

    def _collect_trace_ids(self) -> list[str]:
        return [
            f"{evt.event_type}:{evt.created_at}"
            for evt in self._journal._events
            if evt.data.get("agentrun_id") == self._journal._agentrun_id
        ]


def _ensure_list(obj: object) -> list[Message]:
    if isinstance(obj, list):
        return obj  # type: ignore[return-value]
    return []


def _extract_handoff_target(tool_results: list[Message]) -> str:
    for msg in tool_results:
        if msg.name == "handoff_to_agent" and msg.content:
            return msg.content
    return ""
