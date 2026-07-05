"""TurnLoop —— Session 级持久事件循环。

TurnLoop 是 Session 和 AgentRun 之间的事件循环层：
- Session：宏观会话容器
- TurnLoop：per-Session 后台协程，监听触发源，驱动 AgentRun
- AgentRun：TurnLoop 内的一次原子 LLM 执行

TurnLoop 使用 asyncio.Queue 实现事件驱动模式。
触发源包括：USER_INPUT / TOOL_RESULT / RESUME / TIMER / APPROVAL_DONE。

内部逻辑（原子操作封装为方法）：
1. run_forever() — 主事件循环入口
2. push_trigger() — 向事件队列推送触发事件
3. _step() — 单次 AgentRun 执行步骤
4. _assemble_context() — 组装 LLM 上下文消息
5. _invoke_agent_run() — 创建并执行一次 AgentRun
6. _handle_tool_result() — 处理工具执行结果并决定下一步
7. _handle_text_response() — 处理文本回复
"""

from __future__ import annotations

import asyncio
import json as _json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ..journal.events import EventType
from ..llm.base import Message
from ..session.agent_run import RunEndStatus, TriggerType

if TYPE_CHECKING:
    from ..runtime.runtime import Runtime
    from ..runtime.state_store import StateStore, StateSnapshot
    from ..runtime.agent_state import AgentState
    from ..agent.agent import Agent as AgentType, LLMResponse
    from ..tools.base import ToolDefinition
    from ..journal.journal import Journal
    from ..channel.base import Channel


# ============================================================================
# 触发事件
# ============================================================================

@dataclass
class TriggerEvent:
    """TurnLoop 的触发事件。

    字段：
        trigger_type: 触发源类型（TriggerType 枚举）
        data: 触发携带的数据（用户消息文本、工具结果列表等）
        agent_id: 触发源 Agent ID（handoff 场景使用，默认空）
    """

    trigger_type: TriggerType
    """触发源类型"""

    data: object = None
    """触发携带的数据"""

    agent_id: str = ""
    """触发源 Agent ID（默认空，用户输入时不指定）"""


# ============================================================================
# TurnLoop
# ============================================================================

class TurnLoop:
    """Session 级持久事件循环。

    职责：
    - 等待触发事件（asyncio.Queue）
    - 组装上下文（从 trace.jsonl 读历史消息, 从 StateStore 读进度）
    - 创建 AgentRun，调用 Runtime 原子方法
    - 路由：文本回复 → 输出并等待；工具调用 → 执行 → 自触发新 AgentRun
    - 挂起时保存 State，等待新触发

    Args:
        session_id: 所属 Session ID
        agent: 主 Agent 实例（配置+Tools+Runtime）
        runtime: Runtime 执行引擎
        state_store: StateStore 持久化实例
        journal: Journal 观测实例
        channel: 通信通道（CLI/API）
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

        # 事件队列
        self._queue: asyncio.Queue[TriggerEvent] = asyncio.Queue()

        # 状态
        self._active: bool = False
        self._current_state: AgentState | None = None
        self._context_messages: list[Message] = []

    # ======================== 公开入口 ========================

    async def push_trigger(self, event: TriggerEvent) -> None:
        """向事件队列推送触发事件。

        原子操作：
        1. 校验 TurnLoop 已激活
        2. 推送到 asyncio.Queue

        Args:
            event: 触发事件
        """
        if not self._active:
            return
        await self._queue.put(event)

    async def run_forever(self, initial_message: str) -> str:
        """启动事件循环并处理初始消息。

        主事件循环入口。直到收到终止信号才会退出。

        原子操作：
        1. 初始化 Journal 会话
        2. 推送初始 USER_INPUT 触发
        3. 进入事件循环：等待触发 → 执行 step
        4. 正常退出时 finalize

        Args:
            initial_message: 初始用户消息

        Returns:
            最终回复文本（如果是正常退出）
        """
        self._active = True
        final_response: str = ""

        # 推送初始消息
        await self._queue.put(TriggerEvent(
            trigger_type=TriggerType.USER_INPUT,
            data=initial_message,
        ))

        try:
            while self._active:
                trigger: TriggerEvent = await self._queue.get()

                if trigger.trigger_type == TriggerType.USER_INPUT:
                    final_response = await self._step(
                        user_message=str(trigger.data),
                        trigger=TriggerType.USER_INPUT,
                    )

                elif trigger.trigger_type == TriggerType.TOOL_RESULT:
                    final_response = await self._step(
                        user_message="",
                        trigger=TriggerType.TOOL_RESULT,
                        tool_results=trigger.data,
                    )

                elif trigger.trigger_type == TriggerType.RESUME:
                    # 从 StateStore 恢复状态
                    snapshot: StateSnapshot | None = await self._state_store.load(
                        self._session_id
                    )
                    if snapshot is not None:
                        self._current_state = await self._restore_state(snapshot)
                    final_response = await self._step(
                        user_message=str(trigger.data),
                        trigger=TriggerType.RESUME,
                    )

                else:
                    # TIMER / APPROVAL_DONE 等（预留）
                    final_response = await self._step(
                        user_message=str(trigger.data),
                        trigger=trigger.trigger_type,
                    )

        except Exception:
            self._active = False
            raise
        finally:
            self._journal.finalize()

        return final_response

    async def stop(self) -> None:
        """优雅关闭 TurnLoop。"""
        self._active = False

    # ======================== 原子操作：单步执行 ========================

    async def _step(
        self,
        user_message: str,
        trigger: TriggerType,
        tool_results: object = None,
    ) -> str:
        """执行一次完整的 AgentRun 步骤。

        原子操作：
        1. 组装 LLM 上下文消息
        2. 创建 AgentRun 并调用 LLM
        3. 根据 LLM 响应路由：
           - 文本回复 → 输出，返回
           - 工具调用 → 执行工具 → 记录结果 → 自触发新 AgentRun

        Args:
            user_message: 用户消息（TOOL_RESULT 类型时可为空）
            trigger: 触发源类型
            tool_results: 工具执行结果列表（TOOL_RESULT 类型时使用）

        Returns:
            最终文本回复（如果本轮是文本回复），否则继续循环
        """
        # 1. 组装上下文
        context_msgs: list[Message] = await self._assemble_context(
            user_message=user_message,
            trigger=trigger,
            tool_results=tool_results,
        )

        # 2. 创建并执行 AgentRun
        agentrun_id: str = uuid.uuid4().hex[:8]
        self._journal.agentrun_start(agentrun_id, trigger.value)

        started_at: str = datetime.now(timezone.utc).isoformat()
        start_time: float = time.time()

        # 3. 调用 LLM
        try:
            resp: LLMResponse = await self._invoke_agent_run(
                agentrun_id=agentrun_id,
                context_msgs=context_msgs,
            )
        except Exception as e:
            # LLM 调用失败
            duration_ms: int = int((time.time() - start_time) * 1000)
            ended_at: str = datetime.now(timezone.utc).isoformat()
            self._journal.agentrun_end(RunEndStatus.FAILED.value)
            self._journal.error("ERROR", "turnloop.llm", f"{type(e).__name__}: {e}")
            await self._save_agent_run(
                agentrun_id=agentrun_id,
                agent_id=self._agent.agent_id,
                end_status=RunEndStatus.FAILED.value,
                trigger=trigger.value,
                tokens_in=0,
                tokens_out=0,
                duration_ms=duration_ms,
                started_at=started_at,
                ended_at=ended_at,
                error=f"{type(e).__name__}: {e}",
            )
            return f"[ERROR] {e}"

        # 4. 路由
        input_tokens: int = resp.input_tokens
        output_tokens: int = resp.output_tokens
        duration_ms = int((time.time() - start_time) * 1000)
        ended_at = datetime.now(timezone.utc).isoformat()

        if resp.tool_calls:
            # 有工具调用 → 发出 TOOL_WAIT 标记
            self._journal.agentrun_end(RunEndStatus.TOOL_WAIT.value)

            # 记录 assistant 消息（含 tool_calls）
            asst_msg: Message = Message(
                role="assistant",
                content=resp.content or "",
                tool_calls=list(resp.tool_calls) if resp.tool_calls else None,
            )
            self._journal.record_message(asst_msg)
            self._context_messages.append(asst_msg)

            # 保存 AgentRun
            state_snapshot: dict | None = None
            if self._current_state is not None:
                state_snapshot = self._current_state.snapshot()
            await self._save_agent_run(
                agentrun_id=agentrun_id,
                agent_id=self._agent.agent_id,
                end_status=RunEndStatus.TOOL_WAIT.value,
                trigger=trigger.value,
                tokens_in=input_tokens,
                tokens_out=output_tokens,
                duration_ms=duration_ms,
                started_at=started_at,
                ended_at=ended_at,
                state_snapshot=state_snapshot,
                trace_ids=self._collect_trace_ids(),
            )

            # 执行工具
            tool_results_list: list[Message] = []
            if self._runtime.tool_executor is not None:
                tool_results_list = list(await asyncio.gather(*[
                    self._runtime._execute_single_tool(tc)
                    for tc in resp.tool_calls
                ]))

            # 记录工具结果消息
            for tr in tool_results_list:
                self._journal.record_message(tr)
                self._context_messages.append(tr)

            # 自触发下一个 AgentRun
            await asyncio.get_event_loop().create_task(
                self._queue.put(TriggerEvent(
                    trigger_type=TriggerType.TOOL_RESULT,
                    data=tool_results_list,
                ))
            )

            return ""

        else:
            # 文本回复
            self._journal.agentrun_end(RunEndStatus.COMPLETED.value)

            # 记录 assistant 消息
            asst_msg = Message(
                role="assistant",
                content=resp.content or "",
            )
            self._journal.record_message(asst_msg)
            self._context_messages.append(asst_msg)

            state_snapshot = None
            if self._current_state is not None:
                self._current_state.end_status = self._current_state.__class__.end_status
                state_snapshot = self._current_state.snapshot()

            await self._save_agent_run(
                agentrun_id=agentrun_id,
                agent_id=self._agent.agent_id,
                end_status=RunEndStatus.COMPLETED.value,
                trigger=trigger.value,
                tokens_in=input_tokens,
                tokens_out=output_tokens,
                duration_ms=duration_ms,
                started_at=started_at,
                ended_at=ended_at,
                state_snapshot=state_snapshot,
                trace_ids=self._collect_trace_ids(),
            )

            # 内容已在 _invoke_agent_run() 中通过 channel.stream() 实时输出
            return resp.content or ""

    # ======================== 原子操作：上下文组装 ========================

    async def _assemble_context(
        self,
        user_message: str,
        trigger: TriggerType,
        tool_results: object = None,
    ) -> list[Message]:
        """组装 LLM 调用所需的完整上下文消息。

        原子操作：
        1. 解析 agent 的 tool_definitions 和 system_prompt
        2. 构建 SlotContext 并通过 Assembler 生成最终 system_prompt
        3. 附加上下文历史消息
        4. 附加工具结果（如果是 TOOL_RESULT 类型触发）
        5. 裁剪消息以适应 token 预算

        Args:
            user_message: 用户消息文本
            trigger: 触发类型
            tool_results: 工具结果列表

        Returns:
            LLM 输入消息列表
        """
        agent_id: str = self._agent.agent_id
        model: str = self._agent._resolve_model(self._runtime)
        system_prompt: str = self._agent._resolve_system_prompt(self._runtime)
        tool_definitions: list[ToolDefinition] = self._agent._resolve_tool_definitions(
            self._runtime
        )

        # 构建 SlotContext
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

        # 构建消息
        system_msg: Message = Message(role="system", content=system_prompt)

        # 裁剪历史
        history_msgs: list[Message] = list(self._context_messages)
        max_ctx: int = 8000
        if self._runtime.config is not None:
            max_ctx = self._runtime.config.agent.max_context_tokens

        if user_message:
            user_msg: Message = Message(role="user", content=user_message)
            # 记录用户消息
            if trigger != TriggerType.TOOL_RESULT:
                self._journal.record_message(user_msg)
                self._context_messages.append(user_msg)
            return self._runtime._build_messages(
                user_input=user_message,
                system_prompt=system_prompt,
                history=history_msgs,
            )
        else:
            # TOOL_RESULT 触发：不添加新 user message
            return [system_msg] + history_msgs

    # ======================== 原子操作：LLM 调用 ========================

    async def _invoke_agent_run(
        self,
        agentrun_id: str,
        context_msgs: list[Message],
    ) -> LLMResponse:
        """执行一次 LLM 调用（一个 AgentRun）。

        原子操作：
        1. 通过 Runtime._invoke_llm 调用 LLM
        2. 记录 TRACE_MESSAGE 事件

        Args:
            agentrun_id: 当前 AgentRun ID
            context_msgs: 组装好的上下文消息

        Returns:
            LLMResponse
        """
        from ..agent.agent import LLMResponse as LR

        agent_id: str = self._agent.agent_id
        model: str = self._agent._resolve_model(self._runtime)
        tool_definitions: list[ToolDefinition] = self._agent._resolve_tool_definitions(
            self._runtime
        )

        # 记录 prompt_built
        self._journal.prompt_built(
            message_count=len(context_msgs),
            context_length=sum(len(str(m.content or "")) for m in context_msgs),
            system_prompt="",
            tool_count=len(tool_definitions),
        )

        # 调用 LLM
        self._journal.llm_call_start(attempt=1)

        current_content: str = ""
        tool_calls: list[object] = []
        finish_reason: str = "stop"
        input_tokens: int = 0
        output_tokens: int = 0

        stream_enabled: bool = False
        if self._runtime.config is not None:
            stream_enabled = self._runtime.config.llm.stream

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

    # ======================== 原子操作：状态管理 ========================

    async def _restore_state(self, snapshot: StateSnapshot) -> AgentState:
        """从持久化快照恢复 AgentState。

        原子操作：
        1. 创建新 AgentState 实例
        2. 从 snapshot 恢复运行时字段

        Args:
            snapshot: StateSnapshot 实例

        Returns:
            恢复的 AgentState
        """
        from ..runtime.agent_state import AgentState as AS
        state: AS = AS(
            task_id=snapshot.task_id,
            thread_id=snapshot.thread_id,
            agent_id=snapshot.agent_id,
            max_iterations=snapshot.max_iterations,
        )
        snapshot.restore_to(state)
        return state

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
    ) -> None:
        """持久化单个 AgentRun 记录。

        原子操作：
        1. 构建 AgentRun 实例
        2. 通过 AgentRunManager 保存

        Args:
            agentrun_id: AgentRun ID
            agent_id: Agent ID
            end_status: 结束状态
            trigger: 触发源
            tokens_in: 输入 token 数
            tokens_out: 输出 token 数
            duration_ms: 耗时
            started_at: 开始时间
            ended_at: 结束时间
            state_snapshot: 状态快照
            trace_ids: 关联的 trace event IDs
            error: 错误信息
        """
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
        )
        await self._runtime.run_mgr.save(ar, self._session_id)

    def _collect_trace_ids(self) -> list[str]:
        """收集当前 agentrun 的 trace event IDs。

        Returns:
            trace event ID 列表
        """
        return [
            f"{evt.event_type}:{evt.created_at}"
            for evt in self._journal._events
            if evt.data.get("agentrun_id") == self._journal._agentrun_id
        ]
