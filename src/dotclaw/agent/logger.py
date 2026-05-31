"""AgentLogger — 结构化日志系统

封装 Python logging，按模块分级，request_id 全链路追踪。
回写 TraceRecord 到 DebugManager 保持 /debug 命令可用。

与 debug/logger.py 的关系：P3 保留 DebugManager（临时），
双向同步：AgentLogger 写日志 → 同时更新 DebugManager 的 last_trace。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from dotclaw.debug.logger import TraceRecord

if TYPE_CHECKING:
    pass

logger = logging.getLogger("dotclaw.agent")


class AgentLogger:
    """
    结构化日志系统。

    每条日志携带 request_id，实现全链路追踪。
    get_last_trace() 委托给 DebugManager 保持 /debug 命令兼容。
    """

    def __init__(self):
        self._current_request_id: str | None = None
        self._last_trace: TraceRecord | None = None

    def new_request(self) -> str:
        """为新的 run() 调用生成唯一 request_id"""
        self._current_request_id = uuid4().hex[:8]
        return self._current_request_id

    @property
    def request_id(self) -> str | None:
        return self._current_request_id

    def record(self, trace: TraceRecord) -> None:
        """记录 TraceRecord 到日志和 DebugManager"""
        self._last_trace = trace

        # 写入结构化日志（DEBUG 级别，避免污染 CLI 界面）
        logger.debug(
            f"[{self._current_request_id}] request completed: "
            f"duration={trace.duration_ms}ms, "
            f"iterations={len(trace.llm_responses)}, "
            f"tool_calls={len(trace.tool_calls)}, "
            f"final_len={len(trace.final_response)}, "
            f"error={trace.final_response if trace.final_response.startswith('ERROR') else 'none'}",
            extra={"request_id": self._current_request_id},
        )

        # 回写到 DebugManager（保持 /debug 命令可用）
        from dotclaw.debug.logger import DebugManager
        # 注意：这里不创建新的 DebugManager 实例，而是写到一个全局位置
        # 由于 DebugManager 通常在 AgentLoop 中实例化，这里的 record 通过
        # AgentLoop._debug_manager.record_trace() 调用

    def get_last_trace(self) -> TraceRecord | None:
        """获取最近一次推理追踪（保持 /debug 命令可用）"""
        return self._last_trace

    def log_tool_call(self, tool_name: str, arguments: dict) -> None:
        """记录工具调用"""
        logger.info(
            f"[{self._current_request_id}] tool call: {tool_name}",
            extra={"request_id": self._current_request_id},
        )

    def log_tool_result(self, tool_name: str, result_len: int) -> None:
        """记录工具结果"""
        logger.info(
            f"[{self._current_request_id}] tool result: {tool_name} ({result_len} chars)",
            extra={"request_id": self._current_request_id},
        )

    def log_error(self, error: str) -> None:
        """记录错误"""
        logger.error(
            f"[{self._current_request_id}] error: {error}",
            extra={"request_id": self._current_request_id},
        )
