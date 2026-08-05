"""RunTrace 的 OTLP 显式导出器。

将终态完整 ``RunTrace`` 映射为 OpenTelemetry Span，不引入 OTel SDK 自动上报。
调用方显式调用 ``export()``；OTel 异常转换为返回失败，从不影响 Runtime / Trace / Eval。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import RunTrace, SpanKind, TraceSpan, TraceSpanStatus, CONTENT_REDACTED_MARKER

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode, SpanKind as OtelSpanKind

# 字段名命中即脱敏（与 eval/redaction.py 同源）
_SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
    {"token", "api_key", "password", "authorization", "cookie", "secret",
     "key", "apikey", "passwd", "auth"}
)

_MAX_ATTR_VALUE_LEN: int = 10240


@dataclass(frozen=True)
class OtlpExportResult:
    """一次 OTLP 导出的结果摘要。"""

    exported_spans: int
    success: bool
    provider: Any = None
    error: str | None = None


class _TrackedExporter(SpanExporter):
    """包装 SpanExporter 以跟踪 FAILURE 次数。"""

    def __init__(self, wrapped: SpanExporter) -> None:
        self._wrapped = wrapped
        self.failure_count: int = 0

    def export(self, spans):
        result = self._wrapped.export(spans)
        if result is SpanExportResult.FAILURE:
            self.failure_count += 1
        return result

    def shutdown(self):
        self._wrapped.shutdown()

    def force_flush(self, timeout_millis=None):
        return True


class OtlpTraceExporter:
    """把终态完整 ``RunTrace`` 显式导出为 OTLP Span 批次。

    Exporter 返回 ``SpanExportResult.FAILURE`` 时，最终 ``success=False``。
    """

    def __init__(self, exporter: SpanExporter | None = None) -> None:
        self._exporter: SpanExporter | None = exporter

    def export(self, trace: RunTrace, *, include_content: bool = False) -> OtlpExportResult:
        if not trace.run.state.is_ended():
            raise ValueError("非终态 Trace 不能导出为 OTLP；请等待运行完成")

        if trace.is_partial:
            raise ValueError("部分 Trace 不能导出为 OTLP")

        incomplete = [s.span_id for s in trace.spans if s.status is TraceSpanStatus.INCOMPLETE]
        if incomplete:
            raise ValueError(f"Trace 包含 INCOMPLETE Span，无法导出：{incomplete}")

        raw = self._exporter or _default_exporter()
        exporter = _TrackedExporter(raw)
        provider = TracerProvider()

        try:
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        except Exception:
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            provider.add_span_processor(BatchSpanProcessor(exporter))

        tracer = provider.get_tracer("dotclaw.trace.otlp")

        sorted_spans = sorted(
            trace.spans,
            key=lambda s: (s.start_event_sequence is None, s.start_event_sequence or 0),
        )
        span_map: dict[str, otel_trace.Span] = {}

        for ts in sorted_spans:
            ctx = None
            if ts.parent_span_id is not None and ts.parent_span_id in span_map:
                ctx = otel_trace.set_span_in_context(span_map[ts.parent_span_id])

            otel_span = tracer.start_span(
                name=_span_name(ts),
                kind=_otel_kind(ts.kind),
                context=ctx,
                start_time=_parse_time(ts.started_at),
                attributes=_build_attrs(trace, ts, include_content=include_content),
            )

            if ts.ended_at is not None:
                otel_span.set_status(_otel_status(ts.status))
                otel_span.end(end_time=_parse_time(ts.ended_at))
            else:
                otel_span.set_status(StatusCode.ERROR, "未结束")
                otel_span.end()

            span_map[ts.span_id] = otel_span

        try:
            provider.force_flush()
            provider.shutdown()
        except Exception as e:
            return OtlpExportResult(len(sorted_spans), False, provider, f"OTel 导出异常：{e}")

        if exporter.failure_count > 0:
            return OtlpExportResult(len(sorted_spans), False, provider,
                                    f"Exporter 报告 {exporter.failure_count} 次 FAILURE")

        return OtlpExportResult(len(sorted_spans), True, provider)


# ── Span 映射 ───────────────────────────────────────────────────────


def _span_name(span: TraceSpan) -> str:
    tool = span.attributes.get("tool_name", "")
    if span.kind is SpanKind.RUN:
        return f"dotclaw.run.{span.attributes.get('run_id', span.span_id[:8])}"
    if span.kind is SpanKind.LLM:
        return f"dotclaw.llm.{span.attributes.get('model_id', 'call')}"
    if span.kind is SpanKind.TOOL:
        return f"dotclaw.tool.{tool}" if tool else "dotclaw.tool.call"
    if span.kind is SpanKind.APPROVAL:
        return f"dotclaw.approval.{span.attributes.get('approval_id', 'wait')}"
    if span.kind is SpanKind.DELEGATION:
        return f"dotclaw.delegation.{span.attributes.get('target_agent_id', 'child')}"
    return "dotclaw.span.unknown"


def _otel_kind(kind: SpanKind) -> OtelSpanKind:
    return OtelSpanKind.INTERNAL


def _otel_status(status: TraceSpanStatus) -> otel_trace.Status:
    if status is TraceSpanStatus.FAILED:
        return otel_trace.Status(StatusCode.ERROR)
    return otel_trace.Status(StatusCode.OK)


def _parse_time(iso_str: str | None) -> int | None:
    if not iso_str:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1_000_000_000)
    except (ValueError, TypeError):
        return None


# ── 属性构�� ─────────────────────────────────────────────────────────


def _build_attrs(trace: RunTrace, span: TraceSpan, *, include_content: bool = False) -> dict[str, object]:
    attrs: dict[str, object] = {
        "dotclaw.schema_version": trace.schema_version,
        "dotclaw.run_id": trace.run.run_id,
        "dotclaw.span_kind": span.kind.value,
        "dotclaw.span_status": span.status.value,
        "dotclaw.record_hash": trace.source.record_hash,
    }

    for safe_key in (
        "call_index", "model_id", "context_version",
        "call_id", "tool_name", "approval_id", "approved",
        "child_run_id", "target_agent_id", "task_id",
        "outcome", "run_id",
    ):
        val = span.attributes.get(safe_key)
        if val is not None:
            attrs[f"dotclaw.{safe_key}"] = _safe_val(val)

    if span.start_event_sequence is not None:
        attrs["dotclaw.start_event_sequence"] = span.start_event_sequence
    if span.end_event_sequence is not None:
        attrs["dotclaw.end_event_sequence"] = span.end_event_sequence

    if include_content:
        _add_content_attrs(trace, span, attrs)
    elif span.message_ids:
        attrs["dotclaw.message_ids"] = [str(mid) for mid in span.message_ids]
        attrs["dotclaw.content_note"] = CONTENT_REDACTED_MARKER

    return attrs


def _safe_val(val: object) -> object:
    if val is None:
        return ""
    if isinstance(val, bool):
        return str(val).lower()
    s = str(val)
    return s[:_MAX_ATTR_VALUE_LEN]


def _default_exporter() -> SpanExporter:
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter
    return ConsoleSpanExporter()


# ── 内容属性（include_content=True） ─────────────────────────────────


def _add_content_attrs(trace: RunTrace, span: TraceSpan, attrs: dict[str, object]) -> None:
    from dotclaw.eval.scorers._helpers import message_by_id

    for mid in span.message_ids:
        msg = message_by_id(trace, mid)
        if msg is None:
            continue

        safe_content = _redact_content(msg.content)
        if safe_content:
            attrs[f"dotclaw.message.{mid}.content"] = safe_content[:_MAX_ATTR_VALUE_LEN]

        if msg.tool_calls:
            safe_tools = []
            for tc in msg.tool_calls:
                safe_args = {}
                for k, v in tc.arguments.items():
                    v_str = str(v)
                    # 先按字段名脱敏，再按凭证模式脱敏
                    if _is_sensitive_field(k):
                        v_str = CONTENT_REDACTED_MARKER
                    else:
                        v_str = _redact_patterns(v_str)
                    safe_args[k] = v_str
                safe_tools.append({
                    "name": tc.name,
                    "call_id": tc.call_id,
                    "arguments": safe_args,
                })
            import json
            attrs[f"dotclaw.message.{mid}.tool_calls"] = json.dumps(
                safe_tools, ensure_ascii=False,
            )[:_MAX_ATTR_VALUE_LEN]


def _is_sensitive_field(name: str) -> bool:
    """检查字段名是否属于敏感字段。"""
    return name.lower() in _SENSITIVE_FIELD_NAMES


def _redact_content(text: str) -> str:
    """先字段名无关凭证模式脱敏。"""
    return _redact_patterns(text)


def _redact_patterns(text: str) -> str:
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
    result = text
    for p in patterns:
        if p.search(result):
            result = p.sub(CONTENT_REDACTED_MARKER, result)
    return result
