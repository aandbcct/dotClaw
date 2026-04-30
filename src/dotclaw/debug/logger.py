"""调试/日志模块"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class TraceRecord:
    """一次完整推理的追踪记录"""
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
            for tc in self.tool_calls[:3]:  # 最多显示 3 个
                lines.append(f"  - {tc.get('name', '?')}({str(tc.get('arguments', ''))[:40]})")
        if self.final_response:
            lines.append(f"最终回复: {self.final_response[:80]}")
        lines.append(f"耗时: {self.duration_ms}ms")
        lines.append("──" * 10)
        return "\n".join(lines)


class DebugManager:
    """调试管理"""

    def __init__(self, level: str = "INFO", log_file: str | None = None):
        self._setup_logging(level, log_file)
        self.logger = logging.getLogger("dotclaw")
        self._last_trace: TraceRecord | None = None

    def _setup_logging(self, level: str, log_file: str | None):
        handlers: list[logging.Handler] = [logging.StreamHandler()]
        if log_file:
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=handlers,
        )

    def record_trace(self, trace: TraceRecord):
        self._last_trace = trace

    def get_last_trace(self) -> TraceRecord | None:
        return self._last_trace
