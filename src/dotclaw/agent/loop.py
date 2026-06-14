"""Agent 核心循环（Journal：统一观测）—— 纯执行引擎"""

from __future__ import annotations

import asyncio
import time
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
        1. 通过 agent 构建 AgentContext（不可变快照）
        2. 创建 Journal 开始观测
        3. 通过 agent 构建 messages
        4. 调用 LLM（流式）→ 处理 tool_calls → 循环
        5. Journal.finalize() → 返回 AgentResult
        """
        self._running = True
        start_time = time.time()

        # ── Journal：先创建空壳，后面 session_start 填参数 ──
        journal = Journal()

        # 构建上下文（传入 journal 引用）
        context = await self.agent._build_context(user_message, journal)

        # ── Journal：会话开始 ──
        journal.session_start(context, self.agent.config.journal)

        tool_calls_total = 0
        iterations = 0

        try:
            messages = self.agent._build_messages(user_message, context)

            # ── Journal：prompt 构建完成 ──
            est_tokens = sum(len(m.content or "") for m in messages)
            skills_loaded = list(self.agent.skill_registry.list_all()) if self.agent.skill_registry else []
            journal.prompt_built(
                message_count=len(messages),
                context_length=est_tokens,
                system_prompt=context.system_prompt,
                skills_injected=[s.name for s in skills_loaded] if skills_loaded else [],
                tool_count=len(context.tool_definitions),
            )

            final_response = ""
            max_iterations = self.agent.agent_config.max_loop_steps

            for _ in range(max_iterations):
                iterations += 1

                # ── Journal：每轮循环开始 ──
                journal.loop_start()

                # ── 通过 Agent 调用 LLM ──
                llm_resp = await self.agent._invoke_llm(messages, context, journal)

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

                if not llm_resp.tool_calls:
                    final_response = llm_resp.content
                    if self.agent.channel:
                        await self.agent.channel.send("\n")

                    if iterations == 1:
                        journal.empty_action()

                    journal.loop_end("response")
                    break

                tool_calls_total += len(llm_resp.tool_calls)

                # 将 assistant 消息（含 tool_calls）追加到 messages
                messages.append(Message(
                    role="assistant",
                    content=llm_resp.content or "",
                    tool_calls=list(llm_resp.tool_calls),
                ))

                # ── 并行执行工具调用 ──
                tool_messages = await asyncio.gather(*[
                    self.agent._execute_single_tool(tc, journal)
                    for tc in llm_resp.tool_calls
                ])
                messages.extend(tool_messages)

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
                request_id=context.request_id,
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
                request_id=context.request_id,
            )
        finally:
            self._running = False
