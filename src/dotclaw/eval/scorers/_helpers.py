"""Scorer 共享的 Trace 访问助手。

所有 Scorer 都只读取 ``RunTrace`` 的不可变事实；本模块把“按类型取有序 Span、
按 id 取消息、取最终回答”等重复逻辑收敛为一处，Scorer 之间不互相调用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...runtime.domain.facts import MessageRole, RunMessage, RunMessageKind
from ...runtime.domain.state import RunOutcome
from ...trace.models import SpanKind, TraceSpan

if TYPE_CHECKING:
    from ..trace.models import RunTrace


def ordered_spans(trace: "RunTrace", kind: SpanKind) -> list[TraceSpan]:
    """按开始序号升序返回指定类型的 Span。"""
    return sorted(
        (span for span in trace.spans if span.kind is kind),
        key=lambda item: item.start_event_sequence if item.start_event_sequence is not None else 0,
    )


def tool_spans(trace: "RunTrace") -> list[TraceSpan]:
    """有序的工具调用 Span。"""
    return ordered_spans(trace, SpanKind.TOOL)


def approval_spans(trace: "RunTrace") -> list[TraceSpan]:
    """有序的审批 Span。"""
    return ordered_spans(trace, SpanKind.APPROVAL)


def llm_spans(trace: "RunTrace") -> list[TraceSpan]:
    """有序的 LLM 调用 Span。"""
    return ordered_spans(trace, SpanKind.LLM)


def message_by_id(trace: "RunTrace", message_id: str) -> RunMessage | None:
    """按标识在 Trace 消息中查找运行消息。"""
    for message in trace.messages:
        if message.message_id == message_id:
            return message
    return None


def final_assistant_content(trace: "RunTrace") -> str | None:
    """返回最终面向用户的助手回答正文；无则返回 None。"""
    run_message_id: str | None = trace.run.final_message_id
    if run_message_id is not None:
        message = message_by_id(trace, run_message_id)
        if message is not None and message.role is MessageRole.ASSISTANT:
            return message.content
    # 退而求其次：最后一条助手回答型消息。
    for message in reversed(trace.messages):
        if message.role is MessageRole.ASSISTANT and message.kind in (
            RunMessageKind.FINAL_RESPONSE,
            RunMessageKind.LLM_RESPONSE,
        ):
            return message.content
    return None


def run_outcome(trace: "RunTrace") -> RunOutcome | None:
    """返回运行终态结果类别；未结束返回 None。"""
    return trace.run.state.outcome()
