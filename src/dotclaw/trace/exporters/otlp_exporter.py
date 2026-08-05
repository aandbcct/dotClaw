"""RunTrace 的 OTLP 显式导出器。

将终态完整 ``RunTrace`` 映射为 OpenTelemetry Span，不引入 OTel SDK 自动上报。
调用方显式调用 ``export()``；OTel 异常转换为返回失败，从不影响 Runtime / Trace / Eval。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import RunTrace, SpanKind, TraceSpan, TraceSpanStatus, CONTENT_REDACTED_MARKER

# ── OTel 敏感字段隔离 ──
# 以下 OTel import 仅发生在本模块，不向 Runtime 泄漏。
# Runtime 包不应直接或间接引用 OTel。
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode, SpanKind as OtelSpanKind

# 字段名命中即脱敏（与 eval/redaction.py 同源）
_SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
    {"token", "api_key", "password", "authorization", "cookie", "secret",
     "key", "apikey", "passwd", "auth"}
)

# OTel 属性最大长度
_MAX_ATTR_VALUE_LEN: int = 10240


@dataclass(frozen=True)
class OtlpExportResult:
    """一次 OTLP 导出的结果摘要。"""

    exported_spans: int
    """成功映射的 Span 数量。"""
    success: bool
    """导出是否成功。"""
    provider: Any = None
    """OTel TracerProvider 实例（测试用内存 Exporter 时可读取）。"""
    error: str | None = None
    """失败时的错误描述。"""


class OtlpTraceExporter:
    """把终态完整 ``RunTrace`` 显式导出为 OTLP Span 批次。

    内部使用 OTel SDK 的 ``TracerProvider`` 和 ``SpanExporter``；
    不启动自动采集、不干扰 Runtime、不改变 Trace 或 Eval。
    """

    def __init__(self, exporter: SpanExporter | None = None) -> None:
        """用给定的 OTel SpanExporter 初始化；未提供时使用默认 Console Exporter。"""
        self._exporter: SpanExporter | None = exporter

    def export(
        self,
        trace: RunTrace,
        *,
        include_content: bool = False,
    ) -> OtlpExportResult:
        """将 ``RunTrace`` 映射为 OTLP Span 并导出。

        参数：
            trace: 终态完整 RunTrace；``is_partial=True`` 立即抛 ``ValueError``。
            include_content: 是否在属性中携带消息正文与工具输出（仍经脱敏）。

        返回：
            ``OtlpExportResult``——成功/失败、Span 计数与 Provider 引用。
        """
        if trace.is_partial:
            raise ValueError("部分 Trace 不能导出为 OTLP；请先修复运行完整性")

        # 有 INCOMPLETE Span 的 Trace 视为不可靠，拒绝导出
        incomplete = [s.span_id for s in trace.spans if s.status is TraceSpanStatus.INCOMPLETE]
        if incomplete:
            raise ValueError(f"Trace 包含 INCOMPLETE Span，无法导出：{incomplete}")

        # 构建 Provider + Tracer
        exporter = self._exporter or _default_exporter()
        provider = TracerProvider()
        try:
            provider.add_span_processor(
                __import__("opentelemetry.sdk.trace.export", fromlist=["SimpleSpanProcessor"])
                .SimpleSpanProcessor(exporter)
            )
        except Exception:
            # add_span_processor 在某些版本要求 BatchSpanProcessor
            provider.add_span_processor(
                __import__("opentelemetry.sdk.trace.export", fromlist=["BatchSpanProcessor"])
                .BatchSpanProcessor(exporter)
            )

        tracer = provider.get_tracer("dotclaw.trace.otlp")

        # ── 映射全部 Span ──
        # Step 1: 先创建所有 Span（按 start_event_sequence 排序）
        span_map: dict[str, otel_trace.Span] = {}
        sorted_spans = sorted(
            trace.spans,
            key=lambda s: (s.start_event_sequence is None, s.start_event_sequence or 0),
        )

        for ts in sorted_spans:
            ctx = None
            if ts.parent_span_id is not None and ts.parent_span_id in span_map:
                ctx = otel_trace.set_span_in_context(span_map[ts.parent_span_id])

            otel_span = tracer.start_span(
                name=_span_name(ts),
                kind=_otel_kind(ts.kind),
                context=ctx,
                start_time=_parse_time(ts.started_at),
                attributes=_build_attributes(trace, ts, include_content=include_content),
            )

            if ts.ended_at is not None:
                otel_span.set_status(_otel_status(ts.status))
                otel_span.end(end_time=_parse_time(ts.ended_at))
            else:
                # 未结束 Span（不应发生在终态 Trace，但防御性处理）
                otel_span.set_status(StatusCode.ERROR, "未结束")
                otel_span.end()

            span_map[ts.span_id] = otel_span

        # Step 2: 关闭 Provider 触发导出
        try:
            provider.force_flush()
            provider.shutdown()
        except Exception as e:
            return OtlpExportResult(
                exported_spans=len(span_map),
                success=False,
                provider=provider,
                error=f"OTel 导出失败：{e}",
            )

        return OtlpExportResult(
            exported_spans=len(span_map),
            success=True,
            provider=provider,
        )


# ── 内部映射辅助 ──────────────────────────────────────────────────


def _span_name(span: TraceSpan) -> str:
    """按 SpanKind 生成语义名称。"""
    tool_name = span.attributes.get("tool_name", "")
    if span.kind is SpanKind.RUN:
        return f"dotclaw.run.{span.attributes.get('run_id', span.span_id[:8])}"
    elif span.kind is SpanKind.LLM:
        return f"dotclaw.llm.{span.attributes.get('model_id', 'call')}"
    elif span.kind is SpanKind.TOOL:
        return f"dotclaw.tool.{tool_name}" if tool_name else "dotclaw.tool.call"
    elif span.kind is SpanKind.APPROVAL:
        return f"dotclaw.approval.{span.attributes.get('approval_id', 'wait')}"
    elif span.kind is SpanKind.DELEGATION:
        target = span.attributes.get("target_agent_id", "child")
        return f"dotclaw.delegation.{target}"
    return "dotclaw.span.unknown"


def _otel_kind(kind: SpanKind) -> OtelSpanKind:
    """SpanKind → OTel SpanKind。"""
    if kind is SpanKind.RUN:
        return OtelSpanKind.INTERNAL
    return OtelSpanKind.INTERNAL


def _otel_status(status: TraceSpanStatus) -> otel_trace.Status:
    """TraceSpanStatus → OTel Status。FAILED → ERROR，其余 → OK。"""
    if status is TraceSpanStatus.FAILED:
        return otel_trace.Status(StatusCode.ERROR)
    return otel_trace.Status(StatusCode.OK)


def _parse_time(iso_str: str | None) -> int | None:
    """ISO 时间字符串 → OTel 纳秒时间戳；无法解析返回 None。"""
    if not iso_str:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1_000_000_000)
    except (ValueError, TypeError):
        return None


def _build_attributes(
    trace: RunTrace,
    span: TraceSpan,
    *,
    include_content: bool = False,
) -> dict[str, object]:
    """构造 OTel Span 属性，遵循默认脱敏规则。"""
    attrs: dict[str, object] = {
        "dotclaw.schema_version": trace.schema_version,
        "dotclaw.run_id": trace.run.run_id,
        "dotclaw.span_kind": span.kind.value,
        "dotclaw.span_status": span.status.value,
        "dotclaw.record_hash": trace.source.record_hash,
    }

    # ── 安全属性（直接透传，不涉敏感信息）──
    for safe_key in (
        "call_index", "model_id", "context_version",
        "call_id", "tool_name", "approval_id", "approved",
        "child_run_id", "target_agent_id", "task_id",
        "outcome", "run_id",
    ):
        val = span.attributes.get(safe_key)
        if val is not None:
            attrs[f"dotclaw.{safe_key}"] = _safe_attr(val)

    # event sequence
    if span.start_event_sequence is not None:
        attrs["dotclaw.start_event_sequence"] = span.start_event_sequence
    if span.end_event_sequence is not None:
        attrs["dotclaw.end_event_sequence"] = span.end_event_sequence

    # ── 内容属性（仅 include_content=True 时，仍经脱敏）──
    if include_content:
        _add_content_attrs(trace, span, attrs)
    else:
        # 默认模式：正文用占位标记并明确告知
        if span.message_ids:
            attrs["dotclaw.message_ids"] = [str(mid) for mid in span.message_ids]
            attrs["dotclaw.content_note"] = CONTENT_REDACTED_MARKER

    return attrs


def _safe_attr(val: object) -> object:
    """把属性值裁剪到 OTel 允许的范围内。"""
    if val is None:
        return ""
    if isinstance(val, bool):
        return str(val).lower()
    if isinstance(val, (int, float, str)):
        s = str(val)
        return s[:_MAX_ATTR_VALUE_LEN]
    return str(val)[:_MAX_ATTR_VALUE_LEN]


def _redact_value(val: str) -> str:
    """对字符串进行字段名无关的启发式脱敏。"""
    import re
    patterns = (
        re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
        re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),
        re.compile(r"\bghp_[0-9A-Za-z]{36}\b"),
    )
    result = val
    for p in patterns:
        if p.search(result):
            result = p.sub(CONTENT_REDACTED_MARKER, result)
    return result


def _add_content_attrs(trace: RunTrace, span: TraceSpan, attrs: dict[str, object]) -> None:
    """在 include_content=True 下附加消息正文与工具输出（经脱敏）。"""
    from dotclaw.eval.scorers._helpers import message_by_id

    for mid in span.message_ids:
        msg = message_by_id(trace, mid)
        if msg is None:
            continue
        # 对每个消息的内容做启发式脱敏
        safe_content = _redact_value(msg.content)
        if msg.tool_calls:
            safe_tools = [
                {"name": tc.name, "call_id": tc.call_id,
                 "arguments": {k: _redact_value(str(v)) for k, v in tc.arguments.items()}}
                for tc in msg.tool_calls
            ]
            import json
            attrs[f"dotclaw.message.{mid}.tool_calls"] = json.dumps(safe_tools, ensure_ascii=False)[:_MAX_ATTR_VALUE_LEN]
        if safe_content:
            attrs[f"dotclaw.message.{mid}.content"] = safe_content[:_MAX_ATTR_VALUE_LEN]


def _default_exporter() -> SpanExporter:
    """默认 Console Exporter（无外部依赖时可用）。"""
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter
    return ConsoleSpanExporter()
