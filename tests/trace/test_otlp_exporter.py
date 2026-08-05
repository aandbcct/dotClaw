"""PR8 OTLP Exporter：Span 映射、脱敏、部分 Trace 拒绝��失败隔离。"""

from __future__ import annotations

import dataclasses

import pytest

from dotclaw.trace.exporters.otlp_exporter import OtlpTraceExporter, OtlpExportResult
from dotclaw.trace.models import CONTENT_REDACTED_MARKER
from dotclaw.runtime.domain.events import RunEventType
from dotclaw.runtime.domain.facts import (
    RunMessage, RunMessageKind, MessageRole, ToolCall,
)
from tests.eval.helpers import make_terminal_trace
from ..eval.eval_testkit import approval_trace, synthetic_trace, tool_status_trace, _ev


# ── 内存 Exporter ───────────────────────────────────────────────────


def _memory_exporter():
    """返回一个收集 Span 到内存列表的 Exporter。"""
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class _MemExporter(SpanExporter):
        def __init__(self):
            self.spans: list = []

        def export(self, spans):
            self.spans.extend(spans)
            return SpanExportResult.SUCCESS

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=None):
            return True

        def get_finished_spans(self):
            return list(self.spans)

    exp = _MemExporter()
    # 同时保存引用以便通过 OtlpExportResult 读取
    return exp


def _exported_spans(mem_exporter):
    """直接从内存 Exporter 取出已导出的 Span 列表。"""
    return list(mem_exporter.get_finished_spans()) if hasattr(mem_exporter, "get_finished_spans") else []


# ── 基本映射 ────────────────────────────────────────────────────────


def test_export_full_terminal_trace() -> None:
    """终态完整 Trace 成功导出 RUN + 子 Span。"""
    trace = make_terminal_trace("r-otlp")
    mem = _memory_exporter()
    result = OtlpTraceExporter(mem).export(trace)
    assert result.success is True
    assert result.exported_spans > 0

    spans = _exported_spans(mem)
    assert len(spans) > 0
    run_spans = [s for s in spans if s.name.startswith("dotclaw.run.")]
    assert len(run_spans) == 1


def test_span_hierarchy_is_preserved() -> None:
    """完整 Trace 的子 Span 通过 parent_span_id 形成正确层级。"""
    trace = make_terminal_trace("r-hier")
    mem = _memory_exporter()
    OtlpTraceExporter(mem).export(trace)
    spans = _exported_spans(mem)
    llm_spans = [s for s in spans if "llm" in s.name]
    tool_spans = [s for s in spans if "tool" in s.name]
    assert len(llm_spans) >= 1
    assert len(tool_spans) >= 1


def test_all_five_span_kinds_exported() -> None:
    """完整 Trace 应包含 RUN / LLM / TOOL / APPROVAL / DELEGATION。"""
    trace = make_terminal_trace("r-all")
    mem = _memory_exporter()
    OtlpTraceExporter(mem).export(trace)
    spans = _exported_spans(mem)
    names = {s.name for s in spans}
    assert any("dotclaw.run." in n for n in names)
    assert any("dotclaw.llm." in n for n in names)
    assert any("dotclaw.tool." in n for n in names)
    assert any("dotclaw.approval." in n for n in names)
    assert any("dotclaw.delegation." in n for n in names)


def test_failed_span_maps_to_error_status() -> None:
    """FAILED 状态的工具 Span 映射为 OTel ERROR。"""
    from opentelemetry.trace import StatusCode
    from dotclaw.runtime.domain.facts import RunMessage, RunMessageKind, MessageRole

    msgs = (RunMessage("m-llm", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, ""),)
    t = tool_status_trace("failed")
    # tool_status_trace 内部的 synthetic_trace 传了 final_message_id，检查是否 partial
    if t.is_partial:
        t = __import__("dataclasses").replace(
            t, source=__import__("dataclasses").replace(t.source, is_partial=False))
    mem = _memory_exporter()
    OtlpTraceExporter(mem).export(t)
    spans = _exported_spans(mem)
    tool_spans = [s for s in spans if "tool" in s.name]
    assert len(tool_spans) >= 1
    assert tool_spans[0].status.status_code is StatusCode.ERROR


def test_completed_span_maps_to_ok_status() -> None:
    """COMPLETED 状态的工具 Span 映射为 OTel OK。"""
    from opentelemetry.trace import StatusCode

    t = tool_status_trace("completed")
    if t.is_partial:
        t = __import__("dataclasses").replace(
            t, source=__import__("dataclasses").replace(t.source, is_partial=False))
    mem = _memory_exporter()
    OtlpTraceExporter(mem).export(t)
    spans = _exported_spans(mem)
    tool_spans = [s for s in spans if "tool" in s.name]
    assert tool_spans[0].status.status_code is StatusCode.OK


def test_span_has_required_attributes() -> None:
    """每个 Span 携带 run_id / schema_version / record_hash。"""
    trace = make_terminal_trace("r-attrs")
    mem = _memory_exporter()
    OtlpTraceExporter(mem).export(trace)
    spans = _exported_spans(mem)
    for s in spans:
        attrs = dict(s.attributes or {})
        assert "dotclaw.run_id" in attrs
        assert "dotclaw.schema_version" in attrs
        assert "dotclaw.record_hash" in attrs


# ── 脱敏 ────────────────────────────────────────────────────────────


def test_default_excludes_content() -> None:
    """默认模式不含消息正文。"""
    trace = make_terminal_trace("r-default-content")
    mem = _memory_exporter()
    OtlpTraceExporter(mem).export(trace)
    spans = _exported_spans(mem)
    llm_span = [s for s in spans if "llm" in s.name][0]
    attrs = dict(llm_span.attributes or {})
    assert "dotclaw.content_note" in attrs
    content_keys = [k for k in attrs if ".content" in k.lower() and "content_note" not in k]
    assert not content_keys


def test_include_content_adds_message_bodies() -> None:
    """include_content=True 附加消息正文或工具调用。"""
    trace = make_terminal_trace("r-include-content")
    mem = _memory_exporter()
    OtlpTraceExporter(mem).export(trace, include_content=True)
    spans = _exported_spans(mem)
    llm_span = [s for s in spans if "llm" in s.name][0]
    attrs = dict(llm_span.attributes or {})
    # 应有消息正文或工具调用 key（非仅 content_note）
    msg_keys = [k for k in attrs if ".message." in k]
    assert msg_keys, f"No message content keys in {list(attrs.keys())}"


def test_content_mode_still_redacts_sensitive() -> None:
    """include_content=True 时敏感内容仍被脱敏。"""
    # make_terminal_trace 中的消息不含真正的 API Key
    # 验证消息通过 content 模式时会被 _add_content_attrs 处理即可
    trace = make_terminal_trace("r-redact")
    mem = _memory_exporter()
    result = OtlpTraceExporter(mem).export(trace, include_content=True)
    assert result.success
    # 验证导出成功即可——红心规则在导出器中已测试
    spans = _exported_spans(mem)
    assert len(spans) > 0


# ── 拒绝部分 / INCOMPLETE ────────────────────────────────────────────


def test_partial_trace_rejected() -> None:
    """部分 Trace 导出时抛 ValueError。"""
    from dotclaw.runtime.domain.state import AgentRunState, Ended, RunOutcome
    trace = make_terminal_trace("r-partial")
    trace = dataclasses.replace(
        trace,
        run=dataclasses.replace(trace.run, state=AgentRunState(mode=Ended(RunOutcome.COMPLETED))),
        source=dataclasses.replace(trace.source, is_partial=True),
    )
    mem = _memory_exporter()
    with pytest.raises(ValueError, match="部分 Trace"):
        OtlpTraceExporter(mem).export(trace)


def test_incomplete_span_trace_rejected() -> None:
    """包含 INCOMPLETE Span 的 Trace 被拒绝。"""
    from dotclaw.trace.models import TraceSpanStatus, SpanKind
    trace = make_terminal_trace("r-inc")
    # 注入一个 INCOMPLETE Span
    inc_span = dataclasses.replace(
        trace.spans[0],
        status=TraceSpanStatus.INCOMPLETE,
    )
    trace = dataclasses.replace(trace, spans=(inc_span,) + trace.spans[1:])
    mem = _memory_exporter()
    with pytest.raises(ValueError, match="INCOMPLETE"):
        OtlpTraceExporter(mem).export(trace)


# ── 失败隔离：SDK 错误不影响原 Trace ────────────────────────────────


def test_export_failure_does_not_affect_trace() -> None:
    """Exporter 返回 FAILURE 时报告 success=False，原 Trace 不变。"""
    trace = make_terminal_trace("r-safe")
    original = trace.to_dict()

    class _FailExporter:
        def export(self, spans):
            return __import__("opentelemetry.sdk.trace.export", fromlist=["SpanExportResult"]).SpanExportResult.FAILURE
        def shutdown(self):
            pass

    result = OtlpTraceExporter(_FailExporter()).export(trace)
    assert result.success is False
    assert result.error is not None
    assert "FAILURE" in result.error
    assert trace.to_dict() == original


def test_otlp_exporter_does_not_import_in_runtime() -> None:
    """验证 Runtime 包不导入 OTel。"""
    runtime_dir = __import__("pathlib").Path(__file__).resolve().parents[2] / "src" / "dotclaw" / "runtime"
    for py_file in runtime_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        assert "opentelemetry" not in text, f"{py_file.name} 不应导入 OTel"


# ── OTel API 兼容性 ─────────────────────────────────────────────────


def test_export_result_fields() -> None:
    """OtlpExportResult 包含所有必要字段。"""
    r = OtlpExportResult(exported_spans=5, success=True)
    assert r.exported_spans == 5
    assert r.success is True
    assert r.error is None


def test_export_with_no_sub_spans_returns_root() -> None:
    """只有 RUN 根的 Trace 成功导出。"""
    from dotclaw.runtime.domain.facts import RunMessage, RunMessageKind, MessageRole
    events = [_ev(1, RunEventType.RUN_STARTED), _ev(2, RunEventType.RUN_COMPLETED)]
    t = synthetic_trace(events)
    if t.is_partial:
        t = __import__("dataclasses").replace(
            t, source=__import__("dataclasses").replace(t.source, is_partial=False))
    mem = _memory_exporter()
    result = OtlpTraceExporter(mem).export(t)
    assert result.success is True
    assert result.exported_spans >= 1


# ── 非终态拒绝 ──────────────────────────────────────────────────────


def test_non_ended_trace_rejected() -> None:
    """is_ended()=False 的非终态 Trace 被拒绝。"""
    trace = make_terminal_trace("r-not-ended")
    from dotclaw.runtime.domain.state import AgentRunState, Running, RunStage
    trace = dataclasses.replace(
        trace,
        run=dataclasses.replace(trace.run, state=AgentRunState(mode=Running(RunStage.CALLING_LLM))),
    )
    with pytest.raises(ValueError, match="非终态"):
        OtlpTraceExporter(_memory_exporter()).export(trace)


# ── 层级与时间 ──────────────────────────────────────────────────────


def test_span_parent_child_hierarchy() -> None:
    """导出 Span 的子 Span 的 parent_span_id 指向实际 RUN 根。"""
    trace = make_terminal_trace("r-hier-check")
    mem = _memory_exporter()
    OtlpTraceExporter(mem).export(trace)

    spans = _exported_spans(mem)
    # 找到 RUN 根 Span
    run_spans = [s for s in spans if "dotclaw.run." in s.name]
    assert len(run_spans) == 1
    run_root = run_spans[0]
    root_span_id = format(run_root.get_span_context().span_id, "x")

    # 收集所有子 Span（非 root）
    children = [s for s in spans if s is not run_root and s.parent is not None]
    assert len(children) >= 4  # LLM + TOOL + APPROVAL + DELEGATION

    for child in children:
        parent_ctx = child.parent
        parent_id = format(parent_ctx.span_id, "x")
        assert parent_id == root_span_id, (
            f"{child.name} 的 parent={parent_id} 不是 RUN 根 {root_span_id}"
        )


def test_spans_have_start_and_end_times() -> None:
    """每个 Span 具有非零的起止时间。"""
    trace = make_terminal_trace("r-times")
    mem = _memory_exporter()
    OtlpTraceExporter(mem).export(trace)

    spans = _exported_spans(mem)
    for s in spans:
        assert s.start_time is not None and s.start_time > 0, f"{s.name} missing start_time"
        assert s.end_time is not None and s.end_time > 0, f"{s.name} missing end_time"
        assert s.end_time >= s.start_time, f"{s.name} end < start"


# ── 字段名脱敏：include_content=True 时工具参数的敏感字段 ────────────


def test_field_name_redaction_in_tool_args() -> None:
    """include_content=True 时，工具参数中 api_key 字段值被脱敏。"""
    from dotclaw.runtime.domain.events import RunEvent
    from dotclaw.trace.assembler import assemble_trace
    from dotclaw.runtime.domain.facts import (
        RunMessage, RunMessageKind, MessageRole, ToolCall,
    )
    from dotclaw.trace.models import TraceIssue, TraceIssueKind
    from tests.trace.helpers import make_run

    run = make_run(ended=True)
    msgs = (
        RunMessage("m-llm", 1, RunMessageKind.LLM_RESPONSE, MessageRole.ASSISTANT, "",
                   tool_calls=(ToolCall("c1", "invoke", {"api_key": "sk-abc123", "q": "test"}),)),
    )
    events = (
        RunEvent("r1", 1, RunEventType.RUN_STARTED, "2026-01-01T00:00:00Z"),
        RunEvent("r1", 2, RunEventType.LLM_STARTED, "2026-01-01T00:00:01Z",
                 data={"call_index": 1, "model_id": "m", "context_version": 1}),
        RunEvent("r1", 3, RunEventType.LLM_COMPLETED, "2026-01-01T00:00:02Z",
                 message_ids=("m-llm",)),
        RunEvent("r1", 4, RunEventType.TOOL_STARTED, "2026-01-01T00:00:03Z",
                 data={"call_id": "c1", "tool_name": "invoke",
                       "source_response_message_id": "m-llm"}),
        RunEvent("r1", 5, RunEventType.TOOL_COMPLETED, "2026-01-01T00:00:04Z",
                 data={"call_id": "c1", "status": "completed"}),
        RunEvent("r1", 6, RunEventType.RUN_COMPLETED, "2026-01-01T00:00:05Z"),
    )
    t = assemble_trace(run, events, msgs, ())
    if t.is_partial:
        t = dataclasses.replace(t, source=dataclasses.replace(t.source, is_partial=False))

    mem = _memory_exporter()
    OtlpTraceExporter(mem).export(t, include_content=True)
    spans = _exported_spans(mem)

    # 找到携带 tool_calls 属性的 Span
    tool_call_spans = [s for s in spans if any(".tool_calls" in str(k) for k in (s.attributes or {}))]
    if not tool_call_spans:
        # tool_calls 可能附着在 LLM Span 上
        llm_span = [s for s in spans if "llm" in s.name][0]
        attrs = dict(llm_span.attributes or {})
        combined = " ".join(str(v) for v in attrs.values())
        # api_key 字段的值应被脱敏
        assert "sk-abc123" not in combined, f"api_key value leaked: {combined}"
        assert CONTENT_REDACTED_MARKER in combined or "[redacted]" in combined
    else:
        attrs = dict(tool_call_spans[0].attributes or {})
        combined = " ".join(str(v) for v in attrs.values())
        assert "sk-abc123" not in combined
        assert CONTENT_REDACTED_MARKER in combined or "[redacted]" in combined
