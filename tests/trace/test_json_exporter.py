"""JsonTraceExporter 显式导出、安全内容模式与部分 Trace 开关测试。"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from helpers import make_context_version, make_event, make_message, make_run

from dotclaw.runtime.domain.events import RunEventType
from dotclaw.runtime.domain.facts import MessageRole, RunMessageKind
from dotclaw.trace import JsonTraceExporter, assemble_trace


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
    assert "content" not in msg
    assert "content_preview" in msg
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
