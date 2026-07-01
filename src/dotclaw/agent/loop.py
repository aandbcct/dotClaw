"""Agent 核心循环 —— 纯执行引擎，依赖 AgentRuntime。

v3 重构：
  - AgentLoop 只依赖 AgentRuntime
  - Identity 值由调用方预解析后作为参数传入
  - Session 作为参数传入，跨 AgentRun 共享
  - 返回 AgentRun（一次原子调用的完整记录）
  - 不再调用 _finalize_round（持久化由调用方负责）
  - Journal 只发事件，不做指标计数（指标由 AgentRun 承载）
"""

from __future__ import annotations

import asyncio
import json as _json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..llm.base import Message
from ..journal import Journal
from ..session.session import Session
from ..session.agent_run import AgentRun
from .message_utils import trim as msg_trim, _msg_tokens

if TYPE_CHECKING:
    from ..tools.base import ToolDefinition
    from .agent import LLMResponse
    from .runtime import AgentRuntime
    from .slotContext import SlotContext


def _find_project_root() -> Path:
    import dotclaw
    return Path(dotclaw.__file__).parent.parent.parent


class AgentLoop:
    """Agent 主循环 —— 纯执行引擎，依赖 AgentRuntime。

    v3 设计原则：
    - 构造函数只接收 AgentRuntime（纯能力引用）
    - run() 接收 Session + 预解析的 Identity 参数
    - 返回 AgentRun（不持有、不包装 AgentResult）
    - 不负责持久化（由调用方管理）
    """

    def __init__(self, runtime: "AgentRuntime") -> None:
        self._runtime: AgentRuntime = runtime
        self._running: bool = False

    async def run(
        self,
        session: Session,
        user_message: str,
        system_prompt: str,
        tool_definitions: "list[ToolDefinition]",
        model: str,
        max_loop_steps: int,
    ) -> AgentRun:
        """处理一条用户消息，返回 AgentRun。

        完整流程：
        1. 构建 SlotContext + Journal
        2. Assembler 组装 system_prompt
        3. Resume hook: 检测中断并恢复
        4. ReAct 循环：LLM → tool_calls → 执行 → 下一轮
        5. 构建 AgentRun 并返回

        Args:
            session: 运行时上下文（跨 AgentRun 共享）
            user_message: 用户输入文本
            system_prompt: 预解析的 system prompt 文本
            tool_definitions: 预解析的工具定义列表
            model: 预解析的模型名
            max_loop_steps: 最大循环迭代数

        Returns:
            AgentRun（一次原子调用的完整记录）
        """
        self._running = True
        started_at: str = datetime.now().isoformat()
        start_time: float = time.time()

        run_id: str = uuid.uuid4().hex[:12]
        journal: Journal = Journal()

        # ── 构建 SlotContext ──
        slot_ctx: SlotContext = self._build_slot_context(
            session, user_message, journal,
            system_prompt, tool_definitions,
        )

        if self._runtime.assembler is not None:
            self._runtime.assembler.on_new_request()
            system_prompt = await self._runtime.assembler.build_system_prompt(slot_ctx)

        # ── Resume hook ──
        resumed: bool = False
        if self._runtime.config is not None:
            resume_mgr = getattr(self._runtime, '_resume_manager', None)
            if resume_mgr is not None and session.id:
                resume_ctx = resume_mgr.get_resume_context(session.id)
                if resume_ctx:
                    object.__setattr__(slot_ctx, 'request_id', resume_ctx["request_id"])
                    journal.session_start(
                        session_id=session.id,
                        request_id=slot_ctx.request_id,
                        model=model,
                        config=self._runtime.config.journal,
                    )
                    journal.restore_state(resume_ctx.get("state", {}))

                    session.history = resume_ctx["messages"]

                    for tc in resume_ctx["incomplete_tools"]:
                        try:
                            result: Message = await self._execute_single_tool(tc, journal)
                            session.history.append(result)
                            journal.record_message(result)
                        except Exception:
                            err_msg: Message = Message(
                                role="tool",
                                content=f"错误：工具 {tc.name} 恢复执行失败",
                                tool_call_id=tc.id,
                            )
                            session.history.append(err_msg)
                            journal.record_message(err_msg)

                    resumed = True

        if not resumed:
            journal.session_start(
                session_id=session.id,
                request_id=slot_ctx.request_id,
                model=model,
                config=self._runtime.config.journal if self._runtime.config is not None else None,
            )

        # ── 记录用户输入 ──
        user_msg: Message = Message(role="user", content=user_message)
        journal.record_message(user_msg)

        # ── 本次 AgentRun 的指标 ──
        tool_calls_total: int = 0
        tokens_in_total: int = 0
        tokens_out_total: int = 0
        iterations: int = 0
        all_messages: list[Message] = []
        end_status: str = "completed"
        error_msg: str | None = None

        try:
            # ── Journal: prompt built ──
            messages: list[Message] = self._build_messages(
                user_input=user_message,
                system_prompt=system_prompt,
                history=session.history,
            )
            est_tokens: int = sum(len(m.content or "") for m in messages)
            skills_loaded: list = []
            if self._runtime.skill_registry is not None:
                skills_loaded = list(self._runtime.skill_registry.list_all())
            journal.prompt_built(
                message_count=len(messages),
                context_length=est_tokens,
                system_prompt=system_prompt,
                skills_injected=[s.name for s in skills_loaded] if skills_loaded else [],
                tool_count=len(tool_definitions),
            )

            for _loop_idx in range(max_loop_steps):
                iterations += 1

                messages = self._build_messages(
                    user_input=user_message,
                    system_prompt=system_prompt,
                    history=session.history,
                )

                journal.loop_start()

                llm_resp: LLMResponse = await self._invoke_llm(
                    messages=messages,
                    model=model,
                    tool_definitions=tool_definitions,
                    journal=journal,
                )

                tokens_in_total += llm_resp.input_tokens
                tokens_out_total += llm_resp.output_tokens

                llm_status: str = "error" if llm_resp.finish_reason == "error" else (
                    "truncated" if llm_resp.finish_reason == "length" else "success"
                )
                journal.llm_response_end(
                    input_tokens=llm_resp.input_tokens,
                    output_tokens=llm_resp.output_tokens,
                    tps=0.0,
                    status=llm_status,
                    stop_reason=llm_resp.finish_reason,
                )

                asst_msg: Message = Message(
                    role="assistant",
                    content=llm_resp.content or "",
                    tool_calls=list(llm_resp.tool_calls) if llm_resp.tool_calls else None,
                )
                all_messages.append(asst_msg)
                journal.record_message(asst_msg)

                if not llm_resp.tool_calls:
                    if self._runtime.channel is not None:
                        await self._runtime.channel.send("\n")

                    if iterations == 1:
                        journal.empty_action()

                    journal.loop_end("response")
                    break

                tool_calls_total += len(llm_resp.tool_calls)

                tool_messages: list[Message] = list(await asyncio.gather(*[
                    self._execute_single_tool(tc, journal)
                    for tc in llm_resp.tool_calls
                ]))

                for tr in tool_messages:
                    all_messages.append(tr)
                    journal.record_message(tr)

                session.history.append(asst_msg)
                session.history.extend(tool_messages)

                journal.loop_end("tool_call")

        except Exception as e:
            end_status = "failed"
            error_msg = f"{type(e).__name__}: {e}"
            journal.error("ERROR", "agent.loop", error_msg)
            try:
                if self._runtime.channel is not None:
                    self._runtime.channel.print_error(error_msg)
            except Exception:
                pass

        duration_ms: int = int((time.time() - start_time) * 1000)
        ended_at: str = datetime.now().isoformat()

        journal.session_end(end_status, success=(end_status == "completed"),
                            total_duration_ms=duration_ms)
        journal.finalize()

        self._running = False

        return AgentRun(
            run_id=run_id,
            agent_id=session.agent_id,
            parent_run_id="",
            messages=all_messages,
            end_status=end_status,
            tool_calls=tool_calls_total,
            tokens_in=tokens_in_total,
            tokens_out=tokens_out_total,
            iterations=iterations,
            duration_ms=duration_ms,
            error=error_msg,
            started_at=started_at,
            ended_at=ended_at,
        )

    # ======================== 内部方法 ========================

    def _build_slot_context(
        self,
        session: Session,
        user_message: str,
        journal: "Journal",
        system_prompt: str,
        tool_definitions: "list[ToolDefinition]",
    ) -> "SlotContext":
        from .slotContext import SlotContext as SCtx

        request_id: str = uuid.uuid4().hex[:8]
        project_root: Path = _find_project_root()
        max_ctx_tokens: int = 8000
        if self._runtime.config is not None:
            max_ctx_tokens = self._runtime.config.agent.max_context_tokens

        return SCtx(
            query=user_message,
            request_id=request_id,
            session_id=session.id,
            project_root=project_root,
            max_context_tokens=max_ctx_tokens,
            system_prompt=system_prompt,
            tool_definitions=tool_definitions,
            skill_registry=self._runtime.skill_registry,
            memory_manager=self._runtime.memory_mgr,
            knowledge_base=None,
            user_profile=None,
            journal=journal,
        )

    def _build_messages(
        self,
        user_input: str,
        system_prompt: str,
        history: list[Message],
    ) -> list[Message]:
        system_msg: Message = Message(role="system", content=system_prompt)
        user_msg: Message = Message(role="user", content=user_input)

        max_ctx_tokens: int = 8000
        if self._runtime.config is not None:
            max_ctx_tokens = self._runtime.config.agent.max_context_tokens

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
        tool_definitions: "list[ToolDefinition]",
        journal: "Journal",
    ) -> "LLMResponse":
        from .agent import LLMResponse

        current_content: str = ""
        tool_calls: list = []
        finish_reason: str = "stop"
        input_tokens: int = 0
        output_tokens: int = 0

        stream_enabled: bool = False
        if self._runtime.config is not None:
            stream_enabled = self._runtime.config.llm.stream

        async for chunk in self._runtime.llm.chat(
            messages=messages,
            tools=tool_definitions if tool_definitions else None,
            model=model,
            purpose="chat",
            stream=stream_enabled,
            journal=journal,
        ):
            if chunk.content:
                current_content += chunk.content
                if self._runtime.channel is not None:
                    await self._runtime.channel.stream(chunk.content)

            if chunk.tool_call:
                tool_calls.append(chunk.tool_call)

            if chunk.is_final:
                finish_reason = chunk.finish_reason or "stop"
                input_tokens = getattr(chunk, "input_tokens", 0)
                output_tokens = getattr(chunk, "output_tokens", len(current_content))
                break

        return LLMResponse(
            content=current_content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def _execute_single_tool(
        self,
        tc: object,
        journal: "Journal | None",
    ) -> Message:
        try:
            args: dict = _json.loads(tc.arguments)  # type: ignore[attr-defined]
        except (_json.JSONDecodeError, TypeError):
            args = {}

        if self._runtime.tool_executor is None:
            if journal is not None:
                journal.tool_start(tc.name, args=args)  # type: ignore[attr-defined]
                journal.tool_end(tc.name, result_len=0, status="error", error_type="no_executor")  # type: ignore[attr-defined]
            return Message(
                role="tool",
                content="错误：工具执行器未初始化",
                tool_call_id=tc.id,  # type: ignore[attr-defined]
            )

        if self._runtime.channel is not None:
            self._runtime.channel.print_info(
                f"\n🔧 调用工具: {tc.name}({_json.dumps(args, ensure_ascii=False)})"  # type: ignore[attr-defined]
            )

        result = await self._runtime.tool_executor.execute(
            name=tc.name,  # type: ignore[attr-defined]
            arguments=args,
            channel=self._runtime.channel,
            journal=journal,
        )

        if self._runtime.channel is not None:
            self._runtime.channel.print_info(
                f"  结果: {result.output[:100]}{'...' if len(result.output) > 100 else ''}"
            )

        return Message(
            role="tool",
            content=result.output,
            tool_call_id=tc.id,  # type: ignore[attr-defined]
        )
