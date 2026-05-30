"""Agent 核心循环（P3：AgentContext + PromptBuilder + AgentResult）"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from dotclaw.debug.logger import DebugManager, TraceRecord
from ..llm.base import Message
from .result import AgentResult
from .message_utils import validate as msg_validate, trim as msg_trim, clean as msg_clean

if TYPE_CHECKING:
    from ..llm.proxy import LLMProxy
    from ..memory.store import Session, SessionMessage
    from ..channel.base import Channel
    from ..config import Config
    from ..memory.store import SessionManager
    from ..tools.base import ToolRegistry
    from .context import AgentContext
    from .prompt.builder import PromptBuilder
    from .logger import AgentLogger


def _find_project_root() -> Path:
    """从 dotClaw 模块位置向上找到项目根目录"""
    import dotclaw
    return Path(dotclaw.__file__).parent.parent.parent


class AgentLoop:
    """
    Agent 主循环（P3）。

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
        tool_registry: "ToolRegistry | None" = None,
        prompt_builder: "PromptBuilder | None" = None,
        logger: "AgentLogger | None" = None,
    ):
        self.llm = llm
        self.session = session
        self.session_mgr = session_mgr
        self.channel = channel
        self.config = config
        self.model = config.llm.default_model
        self._running = False
        self._tool_registry = tool_registry
        self._prompt_builder = prompt_builder
        self._logger = logger
        self._debug_manager = DebugManager(
            level=config.debug.level,
            log_file=config.debug.log_file,
        )

    async def run(self, user_message: str) -> AgentResult:
        """
        处理一条用户消息，返回 AgentResult。

        完整流程：
        1. 构建 AgentContext（不可变快照）
        2. 通过 PromptBuilder 生成 system prompt → 构建 messages
        3. 调用 LLM（流式）→ 处理 tool_calls → 循环
        4. 返回 AgentResult
        """
        self._running = True
        start_time = time.time()

        # 构建上下文
        context = self._build_context(user_message)

        trace = TraceRecord(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            session_id=self.session.id,
            user_message=user_message,
        )

        tool_calls_total = 0
        iterations = 0

        try:
            messages = self._build_messages(user_message, context)
            trace.messages_sent.append([m.__dict__ for m in messages])

            final_response = ""
            max_iterations = 10

            for i in range(max_iterations):
                iterations = i + 1
                tool_calls_pending = []
                current_content = ""

                async for chunk in self.llm.chat(
                    messages=messages,
                    tools=context.tool_definitions if context.tool_definitions else None,
                    model=context.model,
                    purpose=context.purpose,
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
                tool_calls_total += len(tool_calls_pending)

                # 记录工具调用
                if self._logger:
                    for tc in tool_calls_pending:
                        self._logger.log_tool_call(tc.name, {})

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

                        if self._logger:
                            self._logger.log_tool_result(tc.name, len(result.output))

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

            trace.final_response = final_response
            trace.duration_ms = int((time.time() - start_time) * 1000)
            self._debug_manager.record_trace(trace)

            if self._logger:
                self._logger.record(trace)

            return AgentResult(
                final_text=final_response,
                tool_calls_count=tool_calls_total,
                iterations=iterations,
                duration_ms=trace.duration_ms,
                request_id=context.request_id,
            )

        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            if self._logger:
                self._logger.log_error(error_msg)

            trace.final_response = f"ERROR: {error_msg}"
            trace.duration_ms = int((time.time() - start_time) * 1000)
            self._debug_manager.record_trace(trace)

            return AgentResult(
                final_text="",
                tool_calls_count=tool_calls_total,
                iterations=iterations,
                duration_ms=trace.duration_ms,
                error=error_msg,
                request_id=context.request_id,
            )
        finally:
            self._running = False

    def _build_context(self, user_message: str) -> "AgentContext":
        """构建 AgentContext 不可变快照（在 run() 开头调用）"""
        from .context import AgentContext as Ctx

        # 生成 request_id
        request_id = ""
        if self._logger:
            request_id = self._logger.new_request()

        # 获取项目根目录
        project_root = _find_project_root()

        return Ctx(
            session_id=self.session.id,
            workspace=project_root,
            project_root=project_root,
            model=self.model,
            system_prompt=self.config.agent.system_prompt,
            available_tools=(
                [d.name for d in self._tool_registry.get_definitions()]
                if self._tool_registry else []
            ),
            tool_definitions=(
                self._tool_registry.get_definitions()
                if self._tool_registry else []
            ),
            request_id=request_id,
            purpose="chat",
            max_context_tokens=self.config.agent.max_context_tokens,
            rules=getattr(self.config.agent, "rules", ""),
            channel=self.channel,
        )

    def _build_messages(
        self, user_message: str, context: "AgentContext"
    ) -> list["Message"]:
        """
        构建发送给 LLM 的 messages 列表（P3：PromptBuilder + message_utils）。

        1. PromptBuilder.build(context) 生成 system prompt
        2. 附加历史消息 + 当前用户消息
        3. trim() 按 token 预算裁剪
        4. clean() 清理格式
        """
        messages: list[Message] = []

        # 生成 system prompt
        if self._prompt_builder:
            system_prompt = self._prompt_builder.build(context)
        else:
            system_prompt = context.system_prompt

        messages.append(Message(role="system", content=system_prompt))

        # 历史消息
        for msg in self.session.messages:
            messages.append(Message(
                role=msg.role,
                content=msg.content,
                name=msg.name,
                tool_call_id=msg.tool_call_id,
            ))

        # 当前用户消息
        messages.append(Message(role="user", content=user_message))

        # 裁剪 + 清理
        messages = msg_trim(messages, context.max_context_tokens)
        messages = msg_clean(messages)

        return messages

    def debug_trace(self, channel: "Channel"):
        """输出最近一次推理过程（供 /debug 命令调用）"""
        trace = self._debug_manager.get_last_trace()
        if trace:
            channel.print_info(trace.format_summary())
        else:
            channel.print_info("(no trace yet)")
