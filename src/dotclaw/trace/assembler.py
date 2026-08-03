"""从权威事实纯函数式重建 RunTrace。

``assemble_trace`` 不读文件、不写文件、不调用 Runtime；只消费已加载的
``AgentRun`` / ``RunEvent`` / ``RunMessage`` / ``ContextVersion`` 并产出只读追踪。
语义不完整（缺事件对、缺消息、缺上下文版本、引用冲突）统一表达为 ``TraceIssue``，
绝不抛出读取异常；未闭合 Span 标记为 ``INCOMPLETE``。

``record_hash`` 仅依赖权威事实的稳定序列化，与重建出的 Span / Issue / JSON 内容无关，
因此不受 ``include_content`` 等导出开关影响。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Mapping

from ..runtime.domain.context import ContextVersion
from ..runtime.domain.events import RunEvent, RunEventType
from ..runtime.domain.facts import AgentRun, RunMessage
from ..runtime.domain.state import RunOutcome

from .models import (
    RunTrace,
    RunTraceSource,
    SpanKind,
    TraceIssue,
    TraceIssueKind,
    TraceMetrics,
    TraceSpan,
    TraceSpanStatus,
)

SCHEMA_VERSION = "1.0"

_SUPPORTED_EVENTS = frozenset(
    {
        RunEventType.RUN_STARTED,
        RunEventType.RUN_COMPLETED,
        RunEventType.RUN_FAILED,
        RunEventType.RUN_CANCELLED,
        RunEventType.RUN_ABANDONED,
        RunEventType.LLM_STARTED,
        RunEventType.LLM_COMPLETED,
        RunEventType.TOOL_STARTED,
        RunEventType.TOOL_COMPLETED,
        RunEventType.WAITING_APPROVAL,
        RunEventType.APPROVAL_RESOLVED,
        RunEventType.DELEGATION_REQUESTED,
        RunEventType.DELEGATION_SUBMITTED,
        RunEventType.DELEGATION_COMPLETED,
    }
)

_TOOL_STATUS_MAP: Mapping[str, TraceSpanStatus] = {
    "completed": TraceSpanStatus.COMPLETED,
    "failed": TraceSpanStatus.FAILED,
    "approval_required": TraceSpanStatus.WAITING,
    "cancelled": TraceSpanStatus.CANCELLED,
    "started": TraceSpanStatus.INCOMPLETE,
}

_DELEGATION_OUTCOME_MAP: Mapping[str, TraceSpanStatus] = {
    "completed": TraceSpanStatus.COMPLETED,
    "failed": TraceSpanStatus.FAILED,
}


@dataclass
class _PendingSpan:
    """组装期的可变 Span 草稿；终态时转换为不可变 ``TraceSpan``。"""

    span_id: str
    kind: SpanKind
    parent_span_id: str | None
    started_at: str
    status: TraceSpanStatus
    start_event_sequence: int | None
    message_ids: list[str]
    context_version: int | None
    attributes: dict[str, object]
    ended_at: str | None = None
    end_event_sequence: int | None = None


def _stable_json(value: object) -> str:
    """对权威事实做带排序键的稳定序列化。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parse_ts(value: str) -> datetime | None:
    """宽松解析 ISO 8601 时间戳（兼容尾随 Z 与偏移量）。"""
    if not value:
        return None
    normalized: str = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _duration_ms(started_at: str, ended_at: str) -> int:
    """计算两个 ISO 时间戳之间的毫秒差；缺失或非法返回 0。"""
    start, end = _parse_ts(started_at), _parse_ts(ended_at)
    if start is None or end is None:
        return 0
    return max(0, int(round((end - start).total_seconds() * 1000)))


def _run_status(run: AgentRun) -> TraceSpanStatus:
    """由 Run 状态推导 RUN 根 Span 的状态。"""
    if run.state.is_ended():
        outcome = run.state.outcome()
        if outcome is RunOutcome.COMPLETED:
            return TraceSpanStatus.COMPLETED
        if outcome is RunOutcome.FAILED:
            return TraceSpanStatus.FAILED
        return TraceSpanStatus.CANCELLED
    if run.state.is_suspended():
        return TraceSpanStatus.WAITING
    return TraceSpanStatus.INCOMPLETE


def _record_hash(
    run: AgentRun,
    events: tuple[RunEvent, ...],
    messages: tuple[RunMessage, ...],
    context_versions: tuple[ContextVersion, ...],
) -> str:
    """基于权威事实稳定序列化做 SHA-256；不含任何重建态或导出开关。"""
    parts: list[str] = [_stable_json(run.to_dict())]
    parts.extend(_stable_json(event.to_dict()) for event in sorted(events, key=lambda item: item.sequence))
    parts.extend(_stable_json(message.to_dict()) for message in sorted(messages, key=lambda item: item.sequence))
    parts.extend(
        _stable_json(version.to_dict()) for version in sorted(context_versions, key=lambda item: item.version)
    )
    payload: str = "\n".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assemble_trace(
    run: AgentRun,
    events: tuple[RunEvent, ...],
    messages: tuple[RunMessage, ...],
    context_versions: tuple[ContextVersion, ...],
) -> RunTrace:
    """从权威事实重建 ``RunTrace``。"""
    issues: list[TraceIssue] = []
    message_index: dict[str, RunMessage] = {message.message_id: message for message in messages}
    cv_index: dict[int, ContextVersion] = {version.version: version for version in context_versions}

    run_span_id = f"run:{run.run_id}"
    started_at = run.started_at or (events[0].occurred_at if events else "")
    run_span = _PendingSpan(
        span_id=run_span_id,
        kind=SpanKind.RUN,
        parent_span_id=None,
        started_at=started_at,
        status=_run_status(run),
        start_event_sequence=events[0].sequence if events else None,
        message_ids=[],
        context_version=None,
        attributes={},
        ended_at=run.ended_at,
        end_event_sequence=events[-1].sequence if events else None,
    )
    pending: list[_PendingSpan] = [run_span]

    open_llm: _PendingSpan | None = None
    open_tools: dict[str, _PendingSpan] = {}
    open_approvals: dict[str, _PendingSpan] = {}
    pending_delegation: _PendingSpan | None = None
    approval_to_tool: dict[str, str] = {}
    resolved_tool_calls: set[str] = set()

    for event in sorted(events, key=lambda item: item.sequence):
        event_type = event.event_type
        data = event.data
        if event_type not in _SUPPORTED_EVENTS:
            issues.append(
                TraceIssue(
                    kind=TraceIssueKind.UNSUPPORTED_EVENT,
                    evidence=f"事件类型 {event_type.value} 不在追踪建模范围内",
                    event_sequence=event.sequence,
                )
            )
            continue

        if event_type is RunEventType.RUN_STARTED:
            if not run_span.started_at and event.occurred_at:
                run_span.started_at = event.occurred_at
            if run_span.start_event_sequence is None:
                run_span.start_event_sequence = event.sequence

        elif event_type in {
            RunEventType.RUN_COMPLETED,
            RunEventType.RUN_FAILED,
            RunEventType.RUN_CANCELLED,
            RunEventType.RUN_ABANDONED,
        }:
            run_span.ended_at = run_span.ended_at or event.occurred_at
            run_span.end_event_sequence = event.sequence
            run_span.status = _run_status(run)

        elif event_type is RunEventType.LLM_STARTED:
            call_index = data.get("call_index")
            model_id = data.get("model_id")
            ctx_version = data.get("context_version")
            if open_llm is not None:
                open_llm.status = TraceSpanStatus.INCOMPLETE
                issues.append(
                    TraceIssue(
                        kind=TraceIssueKind.MISSING_EVENT_PAIR,
                        evidence="LLM 调用未以 LLM_COMPLETED 闭合即开始下一次调用",
                        event_sequence=event.sequence,
                        span_id=open_llm.span_id,
                    )
                )
            span_id = f"llm:{call_index}" if call_index is not None else f"llm:{event.sequence}"
            attributes: dict[str, object] = {}
            if model_id is not None:
                attributes["model_id"] = model_id
            if call_index is not None:
                attributes["call_index"] = call_index
            llm_span = _PendingSpan(
                span_id=span_id,
                kind=SpanKind.LLM,
                parent_span_id=run_span_id,
                started_at=event.occurred_at,
                status=TraceSpanStatus.INCOMPLETE,
                start_event_sequence=event.sequence,
                message_ids=[],
                context_version=ctx_version if isinstance(ctx_version, int) else None,
                attributes=attributes,
            )
            pending.append(llm_span)
            open_llm = llm_span
            if isinstance(ctx_version, int) and ctx_version not in cv_index:
                issues.append(
                    TraceIssue(
                        kind=TraceIssueKind.MISSING_CONTEXT_VERSION,
                        evidence=f"LLM 引用了不存在的 Context Version {ctx_version}",
                        event_sequence=event.sequence,
                        span_id=span_id,
                    )
                )

        elif event_type is RunEventType.LLM_COMPLETED:
            response_id = event.message_ids[0] if event.message_ids else None
            if open_llm is None:
                issues.append(
                    TraceIssue(
                        kind=TraceIssueKind.MISSING_EVENT_PAIR,
                        evidence="LLM_COMPLETED 缺少对应的 LLM_STARTED",
                        event_sequence=event.sequence,
                    )
                )
            else:
                open_llm.ended_at = event.occurred_at
                open_llm.end_event_sequence = event.sequence
                open_llm.status = TraceSpanStatus.COMPLETED
                if response_id is not None:
                    open_llm.message_ids.append(response_id)
                    if response_id not in message_index:
                        issues.append(
                            TraceIssue(
                                kind=TraceIssueKind.MISSING_MESSAGE,
                                evidence=f"LLM 响应消息 {response_id} 不在消息事实中",
                                message_id=response_id,
                                event_sequence=event.sequence,
                                span_id=open_llm.span_id,
                            )
                        )
                open_llm = None

        elif event_type is RunEventType.TOOL_STARTED:
            call_id = data.get("call_id")
            tool_name = data.get("tool_name")
            source_id = data.get("source_response_message_id")
            if call_id is not None and call_id in open_tools:
                issues.append(
                    TraceIssue(
                        kind=TraceIssueKind.CONFLICTING_REFERENCE,
                        evidence=f"工具调用 {call_id} 重复开始",
                        event_sequence=event.sequence,
                        span_id=open_tools[call_id].span_id,
                    )
                )
            span_id = f"tool:{call_id}" if call_id is not None else f"tool:{event.sequence}"
            attributes = {"status": data.get("status", "started")}
            if call_id is not None:
                attributes["call_id"] = call_id
            if tool_name is not None:
                attributes["tool_name"] = tool_name
            tool_span = _PendingSpan(
                span_id=span_id,
                kind=SpanKind.TOOL,
                parent_span_id=run_span_id,
                started_at=event.occurred_at,
                status=TraceSpanStatus.INCOMPLETE,
                start_event_sequence=event.sequence,
                message_ids=[],
                context_version=None,
                attributes=attributes,
            )
            if source_id:
                tool_span.message_ids.append(source_id)
                if source_id not in message_index:
                    issues.append(
                        TraceIssue(
                            kind=TraceIssueKind.MISSING_MESSAGE,
                            evidence=f"工具源响应消息 {source_id} 不在消息事实中",
                            message_id=source_id,
                            event_sequence=event.sequence,
                            span_id=span_id,
                        )
                    )
            pending.append(tool_span)
            if call_id is not None:
                open_tools[call_id] = tool_span

        elif event_type is RunEventType.TOOL_COMPLETED:
            call_id = data.get("call_id")
            if call_id is not None and call_id in resolved_tool_calls:
                # 该工具调用已由审批/委派决议闭合，回灌的 TOOL_COMPLETED 视为良性重复。
                continue
            result_id = data.get("result_message_id") or None
            status_str = data.get("status", "completed")
            span = open_tools.get(call_id) if call_id is not None else None
            if span is None:
                issues.append(
                    TraceIssue(
                        kind=TraceIssueKind.MISSING_EVENT_PAIR,
                        evidence=f"TOOL_COMPLETED 缺少对应的 TOOL_STARTED（call_id={call_id}）",
                        event_sequence=event.sequence,
                    )
                )
            else:
                span.ended_at = event.occurred_at
                span.end_event_sequence = event.sequence
                span.status = _TOOL_STATUS_MAP.get(status_str, TraceSpanStatus.INCOMPLETE)
                span.attributes = {**span.attributes, "status": status_str}
                if result_id:
                    span.message_ids.append(result_id)
                    if result_id not in message_index:
                        issues.append(
                            TraceIssue(
                                kind=TraceIssueKind.MISSING_MESSAGE,
                                evidence=f"工具结果消息 {result_id} 不在消息事实中",
                                message_id=result_id,
                                event_sequence=event.sequence,
                                span_id=span.span_id,
                            )
                        )
                if call_id is not None:
                    open_tools.pop(call_id, None)

        elif event_type is RunEventType.WAITING_APPROVAL:
            approval_id = data.get("approval_id")
            call_id = data.get("call_id")
            if approval_id is not None and approval_id in open_approvals:
                issues.append(
                    TraceIssue(
                        kind=TraceIssueKind.CONFLICTING_REFERENCE,
                        evidence=f"审批 {approval_id} 重复等待",
                        event_sequence=event.sequence,
                        span_id=open_approvals[approval_id].span_id,
                    )
                )
            span_id = f"approval:{approval_id}" if approval_id is not None else f"approval:{event.sequence}"
            attributes = {}
            if approval_id is not None:
                attributes["approval_id"] = approval_id
            if call_id is not None:
                attributes["call_id"] = call_id
            appr_span = _PendingSpan(
                span_id=span_id,
                kind=SpanKind.APPROVAL,
                parent_span_id=run_span_id,
                started_at=event.occurred_at,
                status=TraceSpanStatus.WAITING,
                start_event_sequence=event.sequence,
                message_ids=[],
                context_version=None,
                attributes=attributes,
            )
            if event.message_ids:
                appr_span.message_ids.append(event.message_ids[0])
            pending.append(appr_span)
            if approval_id is not None:
                open_approvals[approval_id] = appr_span
                if call_id is not None:
                    approval_to_tool[approval_id] = call_id

        elif event_type is RunEventType.APPROVAL_RESOLVED:
            approval_id = data.get("approval_id")
            approved = bool(data.get("approved", False))
            span = open_approvals.get(approval_id) if approval_id is not None else None
            if span is None:
                issues.append(
                    TraceIssue(
                        kind=TraceIssueKind.MISSING_EVENT_PAIR,
                        evidence=f"APPROVAL_RESOLVED 缺少对应的 WAITING_APPROVAL（approval_id={approval_id}）",
                        event_sequence=event.sequence,
                    )
                )
            else:
                span.ended_at = event.occurred_at
                span.end_event_sequence = event.sequence
                span.status = TraceSpanStatus.COMPLETED if approved else TraceSpanStatus.CANCELLED
                span.attributes = {**span.attributes, "approved": approved}
                if approval_id is not None:
                    open_approvals.pop(approval_id, None)
                # 审批决议闭合其关联的工具调用：引擎在审批通过后重放工具并回灌 TOOL_COMPLETED，
                # 该 TOOL_COMPLETED 视为已决议的良性重复，组装期跳过而非记缺失。
                linked_call = approval_to_tool.get(approval_id) if approval_id is not None else None
                if linked_call is not None:
                    tool_span = open_tools.pop(linked_call, None)
                    if tool_span is not None:
                        tool_span.ended_at = event.occurred_at
                        tool_span.end_event_sequence = event.sequence
                        tool_span.status = TraceSpanStatus.COMPLETED if approved else TraceSpanStatus.CANCELLED
                        resolved_tool_calls.add(linked_call)

        elif event_type is RunEventType.DELEGATION_REQUESTED:
            tool_call_id = data.get("tool_call_id")
            target_agent_id = data.get("target_agent_id")
            if pending_delegation is not None:
                pending_delegation.status = TraceSpanStatus.INCOMPLETE
                issues.append(
                    TraceIssue(
                        kind=TraceIssueKind.MISSING_EVENT_PAIR,
                        evidence="Delegation 请求未闭合即再次请求",
                        event_sequence=event.sequence,
                        span_id=pending_delegation.span_id,
                    )
                )
            span_id = f"delegation:req:{tool_call_id}" if tool_call_id is not None else f"delegation:req:{event.sequence}"
            attributes = {}
            if tool_call_id is not None:
                attributes["tool_call_id"] = tool_call_id
            if target_agent_id is not None:
                attributes["target_agent_id"] = target_agent_id
            del_span = _PendingSpan(
                span_id=span_id,
                kind=SpanKind.DELEGATION,
                parent_span_id=run_span_id,
                started_at=event.occurred_at,
                status=TraceSpanStatus.INCOMPLETE,
                start_event_sequence=event.sequence,
                message_ids=[],
                context_version=None,
                attributes=attributes,
            )
            pending.append(del_span)
            pending_delegation = del_span
            # 委派由同一 call_id 的工具调用触发且不再回灌 TOOL_COMPLETED，故闭合该工具 Span。
            if tool_call_id is not None:
                tool_span = open_tools.pop(tool_call_id, None)
                if tool_span is not None:
                    tool_span.ended_at = event.occurred_at
                    tool_span.end_event_sequence = event.sequence
                    tool_span.status = TraceSpanStatus.COMPLETED
                    resolved_tool_calls.add(tool_call_id)

        elif event_type is RunEventType.DELEGATION_SUBMITTED:
            task_id = data.get("task_id")
            child_run_id = data.get("child_run_id")
            target_agent_id = data.get("target_agent_id")
            if pending_delegation is None:
                pending_delegation = _PendingSpan(
                    span_id=f"delegation:{child_run_id}",
                    kind=SpanKind.DELEGATION,
                    parent_span_id=run_span_id,
                    started_at=event.occurred_at,
                    status=TraceSpanStatus.INCOMPLETE,
                    start_event_sequence=event.sequence,
                    message_ids=[],
                    context_version=None,
                    attributes={},
                )
                pending.append(pending_delegation)
            pending_delegation.span_id = f"delegation:{child_run_id}" if child_run_id is not None else pending_delegation.span_id
            merged = dict(pending_delegation.attributes)
            if task_id is not None:
                merged["task_id"] = task_id
            if child_run_id is not None:
                merged["child_run_id"] = child_run_id
            if target_agent_id is not None:
                merged["target_agent_id"] = target_agent_id
            pending_delegation.attributes = merged
            pending_delegation.status = TraceSpanStatus.WAITING
            pending_delegation.start_event_sequence = pending_delegation.start_event_sequence or event.sequence

        elif event_type is RunEventType.DELEGATION_COMPLETED:
            child_run_id = data.get("child_run_id")
            outcome = data.get("outcome") or ""
            result_id = event.message_ids[0] if event.message_ids else None
            span = pending_delegation
            if span is None:
                issues.append(
                    TraceIssue(
                        kind=TraceIssueKind.MISSING_EVENT_PAIR,
                        evidence=f"DELEGATION_COMPLETED 缺少对应的请求/提交（child_run_id={child_run_id}）",
                        event_sequence=event.sequence,
                    )
                )
            else:
                span.ended_at = event.occurred_at
                span.end_event_sequence = event.sequence
                span.status = _DELEGATION_OUTCOME_MAP.get(outcome, TraceSpanStatus.INCOMPLETE)
                merged = dict(span.attributes)
                if child_run_id is not None:
                    merged["child_run_id"] = child_run_id
                merged["outcome"] = outcome
                span.attributes = merged
                if result_id:
                    span.message_ids.append(result_id)
                    if result_id not in message_index:
                        issues.append(
                            TraceIssue(
                                kind=TraceIssueKind.MISSING_MESSAGE,
                                evidence=f"Delegation 结果消息 {result_id} 不在消息事实中",
                                message_id=result_id,
                                event_sequence=event.sequence,
                                span_id=span.span_id,
                            )
                        )
                pending_delegation = None

    # 收尾：运行结束时仍打开的 Span 标记为不完整。
    if open_llm is not None:
        open_llm.status = TraceSpanStatus.INCOMPLETE
        issues.append(
            TraceIssue(kind=TraceIssueKind.MISSING_EVENT_PAIR, evidence="运行结束但存在未闭合的 LLM 调用", span_id=open_llm.span_id)
        )
    for span in open_tools.values():
        span.status = TraceSpanStatus.INCOMPLETE
        issues.append(
            TraceIssue(kind=TraceIssueKind.MISSING_EVENT_PAIR, evidence="运行结束但存在未闭合的工具调用", span_id=span.span_id)
        )
    for span in open_approvals.values():
        if run.state.is_ended():
            span.status = TraceSpanStatus.INCOMPLETE
            issues.append(
                TraceIssue(kind=TraceIssueKind.MISSING_EVENT_PAIR, evidence="运行已结束但审批未决议", span_id=span.span_id)
            )
    if pending_delegation is not None and run.state.is_ended():
        pending_delegation.status = TraceSpanStatus.INCOMPLETE
        issues.append(
            TraceIssue(kind=TraceIssueKind.MISSING_EVENT_PAIR, evidence="运行已结束但 Delegation 未完成", span_id=pending_delegation.span_id)
        )

    # 指标。RUN 根 Span 不计入不完整计数与耗时汇总。
    llm_ms = 0
    tool_ms = 0
    approval_ms = 0
    longest_tool = 0
    failed_tool = 0
    incomplete_count = 0
    for span in pending[1:]:
        if span.status is TraceSpanStatus.INCOMPLETE:
            incomplete_count += 1
        duration = _duration_ms(span.started_at, span.ended_at or "")
        if span.kind is SpanKind.LLM and span.ended_at:
            llm_ms += duration
        elif span.kind is SpanKind.TOOL:
            if span.ended_at:
                tool_ms += duration
                longest_tool = max(longest_tool, duration)
            if span.status is TraceSpanStatus.FAILED:
                failed_tool += 1
        elif span.kind is SpanKind.APPROVAL and span.ended_at:
            approval_ms += duration

    critical_path = _duration_ms(run.started_at, run.ended_at or (events[-1].occurred_at if events else ""))

    metrics = TraceMetrics(
        llm_duration_ms=llm_ms,
        tool_duration_ms=tool_ms,
        approval_wait_ms=approval_ms,
        longest_tool_duration_ms=longest_tool,
        failed_tool_count=failed_tool,
        incomplete_span_count=incomplete_count,
        critical_path_ms=critical_path,
    )

    key_issue = any(
        issue.kind
        in {
            TraceIssueKind.MISSING_EVENT_PAIR,
            TraceIssueKind.MISSING_MESSAGE,
            TraceIssueKind.MISSING_CONTEXT_VERSION,
            TraceIssueKind.CONFLICTING_REFERENCE,
        }
        for issue in issues
    )
    is_partial = (
        len(events) == 0
        or (not run.state.is_ended())
        or any(span.status is TraceSpanStatus.INCOMPLETE for span in pending)
        or key_issue
    )

    spans = tuple(
        TraceSpan(
            span_id=item.span_id,
            kind=item.kind,
            parent_span_id=item.parent_span_id,
            started_at=item.started_at,
            ended_at=item.ended_at,
            status=item.status,
            start_event_sequence=item.start_event_sequence,
            end_event_sequence=item.end_event_sequence,
            message_ids=tuple(item.message_ids),
            context_version=item.context_version,
            attributes=item.attributes,
        )
        for item in pending
    )

    record_hash = _record_hash(run, events, messages, context_versions)
    source = RunTraceSource(
        run_id=run.run_id,
        session_id=run.session_id,
        schema_version=SCHEMA_VERSION,
        is_partial=is_partial,
        record_hash=record_hash,
        source_run_status=run.state.describe(),
        source_event_sequence=max((event.sequence for event in events), default=None),
        source_message_sequence=max((message.sequence for message in messages), default=None),
        source_context_version_count=len(context_versions),
        assembled_at=datetime.now(UTC).isoformat(),
    )
    return RunTrace(
        schema_version=SCHEMA_VERSION,
        run=run,
        source=source,
        spans=spans,
        messages=messages,
        context_versions=context_versions,
        metrics=metrics,
        issues=tuple(issues),
    )
