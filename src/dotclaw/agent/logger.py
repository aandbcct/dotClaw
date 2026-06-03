"""AgentLogger — 结构化日志系统（Phase 5 合并 DebugManager 能力）

合并内容：
- TraceRecord 从 debug/logger.py 迁移到此处
- _last_trace 直接由 AgentLogger 管理，不再委托 DebugManager
- 日志初始化（_setup_logging）从 DebugManager 迁移到此处
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class TraceRecord:
    """一次完整推理的追踪记录（从 debug/logger.py 迁移 — Phase 5）"""
    timestamp: str
    session_id: str
    user_message: str
    messages_sent: list[dict] = field(default_factory=list)
    llm_responses: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    final_response: str = ""
    duration_ms: int = 0

    def format_summary(self) -> str:
        """格式化摘要（供 /debug 命令展示）"""
        lines = [
            "─── 最近一次推理过程 ───",
            f"用户: {self.user_message[:80]}",
            f"LLM 响应次数: {len(self.llm_responses)}",
            f"工具调用: {len(self.tool_calls)} 次",
        ]
        if self.tool_calls:
            for tc in self.tool_calls[:3]:
                lines.append(f"  - {tc.get('name', '?')}({str(tc.get('arguments', ''))[:40]})")
        if self.final_response:
            lines.append(f"最终回复: {self.final_response[:80]}")
        lines.append(f"耗时: {self.duration_ms}ms")
        lines.append("──" * 10)
        return "\n".join(lines)


logger = logging.getLogger("dotclaw.agent")


class AgentLogger:
    """
    结构化日志系统（合并 DebugManager 能力后 — Phase 5）。

    合并内容：
    - TraceRecord 从 debug/logger.py 迁移到此处
    - _last_trace 直接由 AgentLogger 管理，不再委托 DebugManager
    - 日志初始化（_setup_logging）从 DebugManager 迁移到此处
    """

    def __init__(self, level: str = "INFO", log_file: str | None = None):
        self._current_request_id: str | None = None
        self._last_trace: TraceRecord | None = None
        self._setup_logging(level, log_file)

    def _setup_logging(self, level: str, log_file: str | None):
        """日志初始化（从 DebugManager 迁移）"""
        handlers: list[logging.Handler] = [logging.StreamHandler()]
        if log_file:
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=handlers,
        )

    def new_request(self) -> str:
        """为新的 run() 调用生成唯一 request_id"""
        self._current_request_id = uuid4().hex[:8]
        return self._current_request_id

    @property
    def request_id(self) -> str | None:
        return self._current_request_id

    def record(self, trace: TraceRecord) -> None:
        """记录 TraceRecord"""
        self._last_trace = trace
        logger.debug(
            f"[{self._current_request_id}] request completed: "
            f"duration={trace.duration_ms}ms, "
            f"iterations={len(trace.llm_responses)}, "
            f"tool_calls={len(trace.tool_calls)}",
            extra={"request_id": self._current_request_id},
        )

    def get_last_trace(self) -> TraceRecord | None:
        """获取最近一次推理追踪（原由 DebugManager 提供）"""
        return self._last_trace

    def log_tool_call(self, tool_name: str, arguments: dict) -> None:
        logger.info(f"[{self._current_request_id}] tool call: {tool_name}")

    def log_tool_result(self, tool_name: str, result_len: int) -> None:
        logger.info(f"[{self._current_request_id}] tool result: {tool_name} ({result_len} chars)")

    def log_error(self, error: str) -> None:
        logger.error(f"[{self._current_request_id}] error: {error}")
