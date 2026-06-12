"""Agent 核心循环（Journal：统一观测）"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from ..llm.base import Message
from .result import AgentResult
from .message_utils import validate as msg_validate, trim as msg_trim, clean as msg_clean
from ..journal import Journal

if TYPE_CHECKING:
    from ..llm.proxy import LLMProxy
    from ..memory.store import Session, SessionMessage
    from ..channel.base import Channel
    from ..config import Config
    from ..memory.store import SessionManager
    from ..tools.executor import ToolExecutor
    from ..tools.base import ToolResult
    from .context import AgentContext
    from .prompt.builder import PromptBuilder
    from ..memory.manager import MemoryManager
    from ..skills.registry import SkillRegistry


def _find_project_root() -> Path:
    """从 dotClaw 模块位置向上找到项目根目录"""
    import dotclaw
    return Path(dotclaw.__file__).parent.parent.parent


class AgentLoop:
    """
    Agent 主循环。

    负责：接收消息 → 构建 AgentContext → PromptBuilder 生成 system prompt
         → 调用 LLM → 处理工具调用 → 返回 AgentResult
    """

    def __init__(
        self,
        llm: "LLMProxy",
        session: "Session",
        session_mgr: "SessionManager",
        channel: "Channel",
        config: "Config",
        tool_executor: "ToolExecutor | None" = None,
        prompt_builder: "PromptBuilder | None" = None,
        memory_mgr: "MemoryManager | None" = None,
        skill_registry: "SkillRegistry | None" = None,
    ):
        self.llm = llm
        self.session = session
        self.session_mgr = session_mgr
        self.channel = channel
        self.config = config
        self.model = config.llm.default_model
        self._running = False
        self._tool_executor = tool_executor
        self._prompt_builder = prompt_builder
        self._memory_mgr = memory_mgr
        self._skill_registry = skill_registry

    async def run(self, user_message: str) -> AgentResult:
        """
        处理一条用户消息，返回 AgentResult。

        完整流程：
        1. 构建 AgentContext（不可变快照）
        2. 创建 Journal 开始观测
        3. 通过 PromptBuilder 生成 system prompt → 构建 messages
        4. 调用 LLM（流式）→ 处理 tool_calls → 循环
        5. Journal.finalize() → 返回 AgentResult
        """
        self._running = True
        start_time = time.time()

        # ── Journal：先创建空壳，后面 session_start 填参数 ──
        journal = Journal()

        # 构建上下文（传入 journal 引用）
        context = await self._build_context(user_message, journal)

        # ── Journal：会话开始 ──
        journal.session_start(context, self.config.journal)

        tool_calls_total = 0
        iterations = 0

        try:
            messages = self._build_messages(user_message, context)

            # ── Journal：prompt 构建完成 ──
            est_tokens = sum(len(m.content or "") for m in messages)
            skills_loaded = list(self._skill_registry.list_all()) if self._skill_registry else []
            journal.prompt_built(
                message_count=len(messages),
                context_length=est_tokens,
                system_prompt=context.system_prompt,
                skills_injected=[s.name for s in skills_loaded] if skills_loaded else [],
                tool_count=len(context.tool_definitions),
            )

            final_response = ""
            max_iterations = 10

            for _ in range(max_iterations):
                iterations += 1
                tool_calls_pending = []
                current_content = ""
                loop_finish_reason = "stop"
                input_tokens = 0
                output_tokens = 0

                # ── Journal：每轮循环开始 ──
                journal.loop_start()

                async for chunk in self.llm.chat(
                    messages=messages,
                    tools=context.tool_definitions if context.tool_definitions else None,
                    model=context.model,
                    purpose=context.purpose,
                    stream=self.config.llm.stream,
                    journal=journal,
                ):
                    if chunk.content:
                        current_content += chunk.content
                        await self.channel.stream(chunk.content)

                    if chunk.tool_call:
                        tool_calls_pending.append(chunk.tool_call)

                    if chunk.is_final:
                        loop_finish_reason = chunk.finish_reason or "stop"
                        # 从 chunk 提取 token 信息（如果有的话）
                        input_tokens = getattr(chunk, "input_tokens", 0)
                        output_tokens = getattr(chunk, "output_tokens", len(current_content))
                        break

                # ── Journal：LLM 响应结束 ──
                # llm_response_end status 根据 finish_reason 动态判定
                llm_status = "error" if loop_finish_reason == "error" else (
                    "truncated" if loop_finish_reason == "length" else "success"
                )
                journal.llm_response_end(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens or len(current_content),
                    tps=0.0,
                    status=llm_status,
                    stop_reason=loop_finish_reason,
                )

                if not tool_calls_pending:
                    final_response = current_content
                    await self.channel.send("\n")

                    if iterations == 1:
                        journal.empty_action()

                    journal.loop_end("response")
                    break

                tool_calls_total += len(tool_calls_pending)

                # 将 assistant 消息（含 tool_calls）追加到 messages
                messages.append(Message(
                    role="assistant",
                    content=current_content or "",
                    tool_calls=list(tool_calls_pending),
                ))

                for tc in tool_calls_pending:
                    try:
                        args = json.loads(tc.arguments)
                    except (json.JSONDecodeError, TypeError):
                        args = {}

                    if self._tool_executor:
                        self.channel.print_info(f"\n🔧 调用工具: {tc.name}({json.dumps(args, ensure_ascii=False)})")

                        result = await self._tool_executor.execute(
                            name=tc.name,
                            arguments=args,
                            channel=self.channel,
                            journal=journal,
                        )

                        self.channel.print_info(f"  结果: {result.output[:100]}{'...' if len(result.output) > 100 else ''}")

                        messages.append(Message(
                            role="tool",
                            content=result.output,
                            tool_call_id=tc.id,
                        ))
                    else:
                        journal.tool_start(tc.name)
                        messages.append(Message(
                            role="tool",
                            content="错误：工具执行器未初始化",
                            tool_call_id=tc.id,
                        ))
                        journal.tool_end(tc.name, result_len=0, status="error", error_type="no_executor")

                journal.loop_end("tool_call")

            # 保存会话历史
            from ..memory.store import SessionMessage

            self.session.messages.append(SessionMessage(
                role="user",
                content=user_message,
            ))
            self.session.messages.append(SessionMessage(
                role="assistant",
                content=final_response,
            ))
            await self.session_mgr.save(self.session)

            # P4：flush 触发
            if self._memory_mgr:
                current_round = self.session.messages[-2:]
                await self._memory_mgr.flush_memory(
                    messages=current_round,
                    reason="round_end",
                    journal=journal,
                )

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
                await self.channel.print_error(error_msg)
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

    async def _build_context(self, user_message: str, journal: "Journal") -> "AgentContext":
        """构建 AgentContext 不可变快照"""
        from .context import AgentContext as Ctx

        request_id = uuid.uuid4().hex[:8]
        project_root = _find_project_root()

        # P4：语义检索
        memory_summary = ""
        if self._memory_mgr:
            try:
                journal.memory_retrieval_start()
                results = await self._memory_mgr.search(user_message, max_results=3)
                journal.memory_retrieval(
                    query=user_message[:100],
                    hit_count=len(results),
                )
                if results:
                    memory_summary = "\n".join(
                        f"- ({r.source}:{r.path}) {r.snippet}" for r in results
                    )
            except Exception as e:
                import logging
                logging.getLogger("dotclaw.agent").debug(
                    f"记忆检索失败（不影响对话）: {e}"
                )

        return Ctx(
            session_id=self.session.id,
            workspace=project_root,
            project_root=project_root,
            model=self.model,
            system_prompt=self.config.agent.system_prompt,
            available_tools=(
                [d.name for d in self._tool_executor.get_definitions()]
                if self._tool_executor else []
            ),
            tool_definitions=(
                self._tool_executor.get_definitions()
                if self._tool_executor else []
            ),
            request_id=request_id,
            purpose="chat",
            max_context_tokens=self.config.agent.max_context_tokens,
            rules=getattr(self.config.agent, "rules", ""),
            memory_summary=memory_summary,
            channel=self.channel,
            skill_registry=self._skill_registry,
            journal=journal,
        )

    def _build_messages(
        self, user_message: str, context: "AgentContext"
    ) -> list["Message"]:
        """构建发送给 LLM 的 messages 列表"""
        messages: list[Message] = []

        if self._prompt_builder:
            system_prompt = self._prompt_builder.build(context)
        else:
            system_prompt = context.system_prompt

        messages.append(Message(role="system", content=system_prompt))

        for msg in self.session.messages:
            messages.append(Message(
                role=msg.role,
                content=msg.content,
                name=msg.name,
                tool_call_id=msg.tool_call_id,
            ))

        messages.append(Message(role="user", content=user_message))

        messages = msg_trim(messages, context.max_context_tokens)
        messages = msg_clean(messages)

        return messages
