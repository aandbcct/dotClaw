"""Agent 核心循环（v2）—— 纯执行引擎，依赖 AgentRuntime。

v2 重构：
  - AgentLoop 只依赖 AgentRuntime（不依赖 Agent 整体）
  - Identity 值（system_prompt / tool_definitions / model / max_loop_steps）
    由调用方（Agent.run）预解析后作为参数传入
  - Session 作为参数传入，不存储
  - 返回 AgentRun（包装 AgentResult + Session）
"""

from __future__ import annotations

import asyncio
import json as _json
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from ..llm.base import Message
from ..journal import Journal
from ..session.session import Session
from .result import AgentResult
from .message_utils import trim as msg_trim, _msg_tokens

if TYPE_CHECKING:
    from ..tools.base import ToolDefinition
    from .agent import LLMResponse
    from .runtime import AgentRuntime
    from .slotContext import SlotContext
    from ..session.agent_run import AgentRun


def _find_project_root() -> Path:
    """从 dotClaw 模块位置向上找到项目根目录（包含 config.yaml）。"""
    import dotclaw
    return Path(dotclaw.__file__).parent.parent.parent


class AgentLoop:
    """Agent 主循环 —— 纯执行引擎，依赖 AgentRuntime。

    v2 设计原则：
    - 构造函数只接收 AgentRuntime（纯能力引用）
    - run() 接收 Session + 预解析的 Identity 参数
    - 不持有 Agent 引用，不直接访问 Identity
    """

    def __init__(self, runtime: "AgentRuntime") -> None:
        """构造执行引擎。

        Args:
            runtime: Agent 纯能力引用集合（llm / tool_executor / assembler / channel / ...）
        """
        self._runtime: AgentRuntime = runtime
        self._running: bool = False

    # ======================== 公开 API ========================

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
        4. ReAct 循环：LLM → tool_calls → 执行工具 → 下一轮
        5. Finalize: 保存 conversation + flush memory
        6. 返回 AgentRun

        Args:
            session: 运行时上下文（持有 Conversation + LLM 对话历史）
            user_message: 用户输入文本
            system_prompt: 预解析的 system prompt 文本
            tool_definitions: 预解析的工具定义列表
            model: 预解析的模型名
            max_loop_steps: 最大循环迭代数

        Returns:
            AgentRun（包装 AgentResult + 执行后的 Session）
        """
        self._running = True
        start_time: float = time.time()

        # ── Journal：每次 AgentRun 创建新实例 ──
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
            if resume_mgr is not None and slot_ctx.session_id:
                resume_ctx = resume_mgr.get_resume_context(slot_ctx.session_id)
                if resume_ctx:
                    object.__setattr__(slot_ctx, 'request_id', resume_ctx["request_id"])
                    journal.session_start(
                        session_id=slot_ctx.session_id,
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
                session_id=slot_ctx.session_id,
                request_id=slot_ctx.request_id,
                model=model,
                config=self._runtime.config.journal if self._runtime.config is not None else None,
            )

        # ── 记录用户输入 ──
        user_msg: Message = Message(role="user", content=user_message)
        journal.record_message(user_msg)

        tool_calls_total: int = 0
        iterations: int = 0

        try:
            messages: list[Message] = self._build_messages(
                user_input=user_message,
                system_prompt=system_prompt,
                history=session.history,
            )

            # ── Journal：prompt 构建完成 ──
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

            final_response: str = ""

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
                journal.record_message(asst_msg)

                if not llm_resp.tool_calls:
                    final_response = llm_resp.content

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
                    journal.record_message(tr)

                session.history.append(asst_msg)
                session.history.extend(tool_messages)

                journal.loop_end("tool_call")

            # ── Finalize: 保存 conversation + flush memory ──
            await self._finalize_round(session, user_message, final_response, journal)

            duration_ms: int = int((time.time() - start_time) * 1000)

            journal.session_end("success", success=True, total_duration_ms=duration_ms)
            journal.finalize()

            agent_result: AgentResult = AgentResult(
                final_text=final_response,
                tool_calls_count=tool_calls_total,
                iterations=iterations,
                duration_ms=duration_ms,
                request_id=slot_ctx.request_id,
            )

            from ..session.agent_run import AgentRun as AR
            return AR(result=agent_result, session=session)

        except Exception as e:
            error_msg: str = f"{type(e).__name__}: {e}"

            journal.error("ERROR", "agent.loop", error_msg)

            try:
                if self._runtime.channel is not None:
                    await self._runtime.channel.print_error(error_msg)
            except Exception:
                pass

            duration_ms = int((time.time() - start_time) * 1000)

            journal.session_end("error", success=False, total_duration_ms=duration_ms)
            journal.finalize()

            agent_result = AgentResult(
                final_text="",
                tool_calls_count=tool_calls_total,
                iterations=iterations,
                duration_ms=duration_ms,
                error=error_msg,
                request_id=slot_ctx.request_id,
            )

            from ..session.agent_run import AgentRun as AR
            return AR(result=agent_result, session=session)

        finally:
            self._running = False

    # ======================== 内部方法 ========================

    def _build_slot_context(
        self,
        session: Session,
        user_message: str,
        journal: "Journal",
        system_prompt: str,
        tool_definitions: "list[ToolDefinition]",
    ) -> "SlotContext":
        """构建 SlotContext（上下文工程的输入参数篮）。

        从 Session + 预解析参数 + AgentRuntime 组装 SlotContext。

        Args:
            session: 运行时上下文
            user_message: 用户原始消息
            journal: Journal 观测实例
            system_prompt: 预解析的 system prompt
            tool_definitions: 预解析的工具定义

        Returns:
            SlotContext 数据篮
        """
        from .slotContext import SlotContext as SCtx

        request_id: str = uuid.uuid4().hex[:8]
        project_root: Path = _find_project_root()
        max_ctx_tokens: int = 8000
        if self._runtime.config is not None:
            max_ctx_tokens = self._runtime.config.agent.max_context_tokens

        return SCtx(
            query=user_message,
            request_id=request_id,
            session_id=session.conversation.id,
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
        """从 history + system_prompt 构建 messages。只裁 history。

        Args:
            user_input: 用户当前输入
            system_prompt: 已组装的 system prompt 文本
            history: 易失性 LLM 上下文

        Returns:
            LLM 消息列表
        """
        system_msg: Message = Message(role="system", content=system_prompt)
        user_msg: Message = Message(role="user", content=user_input)

        max_ctx_tokens: int = 8000
        if self._runtime.config is not None:
            max_ctx_tokens = self._runtime.config.agent.max_context_tokens

        budget: int = max_ctx_tokens - _msg_tokens(system_msg) - _msg_tokens(user_msg)

        trimmed_history: list[Message]
        if budget > 0:
            trimmed_history = msg_trim(list(history), budget)
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
        """调用 LLM 并收集完整响应。

        封装：流式接收 → channel 推送 → 收集文本/tool_calls/token 信息。

        Args:
            messages: 待发送的消息列表
            model: 模型名（预解析）
            tool_definitions: 工具定义列表
            journal: Journal 观测实例

        Returns:
            LLMResponse（content + tool_calls + finish_reason + tokens）
        """
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
        """执行单个工具调用，返回 tool 角色 Message。

        Loop 侧通过 asyncio.gather 并行调用此方法，实现工具并行执行。

        Args:
            tc: ToolCall 对象（有 name / arguments / id 属性）
            journal: Journal 观测实例（resume 场景可为 None）

        Returns:
            role="tool" 的 Message
        """
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

    async def _flush_memory(
        self,
        messages: list,
        journal: "Journal",
    ) -> bool:
        """Memory flush 包装 —— 将最近一轮对话写入 L2 记忆。

        Args:
            messages: 最近一轮的 user + assistant 消息
            journal: Journal 观测实例

        Returns:
            是否成功写入
        """
        if self._runtime.memory_mgr is None:
            return False

        try:
            await self._runtime.memory_mgr.flush_memory(
                messages=messages,
                reason="round_end",
                journal=journal,
            )
            return True
        except Exception:
            import logging
            logging.getLogger("dotclaw.agent").debug(
                "Memory flush 失败（不影响对话）"
            )
            return False

    async def _finalize_round(
        self,
        session: Session,
        user_message: str,
        assistant_response: str,
        journal: "Journal",
    ) -> None:
        """After-loop 收尾 —— 由 run() 在 ReAct 循环结束后调用。

        职责：
        - 将 user + assistant 消息追加到 conversation.messages
        - 调用 conversation_mgr.save() 持久化
        - 调用 flush_memory() 触发 L2 日记忆写入

        Args:
            session: 运行时上下文
            user_message: 用户原始消息
            assistant_response: Agent 最终回复
            journal: Journal 观测实例
        """
        from ..storage.conversation import ConversationMessage

        session.conversation.messages.append(ConversationMessage(
            role="user",
            content=user_message,
        ))
        session.conversation.messages.append(ConversationMessage(
            role="assistant",
            content=assistant_response,
        ))
        await self._runtime.conversation_mgr.save(session.conversation)

        # 清空本轮 history（下轮 AgentRun 新建 Session 时重新开始）
        session.history.clear()

        if self._runtime.memory_mgr is not None:
            current_round = session.conversation.messages[-2:]
            await self._flush_memory(current_round, journal)
