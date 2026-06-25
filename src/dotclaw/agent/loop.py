"""Agent 核心循环（Journal：统一观测）—— 纯执行引擎"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime as _dt, timezone as _tz
from typing import TYPE_CHECKING

from ..llm.base import Message
from .result import AgentResult
from ..journal import Journal

if TYPE_CHECKING:
    from .agent import Agent


class AgentLoop:
    """
    Agent 主循环 —— 纯执行引擎。

    负责：接收 Agent → 构建 Journal → 调用 LLM → 处理工具调用 → 返回 AgentResult。
    所有依赖通过 self.agent 获取，不做业务逻辑决策。
    """

    def __init__(self, agent: "Agent"):
        self.agent = agent
        self._running = False

    async def run(self, user_message: str) -> AgentResult:
        """
        处理一条用户消息，返回 AgentResult。

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_state / trace / memory
        # 然后进入下一轮，直到停机条件满足

        完整流程：
        1. 通过 agent 构建 SlotContext
        2. 创建 Journal 开始观测
        3. Assembler 组装 system_prompt
        4. 通过 agent 构建 messages（含 _history）
        5. 调用 LLM（流式）→ 处理 tool_calls → 循环
        6. Journal.finalize() → 返回 AgentResult
        """
        self._running = True
        start_time = time.time()

        # ── Journal：先创建空壳 ──
        journal = Journal()

        # ── 构建 SlotContext + 使用 Assembler ──
        slot_ctx = self.agent._build_slot_context(user_message, journal)
        self.agent.assembler.on_new_request()
        system_prompt = await self.agent.assembler.build_system_prompt(slot_ctx)

        # Journal: session_start
        journal.session_start(
            session_id=slot_ctx.session_id,
            request_id=slot_ctx.request_id,
            model=self.agent.model,
            config=self.agent.config.journal,
        )

        # ── 记录用户输入 ──
        journal.record_history({
            "loop": -1, "step": "user_input",
            "ts": _dt.now(_tz.utc).isoformat(),
            "role": "user", "content": user_message,
        })

        tool_calls_total = 0
        iterations = 0

        try:
            # 从 _history + system_prompt 构建 messages
            messages = self.agent._build_messages(
                user_input=user_message, system_prompt=system_prompt)

            # ── Journal：prompt 构建完成 ──
            est_tokens = sum(len(m.content or "") for m in messages)
            skills_loaded = list(self.agent.skill_registry.list_all()) if self.agent.skill_registry else []
            journal.prompt_built(
                message_count=len(messages),
                context_length=est_tokens,
                system_prompt=slot_ctx.system_prompt,
                skills_injected=[s.name for s in skills_loaded] if skills_loaded else [],
                tool_count=len(slot_ctx.tool_definitions),
            )

            final_response = ""
            max_iterations = self.agent.agent_config.max_loop_steps

            for loop_idx in range(max_iterations):
                iterations += 1

                # ── 每 turn 重建 messages（包含最新 _history） ──
                messages = self.agent._build_messages(
                    user_input=user_message, system_prompt=system_prompt)

                # ── Journal：每轮循环开始 ──
                journal.loop_start()

                # ── 通过 Agent 调用 LLM ──
                llm_resp = await self.agent._invoke_llm(
                    messages, self.agent.model, slot_ctx.tool_definitions, journal)

                # ── Journal：LLM 响应结束 ──
                llm_status = "error" if llm_resp.finish_reason == "error" else (
                    "truncated" if llm_resp.finish_reason == "length" else "success"
                )
                journal.llm_response_end(
                    input_tokens=llm_resp.input_tokens,
                    output_tokens=llm_resp.output_tokens,
                    tps=0.0,
                    status=llm_status,
                    stop_reason=llm_resp.finish_reason,
                )

                # ── 记录 LLM 返回内容 ──
                journal.record_history({
                    "loop": loop_idx, "step": "llm_response",
                    "ts": _dt.now(_tz.utc).isoformat(),
                    "role": "assistant",
                    "content": llm_resp.content,
                    "tool_calls": [
                        {"id": tc.id, "name": tc.name, "args": tc.arguments}
                        for tc in (llm_resp.tool_calls or [])
                    ] or None,
                    "tokens": {
                        "input": llm_resp.input_tokens,
                        "output": llm_resp.output_tokens,
                    },
                })

                if not llm_resp.tool_calls:
                    final_response = llm_resp.content
                    if self.agent.channel:
                        await self.agent.channel.send("\n")

                    if iterations == 1:
                        journal.empty_action()

                    journal.loop_end("response")
                    break

                tool_calls_total += len(llm_resp.tool_calls)

                # 将 assistant 消息（含 tool_calls）追加
                asst_msg = Message(
                    role="assistant",
                    content=llm_resp.content or "",
                    tool_calls=list(llm_resp.tool_calls),
                )

                # ── 并行执行工具调用 ──
                tool_messages = await asyncio.gather(*[
                    self.agent._execute_single_tool(tc, journal)
                    for tc in llm_resp.tool_calls
                ])

                # ── 记录工具结果 ──
                for tc, tr in zip(llm_resp.tool_calls, tool_messages):
                    journal.record_history({
                        "loop": loop_idx, "step": "tool_result",
                        "ts": _dt.now(_tz.utc).isoformat(),
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": tr.content,
                    })

                # 追加到 _history（下轮 _build_messages 自动包含）
                self.agent._history.append(asst_msg)
                self.agent._history.extend(tool_messages)

                journal.loop_end("tool_call")

            # ── After-loop 收尾：保存会话 + flush 记忆 ──
            await self.agent._finalize_round(user_message, final_response, journal)

            duration_ms = int((time.time() - start_time) * 1000)

            # ── Journal：会话结束 + finalize ──
            journal.session_end("success", success=True, total_duration_ms=duration_ms)
            journal.finalize()

            return AgentResult(
                final_text=final_response,
                tool_calls_count=tool_calls_total,
                iterations=iterations,
                duration_ms=duration_ms,
                request_id=slot_ctx.request_id,
            )

        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"

            journal.error("ERROR", "agent.loop", error_msg)

            # 确保用户能看到错误
            try:
                if self.agent.channel:
                    await self.agent.channel.print_error(error_msg)
            except Exception:
                pass

            duration_ms = int((time.time() - start_time) * 1000)

            # ── Journal：会话结束（失败）──
            journal.session_end("error", success=False, total_duration_ms=duration_ms)
            journal.finalize()

            return AgentResult(
                final_text="",
                tool_calls_count=tool_calls_total,
                iterations=iterations,
                duration_ms=duration_ms,
                error=error_msg,
                request_id=slot_ctx.request_id,
            )
        finally:
            self._running = False
