"""Agent 核心循环（P3：AgentContext + PromptBuilder + AgentResult）"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from ..llm.base import Message
from .result import AgentResult
from .message_utils import validate as msg_validate, trim as msg_trim, clean as msg_clean
from .logger import AgentLogger
from ..metrics.events import AgentEvent, EventType
from ..metrics.snapshot import RunMeta  # P11
from ..metrics.storage import _get_git_commit, _build_config_hash  # P11

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
    from ..skills.registry import SkillRegistry  # P7 新增


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
        tool_executor: "ToolExecutor | None" = None,
        prompt_builder: "PromptBuilder | None" = None,
        logger: "AgentLogger | None" = None,
        memory_mgr: "MemoryManager | None" = None,
        skill_registry: "SkillRegistry | None" = None,  # P7 新增
        metrics_collector: "Any | None" = None,  # P11 新增
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
        self._skill_registry = skill_registry  # P7 新增
        self._metrics_collector = metrics_collector  # P11 新增

        # Phase 5: _logger 直接管理 trace（合并 DebugManager 能力）
        self._logger = logger or AgentLogger(
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
        collector = None

        # 构建上下文（P4：异步语义检索）
        context = await self._build_context(user_message)
        collector = context.metrics_collector

        # ── P11 指标埋点：会话开始 ──
        if collector:
            collector.on_event(AgentEvent(
                timestamp=start_time * 1000,
                event_type=EventType.SESSION_START,
                data={
                    "session_id": self.session.id,
                    "request_id": context.request_id,
                    "task_index": getattr(self, "_task_index", 0),
                },
            ))

        from .logger import TraceRecord
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

                loop_start_ts = time.time()

                # ── P11 指标埋点：ReAct 循环开始 ──
                if collector:
                    collector.on_event(AgentEvent(
                        timestamp=loop_start_ts * 1000,
                        event_type=EventType.REACT_LOOP_START,
                        data={"loop_index": i},
                    ))

                async for chunk in self.llm.chat(
                    messages=messages,
                    tools=context.tool_definitions if context.tool_definitions else None,
                    model=context.model,
                    purpose=context.purpose,
                    stream=self.config.llm.stream,
                    metrics_collector=collector,
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

                    # ── P11 指标埋点：空转 ──
                    if iterations == 1:
                        # If the agent responds without any tool calls, it's an empty action
                        if collector:
                            collector.on_event(AgentEvent(
                                timestamp=time.time() * 1000,
                                event_type=EventType.REACT_EMPTY_ACTION,
                                data={"loop_index": i},
                            ))

                    # ── P11 指标埋点：循环结束 ──
                    if collector:
                        loop_duration = (time.time() - loop_start_ts) * 1000
                        collector.on_event(AgentEvent(
                            timestamp=time.time() * 1000,
                            event_type=EventType.REACT_LOOP_END,
                            data={
                                "loop_index": i,
                                "action": "respond",
                                "duration_ms": round(loop_duration, 1),
                            },
                        ))
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

                    if self._tool_executor:
                        self.channel.print_info(f"\n🔧 调用工具: {tc.name}({json.dumps(args, ensure_ascii=False)})")

                        # ── P11 指标埋点：工具调用开始 ──
                        if collector:
                            collector.on_event(AgentEvent(
                                timestamp=time.time() * 1000,
                                event_type=EventType.TOOL_CALL_START,
                                data={"tool_name": tc.name, "tool_input": args},
                            ))

                        result = await self._tool_executor.execute(
                            name=tc.name,
                            arguments=args,
                            channel=self.channel,
                            metrics_collector=collector,
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
                            content="错误：工具执行器未初始化",
                            tool_call_id=tc.id,
                        ))

                # ── P11 指标埋点：ReAct 循环结束（含工具调用）──
                if collector and tool_calls_pending:
                    loop_duration = (time.time() - loop_start_ts) * 1000
                    first_tc = tool_calls_pending[0]
                    collector.on_event(AgentEvent(
                        timestamp=time.time() * 1000,
                        event_type=EventType.REACT_LOOP_END,
                        data={
                            "loop_index": i,
                            "action": first_tc.name,
                            "duration_ms": round(loop_duration, 1),
                            "tool_calls_count": len(tool_calls_pending),
                        },
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

            # P4：flush 触发（仅在正常完成时）
            if self._memory_mgr and len(self.session.messages) > self.config.memory.flush_threshold:
                import asyncio
                asyncio.create_task(
                    self._memory_mgr.flush_memory(
                        messages=self.session.messages[-self.config.memory.flush_max_messages:],
                        reason="threshold",
                        metrics_collector=self._metrics_collector,
                    )
                )

            trace.final_response = final_response
            trace.duration_ms = int((time.time() - start_time) * 1000)

            # ── P11 指标埋点：会话结束（成功）──
            if collector:
                collector.on_event(AgentEvent(
                    timestamp=time.time() * 1000,
                    event_type=EventType.SESSION_END,
                    data={
                        "session_id": self.session.id,
                        "success": True,
                        "total_loops": iterations,
                        "total_duration_ms": trace.duration_ms,
                    },
                ))
                # ── P11 自动保存快照 ──
                _emit_snapshot(collector, channel=self.channel)

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

            # ── P11 指标埋点：会话结束（失败）──
            if collector:
                collector.on_event(AgentEvent(
                    timestamp=time.time() * 1000,
                    event_type=EventType.SESSION_END,
                    data={
                        "session_id": self.session.id,
                        "success": False,
                        "total_loops": iterations,
                        "total_duration_ms": trace.duration_ms,
                        "error": error_msg,
                    },
                ))
                # ── P11 自动保存快照 ──
                _emit_snapshot(collector, channel=self.channel)

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

    async def _build_context(self, user_message: str) -> "AgentContext":
        """构建 AgentContext 不可变快照（P4：异步语义检索）"""
        from .context import AgentContext as Ctx

        request_id = ""
        if self._logger:
            request_id = self._logger.new_request()

        project_root = _find_project_root()

        # P4：语义检索
        memory_summary = ""
        if self._memory_mgr:
            try:
                retrieval_start = time.time()
                results = await self._memory_mgr.search(user_message, max_results=3)

                # ── P11 指标埋点：记忆检索 ──
                retrieval_duration = (time.time() - retrieval_start) * 1000
                if self._metrics_collector:
                    self._metrics_collector.on_event(AgentEvent(
                        timestamp=time.time() * 1000,
                        event_type=EventType.MEMORY_RETRIEVAL,
                        data={
                            "query": user_message[:100],
                            "hit": bool(results),
                            "top_k": 3,
                            "duration_ms": round(retrieval_duration, 1),
                        },
                    ))
                if results:
                    memory_summary = "\n".join(
                        f"- ({r.source}:{r.path}) {r.snippet}" for r in results
                    )
            except Exception as e:
                logger = __import__('logging').getLogger("dotclaw.agent")
                logger.debug(f"记忆检索失败（不影响对话）: {e}")

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
            skill_registry=self._skill_registry,  # P7 新增
            metrics_collector=self._metrics_collector,  # P11 新增
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
        trace = self._logger.get_last_trace() if self._logger else None
        if trace:
            channel.print_info(trace.format_summary())
        else:
            channel.print_info("(no trace yet)")


def _emit_snapshot(collector: "Any", channel: "Any | None" = None) -> None:
    """会话结束时自动计算快照并保存到 data/snapshots/。

    保存成功后通过 channel 输出提示信息。
    """
    try:
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc)
        run_id = f"run_{ts.strftime('%Y%m%d_%H%M%S')}"

        meta = RunMeta(
            run_id=run_id,
            timestamp=ts.isoformat(),
            git_commit=_get_git_commit(),
            config_hash=_build_config_hash(
                config_path="config.yaml", router_config_path="model_router_config.yaml",
            ),
            test_dataset="interactive",
            test_dataset_size=1,
        )
        filepath = collector.finalize(meta)
        if filepath and channel:
            channel.print_info(f"[metrics] 快照已保存: {filepath}")
    except Exception:
        pass  # 快照保存失败不影响主流程
