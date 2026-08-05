"""RunTrace：Runtime v4 权威事实的只读追踪重建。

对外公开最小接口：``assemble_trace``（纯函数）、``TraceService``（仓储读取）、
``JsonTraceExporter``（显式 JSON 导出）与模型类型。Trace 不写回 Runtime，JSON 文件
不是查询或恢复来源。
"""

from __future__ import annotations

from .assembler import assemble_trace
from .exporters import JsonTraceExporter, OtlpTraceExporter, OtlpExportResult
from .models import (
    CONTENT_REDACTED_MARKER,
    RunTrace,
    RunTraceSource,
    SpanKind,
    TraceIssue,
    TraceIssueKind,
    TraceMetrics,
    TraceSpan,
    TraceSpanStatus,
)
from .service import TraceService

__all__ = [
    "assemble_trace",
    "TraceService",
    "JsonTraceExporter",
    "OtlpTraceExporter",
    "OtlpExportResult",
    "CONTENT_REDACTED_MARKER",
    "RunTrace",
    "RunTraceSource",
    "SpanKind",
    "TraceIssue",
    "TraceIssueKind",
    "TraceMetrics",
    "TraceSpan",
    "TraceSpanStatus",
]
