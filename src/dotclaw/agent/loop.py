"""Agent 核心循环"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from dotclaw.debug.logger import DebugManager, TraceRecord
from ..llm.base import Message

if TYPE_CHECKING:
    from ..llm.proxy import LLMProxy
    from ..memory.store import Session, SessionMessage
    from ..channel.base import Channel
    from ..config import Config
    from ..memory.store import SessionManager
    from ..tools.base import ToolRegistry


class AgentLoop:
    """
    Agent 主循环。

    负责：接收消息 → 构建 messages → 调用 LLM → 处理工具调用 → 返回结果
    """

    def __init__(
        self,
        llm: "LLMProxy",
        session: "Session",
        session_mgr: "SessionManager",
        channel: "Channel",
        config: "Config",
        tool_registry: "ToolRegistry | None" = None,
    ):
        self.llm = llm
        self.session = session
        self.session_mgr = session_mgr
        self.channel = channel
        self.config = config
        self.model = config.llm.default_model
        self._running = False
        self._last_trace: dict | None = None
        self._tool_registry = tool_registry
        self._debug_manager = DebugManager(
            level=config.debug.level,
            log_file=config.debug.log_file,
        )

    async def run(self, user_message: str) -> str:
        """
        处理一条用户消息，返回 Agent 的回复。

        完整流程：
        1. 构建 messages (system + history + user)
        2. 调用 LLM（流式）
        3. 如果有 tool_calls → 执行工具 → 把结果追加到 messages → 回到 2
        4. 返回最终文本回复
        """
        self._running = True
        start_time = time.time()
        trace = TraceRecord(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            session_id=self.session.id,
            user_message=user_message,
        )

        try:
            messages = self._build_messages(user_message)
            trace.messages_sent.append([m.__dict__ for m in messages])

            final_response = ""
            max_iterations = 10

            for i in range(max_iterations):
                tool_calls_pending = []
                current_content = ""

                async for chunk in self.llm.chat(
                    messages=messages,
                    tools=self._tool_registry.get_definitions() if self._tool_registry else None,
                    model=self.model,
                    purpose="chat",
                    stream=self.config.llm.stream,
                ):
                    if chunk.content:
                        current_content += chunk.content
                        await self.channel.stream(chunk.content)

                    if chunk.tool_call:
                        tool_calls_pending.append(chunk.tool_call)

                    if chunk.is_final:
                        break

                if not tool_calls_pending:
                    final_response = current_content
                    await self.channel.send("\n")
                    break

                trace.tool_calls.append([tc.__dict__ for tc in tool_calls_pending])

                # 先将 assistant 消息（含 tool_calls）追加到 messages
                # OpenAI API 要求：assistant(tool_calls) → tool(result) 成对出现
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

                    if self._tool_registry:
                        self.channel.print_info(f"\n🔧 调用工具: {tc.name}({json.dumps(args, ensure_ascii=False)})")
                        result = await self._tool_registry.execute(
                            name=tc.name,
                            arguments=args,
                            channel=self.channel,
                        )
                        trace.tool_results.append({
                            "tool": tc.name,
                            "result": result.output,
                        })

                        self.channel.print_info(f"  结果: {result.output[:100]}{'...' if len(result.output) > 100 else ''}")

                        messages.append(Message(
                            role="tool",
                            content=result.output,
                            tool_call_id=tc.id,
                        ))
                    else:
                        messages.append(Message(
                            role="tool",
                            content="错误：工具注册表未初始化",
                            tool_call_id=tc.id,
                        ))

            # 导入 SessionMessage 用于保存会话历史
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

            trace.final_response = final_response
            trace.duration_ms = int((time.time() - start_time) * 1000)
            self._debug_manager.record_trace(trace)
            self._last_trace = {
                "user_message": user_message,
                "duration_ms": trace.duration_ms,
                "note": "ReAct loop completed",
            }

            return final_response

        except Exception as e:
            self._running = False
            raise
        finally:
            self._running = False

    def _build_messages(self, user_message: str) -> list["Message"]:
        """
        构建发送给 LLM 的 messages 列表。

        ⚠️ Phase 1 简化版：不做上下文截断（Phase 2 实现）
        ⚠️ Phase 1 不注入 MEMORY.md（Phase 2 实现）
        ⚠️ Phase 1 不注入 Skill（Phase 3 实现）
        """
        messages = []

        messages.append(Message(
            role="system",
            content=self.config.agent.system_prompt,
        ))

        for msg in self.session.messages:
            messages.append(Message(
                role=msg.role,
                content=msg.content,
                name=msg.name,
                tool_call_id=msg.tool_call_id,
            ))

        messages.append(Message(
            role="user",
            content=user_message,
        ))

        return messages

    def debug_trace(self, channel: "Channel"):
        """输出最近一次推理过程（供 /debug 命令调用）"""
        trace = self._debug_manager.get_last_trace()
        if trace:
            channel.print_info(trace.format_summary())
        else:
            channel.print_info("(no trace yet)")
