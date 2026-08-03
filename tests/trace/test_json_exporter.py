"""JsonTraceExporter 显式导出、安全内容模式与部分 Trace 开关测试。"""

from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from helpers import make_context_version, make_event, make_message, make_run

from dotclaw.runtime.domain.events import RunEventType
from dotclaw.runtime.domain.facts import MessageRole, RunMessageKind, ToolCall
from dotclaw.trace import CONTENT_REDACTED_MARKER, JsonTraceExporter, assemble_trace


def _completed_trace():
    run = make_run()
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "model-1", "context_version": 1}),
        make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm",)),
        make_event("run-1", 4, RunEventType.RUN_COMPLETED, message_ids=("msg-llm",)),
    )
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER, content="what is 1+1"),
        make_message("msg-llm", 2, RunMessageKind.LLM_RESPONSE, content="the answer is 2"),
    )
    return assemble_trace(run, events, messages, (make_context_version(1),))


def _partial_trace():
    run = make_run(ended=False)
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "model-1", "context_version": 1}),
    )
    messages = (make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER, content="in progress"),)
    return assemble_trace(run, events, messages, (make_context_version(1),))


def test_export_default_omits_full_content(tmp_path):
    trace = _completed_trace()
    exporter = JsonTraceExporter()
    path = tmp_path / "trace.json"
    returned = exporter.export(trace, path)
    assert returned == path
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["source"]["record_hash"] == trace.source.record_hash
    msg = data["messages"][1]
    # 默认模式不得出现任何正文字段（含截断预览）。
    assert "content" not in msg
    assert "content_preview" not in msg
    assert "tool_calls" not in msg
    assert msg["content_redacted"] == CONTENT_REDACTED_MARKER
    assert msg["content_length"] == len("the answer is 2")
    assert msg["content_sha256"] == hashlib.sha256("the answer is 2".encode("utf-8")).hexdigest()
    cv = data["context_versions"][0]
    assert "slots" not in cv


def test_export_partial_requires_explicit_switch(tmp_path):
    trace = _partial_trace()
    assert trace.source.is_partial is True
    exporter = JsonTraceExporter()
    path = tmp_path / "trace.json"
    try:
        exporter.export(trace, path)
        assert False, "expected ValueError for partial without allow_partial"
    except ValueError:
        pass
    # 显式开关后可导出。
    exporter.export(trace, path, allow_partial=True)
    assert path.exists()


def test_export_include_content_exports_full_payload(tmp_path):
    trace = _completed_trace()
    exporter = JsonTraceExporter()
    path = tmp_path / "trace.json"
    exporter.export(trace, path, include_content=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    msg = data["messages"][1]
    assert msg.get("content") == "the answer is 2"
    cv = data["context_versions"][0]
    assert "slots" in cv


def test_export_overwrites_same_path(tmp_path):
    trace = _completed_trace()
    exporter = JsonTraceExporter()
    path = tmp_path / "trace.json"
    exporter.export(trace, path)
    exporter.export(trace, path)
    assert path.exists()


_SECRET_PASSWORD = "hunter2-super-secret-password"
_SECRET_API_KEY = "sk-live-4f3c2b1a0d9e8f7a6b5c4d3e"
_SECRET_BEARER = "Bearer eyJhbGciOiJIUzI1NiJ9.PAYLOAD.SIGNATURE"


def _sensitive_trace():
    """构造正文、工具参数与工具输出中都带敏感串的终态 Trace。"""
    run = make_run()
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "model-1", "context_version": 1}),
        make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm",)),
        make_event("run-1", 4, RunEventType.RUN_COMPLETED, message_ids=("msg-llm",)),
    )
    messages = (
        make_message(
            "msg-input",
            1,
            RunMessageKind.USER_INPUT,
            role=MessageRole.USER,
            content=f"my password is {_SECRET_PASSWORD}",
        ),
        make_message(
            "msg-llm",
            2,
            RunMessageKind.LLM_RESPONSE,
            content=f"use key {_SECRET_API_KEY} to continue",
            tool_calls=(ToolCall(call_id="call-1", name="http", arguments={"authorization": _SECRET_BEARER}),),
        ),
        make_message(
            "msg-tool",
            3,
            RunMessageKind.TOOL_RESULT,
            role=MessageRole.TOOL,
            content=f"response header: {_SECRET_BEARER}",
            tool_call_id="call-1",
            name="http",
        ),
    )
    return assemble_trace(run, events, messages, (make_context_version(1),))


def test_export_default_never_leaks_sensitive_content(tmp_path):
    """敏感内容回归：默认导出的整份文件不得出现任何原始正文片段。"""
    trace = _sensitive_trace()
    path = tmp_path / "trace.json"
    JsonTraceExporter().export(trace, path)
    raw = path.read_text(encoding="utf-8")
    for secret in (_SECRET_PASSWORD, _SECRET_API_KEY, _SECRET_BEARER):
        assert secret not in raw
    # 连正文的可读前缀也不允许出现。
    assert "my password is" not in raw
    assert "use key" not in raw
    assert "response header" not in raw
    # 但结构信息与不可逆摘要仍然保留，便于比对与去重。
    data = json.loads(raw)
    for message, exported in zip(trace.messages, data["messages"], strict=True):
        assert exported["content_redacted"] == CONTENT_REDACTED_MARKER
        assert exported["content_length"] == len(message.content)
        assert exported["content_sha256"] == hashlib.sha256(message.content.encode("utf-8")).hexdigest()
    assert data["messages"][1]["tool_call_count"] == 1


def test_export_include_content_is_the_only_way_to_get_raw_text(tmp_path):
    """显式内容模式才导出原文，确认脱敏差异确实由开关控制。"""
    trace = _sensitive_trace()
    redacted = tmp_path / "redacted.json"
    full = tmp_path / "full.json"
    exporter = JsonTraceExporter()
    exporter.export(trace, redacted)
    exporter.export(trace, full, include_content=True)
    full_raw = full.read_text(encoding="utf-8")
    assert _SECRET_PASSWORD in full_raw
    assert _SECRET_API_KEY in full_raw
    assert _SECRET_BEARER in full_raw
    assert _SECRET_API_KEY not in redacted.read_text(encoding="utf-8")


def test_export_keeps_full_source_metadata(tmp_path):
    """导出文件必须保留来源状态、来源 sequence 与组装时间。"""
    trace = _completed_trace()
    path = tmp_path / "trace.json"
    JsonTraceExporter().export(trace, path)
    source = json.loads(path.read_text(encoding="utf-8"))["source"]
    assert source["source_run_status"] == "ended:completed"
    assert source["source_event_sequence"] == 4
    assert source["source_message_sequence"] == 2
    assert source["source_context_version_count"] == 1
    assert source["is_partial"] is False
    assert source["assembled_at"]
    assert source["record_hash"] == trace.source.record_hash


def test_export_partial_keeps_source_progress(tmp_path):
    """部分 Trace 必须能说明读到哪个 sequence、Run 当时处于什么状态。"""
    trace = _partial_trace()
    path = tmp_path / "trace.json"
    JsonTraceExporter().export(trace, path, allow_partial=True)
    source = json.loads(path.read_text(encoding="utf-8"))["source"]
    assert source["is_partial"] is True
    assert source["source_run_status"] == "running:calling_llm"
    assert source["source_event_sequence"] == 2
    assert source["source_message_sequence"] == 1
    assert source["assembled_at"]


def test_record_hash_independent_of_content_mode(tmp_path):
    trace = _completed_trace()
    exporter = JsonTraceExporter()
    base = tmp_path / "base.json"
    full = tmp_path / "full.json"
    exporter.export(trace, base)
    exporter.export(trace, full, include_content=True)
    base_hash = json.loads(base.read_text(encoding="utf-8"))["source"]["record_hash"]
    full_hash = json.loads(full.read_text(encoding="utf-8"))["source"]["record_hash"]
    assert base_hash == full_hash == trace.source.record_hash
