"""assemble_trace 纯组装与 Issue/部分 Trace 重建测试。"""

from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from .helpers import make_context_version, make_event, make_message, make_run

from dotclaw.runtime.domain.events import RunEventType
from dotclaw.runtime.domain.facts import MessageRole, RunMessageKind, ToolCall
from dotclaw.eval.draft import _tool_output
from dotclaw.runtime.domain.state import RunOutcome
from dotclaw.trace import SpanKind, TraceIssueKind, TraceSpanStatus, assemble_trace
from dotclaw.trace.models import RunTrace


def _find_span(trace: RunTrace, span_id: str):
    for span in trace.spans:
        if span.span_id == span_id:
            return span
    return None


def test_record_hash_stable_and_content_independent():
    run = make_run()
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "model-1", "context_version": 1}),
        make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm",)),
        make_event("run-1", 4, RunEventType.RUN_COMPLETED, message_ids=("msg-llm",)),
    )
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER, content="question"),
        make_message("msg-llm", 2, RunMessageKind.LLM_RESPONSE, content="answer"),
    )
    versions = (make_context_version(1),)

    first = assemble_trace(run, events, messages, versions)
    second = assemble_trace(run, events, messages, versions)
    assert first.source.record_hash == second.source.record_hash

    # record_hash 仅依赖权威事实：修改消息正文必须改变哈希。
    mutated = (make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER, content="changed"),) + messages[1:]
    third = assemble_trace(run, events, mutated, versions)
    assert third.source.record_hash != first.source.record_hash


def test_pure_llm_span_pairing_and_parent():
    run = make_run()
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "model-1", "context_version": 1}),
        make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm",)),
        make_event("run-1", 4, RunEventType.RUN_COMPLETED, message_ids=("msg-llm",)),
    )
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER, content="question"),
        make_message("msg-llm", 2, RunMessageKind.LLM_RESPONSE, content="answer"),
    )
    versions = (make_context_version(1),)

    trace = assemble_trace(run, events, messages, versions)
    assert [s.kind for s in trace.spans] == [SpanKind.RUN, SpanKind.LLM]
    llm = _find_span(trace, "llm:1")
    assert llm is not None
    assert llm.parent_span_id == "run:run-1"
    assert llm.status is TraceSpanStatus.COMPLETED
    assert llm.context_version == 1
    assert llm.message_ids == ("msg-llm",)
    assert llm.attributes.get("model_id") == "model-1"
    assert llm.attributes.get("call_index") == 1
    assert trace.source.is_partial is False
    assert trace.issues == ()


def test_tool_success_and_failure():
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),
        make_message("msg-llm", 2, RunMessageKind.LLM_RESPONSE, content="use tool"),
        make_message("msg-tool", 3, RunMessageKind.TOOL_RESULT, content="result"),
    )

    def build(status: str, outcome: RunOutcome):
        run = make_run(outcome=outcome)
        events = (
            make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
            make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "model-1", "context_version": 1}),
            make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm",)),
            make_event("run-1", 4, RunEventType.TOOL_STARTED, data={"source_response_message_id": "msg-llm", "call_id": "call-1", "tool_name": "search", "status": "started"}),
            make_event("run-1", 5, RunEventType.TOOL_COMPLETED, data={"result_message_id": "msg-tool", "call_id": "call-1", "tool_name": "search", "status": status}),
            make_event("run-1", 6, RunEventType.RUN_COMPLETED, message_ids=("msg-tool",)),
        )
        return assemble_trace(run, events, messages, (make_context_version(1),))

    ok = build("completed", RunOutcome.COMPLETED)
    tool = _find_span(ok, "tool:call-1")
    assert tool.status is TraceSpanStatus.COMPLETED
    assert tool.parent_span_id == "run:run-1"
    assert set(tool.message_ids) == {"msg-llm", "msg-tool"}
    assert tool.attributes.get("tool_name") == "search"
    assert ok.metrics.failed_tool_count == 0
    assert ok.source.is_partial is False

    failed = build("failed", RunOutcome.FAILED)
    tool_f = _find_span(failed, "tool:call-1")
    assert tool_f.status is TraceSpanStatus.FAILED
    assert failed.metrics.failed_tool_count == 1


def test_approval_approved_and_rejected():
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),
        make_message("msg-llm", 2, RunMessageKind.LLM_RESPONSE, content="need approval"),
        make_message("msg-tool", 3, RunMessageKind.TOOL_RESULT, content="approved result"),
    )

    def build(approved: bool, outcome: RunOutcome):
        run = make_run(outcome=outcome)
        events = [
            make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
            make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "model-1", "context_version": 1}),
            make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm",)),
            make_event("run-1", 4, RunEventType.TOOL_STARTED, data={"source_response_message_id": "msg-llm", "call_id": "call-1", "tool_name": "danger", "status": "started"}),
            make_event("run-1", 5, RunEventType.WAITING_APPROVAL, message_ids=("msg-tool",), data={"approval_id": "apr-1", "call_id": "call-1"}),
            make_event("run-1", 6, RunEventType.APPROVAL_RESOLVED, data={"approval_id": "apr-1", "approved": approved}),
        ]
        if approved:
            events.extend((
                make_event("run-1", 7, RunEventType.TOOL_STARTED, data={"source_response_message_id": "msg-llm", "call_id": "call-1", "tool_name": "danger", "status": "started"}),
                make_event("run-1", 8, RunEventType.TOOL_COMPLETED, data={"result_message_id": "msg-tool", "call_id": "call-1", "tool_name": "danger", "status": "completed"}),
            ))
        events.append(make_event("run-1", len(events) + 1, RunEventType.RUN_COMPLETED, message_ids=("msg-tool",)))
        return assemble_trace(run, tuple(events), messages, (make_context_version(1),))

    ok = build(True, RunOutcome.COMPLETED)
    appr = _find_span(ok, "approval:apr-1")
    assert appr.status is TraceSpanStatus.COMPLETED
    assert appr.attributes.get("approved") is True
    assert appr.parent_span_id == "run:run-1"
    assert ok.source.is_partial is False
    assert ok.metrics.approval_wait_ms >= 0

    rejected = build(False, RunOutcome.CANCELLED)
    appr_r = _find_span(rejected, "approval:apr-1")
    assert appr_r.status is TraceSpanStatus.CANCELLED
    assert appr_r.attributes.get("approved") is False
    tool_r = _find_span(rejected, "tool:call-1")
    assert tool_r.status is TraceSpanStatus.INCOMPLETE
    assert tool_r.ended_at is None


def test_semantics_two_keeps_one_tool_span_open_across_approval():
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),
        make_message("msg-llm", 2, RunMessageKind.LLM_RESPONSE, content="need approval"),
        make_message("msg-tool", 3, RunMessageKind.TOOL_RESULT, content="real result"),
    )
    run = make_run()
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "context_version": 1}),
        make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm",)),
        make_event("run-1", 4, RunEventType.TOOL_STARTED, data={"source_response_message_id": "msg-llm", "call_id": "call-1", "tool_name": "danger", "status": "started", "tool_event_semantics": 2}),
        make_event("run-1", 5, RunEventType.WAITING_APPROVAL, message_ids=("msg-llm",), data={"approval_id": "apr-1", "call_id": "call-1", "tool_event_semantics": 2}),
        make_event("run-1", 6, RunEventType.APPROVAL_RESOLVED, data={"approval_id": "apr-1", "approved": True, "tool_event_semantics": 2}),
        make_event("run-1", 7, RunEventType.TOOL_STARTED, data={"source_response_message_id": "msg-llm", "call_id": "call-1", "tool_name": "danger", "status": "started", "tool_event_semantics": 2}),
        make_event("run-1", 8, RunEventType.TOOL_COMPLETED, data={"call_id": "call-1", "status": "completed", "result_message_id": "msg-tool", "tool_event_semantics": 2}),
        make_event("run-1", 9, RunEventType.RUN_COMPLETED),
    )

    trace = assemble_trace(run, events, messages, (make_context_version(1),))
    tools = [span for span in trace.spans if span.kind is SpanKind.TOOL]

    assert len(tools) == 1
    assert tools[0].status is TraceSpanStatus.COMPLETED
    assert tools[0].start_event_sequence == 4
    assert tools[0].end_event_sequence == 8
    assert tools[0].message_ids == ("msg-llm", "msg-tool")
    assert tools[0].attributes["waiting_approval"] is False


def test_legacy_approval_flow_uses_real_terminal_result_not_fake_approval_output():
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),
        make_message("msg-llm", 2, RunMessageKind.LLM_RESPONSE, tool_calls=(ToolCall("call-1", "danger", {}),)),
        make_message("msg-fake", 3, RunMessageKind.TOOL_RESULT, role=MessageRole.TOOL, content="fake approval output", tool_call_id="call-1"),
        make_message("msg-real", 4, RunMessageKind.TOOL_RESULT, role=MessageRole.TOOL, content="real execution output", tool_call_id="call-1"),
    )
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "context_version": 1}),
        make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm",)),
        make_event("run-1", 4, RunEventType.TOOL_STARTED, data={"source_response_message_id": "msg-llm", "call_id": "call-1", "tool_name": "danger", "status": "started"}),
        make_event("run-1", 5, RunEventType.TOOL_COMPLETED, data={"result_message_id": "msg-fake", "call_id": "call-1", "tool_name": "danger", "status": "approval_required"}),
        make_event("run-1", 6, RunEventType.WAITING_APPROVAL, message_ids=("msg-fake",), data={"approval_id": "apr-1", "call_id": "call-1"}),
        make_event("run-1", 7, RunEventType.APPROVAL_RESOLVED, data={"approval_id": "apr-1", "approved": True}),
        make_event("run-1", 8, RunEventType.TOOL_STARTED, data={"source_response_message_id": "msg-llm", "call_id": "call-1", "tool_name": "danger", "status": "started"}),
        make_event("run-1", 9, RunEventType.TOOL_COMPLETED, data={"result_message_id": "msg-real", "call_id": "call-1", "tool_name": "danger", "status": "completed"}),
        make_event("run-1", 10, RunEventType.RUN_COMPLETED),
    )

    trace = assemble_trace(make_run(), events, messages, (make_context_version(1),))
    tools = [span for span in trace.spans if span.kind is SpanKind.TOOL]

    assert len(tools) == 1
    assert tools[0].status is TraceSpanStatus.COMPLETED
    assert tools[0].message_ids == ("msg-llm", "msg-real")
    assert _tool_output(trace, tools[0]) == "real execution output"


def test_waiting_approval_without_start_builds_tool_span_from_source_message():
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),
        make_message("msg-llm", 2, RunMessageKind.LLM_RESPONSE, tool_calls=(ToolCall("call-1", "danger", {}),)),
        make_message("msg-real", 3, RunMessageKind.TOOL_RESULT, role=MessageRole.TOOL, content="result", tool_call_id="call-1"),
    )
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "context_version": 1}),
        make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm",)),
        make_event("run-1", 4, RunEventType.WAITING_APPROVAL, message_ids=("msg-llm",), data={"approval_id": "apr-1", "call_id": "call-1", "tool_event_semantics": 2}),
        make_event("run-1", 5, RunEventType.APPROVAL_RESOLVED, data={"approval_id": "apr-1", "approved": True, "tool_event_semantics": 2}),
        make_event("run-1", 6, RunEventType.TOOL_STARTED, data={"call_id": "call-1", "tool_name": "danger", "source_response_message_id": "msg-llm", "tool_event_semantics": 2}),
        make_event("run-1", 7, RunEventType.TOOL_COMPLETED, data={"call_id": "call-1", "status": "completed", "result_message_id": "msg-real", "tool_event_semantics": 2}),
        make_event("run-1", 8, RunEventType.RUN_COMPLETED),
    )

    trace = assemble_trace(make_run(), events, messages, (make_context_version(1),))
    tools = [span for span in trace.spans if span.kind is SpanKind.TOOL]

    assert len(tools) == 1
    assert tools[0].attributes["tool_name"] == "danger"
    assert tools[0].message_ids == ("msg-llm", "msg-real")


def test_legacy_rejection_waits_for_semantics_two_cancellation_terminal():
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),
        make_message("msg-llm", 2, RunMessageKind.LLM_RESPONSE, tool_calls=(ToolCall("call-1", "danger", {}),)),
    )
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "context_version": 1}),
        make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm",)),
        make_event("run-1", 4, RunEventType.TOOL_STARTED, data={"source_response_message_id": "msg-llm", "call_id": "call-1", "tool_name": "danger", "status": "started"}),
        make_event("run-1", 5, RunEventType.WAITING_APPROVAL, message_ids=("msg-llm",), data={"approval_id": "apr-1", "call_id": "call-1"}),
        make_event("run-1", 6, RunEventType.APPROVAL_RESOLVED, data={"approval_id": "apr-1", "approved": False}),
        make_event("run-1", 7, RunEventType.TOOL_COMPLETED, data={"call_id": "call-1", "tool_name": "danger", "status": "cancelled", "tool_event_semantics": 2}),
        make_event("run-1", 8, RunEventType.RUN_CANCELLED),
    )

    trace = assemble_trace(make_run(outcome=RunOutcome.CANCELLED), events, messages, (make_context_version(1),))
    tools = [span for span in trace.spans if span.kind is SpanKind.TOOL]

    assert len(tools) == 1
    assert tools[0].status is TraceSpanStatus.CANCELLED
    assert tools[0].end_event_sequence == 7
    assert not any(issue.kind is TraceIssueKind.MISSING_EVENT_PAIR for issue in trace.issues)


def test_delegation_full_flow():
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),
        make_message("msg-llm", 2, RunMessageKind.LLM_RESPONSE, content="delegate"),
        make_message("msg-del", 3, RunMessageKind.DELEGATION_RESULT, content="child done"),
    )
    run = make_run()
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "model-1", "context_version": 1}),
        make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm",)),
        make_event("run-1", 4, RunEventType.TOOL_STARTED, data={"source_response_message_id": "msg-llm", "call_id": "call-1", "tool_name": "delegate", "status": "started"}),
        make_event("run-1", 5, RunEventType.DELEGATION_REQUESTED, data={"tool_call_id": "call-1", "target_agent_id": "agent-2"}),
        make_event("run-1", 6, RunEventType.DELEGATION_SUBMITTED, data={"task_id": "task-1", "child_run_id": "child-1", "target_agent_id": "agent-2"}),
        make_event("run-1", 7, RunEventType.DELEGATION_COMPLETED, message_ids=("msg-del",), data={"child_run_id": "child-1", "outcome": "completed"}),
        make_event("run-1", 8, RunEventType.RUN_COMPLETED, message_ids=("msg-del",)),
    )
    trace = assemble_trace(run, events, messages, (make_context_version(1),))
    span = _find_span(trace, "delegation:child-1")
    assert span is not None
    assert span.status is TraceSpanStatus.COMPLETED
    assert span.parent_span_id == "run:run-1"
    assert span.attributes.get("child_run_id") == "child-1"
    assert span.attributes.get("task_id") == "task-1"
    assert span.attributes.get("target_agent_id") == "agent-2"
    assert span.attributes.get("outcome") == "completed"
    assert trace.source.is_partial is False


def test_running_run_is_partial():
    run = make_run(ended=False)
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "model-1", "context_version": 1}),
    )
    messages = (make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),)
    trace = assemble_trace(run, events, messages, (make_context_version(1),))
    assert trace.source.is_partial is True
    run_span = _find_span(trace, "run:run-1")
    assert run_span.status is TraceSpanStatus.INCOMPLETE


def test_no_events_returns_partial_with_only_run_span():
    run = make_run()  # ended 但无事件
    trace = assemble_trace(run, (), (), ())
    assert trace.source.is_partial is True
    assert [s.kind for s in trace.spans] == [SpanKind.RUN]
    assert trace.issues == ()
    # 无事实可读时来源 sequence 应为 None 而非 0，避免与“读到第 0 条”混淆。
    assert trace.source.source_event_sequence is None
    assert trace.source.source_message_sequence is None
    assert trace.source.source_context_version_count == 0


def test_source_metadata_records_read_snapshot():
    """来源元数据必须说明读取快照：状态、最大 sequence、上下文数量、组装时间。"""
    run = make_run()
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "model-1", "context_version": 1}),
        make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm",)),
        make_event("run-1", 4, RunEventType.RUN_COMPLETED, message_ids=("msg-llm",)),
    )
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),
        make_message("msg-llm", 2, RunMessageKind.LLM_RESPONSE),
    )
    versions = (make_context_version(1), make_context_version(2))
    trace = assemble_trace(run, events, messages, versions)
    source = trace.source
    assert source.run_id == "run-1"
    assert source.session_id == "sess-1"
    assert source.source_run_status == "ended:completed"
    assert source.source_event_sequence == 4
    assert source.source_message_sequence == 2
    assert source.source_context_version_count == 2
    # assembled_at 必须是可解析的 ISO 时间戳。
    assert datetime.fromisoformat(source.assembled_at) is not None


def test_source_run_status_tracks_non_terminal_run():
    run = make_run(ended=False)
    events = (make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),)
    messages = (make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),)
    trace = assemble_trace(run, events, messages, (make_context_version(1),))
    assert trace.source.source_run_status == "running:calling_llm"
    assert trace.source.source_event_sequence == 1
    assert trace.source.is_partial is True


def test_record_hash_excludes_assembled_at():
    """同一份权威事实在不同时刻重建：assembled_at 可变，record_hash 必须不变。"""
    run = make_run()
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
        make_event("run-1", 2, RunEventType.RUN_COMPLETED, message_ids=("msg-input",)),
    )
    messages = (make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),)
    versions = (make_context_version(1),)  # 同一份权威事实，避免 created_at 差异干扰
    first = assemble_trace(run, events, messages, versions)
    second = assemble_trace(run, events, messages, versions)
    assert first.source.record_hash == second.source.record_hash
    assert first.source.assembled_at not in first.source.record_hash


def test_missing_message_emits_issue_not_exception():
    run = make_run()
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "model-1", "context_version": 1}),
        make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-ghost",)),
        make_event("run-1", 4, RunEventType.RUN_COMPLETED, message_ids=("msg-ghost",)),
    )
    messages = (make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),)
    trace = assemble_trace(run, events, messages, (make_context_version(1),))
    assert any(i.kind is TraceIssueKind.MISSING_MESSAGE for i in trace.issues)
    assert trace.source.is_partial is True


def test_missing_context_version_emits_issue():
    run = make_run()
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "model-1", "context_version": 99}),
        make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm",)),
        make_event("run-1", 4, RunEventType.RUN_COMPLETED, message_ids=("msg-llm",)),
    )
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),
        make_message("msg-llm", 2, RunMessageKind.LLM_RESPONSE),
    )
    trace = assemble_trace(run, events, messages, (make_context_version(1),))
    assert any(i.kind is TraceIssueKind.MISSING_CONTEXT_VERSION for i in trace.issues)
    assert trace.source.is_partial is True


def test_unpaired_tool_completed_emits_issue():
    run = make_run()
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
        make_event("run-1", 2, RunEventType.TOOL_COMPLETED, data={"result_message_id": "msg-tool", "call_id": "call-x", "tool_name": "x", "status": "completed"}),
        make_event("run-1", 3, RunEventType.RUN_COMPLETED),
    )
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),
        make_message("msg-tool", 2, RunMessageKind.TOOL_RESULT),
    )
    trace = assemble_trace(run, events, messages, (make_context_version(1),))
    assert any(i.kind is TraceIssueKind.MISSING_EVENT_PAIR for i in trace.issues)
    assert _find_span(trace, "tool:call-x") is None


def test_historical_approval_without_data_emits_issue():
    run = make_run()
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
        make_event("run-1", 2, RunEventType.APPROVAL_RESOLVED, data={}),
        make_event("run-1", 3, RunEventType.RUN_COMPLETED),
    )
    messages = (make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),)
    trace = assemble_trace(run, events, messages, (make_context_version(1),))
    assert any(i.kind is TraceIssueKind.MISSING_EVENT_PAIR for i in trace.issues)


def test_recovery_blocked_preserves_uncertainty_reason_on_one_tool_span():
    source = make_message(
        "msg-source", 1, RunMessageKind.LLM_RESPONSE,
        tool_calls=(ToolCall("call-1", "write", {}),),
    )
    events = (
        make_event("run-1", 1, RunEventType.TOOL_STARTED, data={"call_id": "call-1", "tool_name": "write"}),
        make_event("run-1", 2, RunEventType.TOOL_RECOVERY_BLOCKED, message_ids=("msg-source",), data={
            "call_id": "call-1", "reason": "started_outcome_unknown", "attempt": 1,
            "source_response_message_id": "msg-source",
        }),
        make_event("run-1", 3, RunEventType.RUN_FAILED),
    )

    trace = assemble_trace(make_run(outcome=RunOutcome.FAILED), events, (source,), (make_context_version(1),))
    tool_spans = [span for span in trace.spans if span.kind is SpanKind.TOOL]

    assert len(tool_spans) == 1
    assert tool_spans[0].status is TraceSpanStatus.INCOMPLETE
    assert tool_spans[0].attributes["uncertainty_reason"] == "started_outcome_unknown"


def test_approval_trace_does_not_coerce_non_boolean_decision():
    events = (
        make_event("run-1", 1, RunEventType.WAITING_APPROVAL, data={"approval_id": "approval-1", "call_id": "call-1"}),
        make_event("run-1", 2, RunEventType.APPROVAL_RESOLVED, data={"approval_id": "approval-1", "approved": "false"}),
    )

    trace = assemble_trace(make_run(), events, (), (make_context_version(1),))

    assert any(issue.kind is TraceIssueKind.CONFLICTING_REFERENCE for issue in trace.issues)
    approval = next(span for span in trace.spans if span.kind is SpanKind.APPROVAL)
    assert approval.status is TraceSpanStatus.WAITING


def test_all_five_span_kinds_share_run_parent():
    messages = (
        make_message("msg-input", 1, RunMessageKind.USER_INPUT, role=MessageRole.USER),
        make_message("msg-llm", 2, RunMessageKind.LLM_RESPONSE),
        make_message("msg-tool", 3, RunMessageKind.TOOL_RESULT),
        make_message("msg-del", 4, RunMessageKind.DELEGATION_RESULT),
    )
    run = make_run()
    events = (
        make_event("run-1", 1, RunEventType.RUN_STARTED, message_ids=("msg-input",)),
        make_event("run-1", 2, RunEventType.LLM_STARTED, data={"call_index": 1, "model_id": "model-1", "context_version": 1}),
        make_event("run-1", 3, RunEventType.LLM_COMPLETED, message_ids=("msg-llm",)),
        make_event("run-1", 4, RunEventType.TOOL_STARTED, data={"source_response_message_id": "msg-llm", "call_id": "call-1", "tool_name": "search", "status": "started"}),
        make_event("run-1", 5, RunEventType.TOOL_COMPLETED, data={"result_message_id": "msg-tool", "call_id": "call-1", "tool_name": "search", "status": "completed"}),
        make_event("run-1", 6, RunEventType.WAITING_APPROVAL, message_ids=("msg-tool",), data={"approval_id": "apr-1", "call_id": "call-1"}),
        make_event("run-1", 7, RunEventType.APPROVAL_RESOLVED, data={"approval_id": "apr-1", "approved": True}),
        make_event("run-1", 8, RunEventType.DELEGATION_REQUESTED, data={"tool_call_id": "call-1", "target_agent_id": "agent-2"}),
        make_event("run-1", 9, RunEventType.DELEGATION_SUBMITTED, data={"task_id": "task-1", "child_run_id": "child-1", "target_agent_id": "agent-2"}),
        make_event("run-1", 10, RunEventType.DELEGATION_COMPLETED, message_ids=("msg-del",), data={"child_run_id": "child-1", "outcome": "completed"}),
        make_event("run-1", 11, RunEventType.RUN_COMPLETED, message_ids=("msg-del",)),
    )
    trace = assemble_trace(run, events, messages, (make_context_version(1),))
    kinds = {s.kind for s in trace.spans}
    assert kinds == {SpanKind.RUN, SpanKind.LLM, SpanKind.TOOL, SpanKind.APPROVAL, SpanKind.DELEGATION}
    for span in trace.spans:
        if span.kind is SpanKind.RUN:
            assert span.parent_span_id is None
        else:
            assert span.parent_span_id == "run:run-1"
    assert trace.source.is_partial is False
